import importlib.util
import uuid
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "import_extended", Path(__file__).resolve().parent.parent / "scripts" / "import_extended_history.py"
)
imp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(imp)


def row(track="a", ts="2026-01-01T12:00:00Z", ms=200_000, **kw):
    return {
        "ts": ts, "platform": "ios", "ms_played": ms, "conn_country": "US", "ip_addr": "1.2.3.4",
        "master_metadata_track_name": f"Song {track}", "master_metadata_album_artist_name": "Artist",
        "master_metadata_album_album_name": "Album",
        "spotify_track_uri": f"spotify:track:{track}" if track else None,
        "spotify_episode_uri": None, "reason_start": "trackdone", "reason_end": "trackdone",
        "shuffle": False, "skipped": False, "offline": False, **kw,
    }


def existing(track="a", at="2026-01-01T12:00:30.000Z", ms=150_000, session="s1", lid=None, source="sync"):
    return {"listen_id": lid or f"L-{track}-{at}", "track_id": track, "played_at": at,
            "duration_played_ms": ms, "session_id": session, "source": source}


# --- parsing and filtering ---------------------------------------------------

def test_keeps_music_of_30s_or_more_and_drops_the_rest():
    rows = [
        row("a"), row("b", ms=29_999), row("c", ms=30_000),
        row(None, spotify_episode_uri="spotify:episode:x"),  # podcast
        row(None, audiobook_uri="spotify:audiobook:x"),
    ]
    plays = imp.parse_export(rows)
    assert [p["track_id"] for p in plays] == ["a", "c"]
    assert "ip_addr" not in plays[0]
    assert plays[0]["name"] == "Song a" and plays[0]["artist"] == "Artist"


# --- duplicates within the export ----------------------------------------------

def test_same_track_same_end_is_one_play_keeping_the_longer():
    plays = imp.parse_export([row("a", ms=100_000), row("a", ms=120_000), row("b")])
    kept, dropped = imp.dedupe_export(plays)
    assert dropped == 1
    assert sorted((p["track_id"], p["ms_played"]) for p in kept) == [("a", 120_000), ("b", 200_000)]


def test_overlapping_plays_of_a_track_are_one_play():
    # Second "play" ended 40s after the first but claims 120s of listening: they overlap.
    plays = imp.parse_export([row("a", "2026-01-01T12:00:00Z"), row("a", "2026-01-01T12:00:40Z", ms=120_000)])
    assert len(imp.dedupe_export(plays)[0]) == 1


def test_a_real_replay_shortly_after_is_kept():
    # Ended 70s apart with 60s of listening: the replay started after the first ended.
    plays = imp.parse_export([row("a", "2026-01-01T12:00:00Z"), row("a", "2026-01-01T12:01:10Z", ms=60_000)])
    assert len(imp.dedupe_export(plays)[0]) == 2


# --- matching what's already in DynamoDB ----------------------------------------

def test_export_play_matching_a_tracker_row_is_skipped_and_fixes_its_duration():
    plays = imp.parse_export([row("a", "2026-01-01T12:00:00Z", ms=187_000), row("b", "2026-01-01T13:00:00Z")])
    new, fixes, stats = imp.match_existing(plays, [existing("a", "2026-01-01T12:01:20.000Z")], {})
    assert [p["track_id"] for p in new] == ["b"]
    assert fixes == [("L-a-2026-01-01T12:01:20.000Z", 187_000)]
    assert stats["matched"] == 1


def test_no_fix_when_duration_already_matches():
    plays = imp.parse_export([row("a", ms=150_000)])
    new, fixes, _ = imp.match_existing(plays, [existing("a", ms=150_000)], {})
    assert new == [] and fixes == []


def test_more_than_90s_apart_is_a_different_play():
    plays = imp.parse_export([row("a", "2026-01-01T12:00:00Z")])
    new, fixes, _ = imp.match_existing(plays, [existing("a", "2026-01-01T12:01:31.000Z")], {})
    assert len(new) == 1 and fixes == []


def test_relinked_track_id_matches_by_name_and_artist():
    plays = imp.parse_export([row("old", ms=187_000)])
    tracks = {"new": {"track_id": "new", "name": "SONG OLD", "artists": ["Someone", "artist"]}}
    new, fixes, stats = imp.match_existing(plays, [existing("new")], tracks)
    assert new == [] and len(fixes) == 1
    assert stats["matched_by_name"] == 1


