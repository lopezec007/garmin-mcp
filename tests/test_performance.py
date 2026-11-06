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


# -- training readiness -------------------------------------------------------


async def test_training_readiness_single_day_newest_first(client, fake):
    fake.responses["get_training_readiness"] = [
        {
            "userProfilePK": 1,
            "deviceId": 9,
            "calendarDate": "2026-03-15",
            "timestamp": "2026-03-15T06:30:00.0",
            "score": 71,
            "level": "HIGH",
            "inputContext": "AFTER_WAKEUP_RESET",
            "recoveryTime": None,
        },
        {
            "calendarDate": "2026-03-15",
            "timestamp": "2026-03-15T18:00:00.0",
            "score": 40,
            "level": "LOW",
        },
    ]
    out = await call(client, "get_training_readiness")
    assert fake.calls_to("get_training_readiness") == [(("2026-03-15",), {})]
    assert out["date"] == "2026-03-15"
    assert [s["score"] for s in out["snapshots"]] == [40, 71]
    assert out["snapshots"][1] == {
        "calendarDate": "2026-03-15",
        "timestamp": "2026-03-15T06:30:00.0",
        "score": 71,
        "level": "HIGH",
        "inputContext": "AFTER_WAKEUP_RESET",
    }


async def test_training_readiness_single_dict_and_empty(client, fake):
    fake.responses["get_training_readiness"] = {"calendarDate": "2026-03-14", "score": 55}
    out = await call(client, "get_training_readiness", end_date="yesterday")
    assert fake.calls_to("get_training_readiness") == [(("2026-03-14",), {})]
    assert out["snapshots"] == [{"calendarDate": "2026-03-14", "score": 55}]

    fake.responses["get_training_readiness"] = None
    assert await call(client, "get_training_readiness") == {"date": "2026-03-15"}


async def test_training_readiness_range_one_row_per_day(client, fake):
    fake.responses["get_training_readiness_range"] = [
        {"calendarDate": "2026-03-14", "timestamp": "2026-03-14T20:00:00.0", "score": 30},
        {
            "calendarDate": "2026-03-14",
            "timestamp": "2026-03-14T07:00:00.0",
            "score": 80,
            "inputContext": "AFTER_WAKEUP_RESET",
            "deviceId": 9,
        },
        {"calendarDate": "2026-03-13", "timestamp": "2026-03-13T07:00:00.0", "score": 60},
        {"calendarDate": "2026-03-13", "timestamp": "2026-03-13T21:00:00.0", "score": 65},
    ]
    out = await call(client, "get_training_readiness", start_date="2d")
    assert fake.calls_to("get_training_readiness_range") == [(("2026-03-13", "2026-03-15"), {})]
    assert fake.calls_to("get_training_readiness") == []
    assert out["days"] == [
        {"calendarDate": "2026-03-13", "score": 65},
        {"calendarDate": "2026-03-14", "score": 80, "inputContext": "AFTER_WAKEUP_RESET"},
    ]


async def test_training_readiness_range_limit(client, fake):
    error = await call_error(client, "get_training_readiness", start_date="2025-01-01")
    assert "at most 92 days" in error
    assert fake.calls == []


# -- training status ----------------------------------------------------------


async def test_training_status_uses_aggregated_payload(client, fake):
    fake.responses["get_training_status"] = {
        "userId": 1,
        "mostRecentTrainingStatus": {"latestTrainingStatusData": {"9": {"trainingStatus": 4}}},
        "mostRecentTrainingLoadBalance": {"userId": 1, "x": {"monthlyLoadAerobicLow": 500.0}},
        "mostRecentVO2Max": {"generic": {"vo2MaxValue": 52}, "cycling": None},
    }
    out = await call(client, "get_training_status", date="2026-03-10")
    assert fake.calls_to("get_training_status") == [(("2026-03-10",), {})]
    assert fake.calls_to("get_daily_training_status") == []
    assert fake.calls_to("get_training_four_week_load_balance") == []
    assert out == {
        "date": "2026-03-10",
        "mostRecentTrainingStatus": {"latestTrainingStatusData": {"9": {"trainingStatus": 4}}},
        "mostRecentTrainingLoadBalance": {"x": {"monthlyLoadAerobicLow": 500.0}},
        "mostRecentVO2Max": {"generic": {"vo2MaxValue": 52}},
    }


