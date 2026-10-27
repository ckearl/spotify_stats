"""
POST /tag-range — Update all listens within a datetime range.

Overrides session boundaries — useful for bulk manual correction.
"""

import json

from dateutil.parser import isoparse

from utils import auth
from utils.dynamo_client import DynamoClient
from utils.logger import get_logger

log = get_logger(__name__)


def handler(event, context):
    """Lambda entry point. Triggered by API Gateway POST /tag-range."""
    if not auth.authorized(event):
        return auth.UNAUTHORIZED
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Invalid JSON body"})

    start_time = body.get("start_time")
    end_time = body.get("end_time")

    if not start_time or not end_time:
        return _response(400, {
            "error": "Both 'start_time' and 'end_time' are required (ISO 8601 UTC)"
        })

    # Validate timestamps
    try:
        isoparse(start_time)
        isoparse(end_time)
    except (ValueError, TypeError):
        return _response(400, {
            "error": "Invalid timestamp format. Use ISO 8601 (e.g. 2025-01-15T10:00:00Z)"
        })

    updates = {
        "activity_manual": body.get("activity"),
        "location_manual": body.get("location"),
        "device_custom_name": body.get("device_custom_name"),
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        return _response(400, {
            "error": "At least one of 'activity', 'location', or 'device_custom_name' is required"
        })

    try:
        dynamo = DynamoClient()
        listens = dynamo.get_listens_in_range(start_time, end_time)

        if not listens:
            return _response(200, {
                "message": "No listens found in range",
                "updated_count": 0,
            })

        updated_count = 0
        for listen in listens:
            success = dynamo.update_listen_tags(listen["listen_id"], updates)
            if success:
                updated_count += 1

        log.info(
            "Tagged %d listens in range %s to %s",
            updated_count,
            start_time,
            end_time,
        )
        return _response(200, {
            "message": "Range tagged",
            "start_time": start_time,
            "end_time": end_time,
            "updated_count": updated_count,
        })

    except Exception:
        log.exception("tag_range failed")
        return _response(500, {"error": "Internal server error"})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
