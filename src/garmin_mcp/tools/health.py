"""Daily health and wellness tools: day summaries, sleep, heart rate, stress,
Body Battery, HRV, respiration, SpO2, steps, intensity minutes, floors, hydration.

Garmin's wellness endpoints mix a handful of headline numbers with per-minute
arrays that run to thousands of samples. The tools here return the headline
numbers, drop every array by default, and offer the intraday curve only where
it answers real questions (heart rate, stress, Body Battery) -- averaged into
buckets rather than raw.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..session import GarminSession
from ._common import READ, DateArg, EndDateArg, StartDateArg, clean, dump, omit, pick

IntervalArg = Annotated[
    int | None,
    Field(
        ge=5,
        le=240,
        description=(
            "Set to also return the intraday curve, averaged into buckets of this "
            "many minutes (e.g. 15, 30 or 60). Omit for the summary only."
        ),
    ),
]

# Garmin's per-day statistics endpoints accept at most 28 days per request
# (documented for steps and sleep in garminconnect; assumed for their siblings),
# so longer ranges are fetched in 28-day chunks.
_CHUNK_DAYS = 28
_MAX_TREND_DAYS = 366  # light one-row-per-day series
_MAX_DETAIL_DAYS = 31  # heavier per-day payloads
_MAX_CURVE_DAYS = 7  # ranges for which an intraday curve may be requested

_EPOCH = datetime(1970, 1, 1)

# Identifiers and bookkeeping that carry no information for the model.
_NOISE = frozenset(
    {
        "userProfilePK",
        "userProfilePk",
        "userProfileId",
        "userId",
        "userDailySummaryId",
        "uuid",
        "rule",
        "source",
        "privacyProtected",
        "includesWellnessData",
        "includesActivityData",
        "includesCalorieConsumedData",
        "wellnessDescription",
        "durationInMilliseconds",
        "wellnessStartTimeGmt",
        "wellnessStartTimeLocal",
        "wellnessEndTimeGmt",
        "wellnessEndTimeLocal",
        "stressPercentage",
        "restStressPercentage",
        "activityStressPercentage",
        "uncategorizedStressPercentage",
        "lowStressPercentage",
        "mediumStressPercentage",
        "highStressPercentage",
        "bodyBatteryVersion",
        "respirationVersion",
        "respirationAlgorithmVersion",
        "createTimeStamp",
        "remSleepData",
        "skinTempDataExists",
    }
)

_SLEEP_DTO_NOISE = _NOISE | {
    "id",
    "retro",
    "sleepFromDevice",
    "deviceRemCapable",
    "sleepVersion",
    "sleepWindowConfirmed",
    "sleepWindowConfirmationType",
}

# Headline fields kept per day by ``get_daily_summaries``.
_DAILY_HEADLINES = (
    "calendarDate",
    "totalSteps",
    "totalDistanceMeters",
    "totalKilocalories",
    "activeKilocalories",
    "restingHeartRate",
    "averageStressLevel",
    "bodyBatteryHighestValue",
    "bodyBatteryLowestValue",
    "moderateIntensityMinutes",
    "vigorousIntensityMinutes",
    "floorsAscended",
    "sleepingSeconds",
    "averageSpo2",
)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _summary(payload: Any, keep: tuple[str, ...] = ()) -> Any:
    """Drop bookkeeping keys and every array (time series) from a payload.

    Non-dict payloads are returned unchanged. ``keep`` names list-valued keys
    that should survive.
    """
    if not isinstance(payload, Mapping):
        return payload
    return {
        k: v
        for k, v in payload.items()
        if k not in _NOISE and (k in keep or not isinstance(v, list))
    }


def _rows(payload: Any, *keys: str) -> list[Any]:
    """The list of per-day rows in a response that is a list or wraps one."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
        # Unexpected shape: pass it through rather than lose it.
        return [payload] if clean(payload) else []
    return []


def _flatten(row: Any) -> Any:
    """Turn ``{"calendarDate": d, "values": {...}}`` into one flat dict."""
    if isinstance(row, Mapping) and isinstance(row.get("values"), Mapping):
        return {**omit(row, ("values",)), **row["values"]}
    return row


def _check_span(start: str, end: str, max_days: int) -> None:
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if days > max_days:
        raise ToolError(
            f"Date range {start}..{end} spans {days} days; this tool accepts at most "
            f"{max_days}. Narrow the range or split it across several calls."
        )


