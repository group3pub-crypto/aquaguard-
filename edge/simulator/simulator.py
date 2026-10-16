"""Broker-independent simulation core plus the asyncio MQTT runner.

``Simulator.due()`` and ``Simulator.handle_message()`` return the messages to publish, so the
whole behaviour is unit-testable without a broker; ``run_mqtt`` only moves bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from edge.simulator.faults import ActiveFault, FaultType, apply_faults, make_fault
from edge.simulator.patterns import sample_flow
from edge.simulator.profiles import Profile, make_profiles

log = logging.getLogger("aquaguard.edge")

FW_VERSION = "1.2.0"
HEARTBEAT_EVERY_MIN = 5
MAX_CATCHUP_MIN = 500  # skip ahead rather than flood after a long stall/outage
SPEED_MIN, SPEED_MAX = 0.1, 86400.0
CONTROL_FAULT_TOPIC = "aquaguard/control/fault"
CONTROL_SPEED_TOPIC = "aquaguard/control/speed"


# ---------------------------------------------------------------- payload builders (pure)
def make_msg_id(seq: int, salt: int) -> str:
    """8 hex chars; bijective in ``seq`` (odd multiplier mod 2**32) so ids never repeat."""
    return f"{(seq * 2654435761 + salt) & 0xFFFFFFFF:08x}"


def flow_payload(
    msg_id: str, ts: int, flow: float, vol: float, bat: float, rssi: int
) -> dict[str, Any]:
    return {
        "v": 1,
        "id": msg_id,
        "ts": int(ts),
        "flow": round(float(flow), 3),
        "vol": round(float(vol), 3),
        "psi": None,
        "tmp": None,
        "bat": round(float(bat), 1),
        "rssi": int(rssi),
    }


def heartbeat_payload(ts: int, uptime: int, bat: float) -> dict[str, Any]:
    return {
        "v": 1,
        "ts": int(ts),
        "uptime": int(uptime),
        "bat": round(float(bat), 1),
        "fw": FW_VERSION,
    }


def ack_payload(cmd_id: str, state: str, ts: int) -> dict[str, Any]:
    return {"v": 1, "cmd_id": cmd_id, "state": state, "ts": int(ts)}


# ---------------------------------------------------------------- inbound message schemas
class ValveCmd(BaseModel):
    model_config = ConfigDict(extra="ignore")
    v: int = 1
    id: str
    cmd: Literal["open", "close"]
    src: str | None = None
    ts: float | None = None


class FaultControl(BaseModel):
    model_config = ConfigDict(extra="ignore")
    flat_id: str | None = None
    unit: str | int | None = None
    fault_type: FaultType | None = None
    params: dict[str, Any] = {}
    action: Literal["inject", "clear"] = "inject"

    @model_validator(mode="after")
    def _check(self) -> FaultControl:
        if self.flat_id is None and self.unit is None:
            raise ValueError("flat_id or unit required")
        if self.action == "inject" and self.fault_type is None:
            raise ValueError("fault_type required for inject")
        return self


class SpeedControl(BaseModel):
    model_config = ConfigDict(extra="ignore")
    speed: float


@dataclass(frozen=True)
class Outgoing:
    topic: str
    payload: bytes
    qos: int = 1
    retain: bool = False


def _out(topic: str, body: dict[str, Any], retain: bool = False) -> Outgoing:
    return Outgoing(topic, json.dumps(body, separators=(",", ":")).encode(), 1, retain)


# ---------------------------------------------------------------- clock
class SimClock:
    """Maps real (monotonic) seconds to simulated epoch seconds. speed 1 = realtime."""

    def __init__(self, start_epoch: float, speed: float = 1.0, real_now: float = 0.0):
        self._sim0, self._real0, self.speed = float(start_epoch), float(real_now), speed

    def now(self, real_now: float) -> float:
        return self._sim0 + (real_now - self._real0) * self.speed

    def set_speed(self, speed: float, real_now: float) -> None:
        self._sim0, self._real0 = self.now(real_now), real_now  # keep time continuous
        self.speed = min(SPEED_MAX, max(SPEED_MIN, float(speed)))


# ---------------------------------------------------------------- one sensor node
class FlatSimulator:
    def __init__(self, profile: Profile, society: str, seed: int, index: int, start_epoch: float):
        self.profile, self.society, self.seed = profile, society, seed
        self.rng = np.random.default_rng([seed, 3, index])
        base_salt = int(np.random.default_rng([seed, 4, index]).integers(0, 2**32))
        # mix in the boot time: a restarted device restarts `seq` at 1, and the backend dedupes on
        # (flat, id), so identical ids after a reboot would silently drop fresh readings
        self.salt = (base_salt + int(start_epoch)) & 0xFFFFFFFF
        self.valve_open = True
        self.volume_l = 0.0
        self.seq = 0
        self.boot_ts = start_epoch
        self.faults: list[ActiveFault] = []

    def topic(self, leaf: str) -> str:
        return f"aquaguard/{self.society}/{self.profile.unit}/{leaf}"

    def battery(self, ts: float) -> float:
        return max(0.0, 100.0 - (ts - self.boot_ts) / 3600.0 * 0.01)

    def current_flow(self, ts: float) -> float:
        if not self.valve_open:
            return 0.0
        self.faults = [f for f in self.faults if not f.expired(ts)]
        base = sample_flow(self.profile, ts, self.rng, self.seed)
        return apply_faults(base, self.faults, ts, self.rng)

    def flow_message(self, ts: float) -> Outgoing:
        flow = self.current_flow(ts)
        self.volume_l += flow / 1.0  # one reading per simulated minute, L/min * 1 min
        self.seq += 1
        rssi = int(np.clip(-62 + self.rng.normal(0, 3), -90, -40))
        body = flow_payload(
            make_msg_id(self.seq, self.salt), ts, flow, self.volume_l, self.battery(ts), rssi
        )
        return _out(self.topic("flow"), body)

    def heartbeat_message(self, ts: float) -> Outgoing:
        return _out(
            self.topic("heartbeat"), heartbeat_payload(ts, ts - self.boot_ts, self.battery(ts))
        )

    def handle_valve(self, cmd: ValveCmd, ts: float) -> Outgoing:
        self.valve_open = cmd.cmd == "open"  # idempotent; re-acked on duplicates
        state = "open" if self.valve_open else "closed"
        return _out(self.topic("valve/state"), ack_payload(cmd.id, state, ts), retain=True)


# ---------------------------------------------------------------- fleet
class Simulator:
    def __init__(
        self,
        society: str = "demo",
        flats: int = 6,
        speed: float = 1.0,
        seed: int = 42,
        start_epoch: float | None = None,
        unit_base: int = 101,
        flat_ids: dict[str, str] | None = None,
        real_now: float | None = None,
        units: list[str] | None = None,
    ):
        self.society, self.seed = society, seed
        real_now = time.monotonic() if real_now is None else real_now
        start = (time.time() if start_epoch is None else start_epoch) // 60 * 60
        self.clock = SimClock(start, min(SPEED_MAX, max(SPEED_MIN, speed)), real_now)
        self.next_minute = int(start)
        profiles = make_profiles(flats, seed, unit_base, flat_ids, units)
        self.flats = {
            p.unit: FlatSimulator(p, society, seed, i, start) for i, p in enumerate(profiles)
        }
        self._by_flat_id = {fs.profile.flat_id: fs for fs in self.flats.values()}

    # -- topics
    def subscriptions(self) -> list[str]:
        return [f"aquaguard/{self.society}/+/valve/cmd", CONTROL_FAULT_TOPIC, CONTROL_SPEED_TOPIC]

    def resolve(self, unit: str | int | None, flat_id: str | None) -> list[FlatSimulator]:
        if flat_id == "all":
            return list(self.flats.values())
        if unit is not None and str(unit) in self.flats:
            return [self.flats[str(unit)]]
        if flat_id is not None:
            if flat_id in self._by_flat_id:
                return [self._by_flat_id[flat_id]]
            if flat_id in self.flats:  # backend may put the unit number in flat_id
                return [self.flats[flat_id]]
        return []

    # -- time-driven output
    def due(self, real_now: float) -> list[Outgoing]:
        """All flow/heartbeat messages for simulated minutes elapsed since the last call."""
        now_min = int(self.clock.now(real_now) // 60 * 60)
        if now_min - self.next_minute > MAX_CATCHUP_MIN * 60:
            self.next_minute = now_min - MAX_CATCHUP_MIN * 60
        out: list[Outgoing] = []
        while self.next_minute <= now_min:
            ts = self.next_minute
            beat = (ts // 60) % HEARTBEAT_EVERY_MIN == 0
            for fs in self.flats.values():
                out.append(fs.flow_message(ts))
                if beat:
                    out.append(fs.heartbeat_message(ts))
            self.next_minute += 60
        return out

    # -- message-driven output
    def handle_message(
        self, topic: str, payload: bytes, real_now: float | None = None
    ) -> list[Outgoing]:
        real_now = time.monotonic() if real_now is None else real_now
        try:
            data = json.loads(payload)
            if topic == CONTROL_SPEED_TOPIC:
                self.clock.set_speed(SpeedControl(**data).speed, real_now)
                return []
            if topic == CONTROL_FAULT_TOPIC:
                self._handle_fault(FaultControl(**data), self.clock.now(real_now))
                return []
            parts = topic.split("/")
            if len(parts) == 5 and parts[1] == self.society and parts[3:] == ["valve", "cmd"]:
                fs = self.flats.get(parts[2])
                if fs is None:
                    log.warning("valve cmd for unknown unit %s", parts[2])
                    return []
                return [fs.handle_valve(ValveCmd(**data), self.clock.now(real_now))]
        except (ValueError, TypeError, ValidationError) as e:
            log.warning("ignoring bad message on %s: %s", topic, e)
        return []

    def _handle_fault(self, msg: FaultControl, ts: float) -> None:
        targets = self.resolve(msg.unit, msg.flat_id)
        if not targets:
            log.warning(
                "fault control for unknown flat (unit=%s flat_id=%s)", msg.unit, msg.flat_id
            )
        for fs in targets:
            if msg.action == "clear":
                fs.faults = [
                    f
                    for f in fs.faults
                    if msg.fault_type is not None and f.fault_type != msg.fault_type
                ]
            else:
                fs.faults.append(make_fault(msg.fault_type, ts, fs.rng, msg.params))


# ---------------------------------------------------------------- MQTT runner
async def run_mqtt(
    sim: Simulator,
    host: str,
    port: int,
    stop: asyncio.Event,
    username: str | None = None,
    password: str | None = None,
    tick_s: float = 0.2,
) -> None:
    import aiomqtt

    backoff = 1.0
    while not stop.is_set():
        try:
            async with aiomqtt.Client(
                hostname=host,
                port=port,
                username=username,
                password=password,
                identifier=f"aquaguard-edge-{sim.society}",
                keepalive=30,
            ) as client:
                backoff = 1.0
                log.info("connected to %s:%s", host, port)
                for t in sim.subscriptions():
                    await client.subscribe(t, qos=1)

                async def publish_loop() -> None:
                    while True:
                        for m in sim.due(time.monotonic()):
                            await client.publish(m.topic, m.payload, qos=m.qos, retain=m.retain)
                        await asyncio.sleep(tick_s)

                async def consume_loop() -> None:
                    async for msg in client.messages:
                        for m in sim.handle_message(str(msg.topic), bytes(msg.payload)):
                            await client.publish(m.topic, m.payload, qos=m.qos, retain=m.retain)

                tasks = [
                    asyncio.create_task(publish_loop()),
                    asyncio.create_task(consume_loop()),
                    asyncio.create_task(stop.wait()),
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for d in done:
                    d.result()  # re-raise MqttError to trigger reconnect
        except aiomqtt.MqttError as e:
            if stop.is_set():
                break
            log.warning("mqtt error (%s); reconnecting in %.0fs", e, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)
    log.info("stopped")


# ---------------------------------------------------------------- per-device credentials
def device_username(society: str, unit: str) -> str:
    return f"{society}/{unit}"


def device_password(secret: str, society: str, unit: str) -> str:
    """Same derivation as infra/mosquitto/entrypoint.sh: first 32 hex of sha256(secret:user)."""
    return hashlib.sha256(f"{secret}:{device_username(society, unit)}".encode()).hexdigest()[:32]


async def run_mqtt_per_device(
    sim: Simulator,
    host: str,
    port: int,
    stop: asyncio.Event,
    device_secret: str,
    control_username: str | None = None,
    control_password: str | None = None,
    tick_s: float = 0.2,
) -> None:
    """One broker connection per simulated flat, each with its own broker account (ACL-limited
    to that flat's topics), plus one control connection for fault/speed messages."""
    import aiomqtt

    queues: dict[str, asyncio.Queue[Outgoing]] = {u: asyncio.Queue() for u in sim.flats}

    async def reconnecting(name: str, body) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                await body()
            except aiomqtt.MqttError as e:
                if stop.is_set():
                    return
                log.warning("%s: mqtt error (%s); reconnecting in %.0fs", name, e, backoff)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)

    async def device(unit: str) -> None:
        async def body() -> None:
            async with aiomqtt.Client(
                hostname=host,
                port=port,
                username=device_username(sim.society, unit),
                password=device_password(device_secret, sim.society, unit),
                identifier=f"aquaguard-edge-{sim.society}-{unit}",
                keepalive=30,
            ) as client:
                await client.subscribe(f"aquaguard/{sim.society}/{unit}/valve/cmd", qos=1)

                async def send() -> None:
                    while True:
                        m = await queues[unit].get()
                        await client.publish(m.topic, m.payload, qos=m.qos, retain=m.retain)

                async def recv() -> None:
                    async for msg in client.messages:
                        for m in sim.handle_message(str(msg.topic), bytes(msg.payload)):
                            await client.publish(m.topic, m.payload, qos=m.qos, retain=m.retain)

                tasks = [asyncio.create_task(send()), asyncio.create_task(recv())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for d in done:
                    d.result()

        await reconnecting(f"device {unit}", body)

    async def control() -> None:
        async def body() -> None:
            async with aiomqtt.Client(
                hostname=host,
                port=port,
                username=control_username,
                password=control_password,
                identifier=f"aquaguard-edge-{sim.society}-control",
                keepalive=30,
            ) as client:
                await client.subscribe(CONTROL_FAULT_TOPIC, qos=1)
                await client.subscribe(CONTROL_SPEED_TOPIC, qos=1)
                async for msg in client.messages:
                    sim.handle_message(str(msg.topic), bytes(msg.payload))

        await reconnecting("control", body)

    async def ticker() -> None:
        while True:
            for m in sim.due(time.monotonic()):
                queues[m.topic.split("/")[2]].put_nowait(m)
            await asyncio.sleep(tick_s)

    tasks = [asyncio.create_task(device(u)) for u in sim.flats]
    tasks.append(asyncio.create_task(ticker()))
    if control_username:
        tasks.append(asyncio.create_task(control()))
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("stopped")
