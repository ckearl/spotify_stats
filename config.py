"""
Configuration for Spotify Listening History Tracker.

Loads settings from environment variables with sensible defaults.
"""

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Local runs read .env (Lambdas get real environment variables, which win)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(Path(__file__).resolve().parent / ".env")

# ---------------------------------------------------------------------------
# Spotify API credentials (required)
# ---------------------------------------------------------------------------
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REFRESH_TOKEN = os.environ.get("SPOTIFY_REFRESH_TOKEN", "")

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

# ---------------------------------------------------------------------------
# AWS / DynamoDB
# ---------------------------------------------------------------------------
DYNAMODB_REGION = os.environ.get("DYNAMODB_REGION", "us-west-2")

TABLE_TRACKS = os.environ.get("TABLE_TRACKS", "SpotifyTracks")
TABLE_LISTENS = os.environ.get("TABLE_LISTENS", "SpotifyListens")
TABLE_DEVICE_HISTORY = os.environ.get("TABLE_DEVICE_HISTORY", "SpotifyDeviceHistory")
TABLE_DEVICE_MAPPINGS = os.environ.get("TABLE_DEVICE_MAPPINGS", "SpotifyDeviceMappings")
TABLE_PLAYBACK_STATE = os.environ.get("TABLE_PLAYBACK_STATE", "SpotifyPlaybackState")

# GSI names
GSI_TRACK_PLAYED_AT = "TrackPlayedAtIndex"
GSI_SESSION = "SessionIndex"
GSI_PLAYED_AT = "PlayedAtIndex"

# Fixed partition key value for PlayedAtIndex (single-user system)
PLAYED_AT_PARTITION_KEY = "ALL"

# Fixed partition key value for DeviceHistory (enables range queries on timestamp)
DEVICE_HISTORY_PARTITION_KEY = "DEVICE"

# ---------------------------------------------------------------------------
# Timezone & schedule
# ---------------------------------------------------------------------------
USER_TIMEZONE = os.environ.get("USER_TIMEZONE", "America/Denver")

# Session boundary: new session if gap exceeds this (milliseconds)
SESSION_GAP_MS = 20 * 60 * 1000  # 20 minutes

# Device carry-forward resets after this many hours of silence
DEVICE_RESET_GAP_HOURS = 5

# "New day" threshold hour in local time (device carry-forward resets here)
NEW_DAY_HOUR = 4  # 4:00 AM local time

# Device polling buffer for matching listens to devices (minutes)
DEVICE_BUFFER_MINUTES = 15

# DeviceHistory TTL in seconds (90 days)
DEVICE_HISTORY_TTL_SECONDS = 90 * 24 * 60 * 60

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# Retry settings
# ---------------------------------------------------------------------------
MAX_RETRIES = 3
RETRY_BASE_DELAY_S = 1  # seconds; doubles each retry

# ---------------------------------------------------------------------------
# Activity / Location Schedule
# ---------------------------------------------------------------------------
# TODO: Fill in your personal schedule before deployment.
# Format: "HH:MM-HH:MM": ("activity", "location")
# Times are in your local timezone (USER_TIMEZONE).
# If no range matches a listen's timestamp, activity and location will be null.
ACTIVITY_SCHEDULE = {
    "weekday": {
        "07:30-16:00": ("focus", "work"),
        # "06:00-08:00": ("exercise", "gym"),
        # "17:00-18:00": ("exercise", "outdoor"),
    },
    "weekend": {
        # "09:00-11:00": ("exercise", "gym"),
    },
}