def _chunks(start: str, end: str, size: int = _CHUNK_DAYS) -> Iterator[tuple[str, str]]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    while first <= last:
        stop = min(first + timedelta(days=size - 1), last)
        yield first.isoformat(), stop.isoformat()
        first = stop + timedelta(days=1)


async def _daily_rows(
    garmin: GarminSession, method: str, start: str, end: str, *keys: str
) -> list[Any]:
    """Call a ``(start, end)`` range method in 28-day chunks and merge the rows."""
    rows: list[Any] = []
    for chunk_start, chunk_end in _chunks(start, end):
        rows.extend(_rows(await garmin.call(method, chunk_start, chunk_end), *keys))
    return rows


def _for_day(what: str, day: str, payload: Any) -> str:
    """Serialise a single-day result, always saying which date it is for."""
    body = clean(payload)
    if not body or (isinstance(body, Mapping) and set(body) <= {"calendarDate"}):
        return dump({"calendarDate": day, "message": f"No {what} data recorded for {day}."})
    if isinstance(body, Mapping) and "calendarDate" not in body:
        body = {"calendarDate": day, **body}
    return dump(body)


def _for_range(what: str, start: str, end: str, days: list[Any], **extra: Any) -> str:
    """Serialise a multi-day result, echoing the resolved range."""
    out: dict[str, Any] = {"start_date": start, "end_date": end, **extra, "days": days}
    if not clean(days):
        out["message"] = f"No {what} data recorded between {start} and {end}."
    return dump(out)


# -- intraday curves ------------------------------------------------------------


def _utc_offset(payload: Mapping[str, Any]) -> timedelta | None:
    """Local-minus-GMT offset, from a payload's paired start timestamps."""
    try:
        gmt = datetime.fromisoformat(str(payload["startTimestampGMT"]))
        local = datetime.fromisoformat(str(payload["startTimestampLocal"]))
    except (KeyError, ValueError):
        return None
    offset = local.replace(tzinfo=None) - gmt.replace(tzinfo=None)
    return offset if abs(offset) <= timedelta(hours=15) else None


def _value_index(payload: Mapping[str, Any], prefix: str, name: str) -> int | None:
    """Column of the sample arrays that holds ``name``, per Garmin's descriptors.

    Descriptor lists look like ``[{"key": "timestamp", "index": 0}, ...]`` with
    key names that vary per endpoint, so match on value types, not key names.
    """
    for key, descriptors in payload.items():
        lowered = key.lower()
        if not (lowered.startswith(prefix.lower()) and "descriptor" in lowered):
            continue
        for descriptor in descriptors if isinstance(descriptors, list) else []:
            if not isinstance(descriptor, Mapping):
                continue
            label = next((v for v in descriptor.values() if isinstance(v, str)), "")
            index = next(
                (v for v in descriptor.values() if isinstance(v, int) and not isinstance(v, bool)),
                None,
            )
            if index is not None and name in label.lower():
                return index
    return None


def _sample_value(row: Any, index: int | None) -> float | None:
    """The measurement in a ``[timestamp, ...]`` sample (negative = no reading)."""
    if not isinstance(row, list | tuple) or not row or not _is_number(row[0]):
        return None
    candidates = (row[index],) if index is not None and 0 < index < len(row) else row[1:]
    value = next((v for v in candidates if _is_number(v)), None)
    return value if value is not None and value >= 0 else None


