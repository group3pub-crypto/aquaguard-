"""FlowPayload: existing fields + the optional firmware extras (tot, vbat, valve, fw, err)."""

import time

import pytest
from pydantic import ValidationError

from app.schemas.mqtt import FlowPayload

BASE = {"v": 1, "id": "a1b2c3d4", "ts": time.time() - 5, "flow": 1.5, "vol": 0.1}


def parse(**over):
    return FlowPayload.model_validate({**BASE, **over})


def test_minimal_payload_still_valid_and_extras_default_to_none():
    p = parse()
    assert (p.tot, p.vbat, p.valve, p.fw, p.err) == (None, None, None, None, None)


def test_all_optional_extras_accepted():
    p = parse(
        tot=1234.5,
        vbat=3.71,
        valve="closed",
        fw="1.4.2-rc1",
        err=["flow_stuck", "psi_open"],
        psi=40,
        tmp=21.5,
        bat=90,
        rssi=-60,
    )
    assert p.tot == 1234.5 and p.vbat == 3.71 and p.valve == "closed"
    assert p.fw == "1.4.2-rc1" and p.err == ["flow_stuck", "psi_open"]


@pytest.mark.parametrize(
    ("raw", "want"), [(99.9, 100), (99.4, 99), (0, 0), (0.4, 0), (100, 100), (55, 55)]
)
def test_fractional_battery_is_rounded_to_an_integer(raw, want):
    p = parse(bat=raw)
    assert p.bat == want and isinstance(p.bat, int)


@pytest.mark.parametrize("bad", [100.6, -0.6, 101, -1, "abc", True, float("nan"), None.__class__])
def test_battery_out_of_range_or_not_a_number_is_rejected(bad):
    with pytest.raises(ValidationError):
        parse(bat=bad)


@pytest.mark.parametrize(
    "extra",
    [
        {"tot": -0.1},
        {"tot": "x"},
        {"tot": float("inf")},
        {"vbat": 2.49},
        {"vbat": 4.51},
        {"vbat": "3.7v"},
        {"valve": "ajar"},
        {"valve": "OPEN"},
        {"valve": 1},
        {"fw": "x" * 33},
        {"err": ["a"] * 9},
        {"err": ["x" * 25]},
        {"err": [""]},
        {"err": "flow_stuck"},
        {"err": [1, 2]},
    ],
)
def test_extras_are_strictly_bounded(extra):
    with pytest.raises(ValidationError):
        parse(**extra)


@pytest.mark.parametrize(
    "extra",
    [
        {"vbat": 2.5},
        {"vbat": 4.5},
        {"tot": 0},
        {"fw": "x" * 32},
        {"err": ["a"] * 8},
        {"err": ["x" * 24]},
        {"err": []},
    ],
)
def test_extras_accept_their_exact_bounds(extra):
    assert parse(**extra)


def test_unknown_keys_are_still_ignored_consistently():
    p = parse(brand_new_field="x", tot=1)
    assert not hasattr(p, "brand_new_field") and p.tot == 1


def test_existing_bounds_are_unchanged():
    for bad in (
        {"flow": -1},
        {"flow": 501},
        {"vol": -1},
        {"psi": 501},
        {"tmp": 151},
        {"rssi": 1},
        {"v": 2},
        {"id": "xyz"},
    ):
        with pytest.raises(ValidationError):
            parse(**bad)


def test_future_timestamp_is_still_rejected():
    with pytest.raises(ValidationError):
        parse(ts=time.time() + 10_000)


def test_json_round_trip_as_sent_by_firmware():
    raw = (
        b'{"v":1,"id":"a1b2c3d4","ts":%d,"flow":0.0,"vol":0.0,"bat":99.9,"tot":88.25,'
        b'"vbat":4.02,"valve":"open","fw":"2.0.1","err":[]}' % int(time.time())
    )
    p = FlowPayload.model_validate_json(raw)
    assert p.bat == 100 and p.tot == 88.25 and p.valve == "open" and p.err == []
