"""Latest firmware-reported device status + safety-relevant error codes.

``FlowPayload`` carries optional ``tot``, ``vbat``, ``valve``, ``fw``, ``err`` (see
docs/mqtt.md). The newest values live in ONE row per flat (``device_status``). When a code
appears that was not in the previous report, a safety-relevant one is recorded in the audit
log and broadcast as a WebSocket ``device_fault`` event so a human sees it.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from app.repositories import AuditRepository, DeviceStatusRepository, ValveRepository
from app.runtime import Runtime

log = structlog.get_logger(__name__)
WRITE_EVERY_S = 60.0  # unchanged status is persisted at most this often per flat

# Codes of edge/firmware/aquaguard_sensor/aquaguard_core.h (names as sent in `err`) plus the
# umbrella names used in the ops docs. critical = a human must act; warning = fix soon.
CRITICAL = {
    "close_unconf": "valve was commanded closed but flow did not stop (or came back)",
    "flow_impossible": "sensor fault: physically impossible flow",
    "stuck_line": "sensor fault: flow sensor stuck",
    "adc_fault": "sensor fault: battery ADC out of range",
    "sensor_fault": "sensor fault",
    "coil_duty": "valve coil duty limit hit",
    "bat_crit": "battery critical",
}
WARNING = {
    "bat_low": "battery low",
    "batt_low": "battery low",
    "ts_est": "device clock not trusted (timestamps reconstructed)",
    "time_invalid": "device clock invalid",
    "backfill_drop": "readings were dropped on the device (history gap)",
    "store_fail": "device storage write failed",
    "rst_brownout": "device reset by brown-out",
    "rst_wdt": "device reset by watchdog",
}
# local leak detectors of the firmware: recorded, but the backend raises its own alerts
INFORMATIONAL = {"burst", "leak_cont", "leak_night"}


def severity_of(code: str) -> str | None:
    if code in CRITICAL:
        return "critical"
    if code in WARNING:
        return "warning"
    return None


def status_dict(row: Any) -> dict[str, Any]:
    return {
        "firmware_version": row.firmware_version,
        "battery_pct": row.battery_pct,
        "battery_volts": row.battery_volts,
        "total_volume": row.total_volume,
        "valve_reported": row.valve_reported,
        "err_codes": list(row.err_codes or []),
        "reported_at": row.reported_at.isoformat() if row.reported_at else None,
    }


async def apply_report(
    rt: Runtime,
    flat_id: UUID,
    society_id: UUID,
    ts: datetime,
    *,
    fw: str | None = None,
    bat: float | None = None,
    vbat: float | None = None,
    tot: float | None = None,
    valve: str | None = None,
    err: list[str] | None = None,
) -> bool:
    """Store a device report. ``err=None`` leaves the error list alone (heartbeats). Older
    reports never overwrite newer ones. Returns True when a row was written. Never raises."""
    sig = (fw, valve, tuple(err) if err is not None else None)
    prev = rt.device_cache.get(flat_id)
    mono = time.monotonic()
    if prev and prev[0] == sig and mono - prev[1] < WRITE_EVERY_S:
        return False
    try:
        events: list[dict[str, Any]] = []
        async with rt.db.session() as session:
            row = await DeviceStatusRepository(session).lock_or_create(flat_id)
            if row.reported_at is not None and ts < row.reported_at:
                return False  # out-of-order / replayed report
            old_err = set(row.err_codes or [])
            changed = (fw is not None and fw != row.firmware_version) or (
                valve is not None and valve != row.valve_reported
            )
            if fw is not None:
                row.firmware_version = fw
            if bat is not None:
                row.battery_pct = float(bat)
            if vbat is not None:
                row.battery_volts = vbat
            if tot is not None:
                row.total_volume = tot
            if valve is not None:
                row.valve_reported = valve
            if err is not None:
                new_err = list(dict.fromkeys(err))
                changed = changed or set(new_err) != old_err
                row.err_codes = new_err
                for code in new_err:
                    sev = severity_of(code)
                    if code in old_err or sev is None:
                        continue
                    ev: dict[str, Any] = {"code": code, "severity": sev}
                    if code == "close_unconf":
                        last = next(
                            (
                                c
                                for c in await ValveRepository(session).recent(flat_id, 5)
                                if c.command == "close"
                            ),
                            None,
                        )
                        ev["valve_command"] = str(last.id) if last else None
                        ev["action_required"] = (
                            "water may still be flowing: check the valve on site"
                        )
                    events.append(ev)
            row.reported_at = ts
            for ev in events:
                await AuditRepository(session).record(
                    entity_type="device",
                    entity_id=flat_id,
                    action="device_fault",
                    actor="device",
                    details={**ev, "err_codes": list(row.err_codes)},
                    flat_id=flat_id,
                    society_id=society_id,
                )
            snapshot = status_dict(row)
        rt.device_cache[flat_id] = (sig, mono)
        keys = [str(flat_id), str(society_id)]
        for ev in events:
            log.warning("device_fault", flat=str(flat_id), **ev)
            rt.counters["device_fault"] = rt.counters.get("device_fault", 0) + 1
            await rt.ws.broadcast({"type": "device_fault", "flat_id": str(flat_id), **ev}, keys)
        if changed:
            await rt.ws.broadcast(
                {"type": "sensor", "flat_id": str(flat_id), "status": "online", "device": snapshot},
                keys,
            )
        return True
    except Exception:
        log.exception("device_status_failed", flat=str(flat_id))
        return False
