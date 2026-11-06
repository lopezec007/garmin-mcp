"""Training and performance metrics: readiness, training status, VO2 max, race
predictions, endurance/hill score, fitness age, thresholds, zones, power curve.

Several tools here are "current value or trend" tools: called without dates
they return the current/latest value, called with a date range they return a
history, aggregated more coarsely as the range grows.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..session import GarminSession
from ._common import READ, DateArg, EndDateArg, StartDateArg, dump, omit, pick

# Longest range served as one point per day before switching to coarser
# aggregation (or, for daily-only endpoints, refusing).
DAILY_MAX_DAYS = 92
# garminconnect rejects race-prediction ranges longer than a year.
RACE_PREDICTION_MAX_DAYS = 366

_ID_KEYS = frozenset({"userId", "userProfilePK", "userProfilePk", "userProfileId"})

_READINESS_TREND_KEYS = (
    "calendarDate",
    "score",
    "level",
    "feedbackShort",
    "sleepScore",
    "recoveryTime",
    "hrvFactorFeedback",
    "acwrFactorFeedback",
    "stressHistoryFactorFeedback",
    "inputContext",
)

SportArg = Annotated[
    str,
    Field(description="Garmin sport key for the curve, e.g. 'cycling' (default) or 'running'."),
]


def _strip_ids(value: Any) -> Any:
    """Recursively drop account-id keys, which are pure noise for the model."""
    if isinstance(value, dict):
        return {k: _strip_ids(v) for k, v in value.items() if k not in _ID_KEYS}
    if isinstance(value, list):
        return [_strip_ids(v) for v in value]
    return value


def _span_days(start: str, end: str) -> int:
    """Number of days in the inclusive range ``start``..``end``."""
    return (_date.fromisoformat(end) - _date.fromisoformat(start)).days + 1


def _check_span(start: str, end: str, max_days: int, what: str) -> None:
    days = _span_days(start, end)
    if days > max_days:
        raise ToolError(
            f"{what} covers at most {max_days} days per call; {start}..{end} is {days} days. "
            "Narrow the range or split it into several calls."
        )


def _aggregation(start: str, end: str, *, coarse: tuple[str, ...] = ("weekly", "monthly")) -> str:
    """Pick the aggregation for a range: daily up to ``DAILY_MAX_DAYS``, then
    the first of ``coarse`` up to a year, then the last of ``coarse``."""
    days = _span_days(start, end)
    if days <= DAILY_MAX_DAYS:
        return "daily"
    return coarse[0] if days <= 366 else coarse[-1]


def _snapshots(raw: Any) -> list[dict[str, Any]]:
    """Normalise a training-readiness response (list, single dict or nothing)."""
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict) and s]


def _snapshot_time(snapshot: dict[str, Any]) -> str:
    return str(snapshot.get("timestamp") or snapshot.get("timestampLocal") or "")


def _snapshot_day(snapshot: dict[str, Any]) -> str:
    return str(snapshot.get("calendarDate") or str(snapshot.get("timestampLocal") or "")[:10])


def _readiness_trend(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per day: the after-wake-up snapshot if tagged, else the latest."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for snapshot in snapshots:
        by_day.setdefault(_snapshot_day(snapshot), []).append(snapshot)
    rows = []
    for day in sorted(by_day):
        entries = by_day[day]
        chosen = next(
            (s for s in entries if s.get("inputContext") == "AFTER_WAKEUP_RESET"),
            max(entries, key=_snapshot_time),
        )
        rows.append(pick(chosen, _READINESS_TREND_KEYS) or chosen)
    return rows


def register(mcp: MCPServer, garmin: GarminSession) -> None:
    @mcp.tool(**READ)
    async def get_training_readiness(
        start_date: StartDateArg = None, end_date: EndDateArg = None
    ) -> str:
        """Training Readiness: how ready the body is for a hard session (score
        0-100 with level, feedback and the contributing factors: sleep,
        recovery time in minutes, HRV, acute load, stress history).
        Without dates, or for a single day, returns every snapshot Garmin
        computed that day, newest first (`inputContext` AFTER_WAKEUP_RESET is
        the morning reading). With a range (max 92 days) returns one compact
        row per day -- the morning reading when tagged, else the day's latest.
        Needs a watch that supports Training Readiness."""
        start, end = await garmin.date_range(start_date, end_date, default_days=1)
        if start == end:
            snapshots = _snapshots(await garmin.call("get_training_readiness", end))
            snapshots.sort(key=_snapshot_time, reverse=True)
            return dump(
                {
                    "date": end,
                    "snapshots": [omit(s, (*_ID_KEYS, "deviceId")) for s in snapshots],
                }
            )
        _check_span(start, end, DAILY_MAX_DAYS, "Training readiness history")
        snapshots = _snapshots(await garmin.call("get_training_readiness_range", start, end))
        return dump({"start": start, "end": end, "days": _readiness_trend(snapshots)})

    @mcp.tool(**READ)
    async def get_training_status(date: DateArg = None) -> str:
        """Training status as of a date: the status verdict (productive,
        maintaining, overreaching, ...), acute vs chronic training load and
        their ratio (ACWR), and the four-week load focus (low aerobic / high
        aerobic / anaerobic load against Garmin's target ranges); usually also
        the most recent VO2 max. Use it to answer "am I training too much or
        too little, and of what kind?"."""
        day = await garmin.date(date)
        status = _strip_ids(await garmin.call("get_training_status", day))
        out: dict[str, Any] = dict(status) if isinstance(status, dict) else {"status": status}
        # The aggregated endpoint normally embeds both of these; fetch them
        # from their dedicated endpoints only when it did not.
        if not out.get("mostRecentTrainingStatus"):
            out["dailyTrainingStatus"] = _strip_ids(
                await garmin.call("get_daily_training_status", day)
            )
        if not out.get("mostRecentTrainingLoadBalance"):
            out["trainingLoadBalance"] = _strip_ids(
                await garmin.call("get_training_four_week_load_balance", day)
            )
        return dump({"date": day, **out})

    @mcp.tool(**READ)
    async def get_vo2max(start_date: StartDateArg = None, end_date: EndDateArg = None) -> str:
        """VO2 max. Without dates, or for a single day, returns the latest
        record as of that day: `generic` is the running/general VO2 max,
        `cycling` the cycling one, plus `heatAltitudeAcclimation` (heat and
        altitude acclimation status). With a range returns the VO2 max history
        for a trend: daily records up to 92 days, weekly up to a year, monthly
        beyond that."""
        start, end = await garmin.date_range(start_date, end_date, default_days=1)
        if start == end:
            return dump(_strip_ids(await garmin.call("get_max_metrics_latest", end)))
        aggregation = _aggregation(start, end)
        history = await garmin.call("get_max_metrics_range", start, end, aggregation)
        return dump(
            {"start": start, "end": end, "aggregation": aggregation, "history": _strip_ids(history)}
        )

    @mcp.tool(**READ)
    async def get_race_predictions(
        start_date: StartDateArg = None, end_date: EndDateArg = None
    ) -> str:
        """Garmin's predicted race times for 5K, 10K, half marathon and
        marathon. Without dates returns the current predictions; with a range
        (max 366 days) returns how they evolved -- one entry per day up to 92
        days, otherwise one per month."""
        if start_date is None and end_date is None:
            return dump(_strip_ids(await garmin.call("get_race_predictions")))
        start, end = await garmin.date_range(start_date, end_date, default_days=1)
        _check_span(start, end, RACE_PREDICTION_MAX_DAYS, "Race prediction history")
        aggregation = _aggregation(start, end, coarse=("monthly",))
        history = await garmin.call("get_race_predictions", start, end, aggregation)
        return dump(
            {"start": start, "end": end, "aggregation": aggregation, "history": _strip_ids(history)}
        )

    @mcp.tool(**READ)
    async def get_endurance_score(
        start_date: StartDateArg = None, end_date: EndDateArg = None
    ) -> str:
        """Endurance Score: Garmin's rating of the ability to sustain
        prolonged effort. Without dates, or for a single day, returns that
        day's precise score with its classification and contributors; with a
        range returns the trend (daily values up to 92 days, weekly averages
        beyond). Needs a watch that supports Endurance Score."""
        start, end = await garmin.date_range(start_date, end_date, default_days=1)
        if start == end:
            return dump(_strip_ids(await garmin.call("get_endurance_score", end)))
        aggregation = _aggregation(start, end, coarse=("weekly",))
        return dump(_strip_ids(await garmin.call("get_endurance_score", start, end, aggregation)))

    @mcp.tool(**READ)
    async def get_hill_score(start_date: StartDateArg = None, end_date: EndDateArg = None) -> str:
        """Hill Score: Garmin's rating of uphill running ability (overall
        score with strength and endurance components). Without dates, or for a
        single day, returns that day's score; with a range (max 92 days)
        returns daily values for a trend. Needs a watch that supports Hill
        Score."""
        start, end = await garmin.date_range(start_date, end_date, default_days=1)
        if start == end:
            return dump(_strip_ids(await garmin.call("get_hill_score", end)))
        _check_span(start, end, DAILY_MAX_DAYS, "Hill score history")
        return dump(_strip_ids(await garmin.call("get_hill_score", start, end)))

    @mcp.tool(**READ)
    async def get_fitness_age(date: DateArg = None) -> str:
        """Fitness Age as of a date: Garmin's estimate of how old the body
        behaves versus chronological age, with the components that drive it
        and the achievable target."""
        day = await garmin.date(date)
        data = await garmin.call("get_fitness_age_details", day)
        if not data:  # older accounts/devices: fall back to the legacy endpoint
            data = await garmin.call("get_fitnessage_data", day)
        return dump(_strip_ids(data))

    @mcp.tool(**READ)
    async def get_lactate_threshold(
        start_date: StartDateArg = None, end_date: EndDateArg = None
    ) -> str:
        """Running lactate threshold: threshold heart rate, speed and running
        power. Without dates returns the latest values (`speed_and_heart_rate`
        and `power`); with a range returns the history of each (`speed`,
        `heart_rate`, `power`) -- daily values up to 92 days, weekly up to a
        year, monthly beyond. Speed is Garmin's raw value; do not assume its
        unit."""
        if start_date is None and end_date is None:
            return dump(_strip_ids(await garmin.call("get_lactate_threshold", latest=True)))
        start, end = await garmin.date_range(start_date, end_date, default_days=1)
        aggregation = _aggregation(start, end)
        history = await garmin.call(
            "get_lactate_threshold",
            latest=False,
            start_date=start,
            end_date=end,
            aggregation=aggregation,
        )
        out = history if isinstance(history, dict) else {"history": history}
        return dump({"start": start, "end": end, "aggregation": aggregation, **_strip_ids(out)})

    @mcp.tool(**READ)
    async def get_cycling_ftp(start_date: StartDateArg = None, end_date: EndDateArg = None) -> str:
        """Cycling Functional Threshold Power (FTP). Without dates returns the
        latest FTP; with a range returns the FTP history (daily values up to
        92 days, weekly up to a year, monthly beyond). Empty if no cycling FTP
        has been detected or set."""
        if start_date is None and end_date is None:
            return dump(_strip_ids(await garmin.call("get_cycling_ftp")))
        start, end = await garmin.date_range(start_date, end_date, default_days=1)
        aggregation = _aggregation(start, end)
        history = await garmin.call(
            "get_functional_threshold_power_range",
            start,
            end,
            sport="CYCLING",
            aggregation=aggregation,
        )
        return dump(
            {"start": start, "end": end, "aggregation": aggregation, "history": _strip_ids(history)}
        )

    @mcp.tool(**READ)
    async def get_heart_rate_zones() -> str:
        """The configured heart rate zones for each sport profile: zone floors,
        the max / resting / lactate-threshold heart rates they derive from and
        the calculation method. This is the zone setup, not measured heart
        rate data."""
        return dump(_strip_ids(await garmin.call("get_heart_rate_zones")))

    @mcp.tool(**READ)
    async def get_power_zones() -> str:
        """The configured power zones for every sport that has them (cycling,
        running): zone floors and the functional threshold power they are
        based on."""
        return dump(_strip_ids(await garmin.call("get_power_zones")))

    @mcp.tool(**READ)
    async def get_power_curve(
        start_date: StartDateArg = None,
        end_date: EndDateArg = None,
        sport: SportArg = "cycling",
    ) -> str:
        """Power curve: the best average power held for each duration (a few
        seconds up to hours) across all activities in a date range (default:
        the last 90 days). Use it to judge sprint vs sustained power or to
        compare two periods. Empty without power-meter data."""
        start, end = await garmin.date_range(start_date, end_date, default_days=90)
        curve = await garmin.call("get_power_curve", start, end, sport)
        out = curve if isinstance(curve, dict) else {"curve": curve}
        return dump({"start": start, "end": end, "sport": sport, **_strip_ids(out)})
