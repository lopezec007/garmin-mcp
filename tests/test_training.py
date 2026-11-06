from conftest import call, call_error

RUNNING = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}

WORKOUT = {
    "workoutId": 42,
    "ownerId": 9,
    "workoutName": "Tempo 20",
    "description": None,
    "sportType": RUNNING,
    "author": {"userProfilePk": 9, "displayName": "me"},
    "estimatedDurationInSecs": 2400,
    "estimatedDistanceInMeters": 8000.0,
    "createdDate": "2026-01-02T10:00:00.0",
    "workoutSegments": [
        {
            "segmentOrder": 1,
            "sportType": RUNNING,
            "workoutSteps": [
                {
                    "type": "ExecutableStepDTO",
                    "stepId": 1,
                    "stepOrder": 1,
                    "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
                    "endCondition": {
                        "conditionTypeId": 2,
                        "conditionTypeKey": "time",
                        "displayOrder": 2,
                        "displayable": True,
                    },
                    "endConditionValue": 600.0,
                    "targetType": {
                        "workoutTargetTypeId": 1,
                        "workoutTargetTypeKey": "no.target",
                        "displayOrder": 1,
                    },
                    "strokeType": {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0},
                    "weightUnit": {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0},
                },
                {
                    "type": "RepeatGroupDTO",
                    "stepOrder": 2,
                    "numberOfIterations": 4,
                    "workoutSteps": [
                        {
                            "type": "ExecutableStepDTO",
                            "stepOrder": 3,
                            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                            "endCondition": {"conditionTypeId": 3, "conditionTypeKey": "distance"},
                            "endConditionValue": 1000.0,
                            "targetType": {
                                "workoutTargetTypeId": 6,
                                "workoutTargetTypeKey": "pace.zone",
                            },
                            "targetValueOne": 3.7,
                            "targetValueTwo": 3.9,
                        }
                    ],
                },
            ],
        }
    ],
}


# -- workout library -----------------------------------------------------------


async def test_list_workouts(client, fake):
    fake.responses["get_workouts"] = [WORKOUT]
    out = await call(client, "list_workouts")
    assert fake.calls_to("get_workouts") == [((0, 20), {})]
    assert out == {
        "count": 1,
        "workouts": [
            {
                "workoutId": 42,
                "workoutName": "Tempo 20",
                "sport": "running",
                "estimatedDurationInSecs": 2400,
                "estimatedDistanceInMeters": 8000.0,
                "createdDate": "2026-01-02T10:00:00.0",
            }
        ],
    }


async def test_list_workouts_paging_and_sport_filter(client, fake):
    fake.responses["get_workouts"] = [WORKOUT, {**WORKOUT, "workoutId": 43}]
    out = await call(client, "list_workouts", limit=2, offset=4, sport="Running")
    assert fake.calls_to("get_workouts") == [((4, 2), {"sport_type_keys": ["running"]})]
    assert out["more_available"] is True
    assert [w["workoutId"] for w in out["workouts"]] == [42, 43]


async def test_get_workout_collapses_type_descriptors(client, fake):
    fake.responses["get_workout_by_id"] = WORKOUT
    out = await call(client, "get_workout", workout_id=42)
    assert fake.calls_to("get_workout_by_id") == [((42,), {})]
    assert "author" not in out and "ownerId" not in out
    assert out["sportType"] == "running"
    warmup, repeat = out["workoutSegments"][0]["workoutSteps"]
    assert warmup == {
        "type": "ExecutableStepDTO",
        "stepId": 1,
        "stepOrder": 1,
        "stepType": "warmup",
        "endCondition": "time",
        "endConditionValue": 600.0,
        "targetType": "no.target",
        # a unit carries its conversion factor, so it is not collapsed
        "weightUnit": {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0},
    }
    assert repeat["numberOfIterations"] == 4
    assert repeat["workoutSteps"][0]["targetType"] == "pace.zone"
    assert repeat["workoutSteps"][0]["targetValueTwo"] == 3.9


# -- calendar and schedule -----------------------------------------------------


def calendar_months(year, month):
    items = {
        (2026, 3): [
            {"id": 1, "itemType": "workout", "date": "2026-03-20", "title": "too early"},
            {
                "id": 2,
                "itemType": "workout",
                "date": "2026-03-30",
                "title": "Long run",
                "workoutId": 42,
                "isRace": False,
                "duration": None,
            },
            {"id": 3, "itemType": "activity", "date": "2026-03-26", "title": "Morning Run"},
            # Month views overlap: this April item is also in the April payload.
            {"id": 4, "itemType": "event", "date": "2026-04-02", "title": "City 10K"},
        ],
        (2026, 4): [
            {"id": 4, "itemType": "event", "date": "2026-04-02", "title": "City 10K"},
            {"id": 5, "itemType": "note", "date": "2026-04-01", "title": "Taper"},
            {"id": 6, "itemType": "workout", "date": "2026-04-20", "title": "too late"},
        ],
    }
    return {"year": year, "month": month - 1, "calendarItems": items[(year, month)]}


