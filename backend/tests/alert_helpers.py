"""Shared helpers for alert / valve state-machine tests."""

import uuid
from datetime import timedelta

from app.models import FlowReading
from app.repositories import AlertRepository, FlatRepository
from app.services import alert_engine as ae
from app.services import valve_control
from tests.conftest import T0

H = timedelta(hours=1)
WARN = timedelta(seconds=300)
LATE = T0 + 6 * H  # an alert created at T0 may be shut off from here on


async def get(db, alert_id):
    async with db.session() as s:
        return await AlertRepository(s).get(alert_id)


async def escalate(db, rt, alert_id, now, **kw):
    async with db.session() as s:
        alert = await AlertRepository(s).get(alert_id)
        return await ae.escalate_alert(s, rt, alert, now, **kw)


async def ack(db, rt, flat_id, short_id, state, now):
    async with db.session() as s:
        flat = await FlatRepository(s).get(flat_id)
        return await valve_control.handle_ack(s, rt, flat, short_id, state, now)


async def add_readings(db, fid, flows, end, step_s=30):
    async with db.session() as s:
        for i, f in enumerate(flows):
            s.add(
                FlowReading(
                    flat_id=fid,
                    timestamp=end - timedelta(seconds=step_s * (len(flows) - i)),
                    flow_rate=f,
                    volume=0.1,
                    message_id=uuid.uuid4().hex[:8],
                )
            )


def audit_actions(rows):
    return [r.action for r in rows]
