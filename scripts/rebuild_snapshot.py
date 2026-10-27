"""
Rebuild the dashboard snapshot in full by scanning both tables.

The data Lambda only refreshes the snapshot's last few days: with the imported
history a full scan takes ~15 minutes at 5 RCU, far past its timeout. Run this
locally after a bulk import or anything else that changes old listens.

Listens appended while the scan runs are caught by the recent-days refresh that
follows the write.

Usage:
    python scripts/rebuild_snapshot.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402

from utils import snapshot  # noqa: E402
from utils.dynamo_client import DynamoClient  # noqa: E402


def dashboard_bucket() -> str:
    outputs = boto3.client("cloudformation").describe_stacks(StackName="spotify-tracker")["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == "DashboardBucketName")


def main() -> None:
    bucket, s3, dynamo = dashboard_bucket(), boto3.client("s3"), DynamoClient()
    started = time.monotonic()
    print("Scanning Tracks and Listens (this takes a while at 5 RCU)...")
    snap = snapshot.build_full(dynamo)
    print(f"Scanned {len(snap['tracks'])} tracks, {len(snap['listens'])} listens in {time.monotonic() - started:.0f}s")
    snapshot.write(s3, bucket, snap)
    snap = snapshot.refresh(s3, bucket, dynamo)
    print(f"Wrote snapshot: {len(snap['listens'])} listens, newest {snap['newest_played_at']}")


if __name__ == "__main__":
    main()