async def test_get_training_calendar_spans_months(client, fake):
    fake.responses["get_scheduled_workouts"] = calendar_months
    out = await call(
        client, "get_training_calendar", start_date="2026-03-25", end_date="2026-04-05"
    )
    assert fake.calls_to("get_scheduled_workouts") == [((2026, 3), {}), ((2026, 4), {})]
    assert out == {
        "start_date": "2026-03-25",
        "end_date": "2026-04-05",
        "count": 3,
        "items": [
            {
                "id": 2,
                "itemType": "workout",
                "date": "2026-03-30",
                "title": "Long run",
                "workoutId": 42,
            },
            {"id": 5, "itemType": "note", "date": "2026-04-01", "title": "Taper"},
            {"id": 4, "itemType": "event", "date": "2026-04-02", "title": "City 10K"},
        ],
    }


async def test_get_training_calendar_defaults_to_next_two_weeks(client, fake):
    fake.responses["get_scheduled_workouts"] = {
        "calendarItems": [
            {"id": 7, "itemType": "activity", "date": "2026-03-15", "title": "Morning Run"},
            {"id": 8, "itemType": "workout", "date": "2026-03-16", "title": "Easy"},
        ]
    }
    out = await call(client, "get_training_calendar", include_completed=True)
    assert fake.calls_to("get_scheduled_workouts") == [((2026, 3), {})]
    assert (out["start_date"], out["end_date"]) == ("2026-03-15", "2026-03-28")
    assert [item["id"] for item in out["items"]] == [7, 8]


async def test_forward_range_errors(client, fake):
    error = await call_error(client, "get_training_calendar", end_date="2026-03-01")
    assert "after end date" in error
    error = await call_error(
        client, "get_scheduled_workouts", start_date="2026-01-01", end_date="2026-12-31"
    )
    assert "at most 92 days" in error
    assert fake.calls_to("get_scheduled_workouts") == []
    assert fake.calls_to("get_scheduled_workouts_by_period") == []


async def test_get_scheduled_workouts(client, fake):
    fake.responses["get_scheduled_workouts_by_period"] = [
        {"workoutScheduleId": 2, "calendarDate": "2026-03-20", "workout": WORKOUT},
        {
            "workoutScheduleId": 1,
            "calendarDate": "2026-03-17",
            "workout": {"workoutId": 7, "workoutName": "Easy", "sportType": RUNNING},
        },
    ]
    out = await call(client, "get_scheduled_workouts", start_date="2026-03-16")
    assert fake.calls_to("get_scheduled_workouts_by_period") == [(("2026-03-16", "2026-03-29"), {})]
    assert out["count"] == 2
    assert out["scheduled"][0] == {
        "workoutScheduleId": 1,
        "calendarDate": "2026-03-17",
        "workout": {"workoutId": 7, "workoutName": "Easy", "sportType": "running"},
    }
    later = out["scheduled"][1]["workout"]
    assert later["workoutName"] == "Tempo 20"
    assert "workoutSegments" not in later and "author" not in later


async def test_get_next_scheduled_workout(client, fake):
    fake.responses["get_next_scheduled_workout"] = {
        "id": 2,
        "itemType": "workout",
        "date": "2026-03-16",
        "title": "Tempo 20",
        "workoutId": 42,
        "isRace": False,
    }
    fake.responses["get_workout_by_id"] = WORKOUT
    out = await call(client, "get_next_scheduled_workout")
    assert fake.calls_to("get_workout_by_id") == [((42,), {})]
    assert out["scheduled"] == {
        "id": 2,
        "itemType": "workout",
        "date": "2026-03-16",
        "title": "Tempo 20",
        "workoutId": 42,
    }
    assert out["workout"]["workoutSegments"][0]["workoutSteps"][0]["stepType"] == "warmup"


async def test_get_next_scheduled_workout_without_steps_or_result(client, fake):
    fake.responses["get_next_scheduled_workout"] = {"date": "2026-03-16", "workoutId": 42}
    out = await call(client, "get_next_scheduled_workout", include_steps=False)
    assert out == {"scheduled": {"date": "2026-03-16", "workoutId": 42}}
    assert fake.calls_to("get_workout_by_id") == []

    fake.responses["get_next_scheduled_workout"] = {}
    out = await call(client, "get_next_scheduled_workout")
    assert "No workout is scheduled" in out["message"]
    assert fake.calls_to("get_workout_by_id") == []


# -- events --------------------------------------------------------------------


async def test_get_upcoming_events(client, fake):
    race = {"id": 11, "eventName": "City 10K", "date": "2026-04-02", "note": None}
    fake.responses["get_upcoming_events"] = [race]
    fake.responses["get_primary_event"] = race
    out = await call(client, "get_upcoming_events")
    assert fake.calls_to("get_upcoming_events") == [((365, 10), {})]
    assert out == {
        "count": 1,
        "events": [{"id": 11, "eventName": "City 10K", "date": "2026-04-02"}],
        "primary_event_id": 11,
    }


