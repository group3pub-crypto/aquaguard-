"""ApiClient against a simulator that validates every request/response against the backend OpenAPI."""

import json

import pytest

from bot.api_client import ApiError
from tests.support import AL1, F101, SNAPSHOT, BackendSim, alert, check_request, check_response, make_api


def test_snapshot_has_expected_paths():
    for p in (
        "/api/v1/alerts",
        "/api/v1/tools/flat-usage",
        "/api/v1/tools/top-losses",
        "/api/v1/tools/valve-control",
        "/api/v1/alerts/{alert_id}/snooze",
    ):
        assert p in SNAPSHOT["paths"]
    states = SNAPSHOT["paths"]["/api/v1/alerts"]["get"]["parameters"]
    assert any("auto_shutoff" in json.dumps(x) for x in states)
    props = SNAPSHOT["components"]["schemas"]["AlertOut"]["properties"]
    assert {"shutoff_warned_at", "shutoff_gave_up_at"} <= set(props)
    assert "/api/v1/auth/refresh" in SNAPSHOT["paths"]
    assert "/api/v1/users/by-username/{username}" in SNAPSHOT["paths"]
    assert "telegram_id" in SNAPSHOT["components"]["schemas"]["UserUpdate"]["properties"]


def test_wrong_old_shapes_are_rejected_by_checker():
    with pytest.raises(AssertionError):
        check_request("POST", "/api/v1/tools/flat-usage", {}, {"flat_id": F101, "period": "today"})
    with pytest.raises(AssertionError):
        check_request("POST", "/api/v1/tools/top-losses", {}, {"society_id": F101, "top_n": 5})
    with pytest.raises(AssertionError):
        check_request("GET", "/api/v1/alerts", {"state": "watch"} | {"bogus": "1"}, None)
    with pytest.raises(AssertionError):
        check_response("GET", "/api/v1/alerts", [{"id": "x", "unit": "A-1"}])


async def test_every_wrapper_hits_real_endpoints():
    sim = BackendSim([alert(AL1, F101, "alert")])
    api = make_api(sim)
    assert (await api.alerts(state="auto_shutoff"))[0:0] == []
    assert len(await api.active_alerts()) == 1
    assert (await api.flat_usage(F101, 48))["volume_l"] > 0
    assert (await api.top_losses(3, 2))["losses"]
    assert "Weekly" in await api.weekly_report()
    assert (await api.society_stats())["flats"] == 2
    await api.ack_alert(AL1)
    await api.snooze_alert(AL1, 30)
    await api.resolve_alert(AL1)
    cmd = await api.set_valve("a-101", "close", "telegram:1")
    assert cmd["status"] == "pending" and cmd["on_behalf_of"] == "telegram:1"
    assert cmd["on_behalf_of_verified"] is False
    await api.override_alert(AL1, "why", "telegram:1")
    sent = {p: b for _, p, _, b in sim.calls}
    assert sent["/api/v1/tools/flat-usage"] == {"flat_id": F101, "hours": 48}
    assert sent["/api/v1/tools/top-losses"]["limit"] == 2 and sent["/api/v1/tools/top-losses"]["days"] == 3
    assert sent["/api/v1/tools/valve-control"]["on_behalf_of"] == "telegram:1"
    assert "issued_by" not in sent["/api/v1/tools/valve-control"]
    assert await api.unit_label(F101) == "A-101"
    assert await api.unit_label("deadbeef-0000") == "deadbeef"
    with pytest.raises(ApiError):
        await api.find_flat("Z-999")
    await api.aclose()
