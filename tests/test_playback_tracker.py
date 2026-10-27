from datetime import datetime, timedelta, timezone

from utils.playback_tracker import PAUSE_TIMEOUT, advance, counts_after

T0 = datetime(2026, 10, 25, 18, 0, 0, tzinfo=timezone.utc)
MIN = 60_000


def pb(track_id="t1", progress_ms=0, duration_ms=200_000, playing=True, device="spotify-player"):
    return {
        "is_playing": playing,
        "currently_playing_type": "track",
        "progress_ms": progress_ms,
        "device": {"name": device, "type": "Computer"},
        "context": {"type": "playlist", "uri": "spotify:playlist:x"},
        "item": {"id": track_id, "type": "track", "duration_ms": duration_ms, "name": track_id},
    }


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_counts_after_is_30s_or_half_a_short_track():
    assert counts_after(200_000) == 30_000
    assert counts_after(40_000) == 20_000
    assert counts_after(0) == 1_000


def test_first_sight_of_a_track_starts_tracking_without_logging():
    state, done = advance(None, pb(progress_ms=20_000), at(0))
    assert done is None
    assert state["track_id"] == "t1"
    assert state["listened_ms"] == 20_000
    assert state["device_name"] == "spotify-player"
    assert state["started_at"] == (at(0) - timedelta(seconds=20)).isoformat()


def test_progress_accumulates_across_polls():
    s, _ = advance(None, pb(progress_ms=10_000), at(0))
    s, done = advance(s, pb(progress_ms=70_000), at(1))
    assert done is None
    assert s["listened_ms"] == 70_000


def test_track_change_finishes_previous_play_and_credits_the_gap():
    s, _ = advance(None, pb("t1", progress_ms=10_000), at(0))
    s, _ = advance(s, pb("t1", progress_ms=130_000), at(2))
    # t2 started 20s before this poll, so t1 ended ~40s after the last poll.
    s, done = advance(s, pb("t2", progress_ms=20_000), at(3))
    assert done["track_id"] == "t1"
    assert done["duration_played_ms"] == 170_000
    assert done["played_at"] == (at(3) - timedelta(seconds=20)).isoformat()
    assert done["device_spotify_name"] == "spotify-player"
    assert done["context_type"] == "playlist"
    assert s["track_id"] == "t2"


def test_credit_never_exceeds_track_length():
    s, _ = advance(None, pb("t1", progress_ms=190_000, duration_ms=200_000), at(0))
    s, done = advance(s, pb("t2", progress_ms=5_000), at(5))
    assert done["duration_played_ms"] == 200_000


def test_skipped_track_is_not_logged():
    s, _ = advance(None, pb("t1", progress_ms=5_000), at(0))
    s, done = advance(s, pb("t2", progress_ms=10_000), at(0.25))
    assert done is None
    assert s["track_id"] == "t2"


def test_replaying_the_same_track_counts_twice():
    s, _ = advance(None, pb("t1", progress_ms=150_000), at(0))
    s, done = advance(s, pb("t1", progress_ms=15_000), at(1))  # progress jumped back: restart
    assert done["track_id"] == "t1"
    assert s["listened_ms"] == 15_000


def test_short_pause_keeps_the_play_open():
    s, _ = advance(None, pb(progress_ms=60_000), at(0))
    s, done = advance(s, pb(progress_ms=60_000, playing=False), at(3))
    assert done is None and s["track_id"] == "t1"
    s, done = advance(s, pb(progress_ms=90_000), at(4))
    assert done is None
    assert s["listened_ms"] == 90_000


def test_stopping_finishes_the_play_after_the_pause_timeout():
    s, _ = advance(None, pb(progress_ms=60_000), at(0))
    s, done = advance(s, None, at(0) + PAUSE_TIMEOUT - timedelta(seconds=1))
    assert done is None and s is not None
    s, done = advance(s, None, at(0) + PAUSE_TIMEOUT + timedelta(seconds=1))
    assert s is None
    assert done["duration_played_ms"] == 60_000
    assert done["played_at"] == at(0).isoformat()


def test_podcasts_and_ads_are_ignored():
    episode = pb(progress_ms=60_000)
    episode["currently_playing_type"] = "episode"
    episode["item"]["type"] = "episode"
    s, done = advance(None, episode, at(0))
    assert s is None and done is None


def test_seeking_forward_does_not_overcount():
    s, _ = advance(None, pb(progress_ms=10_000, duration_ms=400_000), at(0))
    s, _ = advance(s, pb(progress_ms=300_000, duration_ms=400_000), at(1))  # seek ahead
    assert s["listened_ms"] <= 10_000 + 65_000
