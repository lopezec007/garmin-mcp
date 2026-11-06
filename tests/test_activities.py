from conftest import call, call_error

RUN = {
    "activityId": 111,
    "activityName": "Morning Run",
    "startTimeLocal": "2026-03-14 07:30:00",
    "startTimeGMT": "2026-03-14 06:30:00",
    "activityType": {"typeId": 1, "typeKey": "running", "parentTypeId": 17},
    "distance": 10012.330078125,
    "duration": 3000.5,
    "elevationGain": 85.0,
    "averageSpeed": 3.3369998931884766,
    "averageHR": 150.0,
    "maxHR": 171.0,
    "calories": 700.0,
    "aerobicTrainingEffect": 3.4,
    "activityTrainingLoad": 120.5,
    "avgPower": None,
    "ownerDisplayName": "someone",
    "hasPolyline": True,
}


async def test_list_activities_defaults_to_most_recent(client, fake):
    fake.responses["search_activities"] = [RUN]
    out = await call(client, "list_activities")

    assert fake.calls_to("search_activities") == [((0, 20), {})]
    assert out["count"] == 1
    assert "more_available" not in out
    assert out["activities"] == [
        {
            "activityId": 111,
            "activityName": "Morning Run",
            "activityType": "running",
            "startTimeLocal": "2026-03-14 07:30:00",
            "distance": 10012.33,
            "duration": 3000.5,
            "elevationGain": 85.0,
            "averageSpeed": 3.337,
            "averageHR": 150.0,
            "maxHR": 171.0,
            "calories": 700.0,
            "aerobicTrainingEffect": 3.4,
            "activityTrainingLoad": 120.5,
        }
    ]


async def test_list_activities_passes_filters(client, fake):
    fake.responses["search_activities"] = [RUN, {**RUN, "activityId": 110}]
    out = await call(
        client,
        "list_activities",
        start_date="7d",
        end_date="yesterday",
        activity_type="Running",
        activity_subtype="trail_running",
        limit=2,
    )

    assert fake.calls_to("search_activities") == [
        (
            (0, 2),
            {
                "startDate": "2026-03-08",
                "endDate": "2026-03-14",
                "activityType": "running",
                "activitySubType": "trail_running",
            },
        )
    ]
    assert [a["activityId"] for a in out["activities"]] == [111, 110]
    assert out["more_available"] is True


async def test_list_activities_start_date_only(client, fake):
    fake.responses["search_activities"] = []
    out = await call(client, "list_activities", start_date="2026-03-01")

    assert fake.calls_to("search_activities") == [((0, 20), {"startDate": "2026-03-01"})]
    assert out == {"count": 0}


async def test_list_activities_rejects_bad_input(client, fake):
    assert "Invalid date" in await call_error(client, "list_activities", start_date="last week")
    assert "after end date" in await call_error(
        client, "list_activities", start_date="2026-03-10", end_date="2026-03-01"
    )
    assert "requires activity_type" in await call_error(
        client, "list_activities", activity_subtype="trail_running"
    )
    assert fake.calls_to("search_activities") == []


