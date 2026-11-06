"""Account, device, gear and achievement tools: who the user is, their
devices, their gear (shoes, bikes) with usage totals, personal records,
goals and earned badges."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..session import GarminSession
from ._common import READ, dump, pick

# Fields of a legacy gear record (``get_gear``) worth showing.
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
_GEAR_STATS_KEYS = ("totalDistance", "totalActivities")

_GEAR_ACTIVITY_KEYS = (
    "activityId",
    "activityName",
    "startTimeLocal",
    "distance",
    "duration",
)

# Personal-record ``typeId`` -> (what the record is, unit of ``value``).
# 1-7 are documented by garminconnect; the rest are Garmin's long-standing ids.
_RECORD_TYPES: dict[int, tuple[str, str]] = {
    1: ("fastest 1 km run", "s"),
    2: ("fastest 1 mile run", "s"),
    3: ("fastest 5 km run", "s"),
    4: ("fastest 10 km run", "s"),
    5: ("fastest half marathon", "s"),
    6: ("fastest marathon", "s"),
    7: ("longest run", "m"),
    8: ("longest ride", "m"),
    9: ("most elevation gain in a ride", "m"),
    10: ("best 20-minute average power", "W"),
    11: ("fastest 40 km ride", "s"),
    12: ("most steps in a day", "steps"),
    13: ("most steps in a week", "steps"),
    14: ("most steps in a month", "steps"),
    15: ("longest step-goal streak", "days"),
    17: ("longest swim", "m"),
    18: ("fastest 100 m swim", "s"),
    19: ("fastest 100 yd swim", "s"),
    20: ("fastest 400 m swim", "s"),
    21: ("fastest 500 yd swim", "s"),
    22: ("fastest 750 m swim", "s"),
    23: ("fastest 1000 m swim", "s"),
    24: ("fastest 1000 yd swim", "s"),
    25: ("fastest 1500 m swim", "s"),
    26: ("fastest 1650 yd swim", "s"),
}

# Where a record's date may be found, most useful first.
_RECORD_DATE_KEYS = (
    "activityStartDateTimeLocalFormatted",
    "prStartTimeLocalFormatted",
    "actStartDateTimeInGMTFormatted",
    "prStartTimeGmtFormatted",
)

_BADGE_KEYS = (
    "badgeId",
    "badgeName",
    "badgeEarnedDate",
    "badgeEarnedNumber",
    "badgePoints",
    "badgeAssocDataId",
    "badgeAssocDataName",
)


def _type_key(value: Any) -> Any:
    """``{"typeKey": "running", ...}`` -> ``"running"``; other values unchanged."""
    if isinstance(value, dict):
        return value.get("typeKey") or value
    return value


def _record(entry: Any) -> Any:
    if not isinstance(entry, dict):
        return entry
    type_id = entry.get("typeId")
    known = _RECORD_TYPES.get(type_id) if isinstance(type_id, int) else None
    label, unit = known or (None, None)
    return {
        "typeId": type_id,
        "record": label or entry.get("prTypeLabelKey"),
        "value": entry.get("value"),
        "unit": unit,
        "date": next((entry[k] for k in _RECORD_DATE_KEYS if entry.get(k)), None),
        **pick(entry, ("activityId", "activityName", "activityType")),
    }


def register(mcp: MCPServer, garmin: GarminSession) -> None:
    async def profile_number() -> Any:
        """The user's profile number, which Garmin's gear list is keyed by."""
        device = await garmin.call("get_device_last_used")
        number = device.get("userProfileNumber") if isinstance(device, dict) else None
        if number is None:
            # Accounts without a synced device: the user-settings id is the same number.
            settings = await garmin.call("get_user_profile")
            number = settings.get("id") if isinstance(settings, dict) else None
        if number is None:
            raise ToolError("Could not determine the Garmin user profile number needed for gear.")
        return number

    @mcp.tool(**READ)
    async def get_user_profile() -> str:
        """Who the Garmin account belongs to: name, unit system, timezone, and
        the user's physiological settings (age, height, weight, VO2 max, sleep
        schedule). Call this first to learn which units other tools report in."""
        settings = await garmin.call("get_user_profile")
        if not isinstance(settings, dict):
            settings = {}
        return dump(
            {
                "full_name": await garmin.call("get_full_name"),
                "unit_system": await garmin.call("get_unit_system"),
                "timezone": await garmin.call("get_user_timezone"),
                "today": (await garmin.today()).isoformat(),
                "user_data": settings.get("userData"),
                "sleep": settings.get("userSleep"),
            }
        )

    @mcp.tool(**READ)
    async def get_devices() -> str:
        """List the Garmin devices registered to the account (watches, bike
        computers, scales) with model, firmware version and device id."""
        devices = await garmin.call("get_devices")
        if not isinstance(devices, list):
            return dump(devices)
        keys = (
            "deviceId",
            "displayName",
            "productDisplayName",
            "deviceTypeName",
            "deviceStatusName",
            "currentFirmwareVersion",
            "primary",
            "primaryActivityTrackerIndicator",
            "lastUsed",
        )
        return dump([(pick(d, keys) or d) if isinstance(d, dict) else d for d in devices])

    @mcp.tool(**READ)
    async def get_device_last_used() -> str:
        """The device that most recently synced with Garmin Connect and when --
        useful for checking whether today's data is up to date."""
        return dump(await garmin.call("get_device_last_used"))

    # -- gear -----------------------------------------------------------------

    @mcp.tool(**READ)
    async def list_gear(
        include_retired: Annotated[
            bool, Field(description="Also list retired gear (off by default).")
        ] = False,
    ) -> str:
        """The user's gear (shoes, bikes, ...): name, make/model, type, status,
        first-use date, retirement threshold (`maximumMeters`) and lifetime
        usage (`totalDistance` in metres, `totalActivities`), plus the gear
        `uuid` that get_gear_activities needs. Use it for "how many km are on
        my shoes?" or to see which gear is in rotation."""
        gear = await garmin.call("get_gear", await profile_number())
        if not isinstance(gear, list):
            return dump(gear)
        items: list[Any] = []
        for record in gear:
            if not isinstance(record, dict):
                items.append(record)
                continue
            retired = str(record.get("gearStatusName") or "").lower() == "retired"
            if retired and not include_retired:
                continue
            item = pick(record, _GEAR_KEYS) or dict(record)
            if record.get("uuid"):
                stats = await garmin.call("get_gear_stats", record["uuid"])
                item.update(pick(stats if isinstance(stats, dict) else None, _GEAR_STATS_KEYS))
            items.append(item)
        return dump(items)

    @mcp.tool(**READ)
    async def get_gear_activities(
        gear_uuid: Annotated[str, Field(description="Gear uuid from list_gear.")],
        limit: Annotated[
            int, Field(ge=1, le=100, description="Maximum number of activities to return.")
        ] = 10,
    ) -> str:
        """Usage of one piece of gear: lifetime totals (`totalDistance` in
        metres, `totalActivities`) and the most recent activities it was used
        in (id, name, type, local start time, distance in m, duration in s).
        Pass an activityId on to get_activity for more."""
        stats = await garmin.call("get_gear_stats", gear_uuid)
        activities = await garmin.call("get_gear_activities", gear_uuid, limit)
        if isinstance(activities, list):
            activities = [
                {"activityType": _type_key(a.get("activityType")), **pick(a, _GEAR_ACTIVITY_KEYS)}
                if isinstance(a, dict) and a.get("activityId") is not None
                else a
                for a in activities[:limit]
            ]
        totals = pick(stats if isinstance(stats, dict) else None, _GEAR_STATS_KEYS)
        return dump(
            {
                "uuid": gear_uuid,
                **(totals or {"stats": stats}),
                "activities": activities,
            }
        )

    # -- achievements ---------------------------------------------------------

    @mcp.tool(**READ)
    async def get_personal_records() -> str:
        """The user's personal records as Garmin tracks them: fastest times
        over standard run / ride / swim distances, longest run / ride / swim,
        best power and step records. Each has `record` (what it is), `value`
        with its `unit` (s = seconds, m = metres, W = watts), the date, and the
        activity that set it (activityId usable with get_activity)."""
        records = await garmin.call("get_personal_record")
        if not isinstance(records, list):
            return dump(records)
        return dump([_record(r) for r in records])

    @mcp.tool(**READ)
    async def get_goals(
        status: Annotated[
            Literal["active", "future", "past"],
            Field(description="Which goals: currently running, not yet started, or finished."),
        ] = "active",
        limit: Annotated[
            int, Field(ge=1, le=100, description="Maximum number of goals to return.")
        ] = 30,
    ) -> str:
        """The goals the user set up on Garmin Connect's Goals page (e.g. run
        100 km this month, 3 rides a week): type, target, progress so far and
        date range. Not the automatic daily step / intensity-minute targets.
        `total` is set when there are more goals than `limit` (past goals: the
        last `limit` in Garmin's ascending order are returned)."""
        goals = await garmin.call("get_goals", status)
        if not isinstance(goals, list):
            return dump(goals)
        shown = goals[-limit:] if status == "past" else goals[:limit]
        return dump(
            {
                "status": status,
                "count": len(shown),
                "total": len(goals) if len(goals) > len(shown) else None,
                "goals": shown,
            }
        )

    @mcp.tool(**READ)
    async def get_recent_badges(
        limit: Annotated[
            int, Field(ge=1, le=50, description="Maximum number of badges to return.")
        ] = 10,
    ) -> str:
        """The badges the user earned most recently in Garmin Connect: name,
        date earned, times earned, points and the activity or challenge it was
        awarded for. Use it for "what have I achieved lately?"."""
        badges = await garmin.call("get_latest_earned_badges", limit)
        if not isinstance(badges, list):
            return dump(badges)
        return dump(
            [(pick(b, _BADGE_KEYS) or b) if isinstance(b, dict) else b for b in badges[:limit]]
        )