async def test_training_status_falls_back_to_dedicated_endpoints(client, fake):
    fake.responses["get_training_status"] = None
    fake.responses["get_daily_training_status"] = {"userId": 1, "trainingStatus": 4}
    fake.responses["get_training_four_week_load_balance"] = {"monthlyLoadAnaerobic": 120.0}
    out = await call(client, "get_training_status")
    assert fake.calls_to("get_daily_training_status") == [(("2026-03-15",), {})]
    assert fake.calls_to("get_training_four_week_load_balance") == [(("2026-03-15",), {})]
    assert out == {
        "date": "2026-03-15",
        "dailyTrainingStatus": {"trainingStatus": 4},
        "trainingLoadBalance": {"monthlyLoadAnaerobic": 120.0},
    }


async def test_invalid_date_is_a_tool_error(client, fake):
    error = await call_error(client, "get_training_status", date="15/03/2026")
    assert "Invalid date" in error
    assert fake.calls == []


# -- VO2 max ------------------------------------------------------------------


async def test_vo2max_latest(client, fake):
    fake.responses["get_max_metrics_latest"] = {
        "userId": 1,
        "generic": {"calendarDate": "2026-03-12", "vo2MaxPreciseValue": 52.4},
        "cycling": None,
    }
    out = await call(client, "get_vo2max")
    assert fake.calls_to("get_max_metrics_latest") == [(("2026-03-15",), {})]
    assert out == {"generic": {"calendarDate": "2026-03-12", "vo2MaxPreciseValue": 52.4}}


@pytest.mark.parametrize(
    ("start", "expected_start", "aggregation"),
    [
        ("29d", "2026-02-14", "daily"),
        ("2025-10-01", "2025-10-01", "weekly"),
        ("2024-01-01", "2024-01-01", "monthly"),
    ],
)
async def test_vo2max_history_aggregation(client, fake, start, expected_start, aggregation):
    fake.responses["get_max_metrics_range"] = [{"userId": 1, "generic": {"vo2MaxValue": 51}}]
    out = await call(client, "get_vo2max", start_date=start)
    assert fake.calls_to("get_max_metrics_range") == [
        ((expected_start, "2026-03-15", aggregation), {})
    ]
    assert out == {
        "start": expected_start,
        "end": "2026-03-15",
        "aggregation": aggregation,
        "history": [{"generic": {"vo2MaxValue": 51}}],
    }


# -- race predictions ---------------------------------------------------------


async def test_race_predictions_latest(client, fake):
    fake.responses["get_race_predictions"] = {"userId": 1, "time5K": 1320, "time10K": 2760}
    assert await call(client, "get_race_predictions") == {"time5K": 1320, "time10K": 2760}
    assert fake.calls_to("get_race_predictions") == [((), {})]


async def test_race_predictions_history(client, fake):
    fake.responses["get_race_predictions"] = [{"calendarDate": "2026-03-01", "time5K": 1330}]
    out = await call(client, "get_race_predictions", start_date="2026-03-01")
    assert out["aggregation"] == "daily"
    assert out["history"] == [{"calendarDate": "2026-03-01", "time5K": 1330}]

    await call(client, "get_race_predictions", start_date="2025-06-01", end_date="2026-03-01")
    assert fake.calls_to("get_race_predictions") == [
        (("2026-03-01", "2026-03-15", "daily"), {}),
        (("2025-06-01", "2026-03-01", "monthly"), {}),
    ]


async def test_race_predictions_range_limit(client, fake):
    error = await call_error(client, "get_race_predictions", start_date="2025-01-01")
    assert "at most 366 days" in error
    assert fake.calls == []


# -- endurance / hill score ---------------------------------------------------


async def test_endurance_score(client, fake):
    fake.responses["get_endurance_score"] = {"userProfilePK": 1, "overallScore": 6100}
    assert await call(client, "get_endurance_score", end_date="2026-03-10") == {
        "overallScore": 6100
    }
    await call(client, "get_endurance_score", start_date="2026-01-01", end_date="2026-03-01")
    await call(client, "get_endurance_score", start_date="2025-03-16")
    assert fake.calls_to("get_endurance_score") == [
        (("2026-03-10",), {}),
        (("2026-01-01", "2026-03-01", "daily"), {}),
        (("2025-03-16", "2026-03-15", "weekly"), {}),
    ]


async def test_hill_score(client, fake):
    fake.responses["get_hill_score"] = {"userProfilePK": 1, "overallScore": 48}
    assert await call(client, "get_hill_score") == {"overallScore": 48}
    await call(client, "get_hill_score", start_date="2026-03-01", end_date="2026-03-10")
    assert fake.calls_to("get_hill_score") == [
        (("2026-03-15",), {}),
        (("2026-03-01", "2026-03-10"), {}),
    ]
    assert "at most 92 days" in await call_error(client, "get_hill_score", start_date="120d")


# -- fitness age --------------------------------------------------------------


