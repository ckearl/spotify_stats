#!/usr/bin/env python3
"""
Backfill script: fetches genres from Spotify's Artist endpoint
and updates every track in the Tracks table.

Spotify stores genres on artists, not tracks. This script:
1. Scans all tracks in DynamoDB
2. Collects unique artist IDs (from the Spotify track objects)
3. Batch-fetches artist details from Spotify
4. Maps artist genres onto each track
5. Updates DynamoDB

Usage:
    python scripts/backfill_genres.py
"""

import sys
import time

sys.path.insert(0, ".")

from utils.dynamo_client import DynamoClient
from utils.logger import get_logger
from utils.spotify_client import SpotifyClient

log = get_logger("backfill_genres")


def main():
    spotify = SpotifyClient()
    dynamo = DynamoClient()

    # Step 1: Scan all tracks from DynamoDB
    print("Scanning all tracks from DynamoDB...")
    all_tracks = dynamo.scan_all_tracks()
    print(f"Found {len(all_tracks)} tracks")

    # Step 2: We need artist IDs, but our tracks table only stores artist names.
    # We need to re-fetch track objects from Spotify to get artist IDs.
    # Collect all track IDs
    track_ids = [t["track_id"] for t in all_tracks]

    print("\nFetching track details from Spotify (for artist IDs)...")
    artist_id_to_name = {}  # artist_id -> artist_name
    track_to_artist_ids = {}  # track_id -> [artist_ids]

    for i in range(0, len(track_ids), 50):
        batch = track_ids[i : i + 50]
        track_objects = spotify.get_tracks(batch)

        for track_obj in track_objects:
            tid = track_obj["id"]
            artist_ids = []
            for artist in track_obj.get("artists", []):
                aid = artist.get("id")
                if aid:
                    artist_ids.append(aid)
                    artist_id_to_name[aid] = artist.get("name", "")
            track_to_artist_ids[tid] = artist_ids

        print(f"  Fetched {min(i + 50, len(track_ids))}/{len(track_ids)} tracks...")
        time.sleep(0.1)

    unique_artist_ids = list(artist_id_to_name.keys())
    print(f"\nFound {len(unique_artist_ids)} unique artists")

    # Step 3: Fetch artist details (for genres)
    print("\nFetching artist genres from Spotify...")
    artist_genres = {}  # artist_id -> [genres]

    for i in range(0, len(unique_artist_ids), 50):
        batch = unique_artist_ids[i : i + 50]
        artists = spotify.get_artists(batch)

        for artist in artists:
            artist_genres[artist["id"]] = artist.get("genres", [])

        print(f"  Fetched {min(i + 50, len(unique_artist_ids))}/{len(unique_artist_ids)} artists...")
        time.sleep(0.15)

    # Step 4: Map genres onto tracks and update DynamoDB
    print("\nUpdating tracks with genres...")
    updated = 0
    skipped = 0

    for i, track in enumerate(all_tracks):
        tid = track["track_id"]
        artist_ids = track_to_artist_ids.get(tid, [])

        # Combine genres from all artists, deduplicated, sorted
        genres = set()
        for aid in artist_ids:
            for genre in artist_genres.get(aid, []):
                genres.add(genre)

        genres = sorted(genres)

        if not genres:
            skipped += 1
            continue

        dynamo.update_track_genres(tid, genres)
        updated += 1

        if (i + 1) % 100 == 0:
            print(f"  Updated {i + 1}/{len(all_tracks)}...")
            time.sleep(0.05)

    print(f"\nDone! Updated {updated} tracks with genres, {skipped} had no genres available.")


if __name__ == "__main__":
    main()
