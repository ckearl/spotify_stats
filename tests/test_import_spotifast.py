import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "import_spotifast", Path(__file__).resolve().parent.parent / "scripts" / "import_spotifast_history.py"
)
imp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(imp)


def play(track_id, counted_at, duration_ms=200_000, context=None):
    return {
        "played_at": counted_at,
        "context": context,
        "track": {"id": track_id, "name": f"Song {track_id}", "duration_ms": duration_ms,
                  "artists": [{"name": "Artist"}], "album": {"name": "Album", "release_date": "2020"}},
    }


def test_plays_become_listens_ending_when_the_play_ended():
    # Spotifast stamps a play 30s in. Next play counted 3m later → this one ran ~3m.
    history = [
        play("b", "2026-10-22T23:20:18Z"),
        play("a", "2026-10-22T23:17:18Z", context={"type": "album", "uri": "spotify:album:x"}),
    ]
    listens = imp.plan_listens(history)
    assert [l["track_id"] for l in listens] == ["a", "b"]
    a = listens[0]
    assert a["duration_played_ms"] == 180_000
    assert a["played_at"] == "2026-10-22T23:19:48.000Z"  # started 23:16:48, ran 3m
    assert a["device_spotify_name"] == "Spotifast"
    assert a["source"] == "spotifast_import"
    assert a["context_type"] == "album"
    assert listens[0]["session_id"] == listens[1]["session_id"]


def test_short_tracks_count_from_half_way():
    listens = imp.plan_listens([play("s", "2026-10-22T23:00:20Z", duration_ms=40_000)])
    # Counted at 20s (half of 40s); last play assumed to run to the end.
    assert listens[0]["duration_played_ms"] == 40_000
    assert listens[0]["played_at"] == "2026-10-22T23:00:40.000Z"


def test_a_long_gap_starts_a_new_session():
    listens = imp.plan_listens([play("a", "2026-10-22T10:00:00Z"), play("b", "2026-10-22T15:00:00Z")])
    assert listens[0]["session_id"] != listens[1]["session_id"]


def test_track_records_for_storage():
    t = imp.track_record(play("a", "2026-10-22T10:00:00Z")["track"])
    assert t == {"track_id": "a", "name": "Song a", "artists": ["Artist"], "album": "Album",
                 "duration_ms": 200_000, "popularity": 0, "release_date": "2020", "genres": []}