async def test_get_activity(client, fake):
    fake.responses["get_activity"] = {
        "activityId": 111,
        "activityUUID": {"uuid": "abc"},
        "activityName": "Morning Run",
        "userProfileId": 42,
        "activityTypeDTO": {"typeId": 1, "typeKey": "running"},
        "eventTypeDTO": {"typeId": 9, "typeKey": "uncategorized"},
        "accessControlRuleDTO": {"typeId": 2, "typeKey": "private"},
        "timeZoneUnitDTO": {"unitId": 124, "unitKey": "Europe/Madrid"},
        "metadataDTO": {
            "lapCount": 10,
            "hasSplits": True,
            "fileFormat": {"formatId": 7, "formatKey": "fit"},
            "activityImages": [{"url": "https://example.com/presigned"}],
        },
        "summaryDTO": {
            "startTimeLocal": "2026-03-14T07:30:00.0",
            "distance": 10012.330078125,
            "duration": 3000.5,
            "startLatitude": 40.41678912345678,
            "maxHR": None,
        },
        "locationName": "Madrid",
    }
    out = await call(client, "get_activity", activity_id=111)

    assert fake.calls_to("get_activity") == [((111,), {})]
    assert out == {
        "activityId": 111,
        "activityName": "Morning Run",
        "activityType": "running",
        "eventType": "uncategorized",
        "timeZone": "Europe/Madrid",
        "summaryDTO": {
            "startTimeLocal": "2026-03-14T07:30:00.0",
            "distance": 10012.33,
            "duration": 3000.5,
            "startLatitude": 40.416789,
        },
        "locationName": "Madrid",
        "metadata": {"lapCount": 10, "hasSplits": True},
    }


async def test_get_activity_accepts_string_id(client, fake):
    fake.responses["get_activity"] = {"activityId": 111}
    await call(client, "get_activity", activity_id="111")
    assert fake.calls_to("get_activity") == [(("111",), {})]


async def test_get_activity_reports_library_validation_error(client, fake):
    fake.responses["get_activity"] = ValueError("activity_id must be an integer")
    error = await call_error(client, "get_activity", activity_id="abc")
    assert "activity_id must be an integer" in error


async def test_get_activity_splits_laps(client, fake):
    fake.responses["get_activity_splits"] = {
        "activityId": 111,
        "lapDTOs": [
            {
                "lapIndex": 1,
                "distance": 1000.0,
                "duration": 300.12345,
                "averageHR": 148.0,
                "startLatitude": 40.4,
                "startLongitude": -3.7,
                "lengthDTOs": [{"distance": 25.0}],
                "connectIQMeasurement": [{"value": "1"}],
            },
            {"lapIndex": 2, "distance": 1000.0, "duration": 295.0},
        ],
        "eventDTOs": [{"startTimeGMT": "2026-03-14T06:30:00.0"}],
    }
    out = await call(client, "get_activity_splits", activity_id=111)

    assert fake.calls_to("get_activity_splits") == [((111,), {})]
    assert fake.calls_to("get_activity_typed_splits") == []
    assert out == {
        "activityId": 111,
        "laps": [
            {"lapIndex": 1, "distance": 1000.0, "duration": 300.123, "averageHR": 148.0},
            {"lapIndex": 2, "distance": 1000.0, "duration": 295.0},
        ],
    }


async def test_get_activity_splits_typed(client, fake):
    fake.responses["get_activity_typed_splits"] = {
        "activityId": 111,
        "splits": [{"type": "INTERVAL_ACTIVE", "distance": 400.0, "endLatitude": 40.4}],
    }
    out = await call(client, "get_activity_splits", activity_id=111, split_type="typed")

    assert fake.calls_to("get_activity_typed_splits") == [((111,), {})]
    assert fake.calls_to("get_activity_splits") == []
    assert out == {"activityId": 111, "splits": [{"type": "INTERVAL_ACTIVE", "distance": 400.0}]}


async def test_get_activity_zones(client, fake):
    zones = [
        {"zoneNumber": 1, "secsInZone": 120.0, "zoneLowBoundary": 95},
        {"zoneNumber": 2, "secsInZone": 1800.0, "zoneLowBoundary": 114},
    ]
    fake.responses["get_activity_hr_in_timezones"] = zones
    fake.responses["get_activity_power_in_timezones"] = [
        {"zoneNumber": 1, "secsInZone": 60.0, "zoneLowBoundary": 0}
    ]

    out = await call(client, "get_activity_zones", activity_id=111)
    assert out == {"activityId": 111, "zone_type": "heart_rate", "zones": zones}
    assert fake.calls_to("get_activity_hr_in_timezones") == [((111,), {})]
    assert fake.calls_to("get_activity_power_in_timezones") == []

    out = await call(client, "get_activity_zones", activity_id=111, zone_type="power")
    assert out["zone_type"] == "power"
    assert out["zones"][0]["secsInZone"] == 60.0
    assert fake.calls_to("get_activity_power_in_timezones") == [((111,), {})]


