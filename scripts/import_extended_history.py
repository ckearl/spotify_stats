"""
Import Spotify's Extended Streaming History export into the Tracks and Listens tables.

Keeps music plays of 30s or more (podcasts, audiobooks, video and shorter plays
are dropped), removes duplicates within the export, and skips plays the tracker
already logged (same track, or same name + artist for relinked IDs, within 90s).
For those, the existing listen's duration_played_ms is replaced with the
export's ms_played, which is real listening time where the tracker's is an
estimate. Track metadata the export lacks (duration, release date, all artists)
comes from Spotify's /v1/tracks.

The tables are provisioned for a trickle of live writes, so the import is paced
to leave the live tracker room (~16 hours for the full history). It's safe to
stop and re-run: everything is cached and checkpointed under CACHE_DIR, and
listen IDs are derived from the play, so a repeated write is harmless.

Usage:
    python scripts/import_extended_history.py              # dry run: scan, fetch metadata, report
    python scripts/import_extended_history.py --apply      # write to DynamoDB, then rebuild the snapshot
    python scripts/import_extended_history.py --rescan     # re-read the tables instead of the cached scan

With write capacity raised temporarily (aws dynamodb update-table), pass e.g.
--listen-rate 80 --track-rate 40 to finish in about half an hour. Put the
capacity back to template.yaml's values afterwards.
"""

import argparse
import glob
import json
import re
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dateutil.parser import isoparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import SESSION_GAP_MS  # noqa: E402

DEFAULT_EXPORT = Path.home() / "Downloads/Spotify Extended Streaming History"
# Outside the repo: SAM packages the whole project directory into every Lambda.
CACHE_DIR = Path.home() / ".cache" / "spotify_stats" / "extended_history"
SOURCE = "extended_history"
MIN_MS = 30_000
MATCH_WINDOW_S = 90
OVERLAP_SLACK_MS = 10_000
NAMESPACE = uuid.UUID("5b0c7e0e-6f1a-4c55-9d3e-6a1f2f3c4d5e")

# Writes per second. Listens has three GSIs at 2 WCU each; every listen write
# (or duration fix) costs one WCU on each. Tracks has 5 WCU and no GSIs.
LISTEN_RATE = 1.5
TRACK_RATE = 4.0


# ----------------------------------------------------------------------------
# Pure steps
# ----------------------------------------------------------------------------

def parse_export(rows: list[dict]) -> list[dict]:
    """Music plays of MIN_MS or more, as plain dicts (no IP address)."""
    plays = []
    for r in rows:
        uri = r.get("spotify_track_uri") or ""
        if not uri.startswith("spotify:track:") or (r.get("ms_played") or 0) < MIN_MS:
            continue
        plays.append({
            "track_id": uri.rsplit(":", 1)[1],
            "ts": r["ts"],
            "ended": isoparse(r["ts"]),
            "ms_played": int(r["ms_played"]),
            "name": r.get("master_metadata_track_name") or "",
            "artist": r.get("master_metadata_album_artist_name") or "",
            "album": r.get("master_metadata_album_album_name") or "",
            "platform": r.get("platform") or "unknown",
            "shuffle": bool(r.get("shuffle")),
            "skipped": bool(r.get("skipped")),
            "reason_start": r.get("reason_start"),
            "reason_end": r.get("reason_end"),
        })
    return plays


