"""Ingest on real PostgreSQL: concurrent duplicates, concurrent distinct messages, health."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.repositories import FlatRepository, ReadingRepository, SensorHealthRepository
from app.services import ingest
from tests.helpers import TOPIC, flow_msg

pytestmark = pytest.mark.integration


async def _fid(db):
    async with db.session() as s:
        return (await FlatRepository(s).get_by_slug_unit("green-valley", "101")).id


async def test_concurrent_duplicate_inserts_store_exactly_one_row(rt, pgdb, society):
    msg = flow_msg(mid="deadbeef")
    await asyncio.gather(*[ingest.handle_message(rt, TOPIC, msg) for _ in range(30)])
    fid = await _fid(pgdb)
    async with pgdb.session() as s:
        assert await ReadingRepository(s).count(fid) == 1
    assert rt.counters["flow_ok"] == 1
    assert rt.counters["flow_duplicate"] == 29
    assert rt.counters["flow_failed"] == 0 and not rt.dead_letters


async def test_concurrent_duplicates_across_repeated_rounds(rt, pgdb, society):
    for rnd in range(5):
        msgs = [flow_msg(mid=f"{rnd:02x}{i % 4:06x}", ts=time.time() - 60 + rnd) for i in range(24)]
        await asyncio.gather(*[ingest.handle_message(rt, TOPIC, m) for m in msgs])
    fid = await _fid(pgdb)
    async with pgdb.session() as s:
        assert await ReadingRepository(s).count(fid) == 5 * 4
    assert rt.counters["flow_failed"] == 0 and not rt.dead_letters


async def test_concurrent_distinct_messages_on_fresh_flat_are_all_stored(rt, pgdb, society):
    """First messages for a flat race on creating its sensor_health row; none may be lost."""
    n = 25
    now = time.time()
    await asyncio.gather(
        *[
            ingest.handle_message(rt, TOPIC, flow_msg(mid=f"{i:08x}", ts=now - 100 + i))
            for i in range(n)
        ]
    )
    fid = await _fid(pgdb)
    async with pgdb.session() as s:
        assert await ReadingRepository(s).count(fid) == n
        health = await SensorHealthRepository(s).get_for_flat(fid)
    assert health.status == "online"
    assert rt.counters["flow_ok"] == n and rt.counters["flow_failed"] == 0, list(rt.dead_letters)


async def test_heartbeat_and_flow_race_on_sensor_health(rt, pgdb, society):
    hb = b'{"v":1,"ts":%d,"uptime":5,"bat":90,"fw":"1.0"}' % int(time.time())
    hb_topic = "aquaguard/green-valley/101/heartbeat"
    await asyncio.gather(
        *[ingest.handle_message(rt, hb_topic, hb) for _ in range(10)],
        *[ingest.handle_message(rt, TOPIC, flow_msg(mid=f"{i:08x}")) for i in range(10)],
    )
    assert rt.counters["heartbeat_failed"] == 0 and rt.counters["flow_failed"] == 0
    assert rt.counters["heartbeat_ok"] == 10 and rt.counters["flow_ok"] == 10


async def test_unknown_flat_and_bad_payload_are_dead_lettered_not_stored(rt, pgdb, society):
    await ingest.handle_message(rt, "aquaguard/green-valley/999/flow", flow_msg())
    await ingest.handle_message(rt, TOPIC, b"{not json")
    assert rt.counters["flow_failed"] == 2 and rt.counters["flow_ok"] == 0
