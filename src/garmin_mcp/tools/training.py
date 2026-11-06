"""Planned-training tools: workout library, calendar, scheduled workouts,
race/goal events, training plans and courses.

Everything here looks forward (what is planned); recorded activities live in
``activities.py``. Payload shapes for these endpoints were taken from the
``garminconnect`` source rather than live responses, so the helpers pass
unknown shapes through instead of assuming keys exist.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import timedelta
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..session import GarminSession
from ._common import READ, dump, omit, pick

_FUTURE_DATE_HELP = "YYYY-MM-DD, 'today', 'tomorrow', or '+<N>d' for N days ahead (e.g. '+14d')"

FromDateArg = Annotated[
    str | None,
    Field(description=f"First day (inclusive): {_FUTURE_DATE_HELP}. Defaults to today."),
]
ToDateArg = Annotated[
    str | None,
    Field(
        description=f"Last day (inclusive): {_FUTURE_DATE_HELP}. "
        "Defaults to 13 days after the first day (a two-week window)."
    ),
]
WorkoutId = Annotated[
    int | str,
    Field(
        description="Numeric workout id (the workoutId from list_workouts, "
        "get_training_calendar or get_scheduled_workouts)."
    ),
]

_DEFAULT_WINDOW_DAYS = 14
_MAX_WINDOW_DAYS = 92

# Keys that only describe how the web app renders a type descriptor.
_DESCRIPTOR_NOISE = frozenset({"displayOrder", "displayable"})

# Top-level workout keys that carry no information for the reader.
_WORKOUT_DROP = (
    "author",
    "ownerId",
    "sharedWithUsers",
    "workoutNameI18nKey",
    "descriptionI18nKey",
    "workoutThumbnailUrl",
)

# Keys holding a workout's full step tree; dropped wherever a workout is only
# embedded for reference (schedules, plan tasks) -- get_workout returns them.
_STEP_TREE_KEYS = frozenset({"workoutSegments", "author", "sharedWithUsers"})

_WORKOUT_SUMMARY_KEYS = (
    "estimatedDurationInSecs",
    "estimatedDistanceInMeters",
    "createdDate",
    "updateDate",
    "workoutProvider",
    "trainingPlanId",
)

# Calendar item types that are not planned training (opt-in via include_completed).
_COMPLETED_ITEM_TYPES = frozenset({"activity"})

_PLAN_LIST_KEY = "trainingPlanList"
_ADAPTIVE_CATEGORY = "FBT_ADAPTIVE"

# Lists longer than this inside a course are treated as track geometry.
_GEOMETRY_MIN_POINTS = 25


def _simplify(value: Any) -> Any:
    """Collapse Garmin's type descriptors to their key.

    ``{"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}`` becomes
    ``"running"`` and an unset descriptor (``{"strokeTypeId": 0}``) becomes
    ``None``. Dicts carrying anything else (``factor``, values, names) are kept
    and simplified recursively.
    """
    if isinstance(value, list):
        return [_simplify(v) for v in value]
    if not isinstance(value, dict):
        return value
    present = {k: v for k, v in value.items() if v is not None}
    keys = [k for k, v in present.items() if k.endswith("Key") and isinstance(v, str)]
    rest = [k for k in present if k not in keys]
    if all(k.endswith("Id") or k in _DESCRIPTOR_NOISE for k in rest):
        if len(keys) == 1:
            return present[keys[0]]
        if not keys and not any(present[k] for k in rest):
            return None
    return {k: _simplify(v) for k, v in present.items()}


def _strip(value: Any, keys: frozenset[str]) -> Any:
    """Recursively remove ``keys`` from nested dicts."""
    if isinstance(value, list):
        return [_strip(v, keys) for v in value]
    if isinstance(value, dict):
        return {k: _strip(v, keys) for k, v in value.items() if k not in keys}
    return value


def _workout_summary(workout: Any) -> Any:
    if not isinstance(workout, dict):
        return workout
    description = workout.get("description")
    if isinstance(description, str) and len(description) > 200:
        description = description[:200] + "..."
    summary = {
        "workoutId": workout.get("workoutId"),
        "workoutName": workout.get("workoutName"),
        "sport": _simplify(workout.get("sportType")),
        "description": description,
        **pick(workout, _WORKOUT_SUMMARY_KEYS),
    }
    return summary if summary["workoutId"] is not None else _simplify(workout)


def _workout_detail(workout: Any) -> Any:
    if not isinstance(workout, dict):
        return workout
    return _simplify(omit(workout, _WORKOUT_DROP))


def _calendar_item(item: dict[str, Any]) -> dict[str, Any]:
    """Drop the ``false`` flags that make up most of a calendar item."""
    return {k: v for k, v in item.items() if v is not False}


def _months(start: str, end: str) -> list[tuple[int, int]]:
    """``(year, month)`` pairs covering ``start``..``end`` (ISO dates)."""
    first, last = _date.fromisoformat(start), _date.fromisoformat(end)
    months, (year, month) = [], (first.year, first.month)
    while (year, month) <= (last.year, last.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _entry_date(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("calendarDate") or entry.get("date") or "")


def _plan_list(payload: Any) -> Any:
    """The list inside ``{"trainingPlanList": [...]}``; other shapes pass through."""
    if isinstance(payload, dict) and isinstance(payload.get(_PLAN_LIST_KEY), list):
        return payload[_PLAN_LIST_KEY]
    return payload


def _trim_tasks(plan: Any, today: str, max_tasks: int) -> Any:
    """Limit a plan's ``taskList`` to the next ``max_tasks`` sessions from today.

    Tasks without a recognisable date are kept in Garmin's order. Adds
    ``taskCount`` (all tasks) so the reader knows the list was shortened.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("taskList"), list):
        return plan
    tasks = plan["taskList"]
    dated = [t for t in tasks if _entry_date(t)]
    if dated and len(dated) == len(tasks):
        upcoming = sorted((t for t in tasks if _entry_date(t)[:10] >= today), key=_entry_date)
    else:
        upcoming = tasks
    return {
        **plan,
        "taskCount": len(tasks),
        "upcomingTaskCount": len(upcoming),
        "taskList": upcoming[:max_tasks],
    }


