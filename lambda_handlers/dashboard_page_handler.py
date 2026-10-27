"""
GET /dashboard, /dashboard/{proxy+} — Serves the dashboard single-page app.

Returns site/index.html from the dashboard bucket for every path; the app does
its own routing. The page holds no data — that comes from /dashboard-data.
"""

import os

import boto3
from botocore.exceptions import ClientError

from utils.logger import get_logger

log = get_logger(__name__)

s3 = boto3.client("s3")

PAGE_KEY = "site/index.html"


def handler(event, context):
    """Lambda entry point. Triggered by API Gateway GET /dashboard*."""
    try:
        obj = s3.get_object(Bucket=os.environ["DASHBOARD_BUCKET"], Key=PAGE_KEY)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return _response(503, "Dashboard not deployed yet.", "text/plain; charset=utf-8")
        log.exception("Failed to load dashboard page")
        return _response(500, "Internal server error", "text/plain; charset=utf-8")

    return _response(200, obj["Body"].read().decode("utf-8"), "text/html; charset=utf-8")


def _response(status_code: int, body: str, content_type: str) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": content_type, "Cache-Control": "no-cache"},
        "body": body,
    }
