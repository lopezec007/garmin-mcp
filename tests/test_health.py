from datetime import UTC, datetime

from conftest import call, call_error


def ms(text: str) -> int:
    """Epoch milliseconds of a UTC wall-clock time."""
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


# -- whole-day summaries ---------------------------------------------------------


async def test_get_daily_summary_defaults_to_today_and_drops_noise(client, fake):
    fake.responses["get_user_summary"] = {
        "userProfileId": 42,
        "uuid": "abc",
        "rule": {"typeKey": "private"},
        "calendarDate": "2026-03-15",
        "totalSteps": 8123,
        "restingHeartRate": 52,
        "averageStressLevel": 31,
        "consumedKilocalories": None,
        "bodyBatteryActivityEventList": [{"eventType": "SLEEP"}],
    }
    out = await call(client, "get_daily_summary")
    assert out == {
        "calendarDate": "2026-03-15",
        "totalSteps": 8123,
        "restingHeartRate": 52,
        "averageStressLevel": 31,
    }
    assert fake.calls_to("get_user_summary") == [(("2026-03-15",), {})]


async def test_get_daily_summary_resolves_relative_dates(client, fake):
    fake.responses["get_user_summary"] = {"totalSteps": 1}
    out = await call(client, "get_daily_summary", date="yesterday")
    assert out == {"calendarDate": "2026-03-14", "totalSteps": 1}
    assert fake.calls_to("get_user_summary") == [(("2026-03-14",), {})]


async def test_invalid_date_is_a_tool_error(client, fake):
    error = await call_error(client, "get_sleep", date="last tuesday")
    assert "Invalid date" in error
    assert fake.calls_to("get_sleep_data") == []


async def test_get_daily_summaries_picks_headlines(client, fake):
    fake.responses["get_daily_summaries"] = {
        "data": [
            {"calendarDate": "2026-03-14", "totalSteps": 5000, "uuid": "x", "netCalorieGoal": 1},
            {"calendarDate": "2026-03-15", "totalSteps": 7000, "restingHeartRate": 50},
        ]
    }
    out = await call(client, "get_daily_summaries", start_date="yesterday")
    assert out == {
        "start_date": "2026-03-14",
        "end_date": "2026-03-15",
        "days": [
            {"calendarDate": "2026-03-14", "totalSteps": 5000},
            {"calendarDate": "2026-03-15", "totalSteps": 7000, "restingHeartRate": 50},
        ],
    }
    assert fake.calls_to("get_daily_summaries") == [(("2026-03-14", "2026-03-15"), {})]


async def test_get_daily_summaries_defaults_to_7_days_and_caps_range(client, fake):
    out = await call(client, "get_daily_summaries")
    assert fake.calls_to("get_daily_summaries") == [(("2026-03-09", "2026-03-15"), {})]
    assert "No daily summary data" in out["message"]

    error = await call_error(client, "get_daily_summaries", start_date="2026-01-01")
    assert "at most 31" in error


async def test_get_daily_summaries_chunks_ranges_over_28_days(client, fake):
    fake.responses["get_daily_summaries"] = lambda start, end: {
        "data": [{"calendarDate": start, "totalSteps": 1}]
    }
    out = await call(client, "get_daily_summaries", start_date="2026-02-13", end_date="2026-03-15")
    assert fake.calls_to("get_daily_summaries") == [
        (("2026-02-13", "2026-03-12"), {}),
        (("2026-03-13", "2026-03-15"), {}),
    ]
    assert [d["calendarDate"] for d in out["days"]] == ["2026-02-13", "2026-03-13"]


# -- sleep -------------------------------------------------------------------------

