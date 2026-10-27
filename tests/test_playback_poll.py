from datetime import datetime, timedelta, timezone

import pytest

from lambda_handlers import device_tracker_handler as h
from lambda_handlers import sync_handler

NOW = datetime(2026, 10, 25, 18, 3, 0, tzinfo=timezone.utc)


def playback(track_id, progress_ms, device="spotify-player"):
    return {
        "is_playing": True,
        "currently_playing_type": "track",
        "progress_ms": progress_ms,
        "device": {"name": device, "type": "Computer"},
        "context": {"type": "album", "uri": "spotify:album:a"},
        "item": {"id": track_id, "type": "track", "duration_ms": 200_000, "name": track_id},
    }


T1_STATE = {
    "track_id": "t1", "duration_ms": 200_000, "device_name": "spotify-player", "device_type": "Computer",
    "context_type": "album", "context_uri": "spotify:album:a",
    "started_at": (NOW - timedelta(minutes=3)).isoformat(),
    "last_seen": (NOW - timedelta(minutes=1)).isoformat(),
    "last_progress_ms": 120_000, "listened_ms": 120_000,
}


class FakeSpotify:
    def __init__(self, pb):
        self.pb = pb

    def get_playback_state(self):
        return self.pb

    def get_tracks(self, ids):
        return [{"id": i, "name": f"Song {i}", "artists": [{"name": "Artist"}], "duration_ms": 200_000,
                 "popularity": 50, "album": {"name": "Album", "release_date": "2020-01-01"}} for i in ids]


class FakeDynamo:
    def __init__(self, state=None, near=False, recent=None, known_tracks=()):
        self.state = state
        self.near = near
        self.recent = recent or []
        self.known = set(known_tracks)
        self.listens, self.tracks, self.devices, self.saved = [], [], [], []

    def get_playback_state(self):
        return self.state

    def save_playback_state(self, state):
        self.saved.append(state)

    def listen_exists_near(self, track_id, played_at, window_s=90):
        return self.near

    def get_recent_listens(self, limit=10):
        return self.recent

    def batch_get_tracks(self, ids):
        return {i: {"track_id": i} for i in ids if i in self.known}

    def put_track(self, t):
        self.tracks.append(t)

    def put_listen(self, l):
        self.listens.append(l)

    def put_device_history(self, r):
        self.devices.append(r)

    def get_device_mapping(self, name):
        return "Laptop" if name == "spotify-player" else None


@pytest.fixture
def run(monkeypatch):
    def _run(pb, dynamo):
        monkeypatch.setattr(h, "_spotify", lambda: FakeSpotify(pb))
        monkeypatch.setattr(h, "DynamoClient", lambda: dynamo)
        monkeypatch.setattr(h, "_now", lambda: NOW)
        return h.handler({"source": "aws.events"}, None)
    return _run


def test_logs_the_finished_play_with_device_session_and_source(run):
    d = FakeDynamo(state=T1_STATE, recent=[{"played_at": (NOW - timedelta(minutes=5)).isoformat(), "session_id": "s-prev"}])
    run(playback("t2", 20_000), d)
    assert len(d.listens) == 1
    l = d.listens[0]
    assert l["track_id"] == "t1"
    assert l["source"] == "playback_poll"
    assert l["device_spotify_name"] == "spotify-player"
    assert l["device_custom_name"] == "Laptop"
    assert l["session_id"] == "s-prev"
    assert l["duration_played_ms"] == 160_000
    assert l["context_type"] == "album"


def test_new_session_after_a_long_gap(run):
    d = FakeDynamo(state=T1_STATE, recent=[{"played_at": (NOW - timedelta(hours=3)).isoformat(), "session_id": "old"}])
    run(playback("t2", 20_000), d)
    assert d.listens[0]["session_id"] != "old"


def test_stores_track_metadata_when_missing(run):
    d = FakeDynamo(state=T1_STATE)
    run(playback("t2", 20_000), d)
    assert [t["track_id"] for t in d.tracks] == ["t1"]
    assert d.tracks[0]["artists"] == ["Artist"]


def test_skips_a_play_already_recorded_nearby(run):
    d = FakeDynamo(state=T1_STATE, near=True, known_tracks=["t1"])
    run(playback("t2", 20_000), d)
    assert d.listens == []


