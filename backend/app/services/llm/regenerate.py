"""Post-commit message upgrade: the alert exists first, the LLM text replaces the template later."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from sqlalchemy import select

from app.models import Alert
from app.services.llm.anthropic_explainer import SOURCE_FALLBACK, SOURCE_LLM

log = structlog.get_logger("app.services.llm")
_tasks: set[asyncio.Task[Any]] = set()
_CLOSED = ("resolved", "overridden")


async def regenerate_message(alert: Any, ctx: dict[str, Any], explainer: Any) -> str | None:
    """Regenerate `alert.message` / `alert.message_source` in place. Returns the new source.

    No-op (returns None) for explainers without `generate` (template backend) and for closed alerts.
    The caller commits. Never raises.
    """
    gen = getattr(explainer, "generate", None)
    if gen is None or getattr(alert, "state", None) in _CLOSED:
        return None
    result = await gen(ctx, cache_ref=str(alert.id))
    alert.message, alert.message_source = result.text, result.source
    return result.source


async def regenerate_alert(
    db: Any, alert_id: Any, ctx: dict[str, Any], explainer: Any
) -> str | None:
    """Own transaction: load the alert, regenerate, commit. `db` is app.database.Database."""
    try:
        async with db.session() as s:
            alert = (
                await s.execute(select(Alert).where(Alert.id == alert_id))
            ).scalar_one_or_none()
            if alert is None:
                return None
            return await regenerate_message(alert, ctx, explainer)
    except Exception as exc:
        log.warning("llm_regenerate_failed", error_type=type(exc).__name__)
        return None


def schedule_regeneration(
    db: Any, alert_id: Any, ctx: dict[str, Any], explainer: Any
) -> asyncio.Task[Any] | None:
    """Fire-and-forget; call AFTER the alert transaction commits. None if unsupported."""
    if not hasattr(explainer, "generate"):
        return None
    task = asyncio.get_running_loop().create_task(regenerate_alert(db, alert_id, ctx, explainer))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


__all__ = [
    "SOURCE_FALLBACK",
    "SOURCE_LLM",
    "regenerate_alert",
    "regenerate_message",
    "schedule_regeneration",
]