async def test_get_activity_weather(client, fake):
    fake.responses["get_activity_weather"] = {
        "temp": 59,
        "apparentTemp": None,
        "relativeHumidity": 70,
        "weatherTypeDTO": {"desc": "Cloudy", "image": None},
    }
    out = await call(client, "get_activity_weather", activity_id=111)

    assert fake.calls_to("get_activity_weather") == [((111,), {})]
    assert out == {"temp": 59, "relativeHumidity": 70, "weatherTypeDTO": {"desc": "Cloudy"}}


def _details(samples: int) -> dict:
    """A details payload whose descriptor order differs from the metrics order."""
    return {
        "activityId": 111,
        "totalMetricsCount": 3000,
        "metricDescriptors": [
            {"metricsIndex": 2, "key": "directHeartRate", "unit": {"key": "bpm"}},
            {"metricsIndex": 0, "key": "sumDuration", "unit": {"key": "second"}},
            {"metricsIndex": 1, "key": "directSpeed", "unit": {"key": "mps"}},
            {"metricsIndex": 3, "key": "directLatitude", "unit": {"key": "dd"}},
            {"metricsIndex": 4, "key": "directAirTemperature", "unit": {"key": "celcius"}},
        ],
        "activityDetailMetrics": [
            {"metrics": [float(i), 2.7990000247955322, 140.0 + i, 40.123456789, None]}
            for i in range(samples)
        ],
        "geoPolylineDTO": {"polyline": [{"lat": 40.1, "lon": -3.7}]},
    }


async def test_get_activity_details_default_columns(client, fake):
    fake.responses["get_activity_details"] = _details(3)
    out = await call(client, "get_activity_details", activity_id=111)

    # maxchart = max_points, maxpoly = 0 (no GPS polyline)
    assert fake.calls_to("get_activity_details") == [((111, 100, 0), {})]
    assert out == {
        "activityId": 111,
        "total_samples": 3000,
        "returned_samples": 3,
        "columns": ["sumDuration", "directHeartRate", "directSpeed"],
        "units": {"sumDuration": "second", "directHeartRate": "bpm", "directSpeed": "mps"},
        "rows": [[0.0, 140.0, 2.799], [1.0, 141.0, 2.799], [2.0, 142.0, 2.799]],
        "other_metrics": ["directLatitude", "directAirTemperature"],
    }


async def test_get_activity_details_requested_metrics_and_bound(client, fake):
    fake.responses["get_activity_details"] = _details(10)  # server ignored the bound
    out = await call(
        client,
        "get_activity_details",
        activity_id=111,
        max_points=4,
        metrics=["directLatitude", "directAirTemperature", "notRecorded"],
    )

    assert fake.calls_to("get_activity_details") == [((111, 4, 0), {})]
    assert out["columns"] == ["directLatitude", "directAirTemperature"]
    assert out["returned_samples"] == len(out["rows"]) <= 4
    assert out["rows"][0] == [40.123457, None]  # nulls keep their column position
    assert "directHeartRate" in out["other_metrics"]


async def test_get_activity_details_unknown_metrics(client, fake):
    fake.responses["get_activity_details"] = _details(2)
    error = await call_error(client, "get_activity_details", activity_id=111, metrics=["nope"])
    assert "directHeartRate" in error


async def test_get_activity_details_without_samples(client, fake):
    fake.responses["get_activity_details"] = {"activityId": 111, "detailsAvailable": False}
    out = await call(client, "get_activity_details", activity_id=111)
    assert out == {"activityId": 111, "returned_samples": 0}