def dedupe_export(plays: list[dict]) -> tuple[list[dict], int]:
    """Collapse rows of the same track whose listening overlaps, keeping the longest.

    A replay that started after the previous play ended is a real second play.
    """
    by_track = defaultdict(list)
    for p in plays:
        by_track[p["track_id"]].append(p)
    kept, dropped = [], 0
    for group in by_track.values():
        group.sort(key=lambda p: (p["ended"], p["ms_played"]))
        last = None
        for p in group:
            gap_ms = (p["ended"] - last["ended"]).total_seconds() * 1000 if last else None
            if last is not None and gap_ms < p["ms_played"] - OVERLAP_SLACK_MS:
                dropped += 1
                if p["ms_played"] > last["ms_played"]:
                    kept[-1] = last = p
                continue
            kept.append(p)
            last = p
    kept.sort(key=lambda p: (p["ended"], p["track_id"]))
    return kept, dropped


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def match_existing(
    plays: list[dict], existing: list[dict], tracks_by_id: dict[str, dict]
) -> tuple[list[dict], list[tuple[str, int]], dict]:
    """Split plays into new ones and duration fixes for listens already in DynamoDB.

    A play matches an existing listen of the same track (or the same name and
    artist, for track IDs Spotify relinked) whose played_at is within
    MATCH_WINDOW_S. Each listen matches at most one play, nearest first. Rows an
    earlier run of this import wrote count as done.
    """
    by_id, by_name = defaultdict(list), defaultdict(list)
    for l in existing:
        entry = (isoparse(l["played_at"]), l)
        by_id[l["track_id"]].append(entry)
        t = tracks_by_id.get(l["track_id"])
        if t:
            by_name[_norm(t.get("name"))].append(entry)

    used: set[str] = set()
    new, fixes = [], []
    stats = {"matched": 0, "matched_by_name": 0, "already_imported": 0}
    window = timedelta(seconds=MATCH_WINDOW_S)
    for p in plays:
        best = None
        for entry, by_name_only in [(e, False) for e in by_id[p["track_id"]]] + [(e, True) for e in by_name[_norm(p["name"])]]:
            at, l = entry
            if l["listen_id"] in used or abs(at - p["ended"]) > window:
                continue
            if by_name_only:
                artists = {_norm(a) for a in tracks_by_id[l["track_id"]].get("artists") or []}
                if _norm(p["artist"]) not in artists:
                    continue
            if best is None or abs(at - p["ended"]) < abs(best[0] - p["ended"]):
                best = (at, l, by_name_only and l["track_id"] != p["track_id"])
        if best is None:
            new.append(p)
            continue
        _, l, by_name_only = best
        used.add(l["listen_id"])
        if l.get("source") == SOURCE:
            stats["already_imported"] += 1
            continue
        stats["matched"] += 1
        stats["matched_by_name"] += by_name_only
        if int(l.get("duration_played_ms") or 0) != p["ms_played"]:
            fixes.append((l["listen_id"], p["ms_played"]))
    return new, fixes, stats


def listen_id(play: dict) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{play['track_id']}|{play['ts']}"))


def assign_sessions(plays: list[dict], existing: list[dict]) -> dict[str, str]:
    """Session ID for each new play (by listen_id).

    Plays run from ended - ms_played to ended; a session breaks when nothing is
    playing for more than SESSION_GAP_MS. A new play in a stretch that also has
    tracker listens takes the session of the nearest one, so existing sessions
    are extended rather than rewritten; otherwise the ID derives from the
    stretch's first play, so re-runs agree.
    """
    events = [(p["ended"] - timedelta(milliseconds=p["ms_played"]), p["ended"], p, None) for p in plays]
    for l in existing:
        end = isoparse(l["played_at"])
        events.append((end - timedelta(milliseconds=int(l.get("duration_played_ms") or 0)), end, None, l))
    events.sort(key=lambda e: (e[0], e[1]))

    gap = timedelta(milliseconds=SESSION_GAP_MS)
    groups, group_end = [], None
    for start, end, p, l in events:
        if group_end is None or start - group_end > gap:
            groups.append([])
            group_end = end
        groups[-1].append((end, p, l))
        group_end = max(group_end, end)

    sessions: dict[str, str] = {}
    for group in groups:
        anchors = [(end, l["session_id"]) for end, _, l in group if l and l.get("session_id")]
        fresh = None
        for end, p, _ in group:
            if p is None:
                continue
            if anchors:
                sessions[listen_id(p)] = min(anchors, key=lambda a: abs(a[0] - end))[1]
            else:
                fresh = fresh or str(uuid.uuid5(NAMESPACE, f"session|{listen_id(p)}"))
                sessions[listen_id(p)] = fresh
    return sessions


def device_type(platform: str) -> str:
    p = platform.lower()
    if "ios" in p or "iphone" in p or "android" in p:
        return "Smartphone"
    if "roku" in p or "_tv" in p:
        return "TV"
    if "cast" in p:
        return "Speaker"
    if any(k in p for k in ("osx", "os x", "windows", "web_player", "linux")):
        return "Computer"
    return "Unknown"


