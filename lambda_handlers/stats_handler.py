"""
GET /stats — Query aggregated listening statistics over a date range.

Returns top tracks, top artists, listening by activity, listening by device,
and session statistics.
"""

import json
from collections import defaultdict

from utils import auth
from utils.dynamo_client import DynamoClient
from utils.logger import get_logger

log = get_logger(__name__)


def handler(event, context):
    """Lambda entry point. Triggered by API Gateway GET /stats."""
    if not auth.authorized(event):
        return auth.UNAUTHORIZED
    params = event.get("queryStringParameters") or {}
    start_date = params.get("start_date")
    end_date = params.get("end_date")

    if not start_date or not end_date:
        return _response(400, {
            "error": "Both 'start_date' and 'end_date' query parameters are required (YYYY-MM-DD)"
        })

    # Convert date strings to full ISO timestamps for DynamoDB range query
    start_time = f"{start_date}T00:00:00Z"
    end_time = f"{end_date}T23:59:59Z"

    try:
        dynamo = DynamoClient()
        listens = dynamo.get_listens_in_range(start_time, end_time)

        if not listens:
            return _response(200, {
                "date_range": {"start": start_date, "end": end_date},
                "total_listening_time_ms": 0,
                "total_tracks": 0,
                "total_sessions": 0,
                "top_tracks": [],
                "top_artists": [],
                "listening_by_activity": {},
                "listening_by_device": {},
            })

        # Collect unique track IDs to fetch names
        track_ids = list({l["track_id"] for l in listens})
        tracks = dynamo.batch_get_tracks(track_ids)

        # Aggregate stats
        total_time_ms = 0
        track_stats = defaultdict(lambda: {"play_count": 0, "total_time_ms": 0})
        artist_stats = defaultdict(lambda: {"play_count": 0, "total_time_ms": 0})
        activity_stats = defaultdict(lambda: {"count": 0, "time_ms": 0})
        device_stats = defaultdict(lambda: {"count": 0, "time_ms": 0})
        sessions = set()

        for listen in listens:
            duration = listen.get("duration_played_ms", 0)
            total_time_ms += duration
            tid = listen["track_id"]
            track_data = tracks.get(tid, {})

            # Track stats
            track_stats[tid]["play_count"] += 1
            track_stats[tid]["total_time_ms"] += duration
            track_stats[tid]["track_name"] = track_data.get("name", "Unknown")
            track_stats[tid]["artist"] = ", ".join(
                track_data.get("artists", ["Unknown"])
            )

            # Artist stats
            for artist in track_data.get("artists", ["Unknown"]):
                artist_stats[artist]["play_count"] += 1
                artist_stats[artist]["total_time_ms"] += duration

            # Activity stats (prefer manual over inferred)
            activity = (
                listen.get("activity_manual")
                or listen.get("activity_inferred")
                or "unknown"
            )
            activity_stats[activity]["count"] += 1
            activity_stats[activity]["time_ms"] += duration

            # Device stats (prefer custom name)
            device = (
                listen.get("device_custom_name")
                or listen.get("device_spotify_name")
                or "unknown"
            )
            device_stats[device]["count"] += 1
            device_stats[device]["time_ms"] += duration

            # Sessions
            if listen.get("session_id"):
                sessions.add(listen["session_id"])

        # Sort and limit top lists
        top_tracks = sorted(
            [
                {
                    "track_name": v["track_name"],
                    "artist": v["artist"],
                    "play_count": v["play_count"],
                    "total_time_ms": v["total_time_ms"],
                }
                for v in track_stats.values()
            ],
            key=lambda x: x["play_count"],
            reverse=True,
        )[:10]

        top_artists = sorted(
            [
                {
                    "artist": k,
                    "play_count": v["play_count"],
                    "total_time_ms": v["total_time_ms"],
                }
                for k, v in artist_stats.items()
            ],
            key=lambda x: x["play_count"],
            reverse=True,
        )[:10]

        result = {
            "date_range": {"start": start_date, "end": end_date},
            "total_listening_time_ms": total_time_ms,
            "total_tracks": len(listens),
            "total_sessions": len(sessions),
            "top_tracks": top_tracks,
            "top_artists": top_artists,
            "listening_by_activity": dict(activity_stats),
            "listening_by_device": dict(device_stats),
        }

        log.info(
            "Stats query: %s to %s, %d listens, %d sessions",
            start_date,
            end_date,
            len(listens),
            len(sessions),
        )
        return _response(200, result)

    except Exception:
        log.exception("stats query failed")
        return _response(500, {"error": "Internal server error"})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