async def test_get_activity_exercise_sets(client, fake):
    fake.responses["get_activity_exercise_sets"] = {
        "activityId": 111,
        "exerciseSets": [
            {
                "exercises": [
                    {"category": "ROW", "name": None, "probability": 12.5},
                    {"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS", "probability": 87.5},
                ],
                "repetitionCount": 8,
                "weight": 60000.0,
                "duration": 42.0,
                "setType": "ACTIVE",
                "messageIndex": 0,
            },
            {"exercises": [], "duration": 90.0, "setType": "REST", "messageIndex": 1},
        ],
    }
    out = await call(client, "get_activity_exercise_sets", activity_id=111)

    assert fake.calls_to("get_activity_exercise_sets") == [((111,), {})]
    assert out == {
        "activityId": 111,
        "sets": [
            {
                "category": "BENCH_PRESS",
                "exercise": "BARBELL_BENCH_PRESS",
                "repetitionCount": 8,
                "weight": 60000.0,
                "duration": 42.0,
                "setType": "ACTIVE",
            },
            {"duration": 90.0, "setType": "REST"},
        ],
    }


async def test_get_activity_gear(client, fake):
    fake.responses["get_activity_gear"] = [
        {
            "gearPk": 9,
            "uuid": "abc123",
            "displayName": "Daily trainers",
            "gearTypeName": "Shoes",
            "userProfilePk": 42,
        }
    ]
    out = await call(client, "get_activity_gear", activity_id=111)

    assert fake.calls_to("get_activity_gear") == [((111,), {})]
    assert out == [{"uuid": "abc123", "displayName": "Daily trainers", "gearTypeName": "Shoes"}]


async def test_get_activity_types(client, fake):
    fake.responses["get_activity_types"] = [
        {"typeId": 17, "typeKey": "all", "parentTypeId": 17},
        {"typeId": 1, "typeKey": "running", "parentTypeId": 17},
        {"typeId": 6, "typeKey": "trail_running", "parentTypeId": 1},
        {"typeId": 7, "typeKey": "street_running", "parentTypeId": 1},
        {"typeId": 99, "typeKey": "secret_running", "parentTypeId": 1, "isHidden": True},
        {"typeId": 2, "typeKey": "cycling", "parentTypeId": 17},
    ]
    out = await call(client, "get_activity_types")
    assert out == {
        "all": ["running", "cycling"],
        "running": ["trail_running", "street_running"],
    }


async def test_get_progress_summary_defaults_to_last_30_days(client, fake):
    fake.responses["get_progress_summary_between_dates"] = [
        {
            "date": "2026-02-14",
            "countOfActivities": 12,
            "stats": {"running": {"distance": {"count": 9, "sum": 8500000.0, "min": None}}},
        }
    ]
    out = await call(client, "get_progress_summary")

    assert fake.calls_to("get_progress_summary_between_dates") == [
        (("2026-02-14", "2026-03-15", "distance", True), {})
    ]
    assert out == {
        "start_date": "2026-02-14",
        "end_date": "2026-03-15",
        "metric": "distance",
        "summary": [
            {
                "date": "2026-02-14",
                "countOfActivities": 12,
                "stats": {"running": {"distance": {"count": 9, "sum": 8500000.0}}},
            }
        ],
    }


async def test_get_progress_summary_explicit_range_and_metric(client, fake):
    await call(
        client,
        "get_progress_summary",
        start_date="2026-01-01",
        end_date="2026-01-31",
        metric="elevationGain",
    )
    assert fake.calls_to("get_progress_summary_between_dates") == [
        (("2026-01-01", "2026-01-31", "elevationGain", True), {})
    ]


async def test_get_progress_summary_invalid_date(client, fake):
    error = await call_error(client, "get_progress_summary", start_date="03/01/2026")
    assert "Invalid date" in error
    assert fake.calls_to("get_progress_summary_between_dates") == []
