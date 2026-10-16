"""Real broker (+ real PostgreSQL): subscribe, ingest end to end, reconnect + resubscribe, ACL."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import aiomqtt
import pytest

from app.config import Settings
from app.mqtt.client import MqttService
from app.repositories import FlatRepository, ReadingRepository
from app.services import ingest
from tests.conftest import make_settings
from tests.helpers import flow_msg

pytestmark = [pytest.mark.integration, pytest.mark.mqtt]
TOPIC = "aquaguard/green-valley/101/flow"


def broker() -> dict:
    return dict(
        hostname=os.environ["TEST_MQTT_HOST"],
        port=int(os.environ["TEST_MQTT_PORT"]),
        username=os.environ["TEST_MQTT_USERNAME"],
        password=os.environ["TEST_MQTT_PASSWORD"],
    )


def mqtt_settings(tmp_path) -> Settings:
    b = broker()
    return make_settings(
        tmp_path,
        mqtt_host=b["hostname"],
        mqtt_port=b["port"],
        mqtt_username=b["username"],
        mqtt_password=b["password"],
    )


async def until(pred, timeout=15.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        res = pred()
        if asyncio.iscoroutine(res):
            res = await res
        if res:
            return res
        await asyncio.sleep(step)
    raise AssertionError("condition not met in time")


async def publish(topic: str, payload: bytes | str, **kw) -> None:
    async with aiomqtt.Client(identifier=f"it-pub-{uuid.uuid4().hex[:8]}", **broker()) as c:
        await c.publish(topic, payload, qos=1, **kw)


async def _has(db, fid, n: int) -> bool:
    return await _count(db, fid) == n


async def _count(db, fid) -> int:
    async with db.session() as s:
        return await ReadingRepository(s).count(fid)


async def test_flow_over_real_broker_lands_in_postgres_and_dedupes(rt, pgdb, society, tmp_path):
    svc = MqttService(
        mqtt_settings(tmp_path),
        lambda t, p: ingest.handle_message(rt, t, p),
        client_id=f"it-backend-{uuid.uuid4().hex[:6]}",
    )
    task = asyncio.create_task(svc.run())
    try:
        await until(lambda: svc.connected)
        async with pgdb.session() as s:
            fid = (await FlatRepository(s).get_by_slug_unit("green-valley", "101")).id
        dup = flow_msg(mid="c0ffee01")
        for _ in range(3):  # QoS1 redelivery / duplicate publish
            await publish(TOPIC, dup)
        await publish(TOPIC, flow_msg(mid="c0ffee02", flow=7.0))
        await until(lambda: _has(pgdb, fid, 2))
        await asyncio.sleep(0.5)
        assert await _count(pgdb, fid) == 2
        assert rt.counters["flow_ok"] == 2 and rt.counters["flow_duplicate"] == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_service_reconnects_and_resubscribes_after_connection_loss(rt, tmp_path):
    seen: list[str] = []

    async def handler(topic: str, payload: bytes) -> None:
        if topic.endswith("/flow"):  # ignore retained valve/state left on a shared broker
            seen.append(topic)

    cid = f"it-backend-{uuid.uuid4().hex[:6]}"
    svc = MqttService(mqtt_settings(tmp_path), handler, client_id=cid)
    task = asyncio.create_task(svc.run())
    try:
        await until(lambda: svc.connected)
        await publish(TOPIC, flow_msg(mid="aaaa0001"))
        await until(lambda: len(seen) == 1)
        # session takeover: a second client with the same id makes the broker drop the first
        async with aiomqtt.Client(identifier=cid, **broker()):
            await until(lambda: not svc.connected, timeout=10)
        # the takeover client has left; the service must come back and resubscribe
        await until(lambda: svc.connected and svc.connect_count >= 2, timeout=30)
        await publish(TOPIC, flow_msg(mid="aaaa0002"))
        await until(lambda: len(seen) == 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_handler_exception_does_not_kill_the_loop(tmp_path):
    calls = []

    async def handler(topic: str, payload: bytes) -> None:
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("boom")

    svc = MqttService(
        mqtt_settings(tmp_path), handler, client_id=f"it-backend-{uuid.uuid4().hex[:6]}"
    )
    task = asyncio.create_task(svc.run())
    try:
        await until(lambda: svc.connected)
        await publish(TOPIC, b"1")
        await until(lambda: len(calls) >= 1)
        await publish(TOPIC, b"2")
        await until(lambda: any(c == b"2" for c in calls))
        assert svc.connected and svc.connect_count == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_unacked_message_is_redelivered_after_crash_mid_processing(tmp_path):
    """At-least-once: a worker that never finishes (process killed) leaves the QoS1 message
    unacked, so the broker redelivers it to the restarted service."""
    cid = f"it-backend-{uuid.uuid4().hex[:6]}"
    got: list[bytes] = []
    started = asyncio.Event()

    async def hang(topic: str, payload: bytes) -> None:
        if topic.endswith("/flow"):
            started.set()
            await asyncio.sleep(3600)

    async def record(topic: str, payload: bytes) -> None:
        if topic.endswith("/flow"):
            got.append(payload)

    svc = MqttService(mqtt_settings(tmp_path), hang, client_id=cid)
    task = asyncio.create_task(svc.run())
    await until(lambda: svc.connected)
    await publish(TOPIC, b"must-not-be-lost")
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()  # "crash": handler never completed, message never acked
    await asyncio.gather(task, return_exceptions=True)

    svc2 = MqttService(mqtt_settings(tmp_path), record, client_id=cid)
    task2 = asyncio.create_task(svc2.run())
    try:
        await until(lambda: got == [b"must-not-be-lost"], timeout=20)
    finally:
        task2.cancel()
        await asyncio.gather(task2, return_exceptions=True)


async def test_anonymous_connection_is_refused():
    b = broker()
    with pytest.raises(aiomqtt.MqttError):
        async with aiomqtt.Client(b["hostname"], b["port"], identifier="it-anon"):
            pass


@pytest.mark.skipif(
    not os.environ.get("TEST_MQTT_DEVICE_SECRET"), reason="TEST_MQTT_DEVICE_SECRET not set"
)
async def test_device_acl_blocks_other_flats_and_foreign_commands():
    """Device 'green-valley/101' may only write its own topics and read its own valve/cmd."""
    import hashlib

    dev = "green-valley/101"
    pw = hashlib.sha256(f"{os.environ['TEST_MQTT_DEVICE_SECRET']}:{dev}".encode()).hexdigest()[:32]
    b = broker()
    received: list[tuple[str, bytes]] = []

    async def listen():
        async with aiomqtt.Client(identifier="it-acl-spy", **b) as spy:
            await spy.subscribe("aquaguard/green-valley/#", qos=1)
            ready.set()
            async for m in spy.messages:
                received.append((str(m.topic), bytes(m.payload)))

    ready = asyncio.Event()
    spy_task = asyncio.create_task(listen())
    await ready.wait()
    try:
        async with aiomqtt.Client(
            b["hostname"], b["port"], username=dev, password=pw, identifier="it-acl-dev"
        ) as d:
            await d.publish("aquaguard/green-valley/102/flow", b"OTHER-FLAT", qos=1)
            await d.publish("aquaguard/green-valley/101/valve/cmd", b"SPOOF-CMD", qos=1)
            await d.publish("aquaguard/control/fault", b"SPOOF-CTRL", qos=1)
            await d.publish("aquaguard/green-valley/101/flow", b"OWN-FLOW", qos=1)
            await d.publish("aquaguard/green-valley/101/valve/state", b"OWN-STATE", qos=1)
        await until(lambda: any(p == b"OWN-STATE" for _, p in received))
        payloads = {p for _, p in received}
        assert {b"OWN-FLOW", b"OWN-STATE"} <= payloads
        assert not payloads & {b"OTHER-FLAT", b"SPOOF-CMD", b"SPOOF-CTRL"}, payloads
    finally:
        spy_task.cancel()
        await asyncio.gather(spy_task, return_exceptions=True)
    with pytest.raises(aiomqtt.MqttError):
        async with aiomqtt.Client(
            b["hostname"], b["port"], username=dev, password="wrong", identifier="it-acl-bad"
        ):
            pass
