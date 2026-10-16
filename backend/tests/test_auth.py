import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from jose import jwt

from app.rate_limit import limiter
from app.services import auth as auth_svc
from tests.conftest import ADMIN_PW, make_settings

API = "/api/v1"
GOOD = {"username": "alice_01", "password": "Sup3rsecret", "name": "Alice"}


async def test_register_creates_resident_without_leaking_hash(client):
    r = await client.post(f"{API}/auth/register", json=GOOD)
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "resident" and body["username"] == "alice_01"
    assert "password" not in body and "hashed_password" not in body


@pytest.mark.parametrize(
    "extra", [{"role": "admin"}, {"is_admin": True}, {"flat_id": str(uuid.uuid4())}]
)
async def test_register_rejects_extra_fields_such_as_role(client, extra):
    r = await client.post(f"{API}/auth/register", json={**GOOD, **extra})
    assert r.status_code == 422
    login = await client.post(
        f"{API}/auth/login", json={"username": "alice_01", "password": "Sup3rsecret"}
    )
    assert login.status_code == 401  # nothing was created


@pytest.mark.parametrize(
    "patch",
    [
        {"password": "short1"},
        {"password": "alllettersnodigit"},
        {"password": "1234567890"},
        {"username": "ab"},
        {"username": "bad name!"},
        {"username": "x" * 51},
        {"name": ""},
    ],
)
async def test_register_validation_rejects_weak_input(client, patch):
    assert (await client.post(f"{API}/auth/register", json={**GOOD, **patch})).status_code == 422


async def test_register_duplicate_username_conflicts(client):
    assert (await client.post(f"{API}/auth/register", json=GOOD)).status_code == 201
    assert (await client.post(f"{API}/auth/register", json=GOOD)).status_code == 409


async def test_login_success_returns_bearer_token(client, world, settings):
    r = await client.post(f"{API}/auth/login", json={"username": "admin", "password": ADMIN_PW})
    body = r.json()
    assert r.status_code == 200
    assert body["token_type"] == "bearer" and body["role"] == "admin"
    assert body["expires_in"] == settings.access_token_minutes * 60
    claims = jwt.decode(body["access_token"], settings.secret_key, algorithms=["HS256"])
    assert claims["username"] == "admin"


async def test_login_failures_are_indistinguishable(client, world):
    bad_pw = await client.post(f"{API}/auth/login", json={"username": "admin", "password": "nope"})
    no_user = await client.post(f"{API}/auth/login", json={"username": "ghost", "password": "nope"})
    assert bad_pw.status_code == no_user.status_code == 401
    assert bad_pw.json() == no_user.json()


async def test_login_verifies_a_hash_even_for_unknown_user(monkeypatch, db):
    calls = []
    real = auth_svc.verify_password
    monkeypatch.setattr(auth_svc, "verify_password", lambda p, h: calls.append(h) or real(p, h))
    from app.repositories import UserRepository

    async with db.session() as s:
        assert await auth_svc.authenticate(UserRepository(s), "ghost", "whatever1") is None
    assert len(calls) == 1 and calls[0] == auth_svc._DUMMY_HASH


async def test_login_rejects_extra_and_missing_fields(client):
    assert (await client.post(f"{API}/auth/login", json={"username": "a"})).status_code == 422


async def test_me_returns_current_user(client, world):
    r = await client.get(f"{API}/auth/me", headers=world.resident)
    assert r.status_code == 200 and r.json()["username"] == "resident"


async def test_expired_token_is_rejected(client, world, settings):
    user = SimpleNamespace(id=uuid.uuid4(), username="x", role="admin")
    token, _ = auth_svc.create_access_token(
        settings, user, now=datetime.now(UTC) - timedelta(hours=3)
    )
    r = await client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def test_tampered_token_is_rejected(client, world):
    token = world.admin["Authorization"].split()[1]
    head, payload, sig = token.split(".")
    flipped = sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")
    for bad in (f"{head}.{payload}.{flipped}", f"{head}.{payload}x.{sig}", "garbage"):
        r = await client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401


async def test_token_signed_with_other_key_or_alg_none_is_rejected(client, world):
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "username": "admin", "role": "admin", "exp": 9999999999},
        "another-secret-key-another-secret-key",
    )
    r = await client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    unsigned = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJ4Iiwicm9sZSI6ImFkbWluIn0."
    r = await client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {unsigned}"})
    assert r.status_code == 401


async def test_role_change_applies_immediately_and_is_audited(client, world, db):
    reg = await client.post(f"{API}/auth/register", json=GOOD)
    uid = reg.json()["id"]
    login = await client.post(
        f"{API}/auth/login", json={"username": "alice_01", "password": "Sup3rsecret"}
    )
    hdr = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (
        await client.put(f"{API}/auth/users/{uid}/role", json={"role": "admin"}, headers=hdr)
    ).status_code == 403
    r = await client.put(
        f"{API}/auth/users/{uid}/role", json={"role": "maintenance"}, headers=world.admin
    )
    assert r.status_code == 200 and r.json()["role"] == "maintenance"
    # same (old) token now carries maintenance rights
    alert_list = await client.get(f"{API}/alerts", headers=hdr)
    assert alert_list.status_code == 200
    from app.repositories import AuditRepository

    async with db.session() as s:
        rows = await AuditRepository(s).find(action="role_changed")
    assert (
        rows
        and rows[0].actor == "admin"
        and rows[0].details == {"from": "resident", "to": "maintenance"}
    )


async def test_role_change_validation(client, world):
    assert (
        await client.put(
            f"{API}/auth/users/{uuid.uuid4()}/role", json={"role": "admin"}, headers=world.admin
        )
    ).status_code == 404
    assert (
        await client.put(
            f"{API}/auth/users/{uuid.uuid4()}/role", json={"role": "root"}, headers=world.admin
        )
    ).status_code == 422


@pytest.fixture
def limited(tmp_path, db):
    from app.main import create_app

    app = create_app(make_settings(tmp_path, rate_limit_enabled=True), db)
    limiter.reset()
    yield app
    limiter.enabled = False
    limiter.reset()


async def test_login_rate_limit_5_per_minute(limited):
    import httpx
    from httpx import ASGITransport

    async with httpx.AsyncClient(transport=ASGITransport(app=limited), base_url="http://t") as c:
        codes = [
            (await c.post(f"{API}/auth/login", json={"username": "u", "password": "p"})).status_code
            for _ in range(7)
        ]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]


async def test_register_rate_limit_3_per_minute(limited):
    import httpx
    from httpx import ASGITransport

    async with httpx.AsyncClient(transport=ASGITransport(app=limited), base_url="http://t") as c:
        codes = [
            (
                await c.post(f"{API}/auth/register", json={**GOOD, "username": f"user{i}"})
            ).status_code
            for i in range(5)
        ]
    assert codes == [201, 201, 201, 429, 429]
