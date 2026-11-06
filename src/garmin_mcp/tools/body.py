"""Body measurements: weight, body composition and blood pressure."""

from __future__ import annotations

from datetime import date as _date
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..session import GarminSession
from ._common import READ, DateArg, EndDateArg, StartDateArg, dump

# Keeps a single response to a size the model can digest.
MAX_RANGE_DAYS = 366

_ID_KEYS = frozenset({"userId", "userProfilePK", "userProfilePk", "userProfileId"})


def _strip_ids(value: Any) -> Any:
    """Recursively drop account-id keys, which are pure noise for the model."""
    if isinstance(value, dict):
        return {k: _strip_ids(v) for k, v in value.items() if k not in _ID_KEYS}
    if isinstance(value, list):
        return [_strip_ids(v) for v in value]
    return value


def _weigh_in(entry: Any) -> Any:
    """Drop the record id and the epoch duplicate of ``calendarDate`` from a weigh-in."""
    if not isinstance(entry, dict):
        return entry
    drop = {"samplePk"}
    if entry.get("calendarDate"):
        drop.add("date")
    return {k: v for k, v in entry.items() if k not in drop}


def _check_span(start: str, end: str, what: str) -> None:
    days = (_date.fromisoformat(end) - _date.fromisoformat(start)).days + 1
    if days > MAX_RANGE_DAYS:
        raise ToolError(
            f"{what} covers at most {MAX_RANGE_DAYS} days per call; {start}..{end} is "
            f"{days} days. Narrow the range or split it into several calls."
        )


def register(mcp: MCPServer, garmin: GarminSession) -> None:
    @mcp.tool(**READ)
    async def get_body_composition(
        start_date: StartDateArg = None, end_date: EndDateArg = None
    ) -> str:
        """Weigh-ins over a date range (default: the last 30 days, max 366):
        each entry has weight and BMI, plus body fat, body water, muscle mass,
        bone mass, visceral fat and metabolic age when a smart scale recorded
        them, and `totalAverage` holds the averages for the range. Days
        without a weigh-in are absent; if the range is empty use
        get_latest_weight.
        Units are Garmin's raw ones: weight and the mass fields are normally
        GRAMS, not kg (70500 = 70.5 kg) -- check the magnitude before quoting;
        body fat and body water are percentages."""
        start, end = await garmin.date_range(start_date, end_date, default_days=30)
        _check_span(start, end, "Body composition")
        data = _strip_ids(await garmin.call("get_body_composition", start, end))
        if isinstance(data, dict) and isinstance(data.get("dateWeightList"), list):
            data = {**data, "dateWeightList": [_weigh_in(e) for e in data["dateWeightList"]]}
        return dump(data)

    @mcp.tool(**READ)
    async def get_latest_weight(date: DateArg = None) -> str:
        """The most recent weigh-in on or before a date (default today),
        however long ago it was -- the quick answer to "what do I weigh now?"
        or "what did I weigh back then?". Includes body composition fields
        when a smart scale recorded them. Weight is in Garmin's raw unit,
        normally GRAMS, not kg (70500 = 70.5 kg) -- check the magnitude."""
        day = await garmin.date(date)
        return dump(_weigh_in(_strip_ids(await garmin.call("get_latest_weight", day))))

    @mcp.tool(**READ)
    async def get_blood_pressure(
        start_date: StartDateArg = None, end_date: EndDateArg = None
    ) -> str:
        """Blood pressure readings over a date range (default: the last 30
        days, max 366): per-day summaries with each measurement's systolic,
        diastolic, pulse, time, category and notes. Only contains data if the
        user logs readings manually or owns a Garmin blood pressure monitor."""
        start, end = await garmin.date_range(start_date, end_date, default_days=30)
        _check_span(start, end, "Blood pressure history")
        return dump(_strip_ids(await garmin.call("get_blood_pressure", start, end)))