SLEEP = {
    "dailySleepDTO": {
        "id": 1,
        "userProfilePK": 42,
        "calendarDate": "2026-03-15",
        "sleepTimeSeconds": 27000,
        "deepSleepSeconds": 5400,
        "sleepStartTimestampGMT": ms("2026-03-14T22:30:00"),
        "sleepStartTimestampLocal": ms("2026-03-14T23:30:00"),
        "sleepScores": {"overall": {"value": 82, "qualifierKey": "GOOD"}},
    },
    "restingHeartRate": 49,
    "avgOvernightHrv": 61.0,
    "remSleepData": True,
    "sleepMovement": [{"startGMT": "2026-03-14T22:30:00.0", "activityLevel": 1.2}] * 50,
    "sleepLevels": [
        {
            "startGMT": "2026-03-14T22:30:00.0",
            "endGMT": "2026-03-14T23:10:00.0",
            "activityLevel": 1.0,
        }
    ],
    "sleepHeartRate": [{"value": 50, "startGMT": 1}] * 50,
    "wellnessEpochRespirationDataDTOList": [{"respirationValue": 14.0}] * 50,
}


async def test_get_sleep_drops_timeseries_by_default(client, fake):
    fake.responses["get_sleep_data"] = SLEEP
    out = await call(client, "get_sleep", date="2026-03-15")
    assert out == {
        "dailySleepDTO": {
            "calendarDate": "2026-03-15",
            "sleepTimeSeconds": 27000,
            "deepSleepSeconds": 5400,
            "sleepStartTimestampGMT": "2026-03-14T22:30:00Z",
            "sleepStartTimestampLocal": "2026-03-14T23:30:00",
            "sleepScores": {"overall": {"value": 82, "qualifierKey": "GOOD"}},
        },
        "calendarDate": "2026-03-15",
        "restingHeartRate": 49,
        "avgOvernightHrv": 61.0,
    }
    assert fake.calls_to("get_sleep_data") == [(("2026-03-15",), {})]


async def test_get_sleep_stage_timeline_is_opt_in(client, fake):
    fake.responses["get_sleep_data"] = SLEEP
    out = await call(client, "get_sleep", include_stage_timeline=True)
    assert out["sleepLevels"] == SLEEP["sleepLevels"]
    assert "sleepMovement" not in out and "sleepHeartRate" not in out
    assert fake.calls_to("get_sleep_data") == [(("2026-03-15",), {})]


async def test_get_sleep_without_data(client, fake):
    fake.responses["get_sleep_data"] = {"dailySleepDTO": {"id": None, "userProfilePK": 42}}
    out = await call(client, "get_sleep", date="3d")
    assert out == {
        "calendarDate": "2026-03-12",
        "message": "No sleep data recorded for 2026-03-12.",
    }


async def test_get_sleep_trend(client, fake):
    fake.responses["get_sleep_daily"] = [
        {"calendarDate": "2026-03-14", "values": {"sleepScore": 80, "totalSleepTimeInSeconds": 1}},
        {"calendarDate": "2026-03-15", "values": {"sleepScore": 75, "spO2": None}},
    ]
    out = await call(client, "get_sleep_trend")
    assert out["start_date"] == "2026-03-02"
    assert out["end_date"] == "2026-03-15"
    assert out["days"] == [
        {"calendarDate": "2026-03-14", "sleepScore": 80, "totalSleepTimeInSeconds": 1},
        {"calendarDate": "2026-03-15", "sleepScore": 75},
    ]
    assert fake.calls_to("get_sleep_daily") == [(("2026-03-02", "2026-03-15"), {})]


# -- heart -------------------------------------------------------------------------

HEART = {
    "userProfilePK": 42,
    "calendarDate": "2026-03-15",
    "startTimestampGMT": "2026-03-14T23:00:00.0",
    "startTimestampLocal": "2026-03-15T00:00:00.0",
    "restingHeartRate": 50,
    "minHeartRate": 46,
    "maxHeartRate": 150,
    "heartRateValueDescriptors": [
        {"key": "timestamp", "index": 0},
        {"key": "heartrate", "index": 1},
    ],
    "heartRateValues": [
        [ms("2026-03-14T23:00:00"), 50],
        [ms("2026-03-14T23:02:00"), 54],
        [ms("2026-03-14T23:20:00"), None],
        [ms("2026-03-14T23:30:00"), 60],
    ],
}


