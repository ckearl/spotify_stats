"""
GET /dashboard-data — Password-protected listening snapshot for the dashboard.

Checks the X-Dashboard-Password header against a SHA-256 hash, then returns a
short-lived presigned URL to the gzipped snapshot in S3 (see utils/snapshot.py).

The snapshot is kept current by whatever logs listens, so a request normally
just presigns (~0.2s). Its recent days are re-read from DynamoDB on ?refresh=1
and once a day by a scheduled {"rebuild": true} event; it's built in full here
only when missing (see snapshot.refresh).
"""

import json
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from config import DYNAMODB_REGION
from utils import auth, snapshot
from utils.dynamo_client import DynamoClient
from utils.logger import get_logger

log = get_logger(__name__)

# Regional endpoint + SigV4: presigned URLs on the global endpoint get a 307
# redirect for new buckets, which the browser fetch can't follow with CORS.
s3 = boto3.client(
    "s3",
    region_name=DYNAMODB_REGION,
    endpoint_url=f"https://s3.{DYNAMODB_REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

SNAPSHOT_KEY = snapshot.SNAPSHOT_KEY
URL_EXPIRES_S = 300


def handler(event, context):
    """Lambda entry point. API Gateway GET /dashboard-data, or the daily rebuild schedule."""
    bucket = os.environ["DASHBOARD_BUCKET"]

    # Scheduled rebuild. API Gateway events never carry a top-level "rebuild" key.
    if event.get("rebuild") is True:
        snapshot.refresh(s3, bucket, DynamoClient())
        return _response(200, {"rebuilt": True})

    if not auth.authorized(event):
        return auth.UNAUTHORIZED

    params = event.get("queryStringParameters") or {}
    try:
        meta = _snapshot_metadata(bucket)
        generated_at = meta.get("generated-at") if meta else None
        if meta is None or params.get("refresh") == "1":
            snap = snapshot.refresh(s3, bucket, DynamoClient())
            generated_at = snap["generated_at"]

        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": SNAPSHOT_KEY},
            ExpiresIn=URL_EXPIRES_S,
        )
        return _response(200, {"url": url, "generated_at": generated_at})

    except Exception:
        log.exception("Dashboard data failed")
        return _response(500, {"error": "Internal server error"})


def _snapshot_metadata(bucket: str) -> dict | None:
    try:
        return s3.head_object(Bucket=bucket, Key=SNAPSHOT_KEY).get("Metadata", {})
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }
