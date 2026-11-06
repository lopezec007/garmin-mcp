import inspect

import pytest
from conftest import call, call_error
from garminconnect import Garmin


@pytest.fixture(autouse=True)
def real_signatures(fake):
    """Every call the tools made must bind to the real ``garminconnect`` signature."""
    yield
    for name, args, kwargs in fake.calls:
        inspect.signature(getattr(Garmin, name)).bind(None, *args, **kwargs)


async def test_body_composition_defaults_to_last_30_days(client, fake):
    fake.responses["get_body_composition"] = {
        "startDate": "2026-02-14",
        "endDate": "2026-03-15",
        "dateWeightList": [
            {
                "samplePk": 1773561600000,
                "date": 1773561600000,
                "calendarDate": "2026-03-15",
                "weight": 70500.0,
                "bmi": 21.8,
                "bodyFat": None,
                "sourceType": "MANUAL",
            }
        ],
        "totalAverage": {"from": 1771027200000, "weight": 70500.0, "muscleMass": None},
    }
    out = await call(client, "get_body_composition")
    assert fake.calls_to("get_body_composition") == [(("2026-02-14", "2026-03-15"), {})]
    assert out["dateWeightList"] == [
        {"calendarDate": "2026-03-15", "weight": 70500.0, "bmi": 21.8, "sourceType": "MANUAL"}
    ]
    assert out["totalAverage"] == {"from": 1771027200000, "weight": 70500.0}


async def test_body_composition_explicit_range_and_odd_payloads(client, fake):
    # An entry without calendarDate keeps its only date field.
    fake.responses["get_body_composition"] = {"dateWeightList": [{"date": 1, "weight": 70000.0}]}
    out = await call(client, "get_body_composition", start_date="2026-01-01", end_date="yesterday")
    assert fake.calls_to("get_body_composition") == [(("2026-01-01", "2026-03-14"), {})]
    assert out == {"dateWeightList": [{"date": 1, "weight": 70000.0}]}

    fake.responses["get_body_composition"] = None
    assert await call(client, "get_body_composition") is None


async def test_body_composition_errors(client, fake):
    assert "Invalid date" in await call_error(client, "get_body_composition", start_date="March")
    too_long = await call_error(client, "get_body_composition", start_date="2024-01-01")
    assert "at most 366 days" in too_long
    reversed_range = await call_error(
        client, "get_body_composition", start_date="2026-03-10", end_date="2026-03-01"
    )
    assert "after end date" in reversed_range
    assert fake.calls == []


async def test_latest_weight(client, fake):
    fake.responses["get_latest_weight"] = {
        "samplePk": 5,
        "date": 1773561600000,
        "calendarDate": "2026-03-02",
        "weight": 71200.0,
        "userProfilePK": 1,
    }
    assert await call(client, "get_latest_weight") == {
        "calendarDate": "2026-03-02",
        "weight": 71200.0,
    }
    await call(client, "get_latest_weight", date="2026-01-01")
    assert fake.calls_to("get_latest_weight") == [(("2026-03-15",), {}), (("2026-01-01",), {})]


async def test_blood_pressure(client, fake):
    fake.responses["get_blood_pressure"] = {
        "from": "2026-03-09",
        "until": "2026-03-15",
        "measurementSummaries": [
            {
                "startDate": "2026-03-12",
                "measurements": [
                    {"systolic": 118, "diastolic": 76, "pulse": 58, "notes": None, "version": 3}
                ],
            }
        ],
    }
    out = await call(client, "get_blood_pressure", start_date="6d")
    assert fake.calls_to("get_blood_pressure") == [(("2026-03-09", "2026-03-15"), {})]
    assert out["measurementSummaries"][0]["measurements"] == [
        {"systolic": 118, "diastolic": 76, "pulse": 58, "version": 3}
    ]

    await call(client, "get_blood_pressure")
    assert fake.calls_to("get_blood_pressure")[1] == (("2026-02-14", "2026-03-15"), {})
    assert "at most 366 days" in await call_error(
        client, "get_blood_pressure", start_date="2024-01-01"
    )
