"""Activity tools: listing, summaries, splits, zones, time series, strength sets."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from garminconnect import parse_activity_detail_metrics
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..session import GarminSession
from ._common import READ, EndDateArg, StartDateArg, dump, omit, pick

ActivityId = Annotated[
    int | str,
    Field(description="Numeric activity id (the activityId returned by list_activities)."),
]

# Fields of an activity-list entry worth showing in a compact summary. Sport-
# specific ones (cadence, power, sets, swolf) are simply absent for other sports.
_SUMMARY_KEYS = (
    "startTimeLocal",
    "locationName",
    "distance",
    "duration",
    "movingDuration",
    "elevationGain",
    "elevationLoss",
    "averageSpeed",
    "maxSpeed",
    "averageHR",
    "maxHR",
    "calories",
    "averageRunningCadenceInStepsPerMinute",
    "averageBikingCadenceInRevPerMinute",
    "avgPower",
    "normPower",
    "maxPower",
    "aerobicTrainingEffect",
    "anaerobicTrainingEffect",
    "trainingEffectLabel",
    "activityTrainingLoad",
    "vO2MaxValue",
    "steps",
    "lapCount",
    "totalSets",
    "totalReps",
    "totalVolume",
    "averageSwolf",
    "description",
)

# Top-level keys of the single-activity payload that are replaced by flattened
# equivalents or carry no information for the reader.
_ACTIVITY_DROP = (
    "activityTypeDTO",
    "eventTypeDTO",
    "timeZoneUnitDTO",
    "metadataDTO",
    "accessControlRuleDTO",
    "userProfileId",
    "activityUUID",
)

# The parts of metadataDTO that say what else exists for the activity.
_METADATA_KEYS = (
    "lapCount",
    "hasSplits",
    "hasChartData",
    "hasPolyline",
    "hasHrTimeInZones",
    "hasPowerTimeInZones",
    "favorite",
    "personalRecord",
    "manualActivity",
    "associatedWorkoutId",
    "associatedCourseId",
    "childIds",
    "childActivityTypes",
)

# Per-lap fields dropped from splits: coordinates and nested bulk (per-length
# swim records, Connect IQ data fields).
_LAP_DROP = (
    "startLatitude",
    "startLongitude",
    "endLatitude",
    "endLongitude",
    "lengthDTOs",
    "connectIQMeasurement",
    "connectIQMeasurements",
)

_GEAR_KEYS = (
    "uuid",
    "displayName",
    "customMakeModel",
    "gearMakeName",
    "gearModelName",
    "gearTypeName",
    "gearStatusName",
    "dateBegin",
    "dateEnd",
    "maximumMeters",
)

# Time-series channels returned by get_activity_details when none are requested.
_DEFAULT_SERIES = (
    "sumDuration",
    "sumDistance",
    "directHeartRate",
    "directSpeed",
    "directElevation",
    "directRunCadence",
    "directBikeCadence",
    "directPower",
)
_TIME_SERIES = ("sumDuration", "sumElapsedDuration", "sumMovingDuration", "directTimestamp")


def _type_key(dto: Any) -> Any:
    """``{"typeKey": "running", ...}`` -> ``"running"`` (anything else unchanged)."""
    return dto.get("typeKey") if isinstance(dto, Mapping) else dto


def _round(value: Any, key: str = "") -> Any:
    """Trim float noise (``2.7990000247955322`` -> ``2.799``), recursively.

    Coordinates keep six decimals (about 0.1 m); everything else three.
    """
    if isinstance(value, float):
        return round(value, 6 if "atitude" in key or "ongitude" in key else 3)
    if isinstance(value, Mapping):
        return {k: _round(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_round(v, key) for v in value]
    return value


def _summary(activity: Any) -> Any:
    if not isinstance(activity, Mapping):
        return activity
    return {
        "activityId": activity.get("activityId"),
        "activityName": activity.get("activityName"),
        "activityType": _type_key(activity.get("activityType")),
        **pick(activity, _SUMMARY_KEYS),
    }


def _rows(payload: Any, list_key: str) -> Any:
    """Trim the per-lap dicts under ``payload[list_key]``; pass other shapes through."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get(list_key), list):
        return payload
    return [omit(row, _LAP_DROP) if isinstance(row, Mapping) else row for row in payload[list_key]]