async def test_get_heart_rate_summary_only_by_default(client, fake):
    fake.responses["get_heart_rates"] = HEART
    out = await call(client, "get_heart_rate")
    assert out == {
        "calendarDate": "2026-03-15",
        "startTimestampGMT": "2026-03-14T23:00:00.0",
        "startTimestampLocal": "2026-03-15T00:00:00.0",
        "restingHeartRate": 50,
        "minHeartRate": 46,
        "maxHeartRate": 150,
    }
    assert fake.calls_to("get_heart_rates") == [(("2026-03-15",), {})]


async def test_get_heart_rate_curve_is_bucketed_in_local_time(client, fake):
    fake.responses["get_heart_rates"] = HEART
    out = await call(client, "get_heart_rate", interval_minutes=30)
    assert out["curve"] == {
        "interval_minutes": 30,
        "time": "local",
        "points": [["2026-03-15T00:00", 52], ["2026-03-15T00:30", 60]],
    }
    assert "heartRateValues" not in out


async def test_curve_falls_back_to_utc_without_local_timestamps(client, fake):
    fake.responses["get_heart_rates"] = {
        "heartRateValues": [[ms("2026-03-14T23:00:00"), 50]],
    }
    out = await call(client, "get_heart_rate", interval_minutes=60)
    assert out["curve"]["time"] == "UTC"
    assert out["curve"]["points"] == [["2026-03-14T23:00Z", 50]]


async def test_interval_minutes_is_validated(client, fake):
    await call_error(client, "get_heart_rate", interval_minutes=1)
    assert fake.calls_to("get_heart_rates") == []


async def test_get_resting_heart_rate(client, fake):
    fake.responses["get_rhr_daily"] = [
        {"calendarDate": "2026-03-13", "value": 50},
        {"calendarDate": "2026-03-14", "value": 53},
    ]
    out = await call(client, "get_resting_heart_rate", start_date="2026-03-10")
    assert out == {
        "start_date": "2026-03-10",
        "end_date": "2026-03-15",
        "average": 51.5,
        "lowest": 50,
        "highest": 53,
        "days": [
            {"calendarDate": "2026-03-13", "restingHeartRate": 50},
            {"calendarDate": "2026-03-14", "restingHeartRate": 53},
        ],
    }
    assert fake.calls_to("get_rhr_daily") == [(("2026-03-10", "2026-03-15"), {})]


async def test_get_resting_heart_rate_default_range_and_no_data(client, fake):
    fake.responses["get_rhr_daily"] = []
    out = await call(client, "get_resting_heart_rate")
    assert fake.calls_to("get_rhr_daily") == [(("2026-02-14", "2026-03-15"), {})]
    assert "No resting heart rate data" in out["message"]


async def test_get_hrv(client, fake):
    fake.responses["get_hrv_data_range"] = {
        "userProfilePk": 42,
        "hrvSummaries": [
            {
                "calendarDate": "2026-03-15",
                "weeklyAvg": 60,
                "lastNightAvg": 64,
                "status": "BALANCED",
                "baseline": {"balancedLow": 55, "balancedUpper": 70},
                "createTimeStamp": "2026-03-15T07:00:00.0",
            }
        ],
    }
    out = await call(client, "get_hrv", start_date="today", end_date="today")
    assert out["days"] == [
        {
            "calendarDate": "2026-03-15",
            "weeklyAvg": 60,
            "lastNightAvg": 64,
            "status": "BALANCED",
            "baseline": {"balancedLow": 55, "balancedUpper": 70},
        }
    ]
    assert fake.calls_to("get_hrv_data_range") == [(("2026-03-15", "2026-03-15"), {})]


