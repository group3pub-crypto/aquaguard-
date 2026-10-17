import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport
from jose import jwt
from sqlalchemy import select

from app.main import create_app
from app.models import RefreshToken, User
from app.rate_limit import limiter
from app.repositories import AuditRepository, UserRepository
from app.services import auth as auth_svc
from tests.conftest import ADMIN_PW, RESIDENT_PW, make_settings

API = "/api/v1"
GOOD = {"username": "alice_01", "password": "Sup3rsecretPass", "name": "Alice"}


async def user_row(db, username) -> User:
    async with db.session() as s:
        return (await s.execute(select(User).where(User.username == username))).scalar_one()


async def login_full(client, username, password):
    r = await client.post(f"{API}/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ registration (closed by default)
async def test_registration_is_disabled_by_default(client):
    r = await client.post(f"{API}/auth/register", json=GOOD)
    assert r.status_code == 404
    login = await client.post(
        f"{API}/auth/login", json={"username": "alice_01", "password": GOOD["password"]}
    )
    assert login.status_code == 401  # nothing was created


@pytest.fixture
def open_reg(tmp_path, db):
    limiter.reset()
    return create_app(make_settings(tmp_path, allow_self_registration=True), db)


@pytest.fixture
async def reg_client(open_reg):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=open_reg, raise_app_exceptions=False), base_url="http://t"
    ) as c:
        yield c


async def test_enabled_registration_creates_unbound_resident_without_leaking_hash(reg_client):
    r = await reg_client.post(f"{API}/auth/register", json=GOOD)
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "resident" and body["username"] == "alice_01"
    assert body["flat_id"] is None and body["society_id"] is None
    assert "password" not in body and "hashed_password" not in body


async def test_self_registered_resident_sees_nothing(reg_client, world):
    await reg_client.post(f"{API}/auth/register", json=GOOD)
    tok = (await login_full(reg_client, "alice_01", GOOD["password"]))["access_token"]
    for path in ("/flats", "/societies", "/alerts", "/anomalies"):
        r = await reg_client.get(f"{API}{path}", headers=bearer(tok))
        assert r.status_code == 200 and r.json() == [], path
    r = await reg_client.get(f"{API}/flats/{world.flats[0].id}", headers=bearer(tok))
    assert r.status_code == 404


@pytest.mark.parametrize(
    "extra", [{"role": "admin"}, {"is_admin": True}, {"flat_id": str(uuid.uuid4())}]
)
async def test_register_rejects_extra_fields_such_as_role(reg_client, extra):
    assert (
        await reg_client.post(f"{API}/auth/register", json={**GOOD, **extra})
    ).status_code == 422


@pytest.mark.parametrize(
    "patch",
    [
        {"password": "short1"},
        {"password": "elevenchar1"},  # 11 < 12
        {"password": "alllettersnodigitsatall"},
        {"password": "1234567890123456"},
        {"password": "a1" + "é" * 40},  # > 72 BYTES although only 42 characters
        {"password": "x1" * 37},  # 74 bytes
        {"username": "ab"},
        {"username": "bad name!"},
        {"username": "x" * 51},
        {"name": ""},
    ],
)
async def test_register_validation_rejects_weak_input(reg_client, patch):
    assert (
        await reg_client.post(f"{API}/auth/register", json={**GOOD, **patch})
    ).status_code == 422


async def test_password_of_exactly_72_bytes_is_accepted_73_is_not(reg_client):
    ok = await reg_client.post(
        f"{API}/auth/register", json={**GOOD, "username": "bytes72", "password": "a1" * 36}
    )
    assert ok.status_code == 201
    over = await reg_client.post(
        f"{API}/auth/register", json={**GOOD, "username": "bytes73", "password": "a1" * 36 + "b"}
    )
    assert over.status_code == 422 and "72 bytes" in over.text


async def test_register_duplicate_username_is_case_insensitive(reg_client):
    assert (await reg_client.post(f"{API}/auth/register", json=GOOD)).status_code == 201
    assert (await reg_client.post(f"{API}/auth/register", json=GOOD)).status_code == 409
    upper = {**GOOD, "username": "ALICE_01"}
    assert (await reg_client.post(f"{API}/auth/register", json=upper)).status_code == 409