def _exercise_set(entry: Any) -> Any:
    """Replace a set's list of exercise guesses with the most probable one."""
    if not isinstance(entry, Mapping):
        return entry
    guesses = [g for g in entry.get("exercises") or [] if isinstance(g, Mapping)]
    best = max(guesses, key=lambda g: g.get("probability") or 0, default={})
    return {
        "category": best.get("category"),
        "exercise": best.get("name"),
        **omit(entry, ("exercises", "messageIndex")),
    }


def register(mcp: MCPServer, garmin: GarminSession) -> None:
    @mcp.tool(**READ)
    async def list_activities(
        start_date: Annotated[
            str | None,
            Field(
                description="Only activities on or after this date: YYYY-MM-DD, 'today', "
                "'yesterday', or '<N>d' for N days ago (e.g. '30d'). Omit for no lower bound."
            ),
        ] = None,
        end_date: Annotated[
            str | None,
            Field(description="Only activities on or before this date. Omit for no upper bound."),
        ] = None,
        activity_type: Annotated[
            str | None,
            Field(
                description="Top-level activity type to filter by: running, cycling, swimming, "
                "hiking, walking, fitness_equipment (strength, cardio, indoor machines), "
                "multi_sport, other. See get_activity_types."
            ),
        ] = None,
        activity_subtype: Annotated[
            str | None,
            Field(
                description="Narrower type within activity_type, e.g. trail_running, "
                "strength_training, lap_swimming. Requires activity_type."
            ),
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=100, description="Maximum number of activities to return.")
        ] = 20,
    ) -> str:
        """List recorded activities (workouts), newest first, as compact summaries:
        id, name, type, local start time, distance (m), duration (s), elevation
        gain (m), speed (m/s), heart rate, calories, training effect and load.
        With no dates it returns the most recent activities. Start here to find
        the activityId that the other activity tools need; `more_available` is
        set when the limit cut the list short. For totals over a period use
        get_progress_summary instead of adding these up."""
        if activity_subtype and not activity_type:
            raise ToolError("activity_subtype requires activity_type (its parent type).")
        filters: dict[str, Any] = {}
        if start_date:
            filters["startDate"] = await garmin.date(start_date)
        if end_date:
            filters["endDate"] = await garmin.date(end_date)
        if start_date and end_date and filters["startDate"] > filters["endDate"]:
            raise ToolError(
                f"start date {filters['startDate']} is after end date {filters['endDate']}"
            )
        if activity_type:
            filters["activityType"] = activity_type.strip().lower()
        if activity_subtype:
            filters["activitySubType"] = activity_subtype.strip().lower()

        found = await garmin.call("search_activities", 0, limit, **filters)
        if isinstance(found, Mapping):
            found = found.get("activityList")
        activities = [_summary(a) for a in found or []][:limit]
        return dump(
            {
                "count": len(activities),
                "more_available": len(activities) >= limit or None,
                "activities": _round(activities),
            }
        )

    @mcp.tool(**READ)
    async def get_activity(activity_id: ActivityId) -> str:
        """Full summary of one activity: every total and average Garmin computed
        (`summaryDTO`: distance in m, durations in s, speeds in m/s, heart rate,
        power, cadence, running dynamics, training effect, temperatures), the
        per-segment `splitSummaries`, and `metadata` flags saying whether
        splits, charts, or zone data exist. Use after list_activities when the
        compact summary is not enough."""
        activity = await garmin.call("get_activity", activity_id)
        if not isinstance(activity, Mapping):
            return dump(activity)
        timezone = activity.get("timeZoneUnitDTO")
        return dump(
            _round(
                {
                    "activityId": activity.get("activityId"),
                    "activityName": activity.get("activityName"),
                    "activityType": _type_key(activity.get("activityTypeDTO")),
                    "eventType": _type_key(activity.get("eventTypeDTO")),
                    "timeZone": timezone.get("unitKey") if isinstance(timezone, Mapping) else None,
                    **omit(activity, _ACTIVITY_DROP),
                    "metadata": pick(activity.get("metadataDTO"), _METADATA_KEYS),
                }
            )
        )

    @mcp.tool(**READ)
    async def get_activity_splits(
        activity_id: ActivityId,
        split_type: Annotated[
            Literal["laps", "typed"],
            Field(
                description="'laps': the recorded laps (auto-lap per km/mile or lap-button "
                "presses). 'typed': Garmin's typed segments -- workout intervals "
                "(active/recovery/rest), run/walk segments, climbing routes, ski runs."
            ),
        ] = "laps",
    ) -> str:
        """Per-lap breakdown of one activity: each lap's distance (m), duration
        (s), average/max speed (m/s), heart rate, cadence, power and elevation
        change. Use for pacing analysis, negative splits or interval sessions;
        choose split_type='typed' for structured workouts and climbing."""
        if split_type == "typed":
            payload = await garmin.call("get_activity_typed_splits", activity_id)
            return dump({"activityId": activity_id, "splits": _round(_rows(payload, "splits"))})
        payload = await garmin.call("get_activity_splits", activity_id)
        return dump({"activityId": activity_id, "laps": _round(_rows(payload, "lapDTOs"))})

    @mcp.tool(**READ)
    async def get_activity_zones(
        activity_id: ActivityId,
        zone_type: Annotated[
            Literal["heart_rate", "power"],
            Field(description="Which zones: heart-rate zones, or power zones (cycling/running)."),
        ] = "heart_rate",
    ) -> str:
        """Time spent in each heart-rate (or power) zone during one activity:
        per zone its number, seconds in the zone and the zone's lower boundary
        (bpm or watts). Use to judge how hard or how polarised a session was."""
        method = (
            "get_activity_power_in_timezones"
            if zone_type == "power"
            else "get_activity_hr_in_timezones"
        )
        zones = await garmin.call(method, activity_id)
        return dump({"activityId": activity_id, "zone_type": zone_type, "zones": _round(zones)})

    @mcp.tool(**READ)
    async def get_activity_weather(activity_id: ActivityId) -> str:
        """Weather Garmin recorded for one activity from the nearest station:
        temperature, feels-like, dew point, humidity, wind speed/direction and
        a condition description. Values are passed through unconverted, and
        Garmin typically reports these temperatures in Fahrenheit even for
        metric accounts -- sanity-check before quoting. Empty for indoor
        activities."""
        return dump(_round(await garmin.call("get_activity_weather", activity_id)))

    @mcp.tool(**READ)
    async def get_activity_details(
        activity_id: ActivityId,
        max_points: Annotated[
            int,
            Field(
                ge=1,
                le=2000,
                description="Maximum number of samples; Garmin downsamples the recording evenly "
                "to fit. Keep it small (50-200) unless fine detail is needed.",
            ),
        ] = 100,
        metrics: Annotated[
            list[str] | None,
            Field(
                description="Channels to return, by Garmin key, e.g. ['sumDuration', "
                "'directHeartRate', 'directGradeAdjustedSpeed']. Omit for the default set "
                "(elapsed time, distance, heart rate, speed, elevation, cadence, power); the "
                "result's `other_metrics` lists what else this activity recorded."
            ),
        ] = None,
    ) -> str:
        """Time series of one activity, downsampled: a table with `columns`
        (channel names), `units` per column and `rows` of samples in time
        order -- heart rate, speed, elevation, cadence, power against elapsed
        seconds and distance. Use for questions about how a metric evolved
        during the activity (heart-rate drift, pacing, climbs). For per-lap
        numbers prefer get_activity_splits, which is far smaller."""
        details = await garmin.call("get_activity_details", activity_id, max_points, 0)
        if not isinstance(details, Mapping):
            return dump(details)

        units: dict[str, Any] = {}
        for descriptor in details.get("metricDescriptors") or []:
            if isinstance(descriptor, Mapping) and isinstance(descriptor.get("key"), str):
                unit = descriptor.get("unit")
                units[descriptor["key"]] = unit.get("key") if isinstance(unit, Mapping) else unit

        if metrics:
            columns = [key for key in dict.fromkeys(metrics) if key in units]
            if units and not columns:
                raise ToolError(
                    "None of the requested metrics were recorded for this activity. "
                    f"Available: {', '.join(units)}"
                )
        else:
            columns = [key for key in _DEFAULT_SERIES if key in units]
            if not any(key in columns for key in _TIME_SERIES):
                columns = [key for key in _TIME_SERIES if key in units][:1] + columns
            if len(columns) < 2:  # unfamiliar recording: show everything it has
                columns = list(units)

        samples = parse_activity_detail_metrics(dict(details))
        if len(samples) > max_points:
            samples = samples[:: -(-len(samples) // max_points)]
        rows = [[_round(sample.get(key), key) for key in columns] for sample in samples]

        return dump(
            {
                "activityId": details.get("activityId", activity_id),
                "total_samples": details.get("totalMetricsCount"),
                "returned_samples": len(rows),
                "columns": columns,
                "units": {key: units[key] for key in columns},
                "rows": rows,
                "other_metrics": [key for key in units if key not in columns],
            }
        )

    @mcp.tool(**READ)
    async def get_activity_exercise_sets(activity_id: ActivityId) -> str:
        """Sets of a strength-training activity in order: exercise category and
        name, repetitions, weight, duration (s) and whether the set was ACTIVE
        or REST. Weight appears to be in grams (20000 = 20 kg). Exercise names
        are the watch's best guess unless the user edited them. Empty for
        activities without sets."""
        payload = await garmin.call("get_activity_exercise_sets", activity_id)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("exerciseSets"), list):
            return dump(payload)
        sets = [_exercise_set(entry) for entry in payload["exerciseSets"]]
        return dump({"activityId": payload.get("activityId", activity_id), "sets": _round(sets)})

    @mcp.tool(**READ)
    async def get_activity_gear(activity_id: ActivityId) -> str:
        """Gear (shoes, bike, ...) linked to one activity: name, make/model,
        type, status and the gear uuid. Empty when no gear was assigned."""
        gear = await garmin.call("get_activity_gear", activity_id)
        if not isinstance(gear, list):
            return dump(gear)
        return dump([pick(g, _GEAR_KEYS) or g for g in gear])

    @mcp.tool(**READ)
    async def get_activity_types() -> str:
        """Garmin's activity type keys as a tree: each parent type mapped to its
        child types. The children of the root type (normally `all`) are the
        `activity_type` filters for list_activities, and their own children
        the `activity_subtype` values. Only needed when the right key is
        unclear."""
        types = await garmin.call("get_activity_types")
        if not isinstance(types, list) or not all(isinstance(t, Mapping) for t in types):
            return dump(types)
        keys = {t.get("typeId"): t.get("typeKey") for t in types}
        tree: dict[str, list[str]] = {}
        for t in types:
            key, parent = t.get("typeKey"), keys.get(t.get("parentTypeId"))
            if not key or t.get("isHidden"):
                continue
            if parent and parent != key:
                tree.setdefault(parent, []).append(key)
            else:
                tree.setdefault(key, [])
        return dump(tree)

    @mcp.tool(**READ)
    async def get_progress_summary(
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        metric: Annotated[
            Literal["distance", "duration", "movingDuration", "elevationGain"],
            Field(description="Which quantity to total."),
        ] = "distance",
    ) -> str:
        """Totals for a date range (default: the last 30 days) grouped by
        activity type: per type the number of activities and the sum, average,
        min and max of the chosen metric. Use for questions like "how far did I
        run this month" instead of adding up list_activities. Units are Garmin's
        raw statistics units, which differ from activity summaries: distance
        appears to be in centimetres and durations in milliseconds -- check
        the magnitude against a known activity before quoting figures."""
        start, end = await garmin.date_range(start_date, end_date, default_days=30)
        summary = await garmin.call("get_progress_summary_between_dates", start, end, metric, True)
        return dump(
            {"start_date": start, "end_date": end, "metric": metric, "summary": _round(summary)}
        )
