"""
Session boundary calculation and listening duration computation.

Groups consecutive listens into sessions based on time gaps.
"""

import uuid

from dateutil.parser import isoparse

from config import SESSION_GAP_MS
from utils.logger import get_logger

log = get_logger(__name__)


def calculate_sessions(
    listens: list[dict],
    session_gap_ms: int = SESSION_GAP_MS,
) -> list[dict]:
    """Assign session IDs and compute duration_played_ms for a list of listens.

    Listens must have at minimum:
      - played_at: ISO 8601 UTC timestamp
      - duration_ms: track duration in milliseconds (from Tracks table)

    A new session starts when either:
      1. Gap between consecutive listens > session_gap_ms (default 20 min)
      2. Gap > previous track's duration_ms (when that duration > session_gap_ms)
         This handles long tracks (>20 min) more accurately.

    Each listen gets:
      - session_id: UUID string grouping consecutive tracks
      - duration_played_ms: estimated actual listening time

    Args:
        listens: List of listen dicts, sorted by played_at ascending.
        session_gap_ms: Threshold in ms for starting a new session.

    Returns:
        The same list with session_id and duration_played_ms populated.
    """
    if not listens:
        return listens

    # Sort to be safe (should already be sorted)
    listens = sorted(listens, key=lambda x: x["played_at"])

    current_session_id = str(uuid.uuid4())
    listens[0]["session_id"] = current_session_id

    for i in range(1, len(listens)):
        prev = listens[i - 1]
        curr = listens[i]

        prev_played = isoparse(prev["played_at"])
        curr_played = isoparse(curr["played_at"])
        gap_ms = (curr_played - prev_played).total_seconds() * 1000

        prev_duration_ms = prev.get("duration_ms", 0)

        # Determine session boundary threshold
        # For long tracks (>20 min), use the track's duration as the gap threshold
        threshold = session_gap_ms
        if prev_duration_ms > session_gap_ms:
            threshold = prev_duration_ms

        if gap_ms > threshold:
            # Start a new session
            current_session_id = str(uuid.uuid4())
            log.debug(
                "New session %s (gap=%.0fs, threshold=%.0fs)",
                current_session_id[:8],
                gap_ms / 1000,
                threshold / 1000,
            )

        curr["session_id"] = current_session_id

    # Calculate duration_played_ms
    for i in range(len(listens)):
        track_duration = listens[i].get("duration_ms", 0)

        if i < len(listens) - 1:
            # Check if next listen is in the same session
            if listens[i]["session_id"] == listens[i + 1]["session_id"]:
                curr_played = isoparse(listens[i]["played_at"])
                next_played = isoparse(listens[i + 1]["played_at"])
                gap_ms = (next_played - curr_played).total_seconds() * 1000
                # Cap at track duration (can't listen longer than the track)
                listens[i]["duration_played_ms"] = int(
                    min(gap_ms, track_duration)
                )
            else:
                # Last track in its session — assume full duration
                listens[i]["duration_played_ms"] = int(track_duration)
        else:
            # Very last track overall — assume full duration
            listens[i]["duration_played_ms"] = int(track_duration)

    return listens