def _fmt(dt) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def listen_item(play: dict, session_id: str) -> dict:
    return {
        "listen_id": listen_id(play),
        "track_id": play["track_id"],
        "played_at": _fmt(play["ended"]),
        "duration_played_ms": play["ms_played"],
        "session_id": session_id,
        "context_type": None,
        "context_uri": None,
        "device_spotify_name": play["platform"],
        "device_spotify_type": device_type(play["platform"]),
        "device_custom_name": None,
        # The activity schedule describes the present, not 2020-2025.
        "activity_inferred": None,
        "location_inferred": None,
        "source": SOURCE,
        "shuffle": play["shuffle"],
        "skipped": play["skipped"],
        "reason_start": play["reason_start"],
        "reason_end": play["reason_end"],
    }


def track_item(track_id: str, api: dict | None, play: dict) -> dict:
    if api:
        album = api.get("album") or {}
        return {
            "track_id": track_id,
            "name": api.get("name", ""),
            "artists": [a["name"] for a in api.get("artists", [])],
            "album": album.get("name", ""),
            "duration_ms": int(api.get("duration_ms") or 0),
            "popularity": int(api.get("popularity") or 0),
            "release_date": album.get("release_date", ""),
            "genres": [],
        }
    return {
        "track_id": track_id,
        "name": play["name"],
        "artists": [play["artist"]] if play["artist"] else [],
        "album": play["album"],
        "duration_ms": 0,
        "popularity": 0,
        "release_date": "",
        "genres": [],
    }


# ----------------------------------------------------------------------------
# I/O: export, cached scans, Spotify metadata, paced writes
# ----------------------------------------------------------------------------

def load_export(folder: Path) -> list[dict]:
    rows = []
    for f in sorted(glob.glob(str(folder / "Streaming_History_Audio_*.json"))):
        rows += json.loads(Path(f).read_text())
    if not rows:
        sys.exit(f"No Streaming_History_Audio_*.json files in {folder}")
    return rows


def cached_scan(name: str, scan, rescan: bool) -> list[dict]:
    path = CACHE_DIR / f"{name}.json"
    if path.exists() and not rescan:
        return json.loads(path.read_text())
    print(f"Scanning {name} (slow at 5 RCU)...")
    items = scan()
    path.write_text(json.dumps(items))
    return items