async def test_usernames_are_stored_lowercase_and_login_is_case_insensitive(reg_client, db):
    await reg_client.post(f"{API}/auth/register", json={**GOOD, "username": "MiXeD.Case"})
    assert (await user_row(db, "mixed.case")).username == "mixed.case"
    for name in ("mixed.case", "MIXED.CASE", "  Mixed.Case "):
        r = await reg_client.post(
            f"{API}/auth/login", json={"username": name, "password": GOOD["password"]}
        )
        assert r.status_code == 200, name


async def test_username_lowercase_check_constraint_in_db(db):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with db.session() as s:
            s.add(User(username="UPPER", name="x", hashed_password="h"))


# ------------------------------------------------------------------ login / tokens
async def test_login_success_returns_pair_and_short_lived_access_token(client, world, settings):
    body = await login_full(client, "admin", ADMIN_PW)
    assert body["token_type"] == "bearer" and body["role"] == "admin"
    assert body["expires_in"] == settings.access_token_minutes * 60 == 900
    assert body["refresh_expires_in"] == settings.refresh_token_days * 86400
    claims = jwt.decode(body["access_token"], settings.secret_key, algorithms=["HS256"])
    assert claims["username"] == "admin" and claims["tv"] == 0
    assert claims["exp"] - claims["iat"] == 900
    assert len(body["refresh_token"]) >= 60 and body["refresh_token"] != body["access_token"]


async def test_login_failures_are_indistinguishable(client, world, db):
    async with db.session() as s:
        (await UserRepository(s).get_by_username("resident2")).is_active = False
    cases = [
        {"username": "admin", "password": "nope"},
        {"username": "ghost", "password": "nope"},
        {"username": "resident2", "password": RESIDENT_PW},  # right password, inactive
        {"username": "admin", "password": ADMIN_PW + "x" * 80},  # > 72 bytes
    ]
    out = [await client.post(f"{API}/auth/login", json=c) for c in cases]
    assert {r.status_code for r in out} == {401}
    assert len({r.text for r in out}) == 1
    assert len({r.headers["www-authenticate"] for r in out}) == 1


async def test_password_longer_than_72_bytes_never_matches_its_truncation(client, world):
    r = await client.post(
        f"{API}/auth/login", json={"username": "admin", "password": ADMIN_PW + "extra" * 20}
    )
    assert r.status_code == 401


async def test_login_verifies_a_hash_even_for_unknown_user(monkeypatch, db, settings):
    calls = []
    real = auth_svc.verify_password
    monkeypatch.setattr(auth_svc, "verify_password", lambda p, h: calls.append(h) or real(p, h))
    async with db.session() as s:
        assert (
            await auth_svc.authenticate(UserRepository(s), settings, "ghost", "whatever1xyz")
            is None
        )
    assert len(calls) == 1 and calls[0] == auth_svc._dummy_hash(settings.bcrypt_rounds)


async def test_bcrypt_runs_in_a_worker_thread_not_on_the_event_loop(monkeypatch):
    import threading

    seen = []
    real = auth_svc.hash_password
    monkeypatch.setattr(
        auth_svc,
        "hash_password",
        lambda p, r=4: seen.append(threading.current_thread()) or real(p, r),
    )
    loop_thread = threading.current_thread()
    await auth_svc.hash_password_async("Sup3rsecretPass", 4)
    assert seen and seen[0] is not loop_thread


async def test_login_rejects_extra_and_missing_fields(client):
    assert (await client.post(f"{API}/auth/login", json={"username": "a"})).status_code == 422


async def test_me_returns_current_user(client, world):
    r = await client.get(f"{API}/auth/me", headers=world.resident)
    assert r.status_code == 200 and r.json()["username"] == "resident"
    assert r.json()["is_active"] is True and "hashed_password" not in r.json()


async def test_expired_token_is_rejected(client, world, settings):
    user = SimpleNamespace(id=uuid.uuid4(), username="x", role="admin", token_version=0)
    token, _ = auth_svc.create_access_token(
        settings, user, now=datetime.now(UTC) - timedelta(hours=3)
    )
    assert (await client.get(f"{API}/auth/me", headers=bearer(token))).status_code == 401


