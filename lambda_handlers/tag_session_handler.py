"""
POST /tag-session — Update all listens in a session with the same manual tags.

Retroactively applies tags to an entire listening session.
"""

import json

from utils import auth
from utils.dynamo_client import DynamoClient
from utils.logger import get_logger

log = get_logger(__name__)


def handler(event, context):
    """Lambda entry point. Triggered by API Gateway POST /tag-session."""
    if not auth.authorized(event):
        return auth.UNAUTHORIZED
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Invalid JSON body"})

    session_id = body.get("session_id")
    if not session_id or not isinstance(session_id, str):
        return _response(400, {"error": "'session_id' is required (string)"})

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
        listens = dynamo.get_listens_by_session(session_id)

        if not listens:
            return _response(400, {
                "error": f"No listens found for session '{session_id}'"
            })

        updated_count = 0
        for listen in listens:
            success = dynamo.update_listen_tags(listen["listen_id"], updates)
            if success:
                updated_count += 1

        log.info(
            "Tagged %d listens in session %s with %s",
            updated_count,
            session_id,
            updates,
        )
        return _response(200, {
            "message": "Session tagged",
            "session_id": session_id,
            "updated_count": updated_count,
        })

    except Exception:
        log.exception("tag_session failed")
        return _response(500, {"error": "Internal server error"})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
