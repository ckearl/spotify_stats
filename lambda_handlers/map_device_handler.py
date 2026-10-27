"""
POST /map-device — Create or update a persistent device name mapping.

Maps a Spotify device name (e.g. "iPhone") to a user-friendly custom name
(e.g. "AirPod Pros"). Future device polls automatically apply the mapping.
"""

import json

from utils import auth
from utils.dynamo_client import DynamoClient
from utils.logger import get_logger

log = get_logger(__name__)


def handler(event, context):
    """Lambda entry point. Triggered by API Gateway POST /map-device."""
    if not auth.authorized(event):
        return auth.UNAUTHORIZED
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Invalid JSON body"})

    spotify_device_name = body.get("spotify_device_name")
    custom_name = body.get("custom_name")

    if not spotify_device_name or not custom_name:
        return _response(400, {
            "error": "Both 'spotify_device_name' and 'custom_name' are required"
        })

    if not isinstance(spotify_device_name, str) or not isinstance(custom_name, str):
        return _response(400, {"error": "Fields must be strings"})

    try:
        dynamo = DynamoClient()
        dynamo.put_device_mapping(spotify_device_name.strip(), custom_name.strip())

        log.info("Mapped '%s' -> '%s'", spotify_device_name, custom_name)
        return _response(200, {
            "message": "Device mapping saved",
            "spotify_device_name": spotify_device_name.strip(),
            "custom_name": custom_name.strip(),
        })

    except Exception:
        log.exception("map_device failed")
        return _response(500, {"error": "Internal server error"})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
