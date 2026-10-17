"""Admin onboarding API: societies, flats, users, bindings, last-admin protection."""

import uuid

import pytest
from sqlalchemy import select

from app.models import Flat, Society, User
from app.repositories import AuditRepository
from tests.conftest import RESIDENT_PW, add_user, login

API = "/api/v1"
PW = "Sup3rsecretPass"


# ------------------------------------------------------------------ societies
async def test_admin_creates_society_and_it_is_audited(client, world, db):
    r = await client.post(
        f"{API}/societies",
        json={"name": "Lotus Heights", "slug": "lotus-heights", "address": "1 Main Rd"},
        headers=world.admin,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["slug"] == "lotus-heights" and body["total_flats"] == 0 and body["address"]
    async with db.session() as s:
        audit = await AuditRepository(s).find(action="society_created")
    assert audit[0].actor == "admin" and audit[0].details["slug"] == "lotus-heights"


@pytest.mark.parametrize(
    "slug",
    ["Upper", "has space", "a/b", "a+b", "a#b", "-lead", "trail-", "dou--ble", "x" * 65, "", "é"],
)
async def test_society_slug_validation(client, world, slug):
    r = await client.post(f"{API}/societies", json={"name": "N", "slug": slug}, headers=world.admin)
    assert r.status_code == 422


async def test_society_duplicate_slug_conflicts(client, world):
    r = await client.post(
        f"{API}/societies", json={"name": "Dup", "slug": "green-valley"}, headers=world.admin
    )
    assert r.status_code == 409


async def test_society_write_endpoints_are_admin_only(client, world):
    for headers in (world.resident, world.maintenance):
        r = await client.post(f"{API}/societies", json={"name": "N", "slug": "nn"}, headers=headers)
        assert r.status_code == 403
        r = await client.patch(
            f"{API}/societies/{world.society.id}", json={"name": "Hijack"}, headers=headers
        )
        assert r.status_code == 403
        r = await client.delete(f"{API}/societies/{world.society.id}", headers=headers)
        assert r.status_code == 403


async def test_society_update_slug_is_immutable_and_name_not_nullable(client, world):
    url = f"{API}/societies/{world.society.id}"
    ok = await client.patch(url, json={"name": "Renamed", "address": "New St"}, headers=world.admin)
    assert (
        ok.status_code == 200
        and ok.json()["name"] == "Renamed"
        and ok.json()["slug"] == "green-valley"
    )
    assert (await client.patch(url, json={"slug": "new"}, headers=world.admin)).status_code == 422
    assert (await client.patch(url, json={"name": None}, headers=world.admin)).status_code == 422
    assert (
        await client.patch(
            f"{API}/societies/{uuid.uuid4()}", json={"name": "x"}, headers=world.admin
        )
    ).status_code == 404


async def test_society_delete_requires_it_to_be_empty(client, world):
    assert (
        await client.delete(f"{API}/societies/{world.society.id}", headers=world.admin)
    ).status_code == 409
    new = (
        await client.post(
            f"{API}/societies", json={"name": "Tmp", "slug": "tmp"}, headers=world.admin
        )
    ).json()
    assert (
        await client.delete(f"{API}/societies/{new['id']}", headers=world.admin)
    ).status_code == 204
    assert (
        await client.get(f"{API}/societies/{new['id']}", headers=world.admin)
    ).status_code == 404


# ------------------------------------------------------------------ flats
async def create_flat(client, world, **kw):
    body = {"society_id": str(world.society.id), "unit_number": "301", **kw}
    return await client.post(f"{API}/flats", json=body, headers=world.admin)


async def test_admin_creates_flat_with_default_device_username_and_counts(client, world, db):
    r = await create_flat(client, world, floor=3, residents=4)
    assert r.status_code == 201
    body = r.json()
    assert body["sensor_id"] == "green-valley.301" and body["floor"] == 3 and body["residents"] == 4
    assert body["valve_state"] == "unknown" and body["baseline_status"] == "learning"
    async with db.session() as s:
        soc = await s.get(Society, world.society.id)
    assert soc.total_flats == 7


async def test_flat_explicit_sensor_id_and_conflicts(client, world):
    ok = await create_flat(client, world, unit_number="302", sensor_id="custom-dev_1")
    assert ok.status_code == 201 and ok.json()["sensor_id"] == "custom-dev_1"
    assert (
        await create_flat(client, world, unit_number="303", sensor_id="custom-dev_1")
    ).status_code == 409
    assert (await create_flat(client, world, unit_number="101")).status_code == 409  # unit exists
    # same unit number in ANOTHER society is fine
    other = await client.post(
        f"{API}/flats",
        json={"society_id": str(world.society_b.id), "unit_number": "101"},
        headers=world.admin,
    )
    assert other.status_code == 201


@pytest.mark.parametrize(
    "patch",
    [
        {"unit_number": "a/b"},
        {"unit_number": "1.2"},  # '.' separates slug and unit in the device username
        {"unit_number": "+"},
        {"unit_number": "#"},
        {"unit_number": ""},
        {"unit_number": "x" * 51},
        {"floor": 1000},
        {"residents": -1},
        {"residents": 51},
        {"sensor_id": "has space"},
        {"extra": 1},
    ],
)
async def test_flat_validation(client, world, patch):
    assert (await create_flat(client, world, **patch)).status_code == 422


async def test_flat_unknown_society_404_and_non_admin_403(client, world):
    r = await client.post(
        f"{API}/flats",
        json={"society_id": str(uuid.uuid4()), "unit_number": "1"},
        headers=world.admin,
    )
    assert r.status_code == 404
    r = await client.post(
        f"{API}/flats",
        json={"society_id": str(world.society.id), "unit_number": "9"},
        headers=world.maintenance,
    )
    assert r.status_code == 403


async def test_flat_update_and_immutable_fields(client, world):
    fid = world.flats[2].id
    url = f"{API}/flats/{fid}"
    ok = await client.patch(
        url, json={"floor": 9, "residents": 2, "sensor_id": "dev-new"}, headers=world.admin
    )
    assert ok.status_code == 200 and ok.json()["sensor_id"] == "dev-new" and ok.json()["floor"] == 9
    for body in ({"unit_number": "999"}, {"society_id": str(world.society_b.id)}):
        assert (await client.patch(url, json=body, headers=world.admin)).status_code == 422
    clash = await client.patch(
        url, json={"sensor_id": world.flats[3].sensor_id}, headers=world.admin
    )
    assert clash.status_code == 409
    assert (
        await client.patch(url, json={"residents": None}, headers=world.admin)
    ).status_code == 422


async def test_flat_delete_updates_count_and_refuses_closed_valve(client, world, db):
    victim, closed = world.flats[4].id, world.flats[5].id
    async with db.session() as s:
        (await s.get(Flat, closed)).valve_state = "closed"
    assert (await client.delete(f"{API}/flats/{closed}", headers=world.admin)).status_code == 409
    assert (await client.delete(f"{API}/flats/{victim}", headers=world.admin)).status_code == 204
    assert (await client.get(f"{API}/flats/{victim}", headers=world.admin)).status_code == 404
    async with db.session() as s:
        assert (await s.get(Society, world.society.id)).total_flats == 5
        audit = await AuditRepository(s).find(action="flat_deleted")
    assert audit and audit[0].actor == "admin"
    assert (
        await client.delete(f"{API}/flats/{uuid.uuid4()}", headers=world.admin)
    ).status_code == 404


# ------------------------------------------------------------------ users
async def create_user(client, world, **kw):
    body = {"username": "newbie", "password": PW, "name": "New B", **kw}
    return await client.post(f"{API}/users", json=body, headers=world.admin)


async def test_admin_creates_users_of_every_role_with_valid_bindings(client, world):
    res = await create_user(client, world, username="res_x", flat_id=str(world.flats[3].id))
    assert res.status_code == 201
    body = res.json()
    assert body["role"] == "resident" and body["flat_id"] == str(world.flats[3].id)
    assert body["society_id"] == str(world.society.id)  # derived from the flat
    assert "password" not in body and "hashed_password" not in body
    m = await create_user(
        client, world, username="maint_x", role="maintenance", society_id=str(world.society_b.id)
    )
    assert m.status_code == 201 and m.json()["society_id"] == str(world.society_b.id)
    a = await create_user(client, world, username="admin_x", role="admin")
    assert a.status_code == 201 and a.json()["flat_id"] is None
    svc = await create_user(client, world, username="svc_x", role="maintenance", is_service=True)
    assert svc.json()["is_service"] is True
    # created accounts can log in
    assert await login(client, "res_x", PW)


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"role": "admin", "flat_id": "F"}, 422),
        ({"role": "admin", "society_id": "S"}, 422),
        ({"role": "maintenance", "flat_id": "F"}, 422),
        ({"role": "resident", "society_id": "S"}, 422),  # resident needs a flat
        ({"role": "resident", "flat_id": "F", "society_id": "OTHER"}, 422),  # mismatch
        ({"role": "resident", "flat_id": "GHOST"}, 404),
        ({"role": "maintenance", "society_id": "GHOST"}, 404),
        ({"role": "superuser"}, 422),
        ({"password": "short1"}, 422),
        ({"password": "nodigitsatallhere"}, 422),
        ({"username": "ab"}, 422),
        ({"username": "bad name"}, 422),
        ({"is_admin": True}, 422),
    ],
)
async def test_user_creation_validation(client, world, kw, code):
    subst = {
        "F": str(world.flats[0].id),
        "S": str(world.society.id),
        "OTHER": str(world.society_b.id),
        "GHOST": str(uuid.uuid4()),
    }
    kw = {k: subst.get(v, v) if isinstance(v, str) else v for k, v in kw.items()}
    assert (await create_user(client, world, **kw)).status_code == code


