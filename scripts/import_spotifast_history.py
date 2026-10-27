"""
Import Spotifast's local play history into the Listens table.

Spotify never records plays from librespot clients, so these plays exist only in
Spotifast's own log (the last 500 plays). Safe to re-run: plays already in the
table (from an earlier import, the playback poller, or the sync) are skipped.

Usage:
    python scripts/import_spotifast_history.py            # dry run: show what would be imported
    python scripts/import_spotifast_history.py --apply    # write to DynamoDB
"""

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from dateutil.parser import isoparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.playback_tracker import counts_after  # noqa: E402
from utils.session_calculator import calculate_sessions  # noqa: E402

DEFAULT_PATH = Path.home() / "Library/Application Support/me.paolino.spotifast/history.json"


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def plan_listens(history: list[dict]) -> list[dict]:
    """Turn Spotifast plays into listen records.

    Spotifast stamps a play when it starts to count (30s in, or half a short
    track), so the start is that time minus the threshold. Listening time is the
    gap to the next play's start, capped at the track length, and played_at is
    when the play ended, matching recently-played.
    """
    rows = []
    for p in history:
        t = p.get("track") or {}
        if not t.get("id") or not p.get("played_at"):
            continue
        duration = int(t.get("duration_ms") or 0)
        start = isoparse(p["played_at"]) - timedelta(milliseconds=counts_after(duration))
        ctx = p.get("context") or {}
        rows.append({
            "track_id": t["id"],
            "played_at": _fmt(start),  # start for now; calculate_sessions measures gaps between starts
            "duration_ms": duration,
            "context_type": ctx.get("type"),
            "context_uri": ctx.get("uri"),
        })

    listens = calculate_sessions(sorted(rows, key=lambda r: r["played_at"]))
    for l in listens:
        l["played_at"] = _fmt(isoparse(l["played_at"]) + timedelta(milliseconds=l["duration_played_ms"]))
        l.pop("duration_ms")
        l.update({
            "device_spotify_name": "Spotifast",
            "device_spotify_type": "Computer",
            "device_custom_name": None,
            "source": "spotifast_import",
        })
    return listens


def track_record(t: dict) -> dict:
    return {
        "track_id": t["id"],
        "name": t.get("name", ""),
        "artists": [a["name"] for a in t.get("artists", [])],
        "album": (t.get("album") or {}).get("name", ""),
        "duration_ms": int(t.get("duration_ms") or 0),
        "popularity": int(t.get("popularity") or 0),
        "release_date": (t.get("album") or {}).get("release_date", ""),
        "genres": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write to DynamoDB (default is a dry run)")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH, help="Spotifast history.json")
    args = parser.parse_args()

    from utils.dynamo_client import DynamoClient

    history = json.loads(args.path.read_text())
    tracks = {p["track"]["id"]: p["track"] for p in history if (p.get("track") or {}).get("id")}
    dynamo = DynamoClient()

    listens = plan_listens(history)
    new = [l for l in listens if not dynamo.listen_exists_near(l["track_id"], l["played_at"], window_s=120)]
    print(f"{len(history)} Spotifast plays -> {len(listens)} listens; {len(listens) - len(new)} already recorded; {len(new)} to import")
    if new:
        print(f"  range: {new[0]['played_at']} .. {new[-1]['played_at']}")
    if not args.apply:
        print("Dry run. Re-run with --apply to write.")
        return

    known = dynamo.batch_get_tracks(list({l["track_id"] for l in new}))
    missing = [track_record(tracks[tid]) for tid in {l["track_id"] for l in new} if tid not in known]
    if missing:
        dynamo.batch_put_tracks(missing)
    dynamo.batch_put_listens(new)
    print(f"Imported {len(new)} listens ({len(missing)} new tracks).")

    import boto3

    from utils import snapshot

    outputs = boto3.client("cloudformation").describe_stacks(StackName="spotify-tracker")["Stacks"][0]["Outputs"]
    bucket = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "DashboardBucketName")
    tracks_by_id = {**{tid: track_record(t) for tid, t in tracks.items()}, **known}
    if snapshot.append(boto3.client("s3"), bucket, new, tracks_by_id):
        print("Added them to the dashboard snapshot.")


if __name__ == "__main__":
    main()