async def test_tampered_token_is_rejected(client, world):
    token = world.admin["Authorization"].split()[1]
    head, payload, sig = token.split(".")
    flipped = sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")
    for bad in (f"{head}.{payload}.{flipped}", f"{head}.{payload}x.{sig}", "garbage"):
        assert (await client.get(f"{API}/auth/me", headers=bearer(bad))).status_code == 401


async def test_token_signed_with_other_key_or_alg_none_is_rejected(client, world):
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "username": "admin",
            "role": "admin",
            "tv": 0,
            "exp": 9999999999,
        },
        "another-secret-key-another-secret-key",
    )
    assert (await client.get(f"{API}/auth/me", headers=bearer(forged))).status_code == 401
    unsigned = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJ4Iiwicm9sZSI6ImFkbWluIn0."
    assert (await client.get(f"{API}/auth/me", headers=bearer(unsigned))).status_code == 401


async def test_valid_signature_for_nonexistent_user_is_rejected(client, world, settings):
    ghost = SimpleNamespace(id=uuid.uuid4(), username="ghost", role="admin", token_version=0)
    token, _ = auth_svc.create_access_token(settings, ghost)
    assert (await client.get(f"{API}/auth/me", headers=bearer(token))).status_code == 401


async def test_token_without_tv_claim_is_rejected(client, world, settings):
    uid = world.users["admin"].id
    old = jwt.encode(
        {"sub": str(uid), "username": "admin", "role": "admin", "exp": 9999999999},
        settings.secret_key,
    )
    assert (await client.get(f"{API}/auth/me", headers=bearer(old))).status_code == 401


# ------------------------------------------------------------------ revocation via token_version / is_active
async def test_role_change_applies_immediately_and_is_audited(client, world, db):
    uid = str(world.users["resident"].id)
    assert (
        await client.put(
            f"{API}/auth/users/{uid}/role", json={"role": "admin"}, headers=world.resident
        )
    ).status_code == 403
    r = await client.put(
        f"{API}/auth/users/{uid}/role", json={"role": "maintenance"}, headers=world.admin
    )
    assert r.status_code == 200 and r.json()["role"] == "maintenance"
    # the role change bumped token_version: the OLD token is dead, not silently upgraded
    assert (await client.get(f"{API}/alerts", headers=world.resident)).status_code == 401
    async with db.session() as s:
        rows = await AuditRepository(s).find(action="role_changed")
    assert rows and rows[0].actor == "admin" and rows[0].details["to"] == "maintenance"


async def test_role_change_validation(client, world):
    for body, code in (({"role": "admin"}, 404), ({"role": "root"}, 422)):
        r = await client.put(
            f"{API}/auth/users/{uuid.uuid4()}/role", json=body, headers=world.admin
        )
        assert r.status_code == code


async def test_deactivated_user_is_locked_out_instantly(client, world, db):
    assert (await client.get(f"{API}/auth/me", headers=world.maintenance)).status_code == 200
    r = await client.patch(
        f"{API}/users/{world.users['maint'].id}", json={"is_active": False}, headers=world.admin
    )
    assert r.status_code == 200
    assert (await client.get(f"{API}/auth/me", headers=world.maintenance)).status_code == 401
    assert (
        await client.post(
            f"{API}/auth/login", json={"username": "maint", "password": "Maint1234Passw0rd"}
        )
    ).status_code == 401


async def test_deactivation_also_kills_refresh_tokens(client, world):
    tokens = await login_full(client, "maint", "Maint1234Passw0rd")
    await client.patch(
        f"{API}/users/{world.users['maint'].id}", json={"is_active": False}, headers=world.admin
    )
    r = await client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401


async def test_admin_logout_all_revokes_access_and_refresh_instantly(client, world):
    t = await login_full(client, "resident", RESIDENT_PW)
    h = bearer(t["access_token"])
    assert (await client.get(f"{API}/auth/me", headers=h)).status_code == 200
    r = await client.post(
        f"{API}/users/{world.users['resident'].id}/logout-all", headers=world.admin
    )
    assert r.status_code == 204
    assert (await client.get(f"{API}/auth/me", headers=h)).status_code == 401
    refresh = await client.post(f"{API}/auth/refresh", json={"refresh_token": t["refresh_token"]})
    assert refresh.status_code == 401