async def test_get_upcoming_events_primary_outside_window(client, fake):
    fake.responses["get_upcoming_events"] = []
    fake.responses["get_primary_event"] = {"id": 12, "eventName": "Marathon"}
    out = await call(client, "get_upcoming_events", days=30, limit=3)
    assert fake.calls_to("get_upcoming_events") == [((30, 3), {})]
    assert out == {"count": 0, "primary_event": {"id": 12, "eventName": "Marathon"}}

    fake.responses["get_primary_event"] = None
    assert await call(client, "get_upcoming_events") == {"count": 0}


# -- training plans ------------------------------------------------------------

PLANS = {
    "trainingPlanList": [
        {
            "trainingPlanId": 100,
            "name": "5K Improver",
            "trainingPlanCategory": "PHASED",
            "trainingType": {"typeId": 3, "typeKey": "Running"},
            "startDate": "2026-02-01",
        },
        {"trainingPlanId": 200, "name": "Coach", "trainingPlanCategory": "FBT_ADAPTIVE"},
    ]
}


async def test_list_training_plans(client, fake):
    fake.responses["get_training_plans"] = PLANS
    out = await call(client, "list_training_plans")
    assert fake.calls_to("get_training_plans") == [((50,), {})]
    assert out[0] == {
        "trainingPlanId": 100,
        "name": "5K Improver",
        "trainingPlanCategory": "PHASED",
        "trainingType": "Running",
        "startDate": "2026-02-01",
    }
    assert out[1]["trainingPlanCategory"] == "FBT_ADAPTIVE"


async def test_get_training_plan_regular_trims_tasks(client, fake):
    fake.responses["get_training_plans"] = PLANS
    fake.responses["get_training_plan_by_id"] = {
        "trainingPlanId": 100,
        "name": "5K Improver",
        "taskList": [
            {"calendarDate": "2026-03-18", "taskWorkout": {"workoutId": 3}},
            {"calendarDate": "2026-03-10", "taskWorkout": {"workoutId": 1}},
            {
                "calendarDate": "2026-03-15",
                "taskWorkout": {"workoutId": 2, "workoutSegments": [{"segmentOrder": 1}]},
            },
            {"calendarDate": "2026-03-21", "taskWorkout": {"workoutId": 4}},
        ],
    }
    out = await call(client, "get_training_plan", plan_id=100, max_tasks=2)
    assert fake.calls_to("get_training_plan_by_id") == [((100,), {})]
    assert fake.calls_to("get_adaptive_training_plan_by_id") == []
    assert out["taskCount"] == 4
    assert out["upcomingTaskCount"] == 3
    assert out["taskList"] == [
        {"calendarDate": "2026-03-15", "taskWorkout": {"workoutId": 2}},
        {"calendarDate": "2026-03-18", "taskWorkout": {"workoutId": 3}},
    ]


async def test_get_training_plan_adaptive(client, fake):
    fake.responses["get_training_plans"] = PLANS
    fake.responses["get_adaptive_training_plan_by_id"] = {"trainingPlanId": 200, "name": "Coach"}
    out = await call(client, "get_training_plan", plan_id=200)
    assert fake.calls_to("get_adaptive_training_plan_by_id") == [((200,), {})]
    assert fake.calls_to("get_training_plan_by_id") == []
    assert out == {"trainingPlanId": 200, "name": "Coach"}


async def test_get_training_plan_explicit_type_skips_lookup(client, fake):
    fake.responses["get_adaptive_training_plan_by_id"] = {"trainingPlanId": 100}
    await call(client, "get_training_plan", plan_id=100, plan_type="adaptive")
    assert fake.calls_to("get_training_plans") == []
    assert fake.calls_to("get_adaptive_training_plan_by_id") == [((100,), {})]


# -- courses -------------------------------------------------------------------

POINTS = [{"latitude": 40.0 + i / 1000, "longitude": -3.7, "elevation": 650.0} for i in range(60)]


async def test_list_courses(client, fake):
    course = {"courseId": 5, "courseName": "River loop", "distanceInMeters": 10200.0}
    fake.responses["get_courses"] = {"coursesForUser": [{**course, "geoPoints": POINTS}]}
    assert await call(client, "list_courses") == [{**course, "geoPointsCount": 60}]

    fake.responses["get_courses"] = [course]
    assert await call(client, "list_courses") == [course]


async def test_get_course_drops_geometry_by_default(client, fake):
    fake.responses["get_course"] = {
        "courseId": 5,
        "courseName": "River loop",
        "activityType": {"typeId": 1, "typeKey": "running"},
        "geoPoints": POINTS,
        "courseLines": [{"courseLineId": 1, "points": POINTS}],
    }
    out = await call(client, "get_course", course_id=5)
    assert fake.calls_to("get_course") == [((5,), {})]
    assert out == {
        "courseId": 5,
        "courseName": "River loop",
        "activityType": "running",
        "geoPointsCount": 60,
        "courseLines": [{"courseLineId": 1, "pointsCount": 60}],
    }

    out = await call(client, "get_course", course_id=5, include_points=True)
    assert len(out["geoPoints"]) == 60
