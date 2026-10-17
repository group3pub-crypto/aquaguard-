"""Service-account auth: login, rotating refresh, single-flight, family revoke -> re-login."""

import asyncio
import logging

import httpx
import pytest

from bot.api_client import ApiClient, ApiError
from tests.support import AL1, BackendSim, make_api


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_login_stores_both_tokens_and_reuses_them():
    sim = BackendSim()
    api = make_api(sim)
    await api.societies()
    await api.societies()
    assert sim.auth.logins == 1 and sim.auth.refreshes == 0


async def test_401_refreshes_once_rotates_and_retries():
    sim = BackendSim()
    api = make_api(sim)
    await api.societies()
    first_refresh = api._refresh
    sim.auth.expire_access()
    assert await api.societies()
    assert sim.auth.refreshes == 1 and sim.auth.logins == 1
    assert api._refresh != first_refresh  # the NEW refresh token is stored
    sim.auth.expire_access()
    await api.societies()  # second rotation uses the rotated token: no reuse
    assert sim.auth.refreshes == 2 and sim.auth.reuse_detected == 0 and sim.auth.logins == 1


async def test_proactive_refresh_before_expiry():
    sim, clock = BackendSim(), Clock()
    api = make_api(sim, clock)
    await api.societies()
    clock.t += 900 - 10  # inside the 30 s skew
    await api.societies()
    assert sim.auth.refreshes == 1 and sim.auth.logins == 1


async def test_refresh_is_single_flight_under_concurrency():
    sim = BackendSim()
    api = make_api(sim)
    await api.societies()
    sim.auth.expire_access()
    results = await asyncio.gather(*(api.societies() for _ in range(8)))
    assert all(results)
    assert sim.auth.refreshes == 1  # one rotation shared by all callers
    assert sim.auth.reuse_detected == 0 and sim.auth.logins == 1


async def test_cold_start_concurrent_calls_log_in_once():
    sim = BackendSim()
    api = make_api(sim)
    await asyncio.gather(*(api.societies() for _ in range(6)))
    assert sim.auth.logins == 1


async def test_reused_refresh_token_revokes_family_and_bot_relogs_in():
    sim = BackendSim()
    api = make_api(sim)
    await api.societies()
    stale = api._refresh
    sim.auth.expire_access()
    await api.societies()  # rotates: `stale` is now spent
    # simulate a stale copy being presented again (e.g. a restarted process / lost response)
    api._refresh = stale
    sim.auth.expire_access()
    assert await api.societies()
    assert sim.auth.reuse_detected == 1  # backend revoked the family ...
    assert sim.auth.logins == 2  # ... and the bot logged in again with credentials
    assert api._refresh is not None and api._refresh != stale


async def test_refresh_transport_error_falls_back_to_login():
    sim = BackendSim()
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/refresh"):
            raise httpx.ConnectError("boom")
        return sim(req)

    api = ApiClient("http://backend", "bot", "pw", transport=httpx.MockTransport(handler), sleep=_nosleep)
    await api.societies()
    sim.auth.expire_access()
    assert await api.societies()
    assert sim.auth.logins == 2
    calls["n"] += 1


async def _nosleep(_):
    return None


async def test_second_401_after_renewal_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/login"):
            return httpx.Response(
                200,
                json={
                    "access_token": "a",
                    "refresh_token": "r" * 24,
                    "role": "admin",
                    "expires_in": 900,
                    "refresh_expires_in": 1,
                },
            )
        return httpx.Response(401)

    api = ApiClient("http://backend", "bot", "pw", transport=httpx.MockTransport(handler), sleep=_nosleep)
    with pytest.raises(ApiError) as e:
        await api.societies()
    assert e.value.status == 401


async def test_failed_login_raises_and_cools_down():
    sim = BackendSim()
    sim.auth.fail_login = True
    clock = Clock()
    api = make_api(sim, clock)
    with pytest.raises(ApiError):
        await api.societies()
    with pytest.raises(ApiError):
        await api.societies()
    assert sim.auth.logins == 1  # second call did not hit the backend again
    clock.t += 60
    sim.auth.fail_login = False
    assert await api.societies()


async def test_5xx_retried_with_jittered_backoff():
    sim = BackendSim()
    delays: list[float] = []
    n = {"c": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/societies":
            n["c"] += 1
            if n["c"] < 3:
                return httpx.Response(503)
        return sim(req)

    async def sleep(d: float) -> None:
        delays.append(d)

    api = ApiClient("http://backend", "bot", "pw", transport=httpx.MockTransport(handler), sleep=sleep)
    assert await api.societies()
    assert len(delays) == 2 and all(0.1 < d < 2.0 for d in delays)
    assert delays[0] != delays[1]


async def test_tokens_and_password_never_logged_or_repr(caplog):
    sim = BackendSim()
    api = make_api(sim)
    with caplog.at_level(logging.DEBUG):
        await api.societies()
        sim.auth.expire_access()
        await api.ack_alert(AL1, "telegram:1")
        sim.auth.fail_login = True
        stale = api._refresh
        api._refresh = None
        sim.auth.expire_access()
        with pytest.raises(ApiError) as e:
            await api.societies()
    blob = caplog.text + repr(api) + str(e.value)
    issued = [stale, *sim.auth.spent, *sim.auth.refresh, *sim.auth.access]
    assert issued and all(secret and secret not in blob for secret in [*issued, "pw"])