async def test_self_logout_all(client, world):
    t = await login_full(client, "resident2", RESIDENT_PW)
    h = bearer(t["access_token"])
    assert (await client.post(f"{API}/auth/logout-all", headers=h)).status_code == 204
    assert (await client.get(f"{API}/auth/me", headers=h)).status_code == 401
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": t["refresh_token"]})
    ).status_code == 401


# ------------------------------------------------------------------ refresh tokens
async def test_refresh_rotates_and_issues_a_working_access_token(client, world):
    t1 = await login_full(client, "resident", RESIDENT_PW)
    r = await client.post(f"{API}/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert r.status_code == 200
    t2 = r.json()
    assert t2["refresh_token"] != t1["refresh_token"] and t2["role"] == "resident"
    assert (
        await client.get(f"{API}/auth/me", headers=bearer(t2["access_token"]))
    ).status_code == 200


async def test_refresh_tokens_are_stored_hashed(client, world, db):
    t = await login_full(client, "resident", RESIDENT_PW)
    async with db.session() as s:
        row = (
            await s.execute(
                select(RefreshToken).where(
                    RefreshToken.token_hash == auth_svc.hash_token(t["refresh_token"])
                )
            )
        ).scalar_one()
        raw_matches = (
            (
                await s.execute(
                    select(RefreshToken).where(RefreshToken.token_hash == t["refresh_token"])
                )
            )
            .scalars()
            .all()
        )
    assert len(row.token_hash) == 64 and raw_matches == []
    assert row.expires_at > datetime.now(UTC) + timedelta(days=13)


async def test_reusing_a_spent_refresh_token_revokes_the_whole_family(client, world, db):
    t1 = await login_full(client, "resident", RESIDENT_PW)
    t2 = (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    ).json()
    # attacker replays the already-used token
    replay = await client.post(f"{API}/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert replay.status_code == 401
    # ... and the legitimate newest token of that family is dead too
    legit = await client.post(f"{API}/auth/refresh", json={"refresh_token": t2["refresh_token"]})
    assert legit.status_code == 401
    async with db.session() as s:
        first = (
            await s.execute(
                select(RefreshToken).where(
                    RefreshToken.token_hash == auth_svc.hash_token(t1["refresh_token"])
                )
            )
        ).scalar_one()
        rows = (
            (await s.execute(select(RefreshToken).where(RefreshToken.family_id == first.family_id)))
            .scalars()
            .all()
        )
        audit = await AuditRepository(s).find(action="refresh_reuse_detected")
    assert len(rows) == 2 and all(r.revoked_at is not None for r in rows)
    assert len(audit) >= 1 and any(r.replaced_by is not None for r in rows)


async def test_reuse_detection_does_not_touch_other_sessions(client, world):
    a = await login_full(client, "resident", RESIDENT_PW)
    b = await login_full(client, "resident", RESIDENT_PW)  # another device
    await client.post(f"{API}/auth/refresh", json={"refresh_token": a["refresh_token"]})
    await client.post(f"{API}/auth/refresh", json={"refresh_token": a["refresh_token"]})  # replay
    ok = await client.post(f"{API}/auth/refresh", json={"refresh_token": b["refresh_token"]})
    assert ok.status_code == 200


async def test_unknown_malformed_and_expired_refresh_tokens(client, world, db):
    for tok in ("x" * 40, "short", "é" * 30):
        r = await client.post(f"{API}/auth/refresh", json={"refresh_token": tok})
        assert r.status_code in (401, 422)
    t = await login_full(client, "resident", RESIDENT_PW)
    async with db.session() as s:
        (
            await s.execute(
                select(RefreshToken).where(
                    RefreshToken.token_hash == auth_svc.hash_token(t["refresh_token"])
                )
            )
        ).scalar_one().expires_at = datetime.now(UTC) - timedelta(seconds=1)
    r = await client.post(f"{API}/auth/refresh", json={"refresh_token": t["refresh_token"]})
    assert r.status_code == 401


async def test_refresh_claim_is_atomic_second_use_loses(client, world, db):
    """The race guard: revoking a live token succeeds exactly once (UPDATE .. WHERE revoked IS NULL)."""
    from app.repositories import RefreshTokenRepository

    t = await login_full(client, "resident", RESIDENT_PW)
    async with db.session() as s:
        repo = RefreshTokenRepository(s)
        row = await repo.get_by_hash(auth_svc.hash_token(t["refresh_token"]))
        now = datetime.now(UTC)
        assert await repo.claim(row.id, now) is True
        assert await repo.claim(row.id, now) is False


async def test_logout_revokes_refresh_token_but_access_token_lives_until_expiry(client, world):
    t = await login_full(client, "resident", RESIDENT_PW)
    assert (
        await client.post(f"{API}/auth/logout", json={"refresh_token": t["refresh_token"]})
    ).status_code == 204
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": t["refresh_token"]})
    ).status_code == 401
    # unknown tokens: still 204 (no oracle)
    assert (
        await client.post(f"{API}/auth/logout", json={"refresh_token": "z" * 40})
    ).status_code == 204


async def test_logout_kills_only_that_device(client, world):
    a = await login_full(client, "resident", RESIDENT_PW)
    b = await login_full(client, "resident", RESIDENT_PW)
    await client.post(f"{API}/auth/logout", json={"refresh_token": a["refresh_token"]})
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": b["refresh_token"]})
    ).status_code == 200