async def test_duplicate_username_is_case_insensitive_409(client, world):
    assert (await create_user(client, world, username="Dupe")).status_code == 201
    assert (await create_user(client, world, username="dupe")).status_code == 409
    assert (await create_user(client, world, username="DUPE")).status_code == 409


async def test_user_endpoints_are_admin_only(client, world):
    uid = world.users["resident"].id
    for headers in (world.resident, world.maintenance):
        assert (await client.get(f"{API}/users", headers=headers)).status_code == 403
        assert (await client.get(f"{API}/users/{uid}", headers=headers)).status_code == 403
        r = await client.patch(f"{API}/users/{uid}", json={"role": "admin"}, headers=headers)
        assert r.status_code == 403
        r = await client.put(f"{API}/users/{uid}/binding", json={}, headers=headers)
        assert r.status_code == 403


async def test_list_users_filters(client, world):
    by_role = (await client.get(f"{API}/users?role=maintenance", headers=world.admin)).json()
    assert {u["username"] for u in by_role} == {"maint", "maint_b"}
    by_soc = (
        await client.get(f"{API}/users?society_id={world.society_b.id}", headers=world.admin)
    ).json()
    assert {u["username"] for u in by_soc} == {"maint_b", "resident_b"}
    q = (await client.get(f"{API}/users?q=RES", headers=world.admin)).json()
    assert {u["username"] for u in q} >= {"resident", "resident2", "resident_b"}
    page = (await client.get(f"{API}/users?limit=3&offset=3", headers=world.admin)).json()
    assert len(page) == 3
    inactive = (await client.get(f"{API}/users?is_active=false", headers=world.admin)).json()
    assert inactive == []


