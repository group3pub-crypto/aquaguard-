"""aiomqtt wrapper: reconnecting subscriber + publisher."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import aiomqtt
import structlog

from app.config import Settings
from app.mqtt.topics import SUBSCRIPTIONS

log = structlog.get_logger(__name__)
Handler = Callable[[str, bytes], Awaitable[None]]


def _enable_manual_ack(client: aiomqtt.Client) -> bool:
    """paho acks QoS1 on receipt by default - before we stored anything. Switch to manual acks
    (uses aiomqtt's underlying paho client; falls back to auto-ack if that API is missing)."""
    try:
        client._client.manual_ack_set(True)
        return True
    except AttributeError:  # pragma: no cover
        return False


class MqttService:
    def __init__(
        self, settings: Settings, handler: Handler, *, client_id: str = "aquaguard-backend"
    ) -> None:
        self.settings = settings
        self.handler = handler
        self.client_id = client_id
        self.connected = False
        self.connect_count = 0
        self.workers = max(1, settings.mqtt_worker_concurrency)
        self._client: aiomqtt.Client | None = None

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        if not self.connected or self._client is None:
            raise ConnectionError("MQTT is not connected")
        await self._client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1)

    async def _consume(self, client: aiomqtt.Client, manual_ack: bool) -> None:
        """Process messages with bounded concurrency; ack to the broker only after the handler
        finished (at-least-once: a crash mid-processing makes the broker redeliver, and the
        (flat, message_id) unique key absorbs the duplicate)."""
        sem = asyncio.Semaphore(self.workers)
        pending: set[asyncio.Task] = set()

        async def work(msg: aiomqtt.Message) -> None:
            try:
                await self.handler(str(msg.topic), bytes(msg.payload or b""))
                if manual_ack:
                    client._client.ack(msg.mid, msg.qos)
            except asyncio.CancelledError:
                raise
            except Exception:  # handlers are written not to raise; never lose the loop
                log.exception("mqtt_handler_error", topic=str(msg.topic))
            finally:
                sem.release()

        try:
            async for msg in client.messages:
                await sem.acquire()
                task = asyncio.create_task(work(msg))
                pending.add(task)
                task.add_done_callback(pending.discard)
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def run(self) -> None:
        """Run forever; reconnect with capped exponential backoff."""
        backoff = 1.0
        s = self.settings
        while True:
            try:
                client = aiomqtt.Client(
                    hostname=s.mqtt_host,
                    port=s.mqtt_port,
                    username=s.mqtt_username,
                    password=s.mqtt_password,
                    identifier=self.client_id,
                    keepalive=30,
                    # persistent session: the broker queues QoS1 messages while we restart
                    # and redelivers them (possible duplicates are absorbed by message_id)
                    clean_session=False,
                    tls_params=(
                        aiomqtt.TLSParameters(ca_certs=str(s.mqtt_ca_file))
                        if s.mqtt_ca_file
                        else None
                    ),
                )
                manual_ack = _enable_manual_ack(client)  # before connect: queued msgs
                async with client:
                    # (re)subscribe on EVERY connection; only then report "connected"
                    for topic in SUBSCRIPTIONS:
                        await client.subscribe(topic, qos=1)
                    self._client, self.connected, backoff = client, True, 1.0
                    self.connect_count += 1
                    log.info("mqtt_connected", host=s.mqtt_host, n=self.connect_count)
                    await self._consume(client, manual_ack)
                    log.warning("mqtt_message_stream_ended")
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as exc:
                log.warning("mqtt_disconnected", error=str(exc), retry_in=backoff)
            except Exception:  # a bug in the handler must not kill the ingest loop for good
                log.exception("mqtt_loop_error", retry_in=backoff)
            finally:
                self.connected, self._client = False, None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
