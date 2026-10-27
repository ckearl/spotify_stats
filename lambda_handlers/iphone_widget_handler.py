"""
GET /iphone-widget — Returns lightweight data optimized for an iPhone widget.

Returns last 10 played songs, today's listening total, and this week's total.
"""

import json
from datetime import datetime, timedelta

from dateutil import tz
from dateutil.parser import isoparse

from config import USER_TIMEZONE
from utils import auth
from utils.dynamo_client import DynamoClient
from utils.logger import get_logger

log = get_logger(__name__)


def handler(event, context):
    """Lambda entry point. Triggered by API Gateway GET /iphone-widget."""
    if not auth.authorized(event):
        return auth.UNAUTHORIZED
    try:
        dynamo = DynamoClient()
        local_tz = tz.gettz(USER_TIMEZONE)
        now_utc = datetime.now(tz.UTC)
        now_local = now_utc.astimezone(local_tz)

        # --- Last 10 played songs ---
        recent = dynamo.get_recent_listens(limit=10)
        track_ids = list({r["track_id"] for r in recent})
        tracks = dynamo.batch_get_tracks(track_ids) if track_ids else {}

        last_played = []
        for listen in recent:
            track_data = tracks.get(listen["track_id"], {})
            played_dt = isoparse(listen["played_at"])
            ago_seconds = (now_utc - played_dt).total_seconds()
            ago = _format_ago(ago_seconds)

            last_played.append({
                "track": track_data.get("name", "Unknown"),
                "artist": ", ".join(track_data.get("artists", ["Unknown"])),
                "album": track_data.get("album", ""),
                "played_at": listen["played_at"],
                "ago": ago,
                "duration_played_ms": listen.get("duration_played_ms", 0),
            })

        # --- Today's total (since midnight local time) ---
        today_start = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        today_start_utc = today_start.astimezone(tz.UTC).isoformat()
        today_end_utc = now_utc.isoformat()

        today_listens = dynamo.get_listens_in_range(
            today_start_utc, today_end_utc
        )
        today_total_ms = sum(
            l.get("duration_played_ms", 0) for l in today_listens
        )

        # --- This week's total (Monday through now) ---
        days_since_monday = now_local.weekday()  # Monday=0
        week_start = (now_local - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_start_utc = week_start.astimezone(tz.UTC).isoformat()

        week_listens = dynamo.get_listens_in_range(
            week_start_utc, today_end_utc
        )
        week_total_ms = sum(
            l.get("duration_played_ms", 0) for l in week_listens
        )

        result = {
            "last_played": last_played,
            "today": {
                "total_ms": today_total_ms,
                "total_minutes": round(today_total_ms / 60000, 1),
                "track_count": len(today_listens),
            },
            "this_week": {
                "total_ms": week_total_ms,
                "total_hours": round(week_total_ms / 3600000, 1),
                "track_count": len(week_listens),
            },
            "as_of": now_utc.isoformat(),
            "timezone": USER_TIMEZONE,
        }

        log.info(
            "Widget data: %d recent, %d today, %d this week",
            len(last_played),
            len(today_listens),
            len(week_listens),
        )
        return _response(200, result)

    except Exception:
        log.exception("iphone_widget query failed")
        return _response(500, {"error": "Internal server error"})


def _format_ago(seconds: float) -> str:
    """Format seconds into a human-readable 'ago' string."""
    if seconds < 60:
        return "just now"
    minutes = int(seconds / 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = int(minutes / 60)
    if hours < 24:
        return f"{hours}h ago"
    days = int(hours / 24)
    return f"{days}d ago"


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