async def test_by_username_lookup_for_admin_and_service_accounts_only(client, world, db):
    ok = await client.get(f"{API}/users/by-username/Resident", headers=world.admin)
    assert ok.status_code == 200 and ok.json()["username"] == "resident"
    assert "hashed_password" not in ok.text
    assert (
        await client.get(f"{API}/users/by-username/ghost", headers=world.admin)
    ).status_code == 404
    assert (
        await client.get(f"{API}/users/by-username/resident", headers=world.bot)
    ).status_code == 200
    # a maintenance-role service account may look users up, a normal maintenance user may not
    await add_user(db, "maintbot", "M41ntBotPassw0rd", "maintenance", is_service=True)
    h = await login(client, "maintbot", "M41ntBotPassw0rd")
    assert (await client.get(f"{API}/users/by-username/resident", headers=h)).status_code == 200
    for headers in (world.maintenance, world.resident, {}):
        r = await client.get(f"{API}/users/by-username/resident", headers=headers)
        assert r.status_code in (401, 403)
    assert (
        await client.get(f"{API}/users/by-username/{'x' * 51}", headers=world.admin)
    ).status_code == 422


async def test_patch_user_fields_and_telegram_uniqueness(client, world):
    uid = world.users["resident"].id
    ok = await client.patch(
        f"{API}/users/{uid}", json={"name": "Renamed", "telegram_id": 4242}, headers=world.admin
    )
    assert (
        ok.status_code == 200
        and ok.json()["name"] == "Renamed"
        and ok.json()["telegram_id"] == 4242
    )
    dup = await client.patch(
        f"{API}/users/{world.users['resident2'].id}",
        json={"telegram_id": 4242},
        headers=world.admin,
    )
    assert dup.status_code == 409
    assert (
        await client.patch(f"{API}/users/{uuid.uuid4()}", json={"name": "x"}, headers=world.admin)
    ).status_code == 404
    assert (
        await client.patch(f"{API}/users/{uid}", json={"username": "other"}, headers=world.admin)
    ).status_code == 422


