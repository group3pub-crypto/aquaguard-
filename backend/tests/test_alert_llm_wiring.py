"""LLM explainer wiring: the alert is stored with the instant template text; the LLM text
replaces it after commit; the alert never depends on the LLM."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.models import Alert
from app.services import llm
from app.services.llm import ExplainResult
from tests.conftest import T0, make_alert


class FakeExplainer:
    source = "template"

    def __init__(self, result: ExplainResult | Exception) -> None:
        self.result = result
        self.ctxs: list[dict] = []

    def message(self, ctx):
        self.ctxs.append(ctx)
        return f"TEMPLATE tier {ctx['tier']}"

    async def generate(self, ctx, *, cache_ref=None):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def _drain():
    while llm.regenerate._tasks:
        await asyncio.gather(*list(llm.regenerate._tasks), return_exceptions=True)


async def _alert(db, aid) -> Alert:
    async with db.session() as s:
        return await s.get(Alert, aid)


async def test_template_first_then_llm_text(rt, db, world):
    rt.explainer = FakeExplainer(ExplainResult("LLM TEXT", "anthropic"))
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.75)
    await _drain()
    a = await _alert(db, aid)
    assert (a.message, a.message_source) == ("LLM TEXT", "anthropic")
    ctx = rt.explainer.ctxs[0]
    assert ctx["tier"] == 3 and ctx["unit"] == "101" and ctx["valve_state_confirmed"] is False


async def test_alert_creation_does_not_wait_for_a_slow_llm(rt, db, world):
    gate = asyncio.Event()

    class Slow(FakeExplainer):
        async def generate(self, ctx, *, cache_ref=None):
            await gate.wait()
            return ExplainResult("LATE", "anthropic")

    rt.explainer = Slow(ExplainResult("x", "anthropic"))
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.75)
    # make_alert returned while the LLM is still "thinking": creation never waits for it
    gate.set()
    await _drain()
    assert (await _alert(db, aid)).message == "LATE"


async def test_fallback_source_recorded(rt, db, world):
    rt.explainer = FakeExplainer(ExplainResult("fallback text", "template_fallback", "timeout"))
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.75)
    await _drain()
    assert (await _alert(db, aid)).message_source == "template_fallback"


async def test_alert_exists_even_when_the_llm_raises(rt, db, world):
    rt.explainer = FakeExplainer(RuntimeError("provider down"))
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.75)
    await _drain()
    a = await _alert(db, aid)
    assert a.state == "ticket" and a.message == "TEMPLATE tier 3" and a.message_source == "template"


async def test_learning_cap_note_goes_in_suffix(rt, db, world):
    from app.repositories import FlatRepository

    async with db.session() as s:
        (await FlatRepository(s).get(world.flats[0].id)).baseline_status = "learning"
    rt.explainer = FakeExplainer(ExplainResult("LLM", "anthropic"))
    _, aid = await make_alert(db, rt, world.flats[0].id, severity=0.95)
    await _drain()
    assert "being learned" in rt.explainer.ctxs[0]["suffix"]


async def test_stale_regeneration_does_not_overwrite_a_newer_tier(rt, db, world):
    alert = SimpleNamespace(id="x", state="alert", tier=3, message="new", message_source="template")
    ex = FakeExplainer(ExplainResult("old tier text", "anthropic"))
    assert await llm.regenerate_message(alert, {"tier": 2}, ex) is None
    assert alert.message == "new"
