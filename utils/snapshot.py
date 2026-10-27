"""
The dashboard's data snapshot: every listen and track as compact JSON in S3.

Appended to by whatever logs new listens (the playback poller, the sync,
imports) so the dashboard never waits for a rebuild. Once a day (and on demand)
the last RECENT_DAYS are re-read from DynamoDB to pick up tag edits and anything
an append missed. A full build scans both tables, which takes minutes at 5 RCU
once the imported history is in, so it only runs locally
(scripts/rebuild_snapshot.py) or when no snapshot exists. Writes use S3
conditional writes so concurrent writers don't drop each other's listens.

Format (gzipped JSON at SNAPSHOT_KEY):
  {"v":1, "generated_at", "newest_played_at",
   "tracks":  [[track_id, name, album, [artists], duration_ms, release_date], ...],
   "listens": [[played_at, track_idx, duration_played_ms, session_id, context_type, activity, location], ...]}
"""

import gzip
import json
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from utils.logger import get_logger

log = get_logger(__name__)

SNAPSHOT_KEY = "data/snapshot.json"
PLACEHOLDER = ["Unknown track", "Unknown album", ["Unknown artist"], 0, None]
RECENT_DAYS = 7


def track_row(t: dict) -> list:
    return [
        t["track_id"],
        t.get("name") or "Unknown track",
        t.get("album") or "Unknown album",
        t.get("artists") or ["Unknown artist"],
        int(t.get("duration_ms") or 0),
        t.get("release_date"),
    ]


def listen_row(l: dict, track_idx: int) -> list:
    return [
        l["played_at"],
        track_idx,
        int(l.get("duration_played_ms") or 0),
        l.get("session_id"),
        l.get("context_type"),
        _tag(l, "activity"),
        _tag(l, "location"),
    ]


def _tag(listen: dict, field: str) -> str | None:
    """Manual tag wins; inferred values are only used when they're strings."""
    for key in (f"{field}_manual", field):
        value = listen.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def build_full(dynamo) -> dict:
    """Scan both tables into a fresh snapshot. Slow: minutes at 5 RCU with the full history."""
    tracks = dynamo.scan_all_tracks()
    listens = sorted(dynamo.scan_all_listens(), key=lambda l: l["played_at"])
    track_rows, index = [], {}
    for t in tracks:
        index[t["track_id"]] = len(track_rows)
        track_rows.append(track_row(t))
    listen_rows = []
    for l in listens:
        if l["track_id"] not in index:
            index[l["track_id"]] = len(track_rows)
            track_rows.append([l["track_id"], *PLACEHOLDER])
        listen_rows.append(listen_row(l, index[l["track_id"]]))
    return {
        "v": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "newest_played_at": listens[-1]["played_at"] if listens else None,
        "tracks": track_rows,
        "listens": listen_rows,
    }


def refresh_recent(dynamo, snap: dict, days: int = RECENT_DAYS, now: datetime | None = None) -> dict:
    """Replace the snapshot's last `days` of listens with what DynamoDB has now. No scans."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    until = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    recent = dynamo.get_listens_in_range(since, until)

    index = {row[0]: i for i, row in enumerate(snap["tracks"])}
    unknown = [
        tid for tid in dict.fromkeys(l["track_id"] for l in recent)
        if tid not in index or snap["tracks"][index[tid]][1:] == PLACEHOLDER
    ]
    found = dynamo.batch_get_tracks(unknown) if unknown else {}
    for tid in unknown:
        row = track_row(found[tid]) if tid in found else [tid, *PLACEHOLDER]
        if tid in index:
            snap["tracks"][index[tid]] = row
        else:
            index[tid] = len(snap["tracks"])
            snap["tracks"].append(row)

    rows = [row for row in snap["listens"] if row[0] < since]
    rows += [listen_row(l, index[l["track_id"]]) for l in recent]
    rows.sort(key=lambda r: r[0])
    snap.update(
        generated_at=now.isoformat(),
        newest_played_at=rows[-1][0] if rows else None,
        listens=rows,
    )
    return snap


def refresh(s3, bucket: str, dynamo, now: datetime | None = None, attempts: int = 4) -> dict:
    """Refresh the stored snapshot's recent listens, or build one in full if there's none."""
    for _ in range(attempts):
        snap, etag = read(s3, bucket)
        snap = build_full(dynamo) if snap is None else refresh_recent(dynamo, snap, now=now)
        try:
            write(s3, bucket, snap, if_match=etag)
            return snap
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                raise
            log.info("Snapshot changed while refreshing; retrying")
    raise RuntimeError("Snapshot kept changing during refresh")


def read(s3, bucket: str) -> tuple[dict | None, str | None]:
    try:
        obj = s3.get_object(Bucket=bucket, Key=SNAPSHOT_KEY)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
            return None, None
        raise
    return json.loads(gzip.decompress(obj["Body"].read())), obj["ETag"]


def write(s3, bucket: str, snap: dict, if_match: str | None = None) -> None:
    body = gzip.compress(json.dumps(snap, separators=(",", ":")).encode())
    extra = {"IfMatch": if_match} if if_match else {}
    s3.put_object(
        Bucket=bucket,
        Key=SNAPSHOT_KEY,
        Body=body,
        ContentType="application/json",
        ContentEncoding="gzip",
        CacheControl="private, no-store",
        Metadata={"newest-played-at": snap["newest_played_at"] or "", "generated-at": snap["generated_at"]},
        **extra,
    )
    log.info("Wrote snapshot: %d tracks, %d listens, %d bytes", len(snap["tracks"]), len(snap["listens"]), len(body))


def append(s3, bucket: str, listens: list[dict], tracks_by_id: dict[str, dict], attempts: int = 4) -> bool:
    """Add listens (and any tracks the snapshot lacks) to the existing snapshot.

    Returns False when there's no snapshot yet; the next full build covers it.
    """
    for _ in range(attempts):
        snap, etag = read(s3, bucket)
        if snap is None:
            return False
        index = {row[0]: i for i, row in enumerate(snap["tracks"])}
        seen = {(row[0], snap["tracks"][row[1]][0]) for row in snap["listens"]}
        for l in listens:
            if (l["played_at"], l["track_id"]) in seen:
                continue
            if l["track_id"] not in index:
                t = tracks_by_id.get(l["track_id"])
                index[l["track_id"]] = len(snap["tracks"])
                snap["tracks"].append(track_row(t) if t else [l["track_id"], *PLACEHOLDER])
            snap["listens"].append(listen_row(l, index[l["track_id"]]))
            seen.add((l["played_at"], l["track_id"]))
            if not snap["newest_played_at"] or l["played_at"] > snap["newest_played_at"]:
                snap["newest_played_at"] = l["played_at"]
        try:
            write(s3, bucket, snap, if_match=etag)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                raise
            log.info("Snapshot changed while appending; retrying")
    log.warning("Gave up appending %d listens to the snapshot", len(listens))
    return False
