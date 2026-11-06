from conftest import call, call_error


async def test_get_user_profile(client, fake):
    fake.responses["get_user_profile"] = {
        "userData": {"weight": 70000.0, "height": 180.0, "vo2MaxRunning": None},
        "userSleep": {"sleepTime": 79200},
    }
    out = await call(client, "get_user_profile")
    assert out["full_name"] == "Test User"
    assert out["today"] == "2026-03-15"
    assert out["user_data"] == {"weight": 70000.0, "height": 180.0}


async def test_get_devices(client, fake):
    fake.responses["get_devices"] = [{"deviceId": 1, "productDisplayName": "Forerunner", "x": 1}]
    assert await call(client, "get_devices") == [
        {"deviceId": 1, "productDisplayName": "Forerunner"}
    ]


# -- gear ----------------------------------------------------------------------

GEAR = [
    {
        "gearPk": 1,
        "uuid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "displayName": "Daily trainers",
        "gearTypeName": "Shoes",
        "gearStatusName": "active",
        "dateBegin": "2025-09-01T00:00:00.0",
        "maximumMeters": 800000.0,
        "userProfilePk": 777,
    },
    {
        "gearPk": 2,
        "uuid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "displayName": "Old racers",
        "gearTypeName": "Shoes",
        "gearStatusName": "retired",
    },
]


def gear_stats(uuid):
    return {"uuid": uuid, "totalDistance": 412345.6, "totalActivities": 48, "processing": False}


async def test_list_gear_resolves_profile_number_and_adds_stats(client, fake):
    fake.responses["get_device_last_used"] = {"userProfileNumber": 777}
    fake.responses["get_gear"] = GEAR
    fake.responses["get_gear_stats"] = gear_stats
    out = await call(client, "list_gear")
    assert fake.calls_to("get_gear") == [((777,), {})]
    assert fake.calls_to("get_gear_stats") == [(("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",), {})]
    assert out == [
        {
            "uuid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "displayName": "Daily trainers",
            "gearTypeName": "Shoes",
            "gearStatusName": "active",
            "dateBegin": "2025-09-01T00:00:00.0",
            "maximumMeters": 800000.0,
            "totalDistance": 412345.6,
            "totalActivities": 48,
        }
    ]


async def test_list_gear_include_retired_and_profile_fallback(client, fake):
    fake.responses["get_device_last_used"] = {}  # no device has synced
    fake.responses["get_user_profile"] = {"id": 888, "userData": {}}
    fake.responses["get_gear"] = GEAR
    fake.responses["get_gear_stats"] = {}  # the library returns {} for gear without stats
    out = await call(client, "list_gear", include_retired=True)
    assert fake.calls_to("get_gear") == [((888,), {})]
    assert [g["displayName"] for g in out] == ["Daily trainers", "Old racers"]
    assert len(fake.calls_to("get_gear_stats")) == 2
    assert "totalDistance" not in out[0]


async def test_list_gear_without_profile_number(client, fake):
    error = await call_error(client, "list_gear")
    assert "profile number" in error
    assert fake.calls_to("get_gear") == []


async def test_get_gear_activities(client, fake):
    uuid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    fake.responses["get_gear_stats"] = gear_stats
    fake.responses["get_gear_activities"] = [
        {
            "activityId": 5,
            "activityName": "Morning Run",
            "activityType": {"typeId": 1, "typeKey": "running"},
            "startTimeLocal": "2026-03-14 07:30:00",
            "distance": 10000.0,
            "duration": 3000.0,
            "ownerId": 777,
        }
    ]
    out = await call(client, "get_gear_activities", gear_uuid=uuid, limit=3)
    assert fake.calls_to("get_gear_stats") == [((uuid,), {})]
    assert fake.calls_to("get_gear_activities") == [((uuid, 3), {})]
    assert out == {
        "uuid": uuid,
        "totalDistance": 412345.6,
        "totalActivities": 48,
        "activities": [
            {
                "activityType": "running",
                "activityId": 5,
                "activityName": "Morning Run",
                "startTimeLocal": "2026-03-14 07:30:00",
                "distance": 10000.0,
                "duration": 3000.0,
            }
        ],
    }


# -- achievements --------------------------------------------------------------


async def test_get_personal_records(client, fake):
    fake.responses["get_personal_record"] = [
        {
            "id": 1,
            "typeId": 3,
            "activityId": 5,
            "activityName": "Parkrun",
            "activityType": "running",
            "value": 1234.5,
            "prStartTimeGmtFormatted": "2026-02-01T08:00:00.0",
            "activityStartDateTimeLocalFormatted": "2026-02-01T09:00:00.0",
        },
        {"id": 2, "typeId": 12, "value": 31000.0, "prStartTimeGmtFormatted": "2025-10-05T00:00"},
        {"id": 3, "typeId": 999, "value": 1.0, "prTypeLabelKey": "pr.label.new"},
    ]
    out = await call(client, "get_personal_records")
    assert fake.calls_to("get_personal_record") == [((), {})]
    assert out == [
        {
            "typeId": 3,
            "record": "fastest 5 km run",
            "value": 1234.5,
            "unit": "s",
            "date": "2026-02-01T09:00:00.0",
            "activityId": 5,
            "activityName": "Parkrun",
            "activityType": "running",
        },
        {
            "typeId": 12,
            "record": "most steps in a day",
            "value": 31000.0,
            "unit": "steps",
            "date": "2025-10-05T00:00",
        },
        {"typeId": 999, "record": "pr.label.new", "value": 1.0},
    ]


async def test_get_goals(client, fake):
    goals = [{"id": i, "goalType": "DISTANCE", "goalValue": 100000.0} for i in range(5)]
    fake.responses["get_goals"] = goals
    out = await call(client, "get_goals")
    assert fake.calls_to("get_goals") == [(("active",), {})]
    assert out == {"status": "active", "count": 5, "goals": goals}

    out = await call(client, "get_goals", status="past", limit=2)
    assert fake.calls_to("get_goals")[-1] == (("past",), {})
    assert out["total"] == 5
    assert [g["id"] for g in out["goals"]] == [3, 4]


async def test_get_recent_badges(client, fake):
    fake.responses["get_latest_earned_badges"] = [
        {
            "badgeId": 9,
            "badgeName": "10K Steps",
            "badgeEarnedDate": "2026-03-10T20:00:00.0",
            "badgeEarnedNumber": 3,
            "badgePoints": 1,
            "badgeUuid": "x",
            "connections": [],
        }
    ]
    out = await call(client, "get_recent_badges", limit=4)
    assert fake.calls_to("get_latest_earned_badges") == [((4,), {})]
    assert out == [
        {
            "badgeId": 9,
            "badgeName": "10K Steps",
            "badgeEarnedDate": "2026-03-10T20:00:00.0",
            "badgeEarnedNumber": 3,
            "badgePoints": 1,
        }
    ]
