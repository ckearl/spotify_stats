#!/usr/bin/env python3
"""
Backfill script: loads all liked/saved songs into the Tracks table.

This pre-caches track metadata and audio features so future syncs
don't need to call the Spotify API for tracks you've already liked.

Usage:
    python scripts/backfill_liked_songs.py

Requires the user-library-read scope on your Spotify token.
"""

import sys
import time

# Add project root to path so imports work
sys.path.insert(0, ".")

from utils.dynamo_client import DynamoClient
from utils.spotify_client import SpotifyClient
from utils.logger import get_logger

log = get_logger("backfill")


def main():
    spotify = SpotifyClient()
    dynamo = DynamoClient()

    # First pass: collect all liked track IDs and basic metadata
    print("Fetching liked songs from Spotify...")
    all_tracks = []
    offset = 0
    total = None

    while True:
        page = spotify.get_liked_songs(limit=50, offset=offset)
        items = page.get("items", [])
        if total is None:
            total = page.get("total", 0)
            print(f"Total liked songs: {total}")

        if not items:
            break

        for item in items:
            track = item.get("track")
            if track and track.get("id"):
                all_tracks.append(track)

        offset += len(items)
        print(f"  Fetched {offset}/{total}...")

        if not page.get("next"):
            break

        # Small delay to avoid rate limiting
        time.sleep(0.1)

    print(f"\nCollected {len(all_tracks)} tracks from liked songs")

    # Check which tracks already exist in DynamoDB
    all_track_ids = [t["id"] for t in all_tracks]
    print("Checking which tracks are already in DynamoDB...")

    existing = set()
    for i in range(0, len(all_track_ids), 100):
        batch = all_track_ids[i : i + 100]
        found = dynamo.batch_get_tracks(batch)
        existing.update(found.keys())
        print(f"  Checked {min(i + 100, len(all_track_ids))}/{len(all_track_ids)}...")

    new_track_ids = [tid for tid in all_track_ids if tid not in existing]
    new_tracks_map = {t["id"]: t for t in all_tracks if t["id"] in set(new_track_ids)}

    print(f"\n{len(existing)} tracks already in DB, {len(new_track_ids)} new tracks to add")

    if not new_track_ids:
        print("Nothing to do!")
        return

    # Fetch audio features for new tracks
    print("\nFetching audio features...")
    all_audio_features = {}
    for i in range(0, len(new_track_ids), 100):
        batch = new_track_ids[i : i + 100]
        features = spotify.get_audio_features(batch)
        all_audio_features.update(features)
        print(f"  Fetched features for {min(i + 100, len(new_track_ids))}/{len(new_track_ids)}...")
        time.sleep(0.2)

    print(f"Got audio features for {len(all_audio_features)} tracks")

    # Build track records and write to DynamoDB
    print("\nWriting to DynamoDB...")
    tracks_to_store = []
    for tid in new_track_ids:
        track_obj = new_tracks_map.get(tid)
        if not track_obj:
            continue

        record = {
            "track_id": tid,
            "name": track_obj.get("name", ""),
            "artists": [a["name"] for a in track_obj.get("artists", [])],
            "album": track_obj.get("album", {}).get("name", ""),
            "duration_ms": track_obj.get("duration_ms", 0),
            "popularity": track_obj.get("popularity", 0),
            "release_date": track_obj.get("album", {}).get("release_date", ""),
            "genres": [],
        }

        af = all_audio_features.get(tid)
        if af:
            record["audio_features"] = af

        tracks_to_store.append(record)

    # Write in batches
    for i in range(0, len(tracks_to_store), 25):
        batch = tracks_to_store[i : i + 25]
        dynamo.batch_put_tracks(batch)
        written = min(i + 25, len(tracks_to_store))
        print(f"  Wrote {written}/{len(tracks_to_store)}...")
        time.sleep(0.1)

    print(f"\nDone! Added {len(tracks_to_store)} tracks to DynamoDB.")
    print(f"Total tracks in DB: ~{len(existing) + len(tracks_to_store)}")


if __name__ == "__main__":
    main()
