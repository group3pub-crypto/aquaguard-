"""Every endpoint: 401 without a token, 403 for an insufficient role."""

import uuid

import pytest

API = "/api/v1"
FID = str(uuid.uuid4())
UID = str(uuid.uuid4())
BODIES = {
    "POST /alerts/{id}/snooze": {"duration_minutes": 15},
    "POST /alerts/{id}/override": {"reason": "x"},
}

# (method, path, minimum role)  user = any authenticated, staff = admin|maintenance, admin
TABLE = [
    ("GET", "/societies", "user"),
    ("GET", f"/societies/{FID}", "user"),
    ("GET", f"/societies/{FID}/stats", "user"),
    ("GET", "/flats", "user"),
    ("GET", f"/flats/{FID}", "user"),
    ("GET", f"/flats/{FID}/readings", "user"),
    ("GET", f"/flats/{FID}/baseline", "user"),
    ("POST", f"/flats/{FID}/baseline/retrain", "admin"),
    ("GET", f"/flats/{FID}/forecast", "user"),
    ("POST", f"/flats/{FID}/forecast/generate", "admin"),
    ("GET", "/anomalies", "user"),
    ("GET", f"/anomalies/{FID}", "user"),
    ("GET", "/alerts", "user"),
    ("GET", f"/alerts/{FID}", "user"),
    ("POST", f"/alerts/{FID}/acknowledge", "staff"),
    ("POST", f"/alerts/{FID}/snooze", "staff"),
    ("POST", f"/alerts/{FID}/override", "admin"),
    ("POST", f"/alerts/{FID}/resolve", "staff"),
    ("GET", f"/valves/{FID}", "user"),
    ("POST", f"/valves/{FID}/open", "admin"),
    ("POST", f"/valves/{FID}/close", "admin"),
    ("POST", "/tools/active-alerts", "user"),
    ("POST", "/tools/flat-usage", "user"),
    ("POST", "/tools/top-losses", "user"),
    ("POST", "/tools/valve-control", "admin"),
    ("POST", "/tools/weekly-report", "user"),
    ("POST", "/demo/inject-fault", "admin"),
    ("POST", "/demo/clear-fault", "admin"),
    ("POST", "/demo/set-speed", "admin"),
    ("GET", "/demo/status", "user"),
    ("GET", "/auth/me", "user"),
    ("PUT", f"/auth/users/{UID}/role", "admin"),
]
IDS = [f"{m} {p.replace(FID, '{id}').replace(UID, '{uid}')}" for m, p, _ in TABLE]


def _call(client, method, path, headers=None):
    return client.request(
        method, API + path, headers=headers or {}, json={} if method != "GET" else None
    )


@pytest.mark.parametrize(("method", "path", "role"), TABLE, ids=IDS)
async def test_requires_authentication(client, world, method, path, role):
    r = await _call(client, method, path)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("method", "path", "role"),
    [t for t in TABLE if t[2] != "user"],
    ids=[i for i, t in zip(IDS, TABLE, strict=True) if t[2] != "user"],
)
async def test_wrong_role_is_forbidden(client, world, method, path, role):
    assert (await _call(client, method, path, world.resident)).status_code == 403
    if role == "admin":
        assert (await _call(client, method, path, world.maintenance)).status_code == 403


@pytest.mark.parametrize(
    ("method", "path", "role"),
    [t for t in TABLE if t[2] == "user" and t[0] == "GET"],
    ids=[i for i, t in zip(IDS, TABLE, strict=True) if t[2] == "user" and t[0] == "GET"],
)
async def test_any_authenticated_role_can_read(client, world, method, path, role):
    r = await _call(client, method, path, world.resident)
    assert r.status_code not in (401, 403)


@pytest.mark.parametrize(("method", "path"), [("GET", "/health"), ("GET", "/health/ready")])
async def test_health_is_public(client, method, path):
    r = await client.request(method, path)
    assert r.status_code == 200


async def test_login_and_register_are_public(client):
    assert (
        await client.post(f"{API}/auth/login", json={"username": "a", "password": "b"})
    ).status_code == 401
    r = await client.post(
        f"{API}/auth/register", json={"username": "pub_user", "password": "Passw0rd99", "name": "P"}
    )
    assert r.status_code == 201
