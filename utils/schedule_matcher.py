"""
Activity and location inference from the user's weekly schedule.

Converts a listen's UTC timestamp to local time, then matches against
configured time ranges to infer what the user was likely doing.
"""

from dateutil import tz
from dateutil.parser import isoparse

from config import ACTIVITY_SCHEDULE, USER_TIMEZONE
from utils.logger import get_logger

log = get_logger(__name__)


def match_schedule(
    played_at_utc: str,
    schedule: dict | None = None,
    timezone_name: str = USER_TIMEZONE,
) -> tuple[str | None, str | None]:
    """Infer activity and location for a listen based on time-of-day schedule.

    Args:
        played_at_utc: ISO 8601 timestamp in UTC.
        schedule: Schedule dict (defaults to ACTIVITY_SCHEDULE from config).
        timezone_name: IANA timezone string (e.g. "America/Denver").

    Returns:
        (activity, location) tuple. Both None if no range matches.
    """
    if schedule is None:
        schedule = ACTIVITY_SCHEDULE

    local_tz = tz.gettz(timezone_name)
    if local_tz is None:
        log.error("Unknown timezone: %s", timezone_name)
        return (None, None)

    dt_utc = isoparse(played_at_utc)
    dt_local = dt_utc.astimezone(local_tz)

    # Determine day type
    # Monday=0 .. Sunday=6
    day_type = "weekday" if dt_local.weekday() < 5 else "weekend"
    day_ranges = schedule.get(day_type, {})

    local_minutes = dt_local.hour * 60 + dt_local.minute

    for time_range, (activity, location) in day_ranges.items():
        start_str, end_str = time_range.split("-")
        start_minutes = _parse_hhmm(start_str)
        end_minutes = _parse_hhmm(end_str)

        if start_minutes < end_minutes:
            # Normal range (e.g. 08:00-16:00)
            if start_minutes <= local_minutes < end_minutes:
                return (activity, location)
        else:
            # Range crosses midnight (e.g. 23:00-01:00)
            if local_minutes >= start_minutes or local_minutes < end_minutes:
                return (activity, location)

    return (None, None)


def _parse_hhmm(hhmm: str) -> int:
    """Convert "HH:MM" string to minutes since midnight."""
    parts = hhmm.strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])
