"""Demo/simulator control. Mounted only when DEMO_MODE is on."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import RT, AdminUser, Session, require_user
from app.mqtt.topics import CONTROL_FAULT, CONTROL_SPEED
from app.repositories import AuditRepository, FlatRepository
from app.schemas.tools import ActiveFault, ClearFaultRequest, DemoStatus, FaultRequest, SpeedRequest

router = APIRouter(prefix="/demo", tags=["demo"])


async def _publish(rt, topic: str, payload: dict) -> None:
    try:
        await rt.publisher.publish(topic, payload)
    except Exception:
        raise HTTPException(503, "MQTT broker unavailable") from None


async def _flat_info(session, flat_id):
    flats = FlatRepository(session)
    flat = await flats.get(flat_id)
    if flat is None:
        raise HTTPException(404, "Flat not found")
    return flat, await flats.society_slug(flat)


def _status(rt) -> DemoStatus:
    return DemoStatus(
        active_faults=[
            ActiveFault(flat_id=f, fault_type=t) for f, t in rt.demo.active_faults.items()
        ],
        speed=rt.demo.speed,
        simulator_running=rt.simulator_running(),
    )


@router.post("/inject-fault", response_model=DemoStatus)
async def inject_fault(body: FaultRequest, admin: AdminUser, session: Session, rt: RT):
    flat, slug = await _flat_info(session, body.flat_id)
    await _publish(
        rt,
        CONTROL_FAULT,
        {
            "v": 1,
            "action": "inject",
            "flat_id": str(flat.id),
            "society": slug,
            "unit": flat.unit_number,
            "fault_type": body.fault_type,
            "params": body.params or {},
        },
    )
    rt.demo.active_faults[flat.id] = body.fault_type
    await AuditRepository(session).record(
        entity_type="demo",
        entity_id=flat.id,
        action="fault_injected",
        actor=admin.username,
        details={"fault_type": body.fault_type},
        flat_id=flat.id,
        society_id=flat.society_id,
    )
    return _status(rt)


@router.post("/clear-fault", response_model=DemoStatus)
async def clear_fault(body: ClearFaultRequest, admin: AdminUser, session: Session, rt: RT):
    flat, slug = await _flat_info(session, body.flat_id)
    await _publish(
        rt,
        CONTROL_FAULT,
        {
            "v": 1,
            "action": "clear",
            "flat_id": str(flat.id),
            "society": slug,
            "unit": flat.unit_number,
        },
    )
    rt.demo.active_faults.pop(flat.id, None)
    await AuditRepository(session).record(
        entity_type="demo",
        entity_id=flat.id,
        action="fault_cleared",
        actor=admin.username,
        flat_id=flat.id,
        society_id=flat.society_id,
    )
    return _status(rt)


@router.post("/set-speed", response_model=DemoStatus)
async def set_speed(body: SpeedRequest, admin: AdminUser, rt: RT):
    await _publish(rt, CONTROL_SPEED, {"v": 1, "speed": body.speed})
    rt.demo.speed = body.speed
    return _status(rt)


@router.get("/status", response_model=DemoStatus, dependencies=[Depends(require_user)])
async def status(rt: RT):
    return _status(rt)
