"""Resident / maintenance data scoping (IDOR) - enumerated from app.routes.

Every route MUST declare a policy marker (see app.dependencies.ACCEPTED_POLICIES). Data routes
("scoped") are then attacked automatically: a caller must get 403 / 404 / an empty result and the
response must not contain a single id of a resource outside their scope. Adding a route without a
policy, or a scoped route this file cannot build a request for, FAILS the suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute

from app.database import Database
from app.dependencies import ACCEPTED_POLICIES
from app.main import create_app
from app.models import FlowReading
from tests.conftest import make_alert, make_settings

API = "/api/v1"


def policies_of(dependant) -> set[str]:
    out: set[str] = set()
    for d in dependant.dependencies:
        kind = getattr(d.call, "policy_kind", None)
        if kind:
            out.add(kind)
        out |= policies_of(d)
    return out


def _flat_routes(app):
    """(route-like with .dependant, full path, methods) for every API route, across FastAPI
    versions (newer ones include routers lazily and expose them via iter_route_contexts)."""
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # older FastAPI: app.routes is already flat
        for r in app.routes:
            if isinstance(r, APIRoute | APIWebSocketRoute):
                yield r, r.path, getattr(r, "methods", None)
        return
    for rc in iter_route_contexts(app.routes):
        if isinstance(rc.original_route, APIRoute | APIWebSocketRoute):
            yield rc, rc.path_format, getattr(rc, "methods", None)


def _routes():
    app = create_app(
        make_settings(__import__("pathlib").Path("/tmp/aq-scope")), Database("sqlite+aiosqlite://")
    )
    rows = []
    for route, path, methods in _flat_routes(app):
        if isinstance(
            route.original_route if hasattr(route, "original_route") else route, APIWebSocketRoute
        ):
            rows.append(("WS", path, route))
        else:
            for m in sorted((methods or set()) - {"HEAD", "OPTIONS"}):
                rows.append((m, path, route))
    return rows


ROUTES = _routes()
IDS = [f"{m} {p}" for m, p, _ in ROUTES]


def test_route_table_is_not_trivially_small():
    assert len(ROUTES) >= 50
    assert sum("scoped" in policies_of(r.dependant) for _, _, r in ROUTES) >= 20


@pytest.mark.parametrize(("method", "path", "route"), ROUTES, ids=IDS)
def test_every_route_declares_a_scope_policy(method, path, route):
    found = policies_of(route.dependant) & ACCEPTED_POLICIES
    assert found, (
        f"{method} {path} has no scope policy: add Depends(get_scope) (CallerScope), AdminUser, "
        "SelfRoute, PublicRoute... to its signature/decorator"
    )


def test_data_routes_never_rely_on_authentication_alone():
    """The old pattern - `dependencies=[Depends(require_user)]` and nothing else - is exactly
    the IDOR bug. A route with only a bare role check (staff) is also incomplete."""
    for method, path, route in ROUTES:
        kinds = policies_of(route.dependant)
        assert kinds != {"role"}, f"{method} {path} checks a role but declares no scope"


# ------------------------------------------------------------------ the automatic attack
@pytest.fixture
async def targets(db, rt, world):
    """Alerts/anomalies/readings in society B (victim) and for peer flat A1."""
    now = datetime.now(UTC)
    async with db.session() as s:
        for flat in (world.flats[0], world.flats[1], world.flats_b[0]):
            s.add(
                FlowReading(
                    flat_id=flat.id,
                    timestamp=now - timedelta(minutes=2),
                    flow_rate=1.0,
                    volume=3.0,
                    message_id="0000000a",
                )
            )
    out = {}
    for name, flat in (("b", world.flats_b[0]), ("a1", world.flats[1]), ("a0", world.flats[0])):
        anomaly_id, alert_id = await make_alert(db, rt, flat.id, severity=0.6, now=now)
        out[name] = {
            "flat": str(flat.id),
            "society": str(flat.society_id),
            "alert": str(alert_id),
            "anomaly": str(anomaly_id),
        }
    return out


PARAM_KEYS = {
    "flat_id": "flat",
    "society_id": "society",
    "alert_id": "alert",
    "anomaly_id": "anomaly",
}


def body_for(method: str, path: str, t: dict) -> list[dict | None]:
    """Request bodies to try for a route (a list: some tools take two alternative shapes)."""
    key = f"{method} {path.removeprefix(API)}"
    table: dict[str, list[dict | None]] = {
        "POST /alerts/{alert_id}/acknowledge": [{}],
        "POST /alerts/{alert_id}/snooze": [{"duration_minutes": 15}],
        "POST /alerts/{alert_id}/override": [{"reason": "x"}],
        "POST /alerts/{alert_id}/resolve": [{}],
        "POST /valves/{flat_id}/open": [{}],
        "POST /valves/{flat_id}/close": [{}],
        "POST /flats/{flat_id}/forecast/generate": [{"horizon_hours": 6}],
        "POST /flats/{flat_id}/baseline/retrain": [None],
        "POST /tools/active-alerts": [{"flat_id": t["flat"]}, {"society_id": t["society"]}, {}],
        "POST /tools/flat-usage": [{"flat_id": t["flat"]}],
        "POST /tools/top-losses": [{"society_id": t["society"]}],
        "POST /tools/valve-control": [{"flat_id": t["flat"], "action": "close"}],
        "POST /tools/weekly-report": [{"society_id": t["society"]}],
    }
    if method in ("GET", "DELETE"):
        return [None]
    assert key in table, f"scope test has no request body for {key}: add it to body_for()"
    return table[key]


def build_requests(method: str, path: str, route: APIRoute, t: dict):
    """Concrete (url, body) pairs attacking resource set `t`."""
    url = path
    for param, key in PARAM_KEYS.items():
        url = url.replace("{" + param + "}", t[key])
    assert "{" not in url, f"unmapped path parameter in {path}"
    queries = [""]
    qnames = {q.name for q in route.dependant.query_params}
    if "flat_id" in qnames:
        queries.append(f"?flat_id={t['flat']}")
    if "society_id" in qnames:
        queries.append(f"?society_id={t['society']}")
    return [(url + q, b) for q in queries for b in body_for(method, path, t)]


def scoped_routes():
    return [(m, p, r) for m, p, r in ROUTES if m != "WS" and "scoped" in policies_of(r.dependant)]


SCOPED = scoped_routes()
SCOPED_IDS = [f"{m} {p}" for m, p, _ in SCOPED]

# attacker -> (victim resource-set name in `targets`, ids that must never appear)
ATTACKS = {
    "resident_vs_foreign_society": ("resident", "b"),
    "resident_vs_peer_flat_same_society": ("resident", "a1"),
    "resident2_vs_flat_a0": ("resident2", "a0"),
    "unbound_vs_own_society": ("unbound", "a0"),
    "unbound_vs_foreign": ("unbound", "b"),
    "maintenance_vs_foreign_society": ("maintenance", "b"),
    "maintenance_b_vs_society_a": ("maintenance_b", "a0"),
}


def leaked(text: str, t: dict, extra: list[str], same_society: bool = False) -> list[str]:
    ids = set(t.values()) | set(extra)
    if same_society:  # a resident may see their OWN society's summary (documented)
        ids.discard(t["society"])
    return sorted(i for i in ids if i in text)


@pytest.mark.parametrize("attack", sorted(ATTACKS))
@pytest.mark.parametrize(("method", "path", "route"), SCOPED, ids=SCOPED_IDS)
async def test_out_of_scope_access_is_denied_empty_or_404(
    client, world, targets, attack, method, path, route
):
    who, victim = ATTACKS[attack]
    headers = getattr(world, who)
    t = targets[victim]
    # own-society summary is legitimately visible to bound members of that society
    same = victim != "b" and who in ("resident", "resident2", "maintenance")
    extra: list[str] = []
    if victim == "b":
        extra = [str(f.id) for f in world.flats_b] + [f.sensor_id for f in world.flats_b]
    else:
        extra = [str(world.flats[0].id)] if victim == "a0" else [str(world.flats[1].id)]
    for url, body in build_requests(method, API + path.removeprefix(API), route, t):
        r = await client.request(method, url, headers=headers, json=body)
        where = f"{attack}: {method} {url}"
        assert r.status_code != 422, f"{where} -> 422, fix body_for(): {r.text[:200]}"
        assert r.status_code < 500, f"{where} -> {r.status_code}"
        if r.status_code == 200:
            data = r.json()
            payload_ok = (
                data in ([], {})
                or (isinstance(data, dict) and data.get("count") == 0)
                or not leaked(r.text, t, extra, same)
            )
            assert payload_ok, (
                f"{where} -> 200 leaked {leaked(r.text, t, extra, victim != 'b' and who != 'maintenance_b')}"
            )
        else:
            assert r.status_code in (403, 404), f"{where} -> {r.status_code}"
        assert not leaked(r.text, t, extra, same), f"{where} leaked ids"


@pytest.mark.parametrize(("method", "path", "route"), SCOPED, ids=SCOPED_IDS)
async def test_out_of_scope_writes_have_no_effect(client, world, targets, db, method, path, route):
    """Besides the status code: nothing changed behind a denied mutation."""
    if method == "GET":
        return
    from app.repositories import AlertRepository, AuditRepository, ValveRepository

    t = targets["b"]
    for who in ("resident", "unbound", "maintenance"):
        for url, body in build_requests(method, path, route, t):
            await client.request(method, url, headers=getattr(world, who), json=body)
    async with db.session() as s:
        alert = await AlertRepository(s).get(uuid.UUID(t["alert"]))
        valves = await ValveRepository(s).recent(world.flats_b[0].id)
        audit = await AuditRepository(s).find(entity_type="valve")
    assert alert.state in ("alert", "ticket") and alert.acknowledged_by is None
    assert valves == [] and audit == []


async def test_owner_still_sees_exactly_their_own_data(client, world, targets):
    own, other = targets["a0"], targets["a1"]
    h = world.resident
    assert (await client.get(f"{API}/flats/{own['flat']}", headers=h)).status_code == 200
    assert (await client.get(f"{API}/alerts/{own['alert']}", headers=h)).status_code == 200
    assert (await client.get(f"{API}/anomalies/{own['anomaly']}", headers=h)).status_code == 200
    assert (await client.get(f"{API}/valves/{own['flat']}", headers=h)).status_code == 200
    assert (await client.get(f"{API}/flats/{own['flat']}/readings", headers=h)).status_code == 200
    assert (await client.get(f"{API}/societies/{own['society']}", headers=h)).status_code == 200
    flats = (await client.get(f"{API}/flats", headers=h)).json()
    assert [f["id"] for f in flats] == [own["flat"]]
    alerts = (await client.get(f"{API}/alerts", headers=h)).json()
    assert [a["id"] for a in alerts] == [own["alert"]]
    anomalies = (await client.get(f"{API}/anomalies", headers=h)).json()
    assert [a["id"] for a in anomalies] == [own["anomaly"]]
    societies = (await client.get(f"{API}/societies", headers=h)).json()
    assert [x["id"] for x in societies] == [own["society"]]
    # the peer flat of the same society is invisible
    assert (await client.get(f"{API}/flats/{other['flat']}", headers=h)).status_code == 404
    assert (await client.get(f"{API}/alerts/{other['alert']}", headers=h)).status_code == 404
    # society-wide aggregates are staff-only
    assert (
        await client.get(f"{API}/societies/{own['society']}/stats", headers=h)
    ).status_code == 403
    for tool, body in (
        ("top-losses", {"society_id": own["society"]}),
        ("weekly-report", {"society_id": own["society"]}),
    ):
        assert (await client.post(f"{API}/tools/{tool}", json=body, headers=h)).status_code == 403
    usage = await client.post(f"{API}/tools/flat-usage", json={"flat_id": own["flat"]}, headers=h)
    assert usage.status_code == 200 and usage.json()["volume_l"] == 3.0
    active = await client.post(
        f"{API}/tools/active-alerts", json={"society_id": own["society"]}, headers=h
    )
    assert [a["id"] for a in active.json()["alerts"]] == [own["alert"]]  # society param narrowed


async def test_maintenance_sees_its_whole_society_and_nothing_else(client, world, targets):
    h = world.maintenance
    flats = (await client.get(f"{API}/flats?limit=500", headers=h)).json()
    assert {f["id"] for f in flats} == {str(f.id) for f in world.flats}
    alerts = (await client.get(f"{API}/alerts", headers=h)).json()
    assert {a["id"] for a in alerts} == {targets["a0"]["alert"], targets["a1"]["alert"]}
    stats = await client.get(f"{API}/societies/{world.society.id}/stats", headers=h)
    assert stats.status_code == 200 and stats.json()["flats"] == 6
    assert (
        await client.get(f"{API}/societies/{world.society_b.id}/stats", headers=h)
    ).status_code == 404
    tool = await client.post(f"{API}/tools/active-alerts", json={}, headers=h)
    assert tool.json()["count"] == 2


async def test_admin_sees_everything(client, world, targets):
    flats = (await client.get(f"{API}/flats?limit=500", headers=world.admin)).json()
    assert len(flats) == 8
    alerts = (await client.get(f"{API}/alerts", headers=world.admin)).json()
    assert len(alerts) == 3
    assert len((await client.get(f"{API}/societies", headers=world.admin)).json()) == 2


async def test_unbound_maintenance_sees_nothing(client, world, db):
    from tests.conftest import add_user, login

    await add_user(db, "floating", "Fl0atingMaint123", "maintenance")
    h = await login(client, "floating", "Fl0atingMaint123")
    for path in ("/flats", "/societies", "/alerts", "/anomalies"):
        assert (await client.get(f"{API}{path}", headers=h)).json() == []
    assert (await client.get(f"{API}/flats/{world.flats[0].id}", headers=h)).status_code == 404


async def test_list_endpoints_filter_in_sql_so_pagination_stays_correct(client, world, db, rt):
    """Post-filtering would return short/empty pages; a SQL filter paginates the visible rows."""
    now = datetime.now(UTC)
    for atype in ["leak", "burst", "overuse", "dripping_tap", "running_toilet"]:
        await make_alert(db, rt, world.flats_b[0].id, atype=atype, severity=0.6, now=now)
    for atype in ("leak", "burst"):
        await make_alert(db, rt, world.flats[0].id, atype=atype, severity=0.6, now=now)
    page = (await client.get(f"{API}/alerts?limit=2", headers=world.resident)).json()
    assert len(page) == 2 and {a["flat_id"] for a in page} == {str(world.flats[0].id)}
    page2 = (await client.get(f"{API}/alerts?limit=2&offset=2", headers=world.resident)).json()
    assert page2 == []


async def test_resident_cannot_widen_scope_with_query_parameters(client, world, targets):
    h = world.resident
    r = await client.get(f"{API}/alerts?flat_id={targets['b']['flat']}", headers=h)
    assert r.json() == []
    r = await client.get(f"{API}/flats?society_id={targets['b']['society']}", headers=h)
    assert r.json() == []
    r = await client.get(f"{API}/anomalies?flat_id={targets['a1']['flat']}", headers=h)
    assert r.json() == []