def _drop_geometry(value: Any) -> Any:
    """Replace long lists (track points, course lines) with their length."""
    if isinstance(value, list):
        return [_drop_geometry(v) for v in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, list) and len(item) > _GEOMETRY_MIN_POINTS:
            out[f"{key}Count"] = len(item)
        else:
            out[key] = _drop_geometry(item)
    return out


def _course_list(payload: Any) -> Any:
    """The course list, whether Garmin returns it bare or wrapped in a dict."""
    if isinstance(payload, dict):
        wrapped = payload.get("coursesForUser")
        if not isinstance(wrapped, list):
            lists = [v for v in payload.values() if isinstance(v, list)]
            wrapped = lists[0] if len(lists) == 1 else None
        if wrapped is None:
            return payload
        payload = wrapped
    return payload


def register(mcp: MCPServer, garmin: GarminSession) -> None:
    async def forward_range(start: str | None, end: str | None) -> tuple[str, str]:
        """Resolve a window that defaults to the next two weeks, not the last ones."""
        start_s = await garmin.date(start)
        if end:
            end_s = await garmin.date(end)
        else:
            first = _date.fromisoformat(start_s)
            end_s = (first + timedelta(days=_DEFAULT_WINDOW_DAYS - 1)).isoformat()
        if start_s > end_s:
            raise ToolError(
                f"start date {start_s} is after end date {end_s}; pass both dates for a past range"
            )
        if (_date.fromisoformat(end_s) - _date.fromisoformat(start_s)).days >= _MAX_WINDOW_DAYS:
            raise ToolError(f"date range too long: ask for at most {_MAX_WINDOW_DAYS} days at once")
        return start_s, end_s

    # -- workout library ------------------------------------------------------

    @mcp.tool(**READ)
    async def list_workouts(
        limit: Annotated[
            int, Field(ge=1, le=100, description="Maximum number of workouts to return.")
        ] = 20,
        offset: Annotated[
            int, Field(ge=0, description="Number of workouts to skip (for paging).")
        ] = 0,
        sport: Annotated[
            str | None,
            Field(
                description="Only workouts of this sport: running, cycling, swimming, "
                "strength_training, cardio_training, yoga, pilates, hiit, mobility, "
                "multi_sport or other."
            ),
        ] = None,
    ) -> str:
        """List the saved workouts in the user's workout library (structured
        workout templates, not recorded activities) as compact summaries: id,
        name, sport, description, estimated duration (s) and distance (m),
        created/updated dates. Use get_workout with a workoutId for the steps;
        `more_available` is set when the limit cut the list short."""
        if sport:
            found = await garmin.call(
                "get_workouts", offset, limit, sport_type_keys=[sport.strip().lower()]
            )
        else:
            found = await garmin.call("get_workouts", offset, limit)
        if not isinstance(found, list):
            return dump(found)
        workouts = [_workout_summary(w) for w in found][:limit]
        return dump(
            {
                "count": len(workouts),
                "more_available": len(workouts) >= limit or None,
                "workouts": workouts,
            }
        )

    @mcp.tool(**READ)
    async def get_workout(workout_id: WorkoutId) -> str:
        """Full structure of one saved workout: its segments and ordered steps
        (warmup / interval / recovery / repeat groups with iteration counts),
        each step's end condition and value (seconds for `time`, metres for
        `distance`, count for `reps`/`iterations`) and intensity target
        (`targetType` with `targetValueOne`/`targetValueTwo` bounds or a
        `zoneNumber`; pace targets are in m/s). Use it to explain or review a
        planned session."""
        return dump(_workout_detail(await garmin.call("get_workout_by_id", workout_id)))

    # -- calendar and schedule ------------------------------------------------

    @mcp.tool(**READ)
    async def get_training_calendar(
        start_date: FromDateArg = None,
        end_date: ToDateArg = None,
        include_completed: Annotated[
            bool,
            Field(
                description="Also include completed activities recorded in the range "
                "(off by default; list_activities gives better activity summaries)."
            ),
        ] = False,
    ) -> str:
        """What is on the Garmin Connect calendar between two dates (default:
        the next two weeks): planned workouts, training-plan sessions, races
        and other events, and notes, one item each with `itemType`, `date`,
        `title` and, for workouts, a `workoutId` usable with get_workout. This
        is the overview of upcoming training. Sessions of an adaptive Garmin
        Coach plan may be missing here -- get_scheduled_workouts includes
        them. Ranges up to 92 days."""
        start, end = await forward_range(start_date, end_date)
        seen: set[tuple[Any, Any, Any]] = set()
        items: list[dict[str, Any]] = []
        for year, month in _months(start, end):
            payload = await garmin.call("get_scheduled_workouts", year, month)
            month_items = payload.get("calendarItems") if isinstance(payload, dict) else None
            for item in month_items or []:
                if not isinstance(item, dict):
                    continue
                day = str(item.get("date") or "")[:10]
                if not start <= day <= end:
                    continue
                if not include_completed and item.get("itemType") in _COMPLETED_ITEM_TYPES:
                    continue
                # Month views overlap by a few days, so the same item can come back twice.
                identity = (item.get("itemType"), item.get("id"), day)
                if item.get("id") is not None and identity in seen:
                    continue
                seen.add(identity)
                items.append(_calendar_item(item))
        items.sort(key=lambda item: str(item.get("date") or ""))
        return dump({"start_date": start, "end_date": end, "count": len(items), "items": items})

    @mcp.tool(**READ)
    async def get_scheduled_workouts(
        start_date: FromDateArg = None, end_date: ToDateArg = None
    ) -> str:
        """Workouts scheduled between two dates (default: the next two weeks),
        including sessions generated by an adaptive Garmin Coach / training
        plan that the plain calendar can miss. Each entry has its date and the
        workout's name, sport, estimated duration (s) and distance (m), and
        workoutId (steps via get_workout). Use get_training_calendar instead
        when races, events and notes matter too. Ranges up to 92 days."""
        start, end = await forward_range(start_date, end_date)
        found = await garmin.call("get_scheduled_workouts_by_period", start, end)
        if not isinstance(found, list):
            return dump(_simplify(_strip(found, _STEP_TREE_KEYS)))
        entries = sorted((_simplify(_strip(e, _STEP_TREE_KEYS)) for e in found), key=_entry_date)
        return dump(
            {"start_date": start, "end_date": end, "count": len(entries), "scheduled": entries}
        )

    @mcp.tool(**READ)
    async def get_next_scheduled_workout(
        include_steps: Annotated[
            bool,
            Field(description="Also fetch the workout's full step structure (as get_workout)."),
        ] = True,
    ) -> str:
        """The next workout on the calendar (today or later, within this and
        next month): its calendar entry (`scheduled`: date, title, sport) and,
        by default, the full workout structure (`workout`: steps, durations,
        targets). Answers "what is my next planned session?" in one call. If
        nothing is found, adaptive Garmin Coach sessions may still exist --
        check get_scheduled_workouts."""
        item = await garmin.call("get_next_scheduled_workout")
        if not item or not isinstance(item, dict):
            return dump(
                {
                    "scheduled": None,
                    "message": "No workout is scheduled on the calendar for this or next month. "
                    "Adaptive plan sessions may only show in get_scheduled_workouts.",
                }
            )
        result: dict[str, Any] = {"scheduled": _calendar_item(item)}
        workout_id = item.get("workoutId")
        if include_steps and workout_id is not None:
            result["workout"] = _workout_detail(await garmin.call("get_workout_by_id", workout_id))
        return dump(result)

    # -- events ---------------------------------------------------------------

    @mcp.tool(**READ)
    async def get_upcoming_events(
        days: Annotated[int, Field(ge=1, le=730, description="How many days ahead to look.")] = 365,
        limit: Annotated[
            int, Field(ge=1, le=50, description="Maximum number of events to return.")
        ] = 10,
    ) -> str:
        """Upcoming races and goal events on the user's calendar (name, date,
        event type, location, distance / time goal), soonest first, plus the
        primary ("A race") event Garmin bases training guidance on:
        `primary_event_id` when it is among the listed events, otherwise the
        whole `primary_event`. Use it for "when is my next race?" and to put
        training in context."""
        events = await garmin.call("get_upcoming_events", days, limit)
        primary = await garmin.call("get_primary_event")
        result: dict[str, Any] = {
            "count": len(events) if isinstance(events, list) else None,
            "events": events,
        }
        if isinstance(primary, dict) and primary:
            primary_id = primary.get("id")
            listed = primary_id is not None and any(
                isinstance(e, dict) and e.get("id") == primary_id
                for e in (events if isinstance(events, list) else [])
            )
            if listed:
                result["primary_event_id"] = primary_id
            else:
                result["primary_event"] = primary
        return dump(result)

    # -- training plans -------------------------------------------------------

    @mcp.tool(**READ)
    async def list_training_plans() -> str:
        """The user's Garmin training plans (Garmin Coach and other plans they
        have joined), each with its trainingPlanId, name, category
        (`FBT_ADAPTIVE` marks an adaptive Garmin Coach plan), sport/type,
        level, status and start/end dates. Check the status and dates to tell
        the current plan from finished ones; pass a trainingPlanId to
        get_training_plan for the schedule."""
        plans = _plan_list(await garmin.call("get_training_plans", 50))
        return dump(_simplify(_strip(plans, _STEP_TREE_KEYS)))

    @mcp.tool(**READ)
    async def get_training_plan(
        plan_id: Annotated[
            int | str, Field(description="Numeric trainingPlanId from list_training_plans.")
        ],
        plan_type: Annotated[
            Literal["auto", "regular", "adaptive"],
            Field(
                description="Which Garmin endpoint to read. 'auto' (default) looks the plan up "
                "in the user's plans and picks 'adaptive' for FBT_ADAPTIVE Garmin Coach plans, "
                "'regular' (phased) otherwise; set it explicitly only if auto fails."
            ),
        ] = "auto",
        max_tasks: Annotated[
            int,
            Field(ge=1, le=200, description="Maximum number of upcoming plan sessions to list."),
        ] = 30,
    ) -> str:
        """Details of one training plan: goal, phases, weekly structure and its
        sessions (`taskList`), limited to the next `max_tasks` sessions from
        today (`taskCount` is the plan's total). Sessions reference workouts by
        workoutId -- use get_workout for their steps. Use it to describe where
        the user is in a plan and what is coming."""
        kind = plan_type
        if kind == "auto":
            plans = _plan_list(await garmin.call("get_training_plans", 50))
            match = next(
                (
                    p
                    for p in (plans if isinstance(plans, list) else [])
                    if isinstance(p, dict) and str(p.get("trainingPlanId")) == str(plan_id).strip()
                ),
                {},
            )
            adaptive = str(match.get("trainingPlanCategory") or "").upper() == _ADAPTIVE_CATEGORY
            kind = "adaptive" if adaptive else "regular"
        method = (
            "get_adaptive_training_plan_by_id" if kind == "adaptive" else "get_training_plan_by_id"
        )
        plan = _strip(await garmin.call(method, plan_id), _STEP_TREE_KEYS)
        today = (await garmin.today()).isoformat()
        return dump(_simplify(_trim_tasks(plan, today, max_tasks)))

    # -- courses --------------------------------------------------------------

    @mcp.tool(**READ)
    async def list_courses() -> str:
        """The user's saved courses (routes to follow on a device): id, name,
        sport, distance (m), elevation gain/loss (m) and start location,
        without the track points. Use get_course with a courseId for one
        course's details."""
        courses = _course_list(await garmin.call("get_courses"))
        return dump(_simplify(_drop_geometry(courses)))

    @mcp.tool(**READ)
    async def get_course(
        course_id: Annotated[int | str, Field(description="Numeric courseId from list_courses.")],
        include_points: Annotated[
            bool,
            Field(
                description="Include the full track geometry (thousands of lat/lon/elevation "
                "points). Leave off unless the coordinates themselves are needed."
            ),
        ] = False,
    ) -> str:
        """One saved course: name, sport, distance (m), elevation gain/loss (m),
        start point and course points (turns, aid stations). The track geometry
        is replaced by point counts (`geoPointsCount`, ...) unless
        include_points is set."""
        course = await garmin.call("get_course", course_id)
        return dump(_simplify(course if include_points else _drop_geometry(course)))
