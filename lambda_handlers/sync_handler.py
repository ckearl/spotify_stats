"""
Main Sync Lambda — fetches recently played tracks every 90 minutes.

This is the core handler that:
1. Fetches last 50 recently played tracks from Spotify
2. Deduplicates against existing listens in DynamoDB
3. Fetches track metadata + audio features for new tracks
4. Calculates session boundaries (with cross-sync continuity)
5. Infers activity/location from the user's schedule
6. Matches device info from DeviceHistory
7. Stores everything in DynamoDB
"""

import json
import os
from datetime import datetime, timedelta, timezone

import boto3
from dateutil import tz
from dateutil.parser import isoparse

from config import (
    DEVICE_BUFFER_MINUTES,
    DEVICE_RESET_GAP_HOURS,
    NEW_DAY_HOUR,
    USER_TIMEZONE,
)
from utils import auth, snapshot
from utils.dynamo_client import DynamoClient
from utils.logger import get_logger
from utils.schedule_matcher import match_schedule
from utils.session_calculator import calculate_sessions
from utils.spotify_client import SpotifyClient

log = get_logger(__name__)


def handler(event, context):
    """Lambda entry point. Triggered by CloudWatch Events or POST /sync."""
    if auth.is_api_request(event) and not auth.authorized(event):
        return auth.UNAUTHORIZED
    log.info("Sync handler invoked")

    try:
        spotify = SpotifyClient()
        dynamo = DynamoClient()

        # ----------------------------------------------------------
        # Step 1: Fetch recently played
        # ----------------------------------------------------------
        items = spotify.get_recently_played(limit=50)
        if not items:
            log.info("No recently played tracks — nothing to sync")
            return _response(200, {"message": "No tracks to sync", "new_listens": 0})

        # ----------------------------------------------------------
        # Step 2: Deduplicate
        # ----------------------------------------------------------
        pairs = [
            (item["track"]["id"], item["played_at"])
            for item in items
        ]
        existing = dynamo.batch_check_listens_exist(pairs)
        # The playback poller may have logged the same play a few seconds apart.
        new_items = [
            item for item in items
            if (item["track"]["id"], item["played_at"]) not in existing
            and not dynamo.listen_exists_near(item["track"]["id"], item["played_at"])
        ]

        dupes_skipped = len(items) - len(new_items)
        if dupes_skipped:
            log.info("Skipped %d duplicate listens", dupes_skipped)

        if not new_items:
            log.info("All %d tracks already synced", len(items))
            return _response(200, {
                "message": "All tracks already synced",
                "new_listens": 0,
                "duplicates_skipped": dupes_skipped,
            })

        # ----------------------------------------------------------
        # Step 3: Fetch metadata for new tracks
        # ----------------------------------------------------------
        unique_track_ids = list({item["track"]["id"] for item in new_items})
        existing_tracks = dynamo.batch_get_tracks(unique_track_ids)
        missing_track_ids = [
            tid for tid in unique_track_ids if tid not in existing_tracks
        ]

        new_tracks_stored = 0
        if missing_track_ids:
            # Fetch full track objects for artist/album info
            track_objects = spotify.get_tracks(missing_track_ids)
            track_map = {t["id"]: t for t in track_objects}

            # Fetch audio features
            audio_features = spotify.get_audio_features(missing_track_ids)

            # Build track records
            tracks_to_store = []
            for tid in missing_track_ids:
                track_obj = track_map.get(tid)
                if not track_obj:
                    log.warning("Track %s not found in Spotify API — skipping", tid)
                    continue

                track_record = {
                    "track_id": tid,
                    "name": track_obj.get("name", ""),
                    "artists": [a["name"] for a in track_obj.get("artists", [])],
                    "album": track_obj.get("album", {}).get("name", ""),
                    "duration_ms": track_obj.get("duration_ms", 0),
                    "popularity": track_obj.get("popularity", 0),
                    "release_date": track_obj.get("album", {}).get("release_date", ""),
                    "genres": [],  # Genres come from artist endpoint, not track
                }

                af = audio_features.get(tid)
                if af:
                    track_record["audio_features"] = af
                else:
                    log.debug("No audio features for track %s", tid)

                tracks_to_store.append(track_record)

            if tracks_to_store:
                dynamo.batch_put_tracks(tracks_to_store)
                new_tracks_stored = len(tracks_to_store)
                # Add to existing_tracks for duration lookup
                for t in tracks_to_store:
                    existing_tracks[t["track_id"]] = t

        # ----------------------------------------------------------
        # Step 4: Build listen records
        # ----------------------------------------------------------
        listens = []
        for item in new_items:
            track = item["track"]
            track_id = track["id"]
            played_at = item["played_at"]

            # Get track duration from DB or API response
            track_data = existing_tracks.get(track_id, {})
            duration_ms = track_data.get(
                "duration_ms", track.get("duration_ms", 0)
            )

            # Context info
            ctx = item.get("context") or {}

            listen = {
                "track_id": track_id,
                "played_at": played_at,
                "duration_ms": duration_ms,  # Temporary — used by session calculator
                "context_type": ctx.get("type"),
                "context_uri": ctx.get("uri"),
            }
            listens.append(listen)

        # Sort by played_at ascending for session calculation
        listens.sort(key=lambda x: x["played_at"])

        # ----------------------------------------------------------
        # Step 5: Calculate sessions (with cross-sync continuity)
        # ----------------------------------------------------------
        recent_listens = dynamo.get_recent_listens(limit=5)
        if recent_listens:
            # Prepend recent listens for session continuity
            recent_listens.sort(key=lambda x: x["played_at"])
            combined = recent_listens + listens
            combined = calculate_sessions(combined)
            # Extract only the new listens (skip the prepended ones)
            recent_ids = {r.get("listen_id") for r in recent_listens}
            listens = [l for l in combined if l.get("listen_id") not in recent_ids]
        else:
            listens = calculate_sessions(listens)

        # ----------------------------------------------------------
        # Step 6: Infer activity/location and match devices
        # ----------------------------------------------------------
        last_device = None
        last_device_time = None

        for listen in listens:
            played_at = listen["played_at"]

            # Schedule matching
            activity, location = match_schedule(played_at)
            listen["activity_inferred"] = activity
            listen["location_inferred"] = location

            # Device matching with carry-forward
            listen_dt = isoparse(played_at)

            # Check reset conditions
            if last_device_time is not None:
                gap_hours = (
                    listen_dt - last_device_time
                ).total_seconds() / 3600
                if gap_hours > DEVICE_RESET_GAP_HOURS:
                    log.debug("Device reset: %.1fh gap", gap_hours)
                    last_device = None
                elif _crosses_new_day(last_device_time, listen_dt):
                    log.debug("Device reset: crossed new day boundary")
                    last_device = None

            # Check DeviceHistory for a recent device poll
            polled = dynamo.get_recent_device(
                played_at, buffer_minutes=DEVICE_BUFFER_MINUTES
            )
            if polled:
                new_device_name = polled.get("device_spotify_name")
                if new_device_name != (last_device or {}).get("device_spotify_name"):
                    last_device = polled

            # Apply device info
            if last_device:
                listen["device_spotify_name"] = last_device.get("device_spotify_name")
                listen["device_spotify_type"] = last_device.get("device_spotify_type")
                listen["device_custom_name"] = last_device.get("device_custom_name")
            else:
                listen["device_spotify_name"] = None
                listen["device_spotify_type"] = None
                listen["device_custom_name"] = None

            last_device_time = listen_dt

            # Clean up temporary field used by session calculator
            listen.pop("duration_ms", None)

        # ----------------------------------------------------------
        # Step 7: Store listens
        # ----------------------------------------------------------
        dynamo.batch_put_listens(listens)
        _append_to_snapshot(listens, existing_tracks)

        sessions_created = len({l["session_id"] for l in listens})
        log.info(
            "Sync complete: %d new listens, %d new tracks, %d sessions, %d dupes skipped",
            len(listens),
            new_tracks_stored,
            sessions_created,
            dupes_skipped,
        )

        return _response(200, {
            "message": "Sync complete",
            "new_listens": len(listens),
            "new_tracks": new_tracks_stored,
            "sessions": sessions_created,
            "duplicates_skipped": dupes_skipped,
        })

    except Exception:
        log.exception("Sync handler failed")
        # Re-raise on scheduled runs so the failure shows up in Lambda's
        # Errors metric; API Gateway callers still get a clean 500.
        if event.get("source") == "aws.events":
            raise
        return _response(500, {"error": "Internal server error"})


def _crosses_new_day(dt1: datetime, dt2: datetime) -> bool:
    """Check if two UTC datetimes cross the NEW_DAY_HOUR boundary in local time."""
    local_tz = tz.gettz(USER_TIMEZONE)
    local1 = dt1.astimezone(local_tz)
    local2 = dt2.astimezone(local_tz)

    # Build the "new day" boundary: NEW_DAY_HOUR on the date of dt2
    boundary = local2.replace(
        hour=NEW_DAY_HOUR, minute=0, second=0, microsecond=0
    )

    # If dt2 is before the boundary hour, the boundary is on the same date
    # If dt1 is before boundary and dt2 is after (or on different dates), it crosses
    return local1 < boundary <= local2


def _s3():
    return boto3.client("s3")


def _append_to_snapshot(listens: list[dict], tracks: dict[str, dict]) -> None:
    """Keep the dashboard snapshot current. Never fails the sync: the daily rebuild repairs gaps."""
    try:
        snapshot.append(_s3(), os.environ["DASHBOARD_BUCKET"], listens, tracks)
    except Exception:
        log.warning("Couldn't append to the dashboard snapshot", exc_info=True)


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
