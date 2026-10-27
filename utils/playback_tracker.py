"""
Turns once-a-minute playback snapshots (/me/player) into finished plays.

Spotify never records plays from librespot clients (spotify_player, spotifyd,
Spotifast) in recently-played, so the sync misses them. Polling the player state
sees every Connect device, including those. This module is pure: given the
previous state, the current snapshot and the time, it returns the new state and,
when a play has just finished, the play to log.

A play counts after 30 seconds, or half of a shorter track (Spotifast's rule).
`played_at` is when the play ended, matching Spotify's recently-played.
"""

from datetime import datetime, timedelta

from dateutil.parser import isoparse

COUNTS_AFTER_MS = 30_000
PAUSE_TIMEOUT = timedelta(minutes=10)
# Progress may lag wall-clock slightly; anything beyond this is a seek, not listening.
SLACK_MS = 5_000
# Progress dropping by more than this on the same track means it restarted.
RESTART_MS = 10_000


def counts_after(duration_ms: int) -> int:
    return max(1_000, min(COUNTS_AFTER_MS, duration_ms // 2))


def advance(state: dict | None, playback: dict | None, now: datetime) -> tuple[dict | None, dict | None]:
    """Fold one playback snapshot into the tracking state.

    Returns (new_state, finished_play_or_None).
    """
    item = (playback or {}).get("item") or {}
    playing = bool(
        playback
        and playback.get("is_playing")
        and playback.get("currently_playing_type") == "track"
        and item.get("type") == "track"
        and item.get("id")
    )

    if not playing:
        if state and now - isoparse(state["last_seen"]) > PAUSE_TIMEOUT:
            return None, _finish(state, extra_ms=0)
        return state, None

    progress = int(playback.get("progress_ms") or 0)
    duration = int(item.get("duration_ms") or 0)

    if state and state["track_id"] == item["id"] and progress >= state["last_progress_ms"] - RESTART_MS:
        wall_ms = (now - isoparse(state["last_seen"])).total_seconds() * 1000
        credit = max(0, min(progress - state["last_progress_ms"], wall_ms + SLACK_MS))
        return {
            **state,
            "listened_ms": int(state["listened_ms"] + credit),
            "last_progress_ms": progress,
            "last_seen": now.isoformat(),
        }, None

    started_at = now - timedelta(milliseconds=progress)
    finished = None
    if state:
        # The previous track ran until roughly when this one started.
        gap_ms = (started_at - isoparse(state["last_seen"])).total_seconds() * 1000
        extra = max(0, min(gap_ms, state["duration_ms"] - state["last_progress_ms"]))
        finished = _finish(state, extra_ms=int(extra))

    device = playback.get("device") or {}
    context = playback.get("context") or {}
    new_state = {
        "track_id": item["id"],
        "duration_ms": duration,
        "device_name": device.get("name"),
        "device_type": device.get("type"),
        "context_type": context.get("type"),
        "context_uri": context.get("uri"),
        "started_at": started_at.isoformat(),
        "last_seen": now.isoformat(),
        "last_progress_ms": progress,
        "listened_ms": min(progress, duration) if duration else progress,
    }
    return new_state, finished


def _finish(state: dict, extra_ms: int) -> dict | None:
    listened = state["listened_ms"] + extra_ms
    if state["duration_ms"]:
        listened = min(listened, state["duration_ms"])
    if listened < counts_after(state["duration_ms"]):
        return None
    ended = isoparse(state["last_seen"]) + timedelta(milliseconds=extra_ms)
    return {
        "track_id": state["track_id"],
        "played_at": ended.isoformat(),
        "duration_played_ms": int(listened),
        "device_spotify_name": state["device_name"],
        "device_spotify_type": state["device_type"],
        "context_type": state["context_type"],
        "context_uri": state["context_uri"],
    }
