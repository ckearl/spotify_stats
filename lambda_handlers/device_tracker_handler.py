"""
Playback poller — runs every minute, logs plays Spotify never records.

Spotify's recently-played (what the sync reads) leaves out plays from librespot
clients: spotify_player, spotifyd, Spotifast. This polls /me/player, which sees
every Connect device, and turns the snapshots into listens (see
utils/playback_tracker.py). Plays the official apps also report are caught by
the overlap check here and in the sync, so nothing is counted twice.

It also records the active device in DeviceHistory whenever a new play starts,
which the sync uses to label listens with a device.
"""

import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from dateutil.parser import isoparse

from config import SESSION_GAP_MS
from utils import snapshot
from utils.dynamo_client import DynamoClient
from utils.logger import get_logger
from utils.playback_tracker import advance
from utils.schedule_matcher import match_schedule
from utils.spotify_client import SpotifyClient

log = get_logger(__name__)

# Reused across warm invocations so the access token isn't refreshed every minute.
_client: SpotifyClient | None = None


def _spotify() -> SpotifyClient:
    global _client
    if _client is None:
        _client = SpotifyClient()
    return _client


def _s3():
    return boto3.client("s3")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def handler(event, context):
    """Lambda entry point. Triggered by EventBridge every minute."""
    try:
        spotify = _spotify()
        dynamo = DynamoClient()
        now = _now()

        playback = spotify.get_playback_state()
        state = dynamo.get_playback_state()
        new_state, finished = advance(state, playback, now)

        logged = _log_play(spotify, dynamo, finished) if finished else False

        if new_state and (not state or new_state["started_at"] != state["started_at"]):
            dynamo.put_device_history({
                "device_spotify_name": new_state["device_name"],
                "device_spotify_type": new_state["device_type"],
                "device_custom_name": dynamo.get_device_mapping(new_state["device_name"] or ""),
            })

        # Saved after logging: if logging fails, the next run finishes the same
        # play again and the overlap check stops a double count.
        dynamo.save_playback_state(new_state)

        return _response(200, {
            "tracking": new_state["track_id"] if new_state else None,
            "logged": finished["track_id"] if logged else None,
        })

    except Exception:
        log.exception("Playback poll failed")
        # Only invoked on a schedule — re-raise so Lambda records the error.
        raise


def _log_play(spotify: SpotifyClient, dynamo: DynamoClient, play: dict) -> bool:
    played_at = _spotify_timestamp(play["played_at"])
    if dynamo.listen_exists_near(play["track_id"], played_at):
        log.info("Play of %s at %s already recorded", play["track_id"], played_at)
        return False

    track = dynamo.batch_get_tracks([play["track_id"]]).get(play["track_id"])
    if track is None:
        for t in spotify.get_tracks([play["track_id"]]):
            track = {
                "track_id": t["id"],
                "name": t.get("name", ""),
                "artists": [a["name"] for a in t.get("artists", [])],
                "album": t.get("album", {}).get("name", ""),
                "duration_ms": t.get("duration_ms", 0),
                "popularity": t.get("popularity", 0),
                "release_date": t.get("album", {}).get("release_date", ""),
                "genres": [],
            }
            dynamo.put_track(track)

    activity, location = match_schedule(played_at)
    listen = {
        **play,
        "played_at": played_at,
        "session_id": _session_for(dynamo, played_at, play["duration_played_ms"]),
        "device_custom_name": dynamo.get_device_mapping(play["device_spotify_name"] or ""),
        "activity_inferred": activity,
        "location_inferred": location,
        "source": "playback_poll",
    }
    dynamo.put_listen(listen)
    log.info("Logged %s on %s (%d ms)", play["track_id"], play["device_spotify_name"], play["duration_played_ms"])
    _append_to_snapshot([listen], {play["track_id"]: track} if track else {})
    return True


def _append_to_snapshot(listens: list[dict], tracks: dict[str, dict]) -> None:
    """Keep the dashboard snapshot current. Never fails the caller: the daily rebuild repairs gaps."""
    try:
        snapshot.append(_s3(), os.environ["DASHBOARD_BUCKET"], listens, tracks)
    except Exception:
        log.warning("Couldn't append to the dashboard snapshot", exc_info=True)


def _session_for(dynamo: DynamoClient, played_at: str, duration_ms: int) -> str:
    """Continue the latest session if this play started within the session gap of it."""
    recent = dynamo.get_recent_listens(limit=1)
    if recent and recent[0].get("session_id"):
        started = isoparse(played_at).timestamp() * 1000 - duration_ms
        gap = started - isoparse(recent[0]["played_at"]).timestamp() * 1000
        if gap <= SESSION_GAP_MS:
            return recent[0]["session_id"]
    return str(uuid.uuid4())


def _spotify_timestamp(iso: str) -> str:
    """Match recently-played's format (2026-10-25T18:02:40.123Z) so timestamps sort together."""
    return isoparse(iso).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