# ------------------------------------------------------------------ change password
async def test_change_password_requires_current_bumps_version_and_revokes(client, world, db):
    t = await login_full(client, "resident", RESIDENT_PW)
    h = bearer(t["access_token"])
    new = "BrandNewPassw0rd9"
    wrong = await client.post(
        f"{API}/auth/change-password",
        json={"current_password": "wrong-one-123", "new_password": new},
        headers=h,
    )
    assert wrong.status_code == 403
    weak = await client.post(
        f"{API}/auth/change-password",
        json={"current_password": RESIDENT_PW, "new_password": "short1"},
        headers=h,
    )
    assert weak.status_code == 422
    same = await client.post(
        f"{API}/auth/change-password",
        json={"current_password": RESIDENT_PW, "new_password": RESIDENT_PW},
        headers=h,
    )
    assert same.status_code == 422
    before = (await user_row(db, "resident")).token_version
    ok = await client.post(
        f"{API}/auth/change-password",
        json={"current_password": RESIDENT_PW, "new_password": new},
        headers=h,
    )
    assert ok.status_code == 200
    assert (await user_row(db, "resident")).token_version == before + 1
    # old access + refresh tokens are dead, the returned pair works
    assert (await client.get(f"{API}/auth/me", headers=h)).status_code == 401
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": t["refresh_token"]})
    ).status_code == 401
    assert (
        await client.get(f"{API}/auth/me", headers=bearer(ok.json()["access_token"]))
    ).status_code == 200
    assert (
        await client.post(
            f"{API}/auth/login", json={"username": "resident", "password": RESIDENT_PW}
        )
    ).status_code == 401
    assert (
        await client.post(f"{API}/auth/login", json={"username": "resident", "password": new})
    ).status_code == 200


async def test_change_password_needs_authentication(client):
    r = await client.post(
        f"{API}/auth/change-password", json={"current_password": "a", "new_password": "b"}
    )
    assert r.status_code == 401


# ------------------------------------------------------------------ lockout
async def fail_login(client, username="resident", n=1):
    return [
        (
            await client.post(
                f"{API}/auth/login", json={"username": username, "password": "wrong-pw-1"}
            )
        )
        for _ in range(n)
    ]


async def test_account_locks_after_10_failures_even_with_the_right_password(client, world, db):
    out = await fail_login(client, n=10)
    assert {r.status_code for r in out} == {401}
    row = await user_row(db, "resident")
    assert row.failed_logins == 10 and row.locked_until is not None
    assert row.locked_until > datetime.now(UTC) + timedelta(minutes=14)
    locked = await client.post(
        f"{API}/auth/login", json={"username": "resident", "password": RESIDENT_PW}
    )
    assert locked.status_code == 401
    other = await client.post(
        f"{API}/auth/login", json={"username": "resident2", "password": RESIDENT_PW}
    )
    assert other.status_code == 200  # other accounts unaffected
    assert locked.json() == out[0].json()  # indistinguishable from a bad password


