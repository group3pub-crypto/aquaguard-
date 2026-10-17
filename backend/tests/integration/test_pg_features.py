"""New features on real PostgreSQL: device_status races, LLM template-first, commissioning."""

from __future__ import annotations

import asyncio
import time

import pytest
import sqlalchemy as sa

from app.models import Alert, AuditLog, DeviceStatus
from app.repositories import FlatRepository
from app.services import ingest, llm
from app.services.llm import ExplainResult
from tests.conftest import make_alert
from tests.helpers import TOPIC, flow_msg

pytestmark = pytest.mark.integration


async def test_concurrent_first_reports_create_one_device_row_and_one_fault(rt, pgdb, society):
    t = time.time() - 60
    msgs = [
        flow_msg(mid=f"{i:08x}", ts=t + i, fw="2.0", err=["close_unconf"], valve="open")
        for i in range(12)
    ]
    await asyncio.gather(*[ingest.handle_message(rt, TOPIC, m) for m in msgs])
    assert rt.counters["flow_failed"] == 0, list(rt.dead_letters)
    async with pgdb.session() as s:
        n = (await s.execute(sa.select(sa.func.count()).select_from(DeviceStatus))).scalar_one()
        faults = (
            (await s.execute(sa.select(AuditLog).where(AuditLog.action == "device_fault")))
            .scalars()
            .all()
        )
        row = (await s.execute(sa.select(DeviceStatus))).scalar_one()
    assert n == 1 and row.err_codes == ["close_unconf"] and row.firmware_version == "2.0"
    assert len(faults) == 1  # FOR UPDATE serialises: the code is "new" exactly once


async def test_template_text_is_committed_before_the_llm_answers(rt, pgdb, society):
    gate = asyncio.Event()

    class Slow:
        source = "template"

        def message(self, ctx):
            return "TEMPLATE"

        async def generate(self, ctx, *, cache_ref=None):
            await gate.wait()
            return ExplainResult("LLM", "anthropic")

    rt.explainer = Slow()
    async with pgdb.session() as s:
        fid = (await FlatRepository(s).get_by_slug_unit("green-valley", "101")).id
    _, aid = await make_alert(pgdb, rt, fid, severity=0.75)
    async with pgdb.session() as s:  # LLM still running, alert already visible with template text
        a = await s.get(Alert, aid)
        assert (a.message, a.message_source) == ("TEMPLATE", "template")
    gate.set()
    while llm.regenerate._tasks:
        await asyncio.gather(*list(llm.regenerate._tasks))
    async with pgdb.session() as s:
        a = await s.get(Alert, aid)
        assert (a.message, a.message_source) == ("LLM", "anthropic")
