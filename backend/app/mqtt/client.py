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


class MqttService:
    def __init__(self, settings: Settings, handler: Handler) -> None:
        self.settings = settings
        self.handler = handler
        self.connected = False
        self._client: aiomqtt.Client | None = None

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        if not self.connected or self._client is None:
            raise ConnectionError("MQTT is not connected")
        await self._client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1)

    async def run(self) -> None:
        """Run forever; reconnect with capped exponential backoff."""
        backoff = 1.0
        s = self.settings
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=s.mqtt_host,
                    port=s.mqtt_port,
                    username=s.mqtt_username,
                    password=s.mqtt_password,
                    identifier="aquaguard-backend",
                ) as client:
                    self._client, self.connected, backoff = client, True, 1.0
                    for topic in SUBSCRIPTIONS:
                        await client.subscribe(topic, qos=1)
                    log.info("mqtt_connected", host=s.mqtt_host)
                    async for msg in client.messages:
                        await self.handler(str(msg.topic), bytes(msg.payload or b""))
            except aiomqtt.MqttError as exc:
                log.warning("mqtt_disconnected", error=str(exc), retry_in=backoff)
            finally:
                self.connected, self._client = False, None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