async def test_get_hrv_tolerates_no_data(client, fake):
    fake.responses["get_hrv_data_range"] = None
    out = await call(client, "get_hrv")
    assert fake.calls_to("get_hrv_data_range") == [(("2026-03-09", "2026-03-15"), {})]
    assert out["message"] == "No HRV data recorded between 2026-03-09 and 2026-03-15."


# -- stress and energy ---------------------------------------------------------------

STRESS = {
    "userProfilePK": 42,
    "calendarDate": "2026-03-15",
    "startTimestampGMT": "2026-03-14T23:00:00.0",
    "startTimestampLocal": "2026-03-15T00:00:00.0",
    "maxStressLevel": 90,
    "avgStressLevel": 35,
    "stressValueDescriptorsDTOList": [
        {"key": "timestamp", "index": 0},
        {"key": "stressLevel", "index": 1},
    ],
    "stressValuesArray": [
        [ms("2026-03-15T08:00:00"), 20],
        [ms("2026-03-15T08:03:00"), 40],
        [ms("2026-03-15T08:06:00"), -1],
        [ms("2026-03-15T09:00:00"), -2],
    ],
    "bodyBatteryValuesArray": [[ms("2026-03-15T08:00:00"), "MEASURED", 60, 2.0]],
}


async def test_get_stress_drops_arrays_by_default(client, fake):
    fake.responses["get_all_day_stress"] = STRESS
    out = await call(client, "get_stress", date="today")
    assert out["avgStressLevel"] == 35
    assert out["maxStressLevel"] == 90
    assert not any(isinstance(v, list) for v in out.values())
    assert "curve" not in out
    assert fake.calls_to("get_all_day_stress") == [(("2026-03-15",), {})]


async def test_get_stress_curve_skips_unmeasured_samples(client, fake):
    fake.responses["get_all_day_stress"] = STRESS
    out = await call(client, "get_stress", interval_minutes=15)
    assert out["curve"]["points"] == [["2026-03-15T09:00", 30]]