async def test_fitness_age(client, fake):
    fake.responses["get_fitness_age_details"] = {"fitnessAge": 31.5, "chronologicalAge": 38}
    out = await call(client, "get_fitness_age", date="yesterday")
    assert out == {"fitnessAge": 31.5, "chronologicalAge": 38}
    assert fake.calls_to("get_fitness_age_details") == [(("2026-03-14",), {})]
    assert fake.calls_to("get_fitnessage_data") == []


async def test_fitness_age_falls_back_to_legacy_endpoint(client, fake):
    fake.responses["get_fitnessage_data"] = {"fitnessAge": 33.0}
    assert await call(client, "get_fitness_age") == {"fitnessAge": 33.0}
    assert fake.calls_to("get_fitnessage_data") == [(("2026-03-15",), {})]


# -- thresholds ---------------------------------------------------------------


async def test_lactate_threshold_latest(client, fake):
    fake.responses["get_lactate_threshold"] = {
        "speed_and_heart_rate": {
            "userProfilePK": 1,
            "calendarDate": "2026-02-20T10:00:00.0",
            "speed": 0.36,
            "heartRate": 171,
            "heartRateCycling": None,
        },
        "power": {},
    }
    out = await call(client, "get_lactate_threshold")
    assert fake.calls_to("get_lactate_threshold") == [((), {"latest": True})]
    assert out == {
        "speed_and_heart_rate": {
            "calendarDate": "2026-02-20T10:00:00.0",
            "speed": 0.36,
            "heartRate": 171,
        }
    }


async def test_lactate_threshold_history(client, fake):
    fake.responses["get_lactate_threshold"] = {
        "speed": [{"from": "2025-09-01", "value": 0.35}],
        "heart_rate": [{"from": "2025-09-01", "value": 170}],
        "power": [],
    }
    out = await call(client, "get_lactate_threshold", start_date="2025-09-01")
    assert fake.calls_to("get_lactate_threshold") == [
        (
            (),
            {
                "latest": False,
                "start_date": "2025-09-01",
                "end_date": "2026-03-15",
                "aggregation": "weekly",
            },
        )
    ]
    assert out == {
        "start": "2025-09-01",
        "end": "2026-03-15",
        "aggregation": "weekly",
        "speed": [{"from": "2025-09-01", "value": 0.35}],
        "heart_rate": [{"from": "2025-09-01", "value": 170}],
    }


async def test_cycling_ftp(client, fake):
    fake.responses["get_cycling_ftp"] = {"userProfilePK": 1, "functionalThresholdPower": 250}
    assert await call(client, "get_cycling_ftp") == {"functionalThresholdPower": 250}
    assert fake.calls_to("get_functional_threshold_power_range") == []

    fake.responses["get_functional_threshold_power_range"] = [{"value": 245}]
    out = await call(client, "get_cycling_ftp", start_date="2026-01-01", end_date="2026-02-01")
    assert out["history"] == [{"value": 245}]
    assert fake.calls_to("get_functional_threshold_power_range") == [
        (("2026-01-01", "2026-02-01"), {"sport": "CYCLING", "aggregation": "daily"})
    ]


# -- zones and power curve ----------------------------------------------------


async def test_heart_rate_and_power_zones(client, fake):
    fake.responses["get_heart_rate_zones"] = [
        {"sport": "DEFAULT", "zone1Floor": 95, "maxHeartRateUsed": 190, "changeState": None}
    ]
    fake.responses["get_power_zones"] = [
        {"userProfilePK": 1, "sport": "CYCLING", "functionalThresholdPower": 250, "zone1Floor": 0}
    ]
    assert await call(client, "get_heart_rate_zones") == [
        {"sport": "DEFAULT", "zone1Floor": 95, "maxHeartRateUsed": 190}
    ]
    assert await call(client, "get_power_zones") == [
        {"sport": "CYCLING", "functionalThresholdPower": 250, "zone1Floor": 0}
    ]
    assert fake.calls_to("get_heart_rate_zones") == [((), {})]
    assert fake.calls_to("get_power_zones") == [((), {})]


async def test_power_curve(client, fake):
    fake.responses["get_power_curve"] = {"entries": [{"duration": 5, "power": 900}]}
    out = await call(client, "get_power_curve")
    assert fake.calls_to("get_power_curve") == [(("2025-12-16", "2026-03-15", "cycling"), {})]
    assert out == {
        "start": "2025-12-16",
        "end": "2026-03-15",
        "sport": "cycling",
        "entries": [{"duration": 5, "power": 900}],
    }
    await call(client, "get_power_curve", start_date="2026-03-01", sport="running")
    assert fake.calls_to("get_power_curve")[1] == (("2026-03-01", "2026-03-15", "running"), {})
