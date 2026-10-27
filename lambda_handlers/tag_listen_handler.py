"""
POST /tag-listen — Update a single listen with manual tags.

Manual values always take precedence over inferred values in queries/analytics.
"""

import json

from utils import auth
from utils.dynamo_client import DynamoClient
from utils.logger import get_logger

log = get_logger(__name__)


def handler(event, context):
    """Lambda entry point. Triggered by API Gateway POST /tag-listen."""
    if not auth.authorized(event):
        return auth.UNAUTHORIZED
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Invalid JSON body"})

    listen_id = body.get("listen_id")
    if not listen_id or not isinstance(listen_id, str):
        return _response(400, {"error": "'listen_id' is required (string)"})

    updates = {
        "activity_manual": body.get("activity"),
        "location_manual": body.get("location"),
        "device_custom_name": body.get("device_custom_name"),
    }

    # Filter out None values
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        return _response(400, {
            "error": "At least one of 'activity', 'location', or 'device_custom_name' is required"
        })

    try:
        dynamo = DynamoClient()
        found = dynamo.update_listen_tags(listen_id, updates)

        if not found:
            return _response(404, {"error": f"Listen '{listen_id}' not found"})

        log.info("Tagged listen %s with %s", listen_id, updates)
        return _response(200, {
            "message": "Listen tagged",
            "listen_id": listen_id,
            "updates": updates,
        })

    except Exception:
        log.exception("tag_listen failed")
        return _response(500, {"error": "Internal server error"})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