BODY_BATTERY_DAY = {
    "date": "2026-03-15",
    "charged": 60,
    "drained": 45,
    "startTimestampGMT": "2026-03-14T23:00:00.0",
    "startTimestampLocal": "2026-03-15T00:00:00.0",
    "bodyBatteryValueDescriptorDTOList": [
        {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
        {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"},
    ],
    "bodyBatteryValuesArray": [
        [ms("2026-03-14T23:00:00"), 30],
        [ms("2026-03-15T06:00:00"), 95],
        [ms("2026-03-15T06:30:00"), 91],
        [ms("2026-03-15T20:00:00"), 22],
    ],
}


async def test_get_body_battery_summarises_each_day(client, fake):
    fake.responses["get_body_battery"] = [BODY_BATTERY_DAY]
    out = await call(client, "get_body_battery")
    assert out == {
        "start_date": "2026-03-09",
        "end_date": "2026-03-15",
        "days": [
            {
                "date": "2026-03-15",
                "charged": 60,
                "drained": 45,
                "startTimestampGMT": "2026-03-14T23:00:00.0",
                "startTimestampLocal": "2026-03-15T00:00:00.0",
                "highest": 95,
                "lowest": 22,
            }
        ],
    }
    assert fake.calls_to("get_body_battery") == [(("2026-03-09", "2026-03-15"), {})]


async def test_get_body_battery_curve(client, fake):
    fake.responses["get_body_battery"] = [BODY_BATTERY_DAY]
    out = await call(
        client, "get_body_battery", start_date="today", end_date="today", interval_minutes=60
    )
    assert out["days"][0]["curve"]["points"] == [
        ["2026-03-15T00:00", 30],
        ["2026-03-15T07:00", 93],
        ["2026-03-15T21:00", 22],
    ]
    assert fake.calls_to("get_body_battery") == [(("2026-03-15", "2026-03-15"), {})]

    error = await call_error(client, "get_body_battery", start_date="10d", interval_minutes=60)
    assert "at most 7" in error


async def test_get_body_battery_reads_status_tagged_samples(client, fake):
    # Some payloads carry [timestamp, status, level, version] without descriptors.
    fake.responses["get_body_battery"] = [
        {
            "date": "2026-03-15",
            "bodyBatteryValuesArray": [[1, "MEASURED", 60, 2.0], [2, "MEASURED", 40, 2.0]],
        }
    ]
    out = await call(client, "get_body_battery", start_date="today")
    assert out["days"] == [{"date": "2026-03-15", "highest": 60, "lowest": 40}]


async def test_get_body_battery_events(client, fake):
    fake.responses["get_body_battery_events"] = [
        {
            "event": {"eventType": "SLEEP", "bodyBatteryImpact": 55, "durationInMilliseconds": 1},
            "activityName": None,
            "averageStress": 12.0,
            "stressValuesArray": [[1, 10]] * 20,
            "bodyBatteryValuesArray": [[1, "MEASURED", 50, 2.0]] * 20,
        }
    ]
    out = await call(client, "get_body_battery_events", date="yesterday")
    assert out == {
        "calendarDate": "2026-03-14",
        "events": [
            {
                "event": {
                    "eventType": "SLEEP",
                    "bodyBatteryImpact": 55,
                    "durationInMilliseconds": 1,
                },
                "averageStress": 12.0,
            }
        ],
    }
    assert fake.calls_to("get_body_battery_events") == [(("2026-03-14",), {})]


async def test_get_body_battery_events_empty(client, fake):
    fake.responses["get_body_battery_events"] = []
    out = await call(client, "get_body_battery_events")
    assert out == {
        "calendarDate": "2026-03-15",
        "message": "No Body Battery event data recorded for 2026-03-15.",
    }


# -- breathing and blood oxygen -----------------------------------------------------


async def test_get_respiration(client, fake):
    fake.responses["get_respiration_data"] = {
        "userProfilePK": 42,
        "calendarDate": "2026-03-15",
        "lowestRespirationValue": 9.0,
        "highestRespirationValue": 21.0,
        "avgWakingRespirationValue": 14.0,
        "avgSleepRespirationValue": 12.0,
        "respirationValuesArray": [[1, 14.0]] * 100,
        "respirationValueDescriptorsDTOList": [{"key": "timestamp", "index": 0}],
    }
    out = await call(client, "get_respiration")
    assert out == {
        "calendarDate": "2026-03-15",
        "lowestRespirationValue": 9.0,
        "highestRespirationValue": 21.0,
        "avgWakingRespirationValue": 14.0,
        "avgSleepRespirationValue": 12.0,
    }
    assert fake.calls_to("get_respiration_data") == [(("2026-03-15",), {})]


async def test_get_spo2(client, fake):
    fake.responses["get_spo2_data"] = {
        "userProfilePK": 42,
        "calendarDate": "2026-03-14",
        "averageSpO2": 95.0,
        "lowestSpO2": 88,
        "lastSevenDaysAvgSpO2": 94.5,
        "spO2HourlyAverages": [[1, 95]] * 24,
        "continuousReadingDTOList": None,
    }
    out = await call(client, "get_spo2", date="1d")
    assert out == {
        "calendarDate": "2026-03-14",
        "averageSpO2": 95.0,
        "lowestSpO2": 88,
        "lastSevenDaysAvgSpO2": 94.5,
    }
    assert fake.calls_to("get_spo2_data") == [(("2026-03-14",), {})]


async def test_single_day_tools_tolerate_none(client, fake):
    fake.responses["get_spo2_data"] = None
    out = await call(client, "get_spo2")
    assert out == {"calendarDate": "2026-03-15", "message": "No SpO2 data recorded for 2026-03-15."}


# -- movement -------------------------------------------------------------------------


async def test_get_steps(client, fake):
    fake.responses["get_daily_steps"] = [
        {"calendarDate": "2026-03-14", "totalSteps": 9000, "totalDistance": 7000, "stepGoal": 8000},
        {"calendarDate": "2026-03-15", "totalSteps": 4000, "totalDistance": 3100, "stepGoal": 8100},
    ]
    out = await call(client, "get_steps", start_date="yesterday", end_date="today")
    assert out == {
        "start_date": "2026-03-14",
        "end_date": "2026-03-15",
        "days": fake.responses["get_daily_steps"],
    }
    assert fake.calls_to("get_daily_steps") == [(("2026-03-14", "2026-03-15"), {})]


async def test_get_steps_default_range_and_limits(client, fake):
    fake.responses["get_daily_steps"] = None
    out = await call(client, "get_steps")
    assert fake.calls_to("get_daily_steps") == [(("2026-03-09", "2026-03-15"), {})]
    assert "No step data" in out["message"]

    assert "at most 366" in await call_error(client, "get_steps", start_date="2025-01-01")
    assert "after end date" in await call_error(
        client, "get_steps", start_date="today", end_date="yesterday"
    )


async def test_get_intensity_minutes(client, fake):
    fake.responses["get_intensity_minutes_data"] = {
        "userProfilePK": 42,
        "calendarDate": "2026-03-15",
        "weeklyModerate": 80,
        "weeklyVigorous": 30,
        "weekGoal": 150,
        "imValuesArray": [[1, 2]] * 40,
    }
    fake.responses["get_daily_intensity_minutes_stats"] = [
        {"calendarDate": "2026-03-15", "values": {"moderateValue": 20, "vigorousValue": 10}}
    ]
    out = await call(client, "get_intensity_minutes", end_date="today")
    assert out == {
        "start_date": "2026-03-09",
        "end_date": "2026-03-15",
        "week_of_end_date": {
            "calendarDate": "2026-03-15",
            "weeklyModerate": 80,
            "weeklyVigorous": 30,
            "weekGoal": 150,
        },
        "days": [{"calendarDate": "2026-03-15", "moderateValue": 20, "vigorousValue": 10}],
    }
    assert fake.calls_to("get_intensity_minutes_data") == [(("2026-03-15",), {})]
    assert fake.calls_to("get_daily_intensity_minutes_stats") == [
        (("2026-03-09", "2026-03-15"), {})
    ]


async def test_get_floors_chunks_long_ranges(client, fake):
    fake.responses["get_daily_floors_stats"] = lambda start, end: [
        {"calendarDate": start, "values": {"floorsAscended": 10.0, "floorsDescended": 8.0}}
    ]
    out = await call(client, "get_floors", start_date="2026-01-01", end_date="2026-02-10")
    assert fake.calls_to("get_daily_floors_stats") == [
        (("2026-01-01", "2026-01-28"), {}),
        (("2026-01-29", "2026-02-10"), {}),
    ]
    assert out["days"] == [
        {"calendarDate": "2026-01-01", "floorsAscended": 10.0, "floorsDescended": 8.0},
        {"calendarDate": "2026-01-29", "floorsAscended": 10.0, "floorsDescended": 8.0},
    ]


# -- hydration ----------------------------------------------------------------------


async def test_get_hydration(client, fake):
    fake.responses["get_hydration_data"] = {
        "userId": 42,
        "calendarDate": "2026-03-15",
        "valueInML": 1500.0,
        "goalInML": 2400.0,
        "sweatLossInML": None,
    }
    out = await call(client, "get_hydration")
    assert out == {"calendarDate": "2026-03-15", "valueInML": 1500.0, "goalInML": 2400.0}
    assert fake.calls_to("get_hydration_data") == [(("2026-03-15",), {})]


async def test_get_hydration_nothing_logged(client, fake):
    fake.responses["get_hydration_data"] = {"userId": 42, "calendarDate": "2026-03-15"}
    out = await call(client, "get_hydration")
    assert out["message"] == "No hydration data recorded for 2026-03-15."