def test_each_tracker_row_matches_at_most_one_play():
    plays = imp.parse_export([row("a", "2026-01-01T12:00:00Z", ms=30_000), row("a", "2026-01-01T12:01:00Z", ms=50_000)])
    new, fixes, _ = imp.match_existing(plays, [existing("a", "2026-01-01T12:00:50.000Z")], {})
    assert len(new) == 1 and len(fixes) == 1


def test_rows_from_an_earlier_run_of_this_import_count_as_done_without_fixes():
    plays = imp.parse_export([row("a", ms=187_000)])
    prior = existing("a", "2026-01-01T12:00:00.000Z", ms=1, source=imp.SOURCE)
    new, fixes, stats = imp.match_existing(plays, [prior], {})
    assert new == [] and fixes == []
    assert stats["already_imported"] == 1


# --- sessions --------------------------------------------------------------------

def test_sessions_split_on_gaps_over_20_minutes_between_plays():
    plays = imp.parse_export([
        row("a", "2026-01-01T12:03:20Z"),  # 12:00:00 - 12:03:20
        row("b", "2026-01-01T12:26:40Z"),  # starts 12:23:20, 20 min after a ended: same session
        row("c", "2026-01-01T13:00:00Z"),  # starts 12:56:40, 30 min gap: new session
    ])
    sessions = imp.assign_sessions(plays, [])
    ids = [sessions[imp.listen_id(p)] for p in plays]
    assert ids[0] == ids[1] != ids[2]
    uuid.UUID(ids[0])
    assert imp.assign_sessions(plays, []) == sessions  # deterministic across runs


def test_new_plays_join_an_adjacent_tracker_session():
    plays = imp.parse_export([row("a", "2026-01-01T11:59:00Z"), row("z", "2026-01-01T15:00:00Z")])
    sessions = imp.assign_sessions(plays, [existing("b", "2026-01-01T12:03:00.000Z", ms=200_000, session="s-live")])
    assert sessions[imp.listen_id(plays[0])] == "s-live"
    assert sessions[imp.listen_id(plays[1])] != "s-live"


# --- records ---------------------------------------------------------------------

def test_listen_item_matches_the_tracker_shape():
    play = imp.parse_export([row("a", "2026-01-01T12:00:00Z", ms=187_000, shuffle=True, platform="osx")])[0]
    item = imp.listen_item(play, "sess")
    assert item == {
        "listen_id": imp.listen_id(play),
        "track_id": "a",
        "played_at": "2026-01-01T12:00:00.000Z",
        "duration_played_ms": 187_000,
        "session_id": "sess",
        "context_type": None,
        "context_uri": None,
        "device_spotify_name": "osx",
        "device_spotify_type": "Computer",
        "device_custom_name": None,
        "activity_inferred": None,
        "location_inferred": None,
        "source": imp.SOURCE,
        "shuffle": True,
        "skipped": False,
        "reason_start": "trackdone",
        "reason_end": "trackdone",
    }


def test_device_types_from_platform():
    assert imp.device_type("iOS 15.2.1 (iPhone12,3)") == "Smartphone"
    assert imp.device_type("web_player osx 10.15.7;chrome 91;desktop") == "Computer"
    assert imp.device_type("Partner roku_tv tcl") == "TV"
    assert imp.device_type("Partner google cast_touch") == "Speaker"
    assert imp.device_type("other") == "Unknown"


def test_track_item_from_api_or_falls_back_to_export_names():
    play = imp.parse_export([row("a")])[0]
    api = {"id": "a", "name": "Real", "duration_ms": 201_000, "popularity": 40,
           "artists": [{"name": "X"}, {"name": "Y"}], "album": {"name": "LP", "release_date": "2019-05-01"}}
    assert imp.track_item("a", api, play) == {
        "track_id": "a", "name": "Real", "artists": ["X", "Y"], "album": "LP",
        "duration_ms": 201_000, "popularity": 40, "release_date": "2019-05-01", "genres": [],
    }
    assert imp.track_item("a", None, play) == {
        "track_id": "a", "name": "Song a", "artists": ["Artist"], "album": "Album",
        "duration_ms": 0, "popularity": 0, "release_date": "", "genres": [],
    }