def fetch_metadata(track_ids: list[str]) -> dict[str, dict | None]:
    """Spotify track objects by requested ID (None when Spotify has no such track)."""
    path = CACHE_DIR / "spotify_tracks.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    todo = [t for t in track_ids if t not in cache]
    if todo:
        from utils.spotify_client import SpotifyClient

        spotify = SpotifyClient()
        print(f"Fetching metadata for {len(todo)} tracks ({(len(todo) + 49) // 50} calls)...")
        for i in range(0, len(todo), 50):
            batch = todo[i : i + 50]
            found = {}
            for t in spotify.get_tracks(batch):
                found[t["id"]] = t
                if (t.get("linked_from") or {}).get("id"):
                    found[t["linked_from"]["id"]] = t
            for tid in batch:
                cache[tid] = found.get(tid)
            path.write_text(json.dumps(cache))
            if (i // 50) % 50 == 49:
                print(f"  {i + len(batch)}/{len(todo)}")
    return {t: cache[t] for t in track_ids}


class Checkpoint:
    """IDs already written, one per line, appended as each write lands."""

    def __init__(self, name: str):
        self.path = CACHE_DIR / f"done_{name}.txt"
        self.done = set(self.path.read_text().split()) if self.path.exists() else set()
        self._file = None

    def add(self, key: str) -> None:
        if self._file is None:
            self._file = self.path.open("a")
        self._file.write(key + "\n")
        self._file.flush()
        self.done.add(key)


def paced(items: list, rate: float, label: str):
    """Yield items no faster than `rate` per second, printing progress."""
    interval, next_at, started = 1 / rate, time.monotonic(), time.monotonic()
    for i, item in enumerate(items, 1):
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        next_at = max(next_at, time.monotonic() - 5) + interval  # small catch-up, no bursts
        yield item
        if i % 500 == 0 or i == len(items):
            left = (len(items) - i) / rate / 3600
            print(f"  {label}: {i}/{len(items)} ({time.monotonic() - started:.0f}s, ~{left:.1f}h left)", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write to DynamoDB (default is a dry run)")
    parser.add_argument("--rescan", action="store_true", help="re-scan the tables instead of using the cached scan")
    parser.add_argument("--listen-rate", type=float, default=LISTEN_RATE, help="listen writes per second")
    parser.add_argument("--track-rate", type=float, default=TRACK_RATE, help="track writes per second")
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT, help="folder with the export's JSON files")
    args = parser.parse_args()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    from utils.dynamo_client import DynamoClient

    dynamo = DynamoClient()

    rows = load_export(args.export)
    plays = parse_export(rows)
    plays, export_dupes = dedupe_export(plays)
    print(f"{len(rows)} export rows -> {len(plays) + export_dupes} music plays >= 30s -> {len(plays)} after {export_dupes} duplicates")

    existing = cached_scan("existing_listens", dynamo.scan_all_listens, args.rescan)
    known_tracks = {t["track_id"]: t for t in cached_scan("existing_tracks", dynamo.scan_all_tracks, args.rescan)}
    print(f"DynamoDB (scan cached in {CACHE_DIR}): {len(existing)} listens, {len(known_tracks)} tracks")

    new, fixes, stats = match_existing(plays, existing, known_tracks)
    sessions = assign_sessions(new, existing)
    first, last = plays[0]["ts"], plays[-1]["ts"]
    in_range = [l for l in existing if first <= l["played_at"] <= last and l.get("source") != SOURCE]
    print(
        f"Already in DynamoDB: {stats['matched']} (by name for relinked IDs: {stats['matched_by_name']}), "
        f"{stats['matched'] - len(fixes)} with the right duration, {len(fixes)} durations to fix"
    )
    if stats["already_imported"]:
        print(f"Imported by an earlier run: {stats['already_imported']}")
    print(f"Tracker listens in the export's range ({first[:10]}..{last[:10]}) with no export match: "
          f"{len(in_range) - stats['matched']} (kept as they are)")
    print(f"New listens: {len(new)} in {len(set(sessions.values()))} sessions")

    first_play = {}
    for p in new:
        first_play.setdefault(p["track_id"], p)
    missing = [tid for tid in first_play if tid not in known_tracks]
    meta = fetch_metadata(missing)
    not_found = [tid for tid, t in meta.items() if t is None]
    print(f"New tracks: {len(missing)} ({len(not_found)} not returned by Spotify; using the export's names)")

    track_cp, listen_cp, fix_cp = Checkpoint("tracks"), Checkpoint("listens"), Checkpoint("fixes")
    todo_tracks = [tid for tid in missing if tid not in track_cp.done]
    todo_listens = [p for p in new if listen_id(p) not in listen_cp.done]
    todo_fixes = [f for f in fixes if f[0] not in fix_cp.done]
    hours = len(todo_tracks) / args.track_rate / 3600 + (len(todo_listens) + len(todo_fixes)) / args.listen_rate / 3600
    print(f"To write: {len(todo_tracks)} tracks, {len(todo_listens)} listens, {len(todo_fixes)} duration fixes (~{hours:.1f}h)")

    if not args.apply:
        print("Dry run. Re-run with --apply to write.")
        return

    for tid in paced(todo_tracks, args.track_rate, "tracks"):
        dynamo.put_track(track_item(tid, meta[tid], first_play[tid]))
        track_cp.add(tid)
    for p in paced(todo_listens, args.listen_rate, "listens"):
        dynamo.put_listen(listen_item(p, sessions[listen_id(p)]))
        listen_cp.add(listen_id(p))
    for lid, ms in paced(todo_fixes, args.listen_rate, "duration fixes"):
        dynamo.update_listen_duration(lid, ms)
        fix_cp.add(lid)
    print("DynamoDB import done. Rebuilding the dashboard snapshot...")

    import subprocess

    subprocess.run([sys.executable, str(Path(__file__).with_name("rebuild_snapshot.py"))], check=True)


if __name__ == "__main__":
    main()
