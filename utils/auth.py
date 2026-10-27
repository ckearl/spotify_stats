"""
Password check shared by every API endpoint.

Requests carry the dashboard password in an X-Dashboard-Password header; only its
SHA-256 is stored (DASHBOARD_PASSWORD_HASH). Scheduled invocations aren't API
requests and don't go through this.
"""

import hashlib
import hmac
import json
import os

UNAUTHORIZED = {
    "statusCode": 401,
    "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
    "body": json.dumps({"error": "unauthorized"}),
}


def is_api_request(event: dict) -> bool:
    return "httpMethod" in event or "requestContext" in event


def authorized(event: dict) -> bool:
    expected = os.environ.get("DASHBOARD_PASSWORD_HASH")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    password = headers.get("x-dashboard-password")
    if not expected or not password:
        return False
    return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), expected)