def _curve(
    samples: Any, index: int | None, interval: int, offset: timedelta | None
) -> dict[str, Any] | None:
    """Average ``[epoch_ms, value]`` samples into ``interval``-minute buckets."""
    width = interval * 60_000
    shift = int((offset or timedelta()).total_seconds() * 1000)
    buckets: dict[int, list[float]] = {}
    for row in samples if isinstance(samples, list) else []:
        value = _sample_value(row, index)
        if value is not None:
            buckets.setdefault((int(row[0]) + shift) // width, []).append(value)
    if not buckets:
        return None
    suffix = "" if offset is not None else "Z"
    return {
        "interval_minutes": interval,
        "time": "local" if offset is not None else "UTC",
        "points": [
            [
                (_EPOCH + timedelta(milliseconds=bucket * width)).strftime("%Y-%m-%dT%H:%M")
                + suffix,
                round(sum(values) / len(values)),
            ]
            for bucket, values in sorted(buckets.items())
        ],
    }


def _iso_timestamps(dto: Mapping[str, Any]) -> dict[str, Any]:
    """Render epoch-millisecond ``*TimestampGMT`` / ``*TimestampLocal`` fields as ISO."""
    out = dict(dto)
    for key, value in dto.items():
        if (
            _is_number(value)
            and value > 10**11
            and key.endswith(("TimestampGMT", "TimestampLocal"))
        ):
            text = (_EPOCH + timedelta(milliseconds=value)).isoformat(timespec="seconds")
            out[key] = f"{text}Z" if key.endswith("GMT") else text
    return out


def _body_battery_day(day: Any, interval: int | None) -> Any:
    if not isinstance(day, Mapping):
        return day
    samples = day.get("bodyBatteryValuesArray")
    index = _value_index(day, "bodyBattery", "level")
    out = _summary(day)
    levels = [v for v in (_sample_value(row, index) for row in samples or []) if v is not None]
    if levels:
        out["highest"] = max(levels)
        out["lowest"] = min(levels)
    if interval:
        out["curve"] = _curve(samples, index, interval, _utc_offset(day))
    return out


def register(mcp: MCPServer, garmin: GarminSession) -> None:
    # -- whole-day summaries ------------------------------------------------------

    @mcp.tool(**READ)
    async def get_daily_summary(date: DateArg = None) -> str:
        """One day's headline wellness numbers in a single call: steps and step
        goal, distance, calories (total/active/BMR), floors, intensity minutes,
        resting/min/max heart rate, stress level and time per stress category,
        Body Battery (high/low/charged/drained), SpO2, respiration, and time
        asleep/sedentary/active. Start here for "how was my day"; use the
        metric-specific tools for more depth. Units are in the field names
        (...Meters, ...Kilocalories, ...Seconds)."""
        day = await garmin.date(date)
        payload = await garmin.call("get_user_summary", day)
        return _for_day("daily summary", day, _summary(payload))

    @mcp.tool(**READ)
    async def get_daily_summaries(
        start_date: StartDateArg = None, end_date: EndDateArg = None
    ) -> str:
        """Compare days side by side: one compact row per day with steps,
        distance, calories, resting heart rate, average stress, Body Battery
        high/low, intensity minutes, floors, time asleep and SpO2. Defaults to
        the last 7 days; at most 31 days per call. Use this for week/month
        overviews across several metrics; for one metric over a longer period
        use its own tool (get_steps, get_resting_heart_rate, get_sleep_trend...)."""
        start, end = await garmin.date_range(start_date, end_date, default_days=7)
        _check_span(start, end, _MAX_DETAIL_DAYS)
        rows = await _daily_rows(garmin, "get_daily_summaries", start, end, "data")
        days = [pick(row, _DAILY_HEADLINES) or _summary(row) for row in rows]
        return _for_range("daily summary", start, end, days)

    # -- sleep --------------------------------------------------------------------

    @mcp.tool(**READ)
    async def get_sleep(date: DateArg = None, include_stage_timeline: bool = False) -> str:
        """The night of sleep that ended on the given date: sleep score and its
        components, time in deep/light/REM/awake (seconds), bed and wake times,
        nap time, and the overnight values the watch recorded (HRV, SpO2,
        respiration and, where available, resting heart rate, stress and Body
        Battery change). Set
        include_stage_timeline to add the list of sleep-stage segments with
        start/end times (activityLevel is Garmin's stage code, believed to be
        0=deep, 1=light, 2=REM, 3=awake) for questions like "when did I wake
        up during the night"."""
        day = await garmin.date(date)
        payload = await garmin.call("get_sleep_data", day)
        if not isinstance(payload, Mapping):
            return _for_day("sleep", day, payload)
        out = _summary(payload)
        dto = payload.get("dailySleepDTO")
        if isinstance(dto, Mapping):
            out["dailySleepDTO"] = _iso_timestamps(omit(dto, _SLEEP_DTO_NOISE))
        if include_stage_timeline:
            out["sleepLevels"] = payload.get("sleepLevels")
        return _for_day("sleep", day, out)

    @mcp.tool(**READ)
    async def get_sleep_trend(start_date: StartDateArg = None, end_date: EndDateArg = None) -> str:
        """Sleep night by night over a date range: one row per night with
        Garmin's nightly sleep statistics (duration, stages, score and related
        overnight values). Defaults to the last 14 nights; at most 366 days per
        call. Use this for sleep patterns and averages over weeks or months;
        use get_sleep for the detail of a single night."""
        start, end = await garmin.date_range(start_date, end_date, default_days=14)
        _check_span(start, end, _MAX_TREND_DAYS)
        rows = await garmin.call("get_sleep_daily", start, end)
        return _for_range("sleep", start, end, [_flatten(r) for r in _rows(rows)])

    # -- heart ----------------------------------------------------------------------

    @mcp.tool(**READ)
    async def get_heart_rate(date: DateArg = None, interval_minutes: IntervalArg = None) -> str:
        """Heart rate for one day: resting, minimum and maximum, plus the 7-day
        average resting heart rate. Set interval_minutes to also get the
        all-day heart-rate curve as [time, average bpm] points per bucket --
        for questions about a particular time of day. For heart rate during a
        workout use the activity tools instead."""
        day = await garmin.date(date)
        payload = await garmin.call("get_heart_rates", day)
        out = _summary(payload)
        if interval_minutes and isinstance(payload, Mapping):
            out["curve"] = _curve(
                payload.get("heartRateValues"),
                _value_index(payload, "heartRate", "heart"),
                interval_minutes,
                _utc_offset(payload),
            )
        return _for_day("heart rate", day, out)

    @mcp.tool(**READ)
    async def get_resting_heart_rate(
        start_date: StartDateArg = None, end_date: EndDateArg = None
    ) -> str:
        """Resting heart rate per day over a date range, with the average,
        lowest and highest for the period. Defaults to the last 30 days; at
        most 366 days per call. Use this for fitness/recovery/illness trends."""
        start, end = await garmin.date_range(start_date, end_date, default_days=30)
        _check_span(start, end, _MAX_TREND_DAYS)
        rows = [
            r
            for r in _rows(await garmin.call("get_rhr_daily", start, end))
            if isinstance(r, Mapping) and _is_number(r.get("value"))
        ]
        values = [r["value"] for r in rows]
        stats: dict[str, Any] = {}
        if values:
            stats = {
                "average": round(sum(values) / len(values), 1),
                "lowest": min(values),
                "highest": max(values),
            }
        days = [
            {"calendarDate": r.get("calendarDate"), "restingHeartRate": r["value"]} for r in rows
        ]
        return _for_range("resting heart rate", start, end, days, **stats)

    @mcp.tool(**READ)
    async def get_hrv(start_date: StartDateArg = None, end_date: EndDateArg = None) -> str:
        """Overnight heart rate variability (HRV), one row per night: last-night
        average, 5-minute high, 7-day (weekly) average, HRV status, and the
        personal baseline range. Defaults to the last 7 nights (pass the same
        start and end date for a single night); at most 366 days per call. Use
        this for recovery and HRV-status questions."""
        start, end = await garmin.date_range(start_date, end_date, default_days=7)
        _check_span(start, end, _MAX_TREND_DAYS)
        rows = await _daily_rows(garmin, "get_hrv_data_range", start, end, "hrvSummaries")
        return _for_range("HRV", start, end, [_summary(r) for r in rows])

    # -- stress and energy ------------------------------------------------------------

    @mcp.tool(**READ)
    async def get_stress(date: DateArg = None, interval_minutes: IntervalArg = None) -> str:
        """Stress for one day: average and maximum stress level. Set
        interval_minutes to also get the all-day stress curve as [time,
        average level] points per bucket (periods without a reading are left
        out) -- for "when was I most stressed". The time spent in each stress
        category is in get_daily_summary; stress across several days is in
        get_daily_summaries."""
        day = await garmin.date(date)
        payload = await garmin.call("get_all_day_stress", day)
        out = _summary(payload)
        if interval_minutes and isinstance(payload, Mapping):
            out["curve"] = _curve(
                payload.get("stressValuesArray"),
                _value_index(payload, "stress", "stress"),
                interval_minutes,
                _utc_offset(payload),
            )
        return _for_day("stress", day, out)

    @mcp.tool(**READ)
    async def get_body_battery(
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        interval_minutes: IntervalArg = None,
    ) -> str:
        """Body Battery (Garmin's energy-reserve score) per day: amount charged
        and drained, and the highest and lowest level reached. Defaults to the
        last 7 days; at most 31 days per call. Set interval_minutes to also get
        each day's level curve as [time, average level] points per bucket
        (ranges of up to 7 days only). To see what charged or drained it, use
        get_body_battery_events."""
        start, end = await garmin.date_range(start_date, end_date, default_days=7)
        _check_span(start, end, _MAX_CURVE_DAYS if interval_minutes else _MAX_DETAIL_DAYS)
        rows = await _daily_rows(garmin, "get_body_battery", start, end)
        days = [_body_battery_day(r, interval_minutes) for r in rows]
        return _for_range("Body Battery", start, end, days)

    @mcp.tool(**READ)
    async def get_body_battery_events(date: DateArg = None) -> str:
        """What moved Body Battery on one day: the list of events Garmin
        detected (sleep, naps, recorded and auto-detected activities, stress
        and recovery periods), each with its timing and Body Battery impact.
        Use this to explain why energy was high or low."""
        day = await garmin.date(date)
        payload = await garmin.call("get_body_battery_events", day)
        events = [_summary(e) for e in _rows(payload)]
        return _for_day("Body Battery event", day, {"events": events} if clean(events) else None)

    # -- breathing and blood oxygen -----------------------------------------------------

    @mcp.tool(**READ)
    async def get_respiration(date: DateArg = None) -> str:
        """Breathing rate for one day: lowest and highest values and the
        average while awake and while asleep."""
        day = await garmin.date(date)
        payload = await garmin.call("get_respiration_data", day)
        return _for_day("respiration", day, _summary(payload))

    @mcp.tool(**READ)
    async def get_spo2(date: DateArg = None) -> str:
        """Blood oxygen saturation (pulse ox, SpO2) for one day: average,
        lowest and latest reading, the average during sleep, and the 7-day
        average. Only available when the watch has pulse-ox tracking enabled."""
        day = await garmin.date(date)
        payload = await garmin.call("get_spo2_data", day)
        return _for_day("SpO2", day, _summary(payload))

    # -- movement -----------------------------------------------------------------------

    @mcp.tool(**READ)
    async def get_steps(start_date: StartDateArg = None, end_date: EndDateArg = None) -> str:
        """Step count per day over a date range, with each day's step goal and
        distance. Defaults to the last 7 days; at most 366 days per call. Use
        this for step trends, streaks and goal tracking."""
        start, end = await garmin.date_range(start_date, end_date, default_days=7)
        _check_span(start, end, _MAX_TREND_DAYS)
        rows = await garmin.call("get_daily_steps", start, end)
        return _for_range("step", start, end, [_summary(_flatten(r)) for r in _rows(rows)])

    @mcp.tool(**READ)
    async def get_intensity_minutes(
        start_date: StartDateArg = None, end_date: EndDateArg = None
    ) -> str:
        """Intensity minutes (moderate and vigorous activity time): one row per
        day over a date range, plus week_of_end_date -- the weekly total and
        weekly goal for the week containing the end date. Defaults to the last
        7 days; at most 366 days per call. Use this for progress against the
        weekly activity goal."""
        start, end = await garmin.date_range(start_date, end_date, default_days=7)
        _check_span(start, end, _MAX_TREND_DAYS)
        week = await garmin.call("get_intensity_minutes_data", end)
        rows = await _daily_rows(garmin, "get_daily_intensity_minutes_stats", start, end)
        days = [_summary(_flatten(r)) for r in rows]
        return _for_range("intensity minutes", start, end, days, week_of_end_date=_summary(week))

    @mcp.tool(**READ)
    async def get_floors(start_date: StartDateArg = None, end_date: EndDateArg = None) -> str:
        """Floors climbed and descended per day over a date range, with the
        daily floors goal. Defaults to the last 7 days; at most 366 days per
        call."""
        start, end = await garmin.date_range(start_date, end_date, default_days=7)
        _check_span(start, end, _MAX_TREND_DAYS)
        rows = await _daily_rows(garmin, "get_daily_floors_stats", start, end)
        return _for_range("floors", start, end, [_summary(_flatten(r)) for r in rows])

    # -- hydration ----------------------------------------------------------------------

    @mcp.tool(**READ)
    async def get_hydration(date: DateArg = None) -> str:
        """Fluid intake logged for one day against the daily hydration goal,
        with Garmin's estimated sweat loss (field names give the unit, e.g.
        valueInML). Empty unless the user logs drinks in Garmin Connect."""
        day = await garmin.date(date)
        payload = await garmin.call("get_hydration_data", day)
        return _for_day("hydration", day, _summary(payload))