async def test_binding_endpoint_enforces_role_rules_and_takes_effect(client, world):
    uid = world.users["unbound"].id
    url = f"{API}/users/{uid}/binding"
    h = world.unbound
    assert (await client.get(f"{API}/flats", headers=h)).json() == []
    bound = await client.put(url, json={"flat_id": str(world.flats[2].id)}, headers=world.admin)
    assert bound.status_code == 200 and bound.json()["society_id"] == str(world.society.id)
    # no re-login needed: scope is read from the DB on every request
    flats = (await client.get(f"{API}/flats", headers=h)).json()
    assert [f["id"] for f in flats] == [str(world.flats[2].id)]
    assert (
        await client.put(url, json={"society_id": str(world.society.id)}, headers=world.admin)
    ).status_code == 422
    assert (
        await client.put(url, json={"flat_id": str(uuid.uuid4())}, headers=world.admin)
    ).status_code == 404
    unbound = await client.put(url, json={}, headers=world.admin)
    assert unbound.json()["flat_id"] is None
    assert (await client.get(f"{API}/flats", headers=h)).json() == []


async def test_changing_role_clears_binding_and_logs_the_user_out(client, world):
    uid = world.users["resident"].id
    r = await client.patch(f"{API}/users/{uid}", json={"role": "maintenance"}, headers=world.admin)
    assert r.status_code == 200
    assert r.json()["flat_id"] is None and r.json()["society_id"] is None
    assert (await client.get(f"{API}/auth/me", headers=world.resident)).status_code == 401
    h = await login(client, "resident", RESIDENT_PW)
    assert (await client.get(f"{API}/flats", headers=h)).json() == []  # unbound maintenance


async def test_full_onboarding_flow_end_to_end(client, world, db):
    soc = (
        await client.post(
            f"{API}/societies", json={"name": "Maple", "slug": "maple"}, headers=world.admin
        )
    ).json()
    flat = (
        await client.post(
            f"{API}/flats",
            json={"society_id": soc["id"], "unit_number": "A-12"},
            headers=world.admin,
        )
    ).json()
    user = (
        await client.post(
            f"{API}/users",
            json={"username": "tenant", "password": PW, "name": "T", "flat_id": flat["id"]},
            headers=world.admin,
        )
    ).json()
    h = await login(client, "tenant", PW)
    assert [f["id"] for f in (await client.get(f"{API}/flats", headers=h)).json()] == [flat["id"]]
    assert (await client.get(f"{API}/flats/{world.flats[0].id}", headers=h)).status_code == 404
    assert user["society_id"] == soc["id"]
    async with db.session() as s:
        actions = {r.action for r in await AuditRepository(s).find(limit=100)}
    assert {"society_created", "flat_created", "user_created"} <= actions


# ------------------------------------------------------------------ last admin protection
async def test_cannot_deactivate_or_demote_the_last_active_admin(client, world, db):
    # world has two admins (admin + bot): deactivating the bot is fine ...
    bot = world.users["bot"].id
    r = await client.patch(f"{API}/users/{bot}", json={"is_active": False}, headers=world.admin)
    assert r.status_code == 200
    # ... now `admin` is the last one
    me = world.users["admin"].id
    assert (
        await client.patch(f"{API}/users/{me}", json={"is_active": False}, headers=world.admin)
    ).status_code == 409
    assert (
        await client.patch(f"{API}/users/{me}", json={"role": "maintenance"}, headers=world.admin)
    ).status_code == 409
    legacy = await client.put(
        f"{API}/auth/users/{me}/role", json={"role": "resident"}, headers=world.admin
    )
    assert legacy.status_code == 409
    async with db.session() as s:
        admin = (await s.execute(select(User).where(User.username == "admin"))).scalar_one()
    assert admin.role == "admin" and admin.is_active


async def test_with_two_admins_one_can_be_demoted_then_the_other_is_protected(client, world):
    me, bot = world.users["admin"].id, world.users["bot"].id
    r = await client.patch(f"{API}/users/{bot}", json={"role": "maintenance"}, headers=world.admin)
    assert r.status_code == 200
    r = await client.patch(f"{API}/users/{me}", json={"is_active": False}, headers=world.admin)
    assert r.status_code == 409


async def test_inactive_admin_does_not_count_as_an_active_admin(client, world, db):
    async with db.session() as s:
        (await s.execute(select(User).where(User.username == "bot"))).scalar_one().is_active = False
    me = world.users["admin"].id
    assert (
        await client.patch(f"{API}/users/{me}", json={"is_active": False}, headers=world.admin)
    ).status_code == 409