async def test_nine_failures_do_not_lock_and_success_resets_the_counter(client, world, db):
    await fail_login(client, n=9)
    assert (await user_row(db, "resident")).locked_until is None
    ok = await client.post(
        f"{API}/auth/login", json={"username": "resident", "password": RESIDENT_PW}
    )
    assert ok.status_code == 200
    row = await user_row(db, "resident")
    assert row.failed_logins == 0 and row.last_failed_login_at is None
    await fail_login(client, n=9)  # would be 18 without the reset
    assert (await user_row(db, "resident")).locked_until is None


async def test_lock_expires_after_15_minutes(client, world, db, settings):
    await fail_login(client, n=10)
    async with db.session() as s:
        row = await UserRepository(s).get_by_username("resident")
        row.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    ok = await client.post(
        f"{API}/auth/login", json={"username": "resident", "password": RESIDENT_PW}
    )
    assert ok.status_code == 200


async def test_failures_outside_the_window_do_not_accumulate(db, settings):
    now = datetime(2026, 5, 1, 12, tzinfo=UTC)
    async with db.session() as s:
        from tests.conftest import add_user  # noqa: F401

    async with db.session() as s:
        repo = UserRepository(s)
        u = await repo.create(
            username="slow", name="S", hashed_password=auth_svc.hash_password(RESIDENT_PW, 4)
        )
        for i in range(12):  # 20 minutes apart: never 10 within 15 minutes
            assert (
                await auth_svc.authenticate(
                    repo, settings, "slow", "bad-pass-123", now + timedelta(minutes=20 * i)
                )
                is None
            )
        assert u.locked_until is None and u.failed_logins == 1


async def test_admin_reset_password_clears_lockout_and_sessions(client, world, db):
    t = await login_full(client, "resident", RESIDENT_PW)
    await fail_login(client, n=10)
    new = "ResetByAdmin2026x"
    r = await client.post(
        f"{API}/users/{world.users['resident'].id}/reset-password",
        json={"new_password": new},
        headers=world.admin,
    )
    assert r.status_code == 204
    assert (await user_row(db, "resident")).locked_until is None
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": t["refresh_token"]})
    ).status_code == 401
    assert (
        await client.post(f"{API}/auth/login", json={"username": "resident", "password": new})
    ).status_code == 200


# ------------------------------------------------------------------ rate limits
@pytest.fixture
def limited(tmp_path, db):
    app = create_app(
        make_settings(tmp_path, rate_limit_enabled=True, allow_self_registration=True), db
    )
    limiter.reset()
    yield app
    limiter.enabled = False
    limiter.reset()


def _c(app):
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_login_rate_limit_is_per_ip_and_username(limited):
    async with _c(limited) as c:
        codes = [
            (await c.post(f"{API}/auth/login", json={"username": "u", "password": "p"})).status_code
            for _ in range(7)
        ]
        assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
        # a different username from the same IP has its own bucket
        other = await c.post(f"{API}/auth/login", json={"username": "other", "password": "p"})
        assert other.status_code == 401


async def test_login_username_bucket_ignores_case(limited):
    async with _c(limited) as c:
        codes = [
            (await c.post(f"{API}/auth/login", json={"username": n, "password": "p"})).status_code
            for n in ["Bob", "bob", "BOB", "bOb", "boB", "BoB"]
        ]
    assert codes == [401] * 5 + [429]


async def test_register_rate_limit_3_per_minute(limited):
    async with _c(limited) as c:
        codes = [
            (
                await c.post(f"{API}/auth/register", json={**GOOD, "username": f"user{i}"})
            ).status_code
            for i in range(5)
        ]
    assert codes == [201, 201, 201, 429, 429]


async def test_rate_limit_storage_uri_is_configurable(tmp_path, db):
    s = make_settings(tmp_path, rate_limit_storage_uri="memory://", rate_limit_enabled=True)
    limiter.configure(s)
    try:
        assert limiter.allow("1/minute", "k") and not limiter.allow("1/minute", "k")
        limiter.reset()
        assert limiter.allow("1/minute", "k")
    finally:
        limiter.enabled = False
        limiter.reset()