def test_records_device_when_a_new_play_starts_and_saves_state(run):
    d = FakeDynamo(state=None)
    run(playback("t1", 10_000), d)
    assert d.listens == []
    assert d.devices[0]["device_spotify_name"] == "spotify-player"
    assert d.saved[-1]["track_id"] == "t1"


def test_continuing_play_does_not_rewrite_device_history(run):
    d = FakeDynamo(state=T1_STATE)
    run(playback("t1", 180_000), d)
    assert d.devices == []
    assert d.saved[-1]["listened_ms"] == 180_000


def test_nothing_playing_and_no_state_is_a_quiet_no_op(run):
    d = FakeDynamo(state=None)
    run(None, d)
    assert d.listens == [] and d.devices == [] and d.saved == [None]


def test_sync_skips_spotify_items_the_poller_already_logged(monkeypatch):
    class SyncSpotify:
        def get_recently_played(self, limit=50):
            return [{"track": {"id": "t1", "duration_ms": 200_000}, "played_at": "2026-10-25T18:02:40Z", "context": None}]

    class SyncDynamo(FakeDynamo):
        def batch_check_listens_exist(self, pairs):
            return set()

    d = SyncDynamo(near=True)
    monkeypatch.setattr(sync_handler, "SpotifyClient", SyncSpotify)
    monkeypatch.setattr(sync_handler, "DynamoClient", lambda: d)
    resp = sync_handler.handler({"source": "aws.events"}, None)
    assert '"new_listens": 0' in resp["body"]
    assert '"duplicates_skipped": 1' in resp["body"]


@pytest.fixture
def appended(monkeypatch):
    calls = []
    monkeypatch.setenv("DASHBOARD_BUCKET", "bucket")
    monkeypatch.setattr(h.snapshot, "append", lambda s3, bucket, listens, tracks: calls.append((bucket, listens, tracks)) or True)
    monkeypatch.setattr(h, "_s3", lambda: object())
    return calls


def test_a_logged_play_is_appended_to_the_dashboard_snapshot(run, appended):
    d = FakeDynamo(state=T1_STATE)
    run(playback("t2", 20_000), d)
    assert len(appended) == 1
    bucket, listens, tracks = appended[0]
    assert bucket == "bucket"
    assert listens[0]["track_id"] == "t1" and listens[0]["source"] == "playback_poll"
    assert tracks["t1"]["name"] == "Song t1"


def test_no_append_when_nothing_was_logged(run, appended):
    run(playback("t1", 180_000), FakeDynamo(state=T1_STATE))
    assert appended == []


def test_snapshot_failure_does_not_lose_the_listen(run, monkeypatch):
    monkeypatch.setenv("DASHBOARD_BUCKET", "bucket")
    monkeypatch.setattr(h, "_s3", lambda: object())
    def boom(*a):
        raise RuntimeError("s3 down")
    monkeypatch.setattr(h.snapshot, "append", boom)
    d = FakeDynamo(state=T1_STATE)
    run(playback("t2", 20_000), d)
    assert len(d.listens) == 1
    assert d.saved[-1]["track_id"] == "t2"


def test_sync_appends_new_listens_to_the_snapshot(monkeypatch, appended):
    class SyncSpotify:
        def get_recently_played(self, limit=50):
            return [{"track": {"id": "t9", "duration_ms": 200_000}, "played_at": "2026-10-25T18:02:40.000Z", "context": None}]

        def get_tracks(self, ids):
            return FakeSpotify(None).get_tracks(ids)

        def get_audio_features(self, ids):
            return {}

    class SyncDynamo(FakeDynamo):
        def batch_check_listens_exist(self, pairs):
            return set()

        def batch_put_tracks(self, ts):
            self.tracks.extend(ts)

        def batch_put_listens(self, ls):
            self.listens.extend(ls)

        def get_recent_device(self, played_at, buffer_minutes=15):
            return None

    monkeypatch.setattr(sync_handler, "SpotifyClient", SyncSpotify)
    monkeypatch.setattr(sync_handler, "DynamoClient", lambda: SyncDynamo())
    monkeypatch.setattr(sync_handler, "_s3", lambda: object())
    sync_handler.handler({"source": "aws.events"}, None)
    assert len(appended) == 1
    assert appended[0][1][0]["track_id"] == "t9"
    assert appended[0][2]["t9"]["name"] == "Song t9"
