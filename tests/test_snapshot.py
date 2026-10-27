import gzip
import json
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from utils import snapshot


class FakeS3:
    """Minimal S3 with ETags and If-Match support."""

    def __init__(self, snap=None, conflicts=0):
        self.body = gzip.compress(json.dumps(snap).encode()) if snap else None
        self.etag = '"v1"' if snap else None
        self.conflicts = conflicts
        self.puts = 0

    def get_object(self, Bucket, Key):
        if self.body is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        import io
        return {"Body": io.BytesIO(self.body), "ETag": self.etag}

    def put_object(self, **kw):
        if self.conflicts:
            self.conflicts -= 1
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        if "IfMatch" in kw and kw["IfMatch"] != self.etag:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.body = kw["Body"]
        self.etag = f'"v{self.puts + 2}"'
        self.puts += 1
        self.last_put = kw

    def snap(self):
        return json.loads(gzip.decompress(self.body))


BASE = {
    "v": 1, "generated_at": "2026-10-25T00:00:00+00:00", "newest_played_at": "2026-10-25T10:00:00Z",
    "tracks": [["t1", "One", "Album", ["A"], 200000, "2020"]],
    "listens": [["2026-10-25T10:00:00Z", 0, 200000, "s1", "album", None, None]],
}
T2 = {"track_id": "t2", "name": "Two", "album": "Album", "artists": ["A"], "duration_ms": 180000.0, "release_date": "2020"}


def listen(track_id, at, ms=180000):
    return {"track_id": track_id, "played_at": at, "duration_played_ms": ms, "session_id": "s2",
            "context_type": "playlist", "activity_inferred": True}


def test_append_adds_listens_and_unseen_tracks():
    s3 = FakeS3(BASE)
    assert snapshot.append(s3, "b", [listen("t2", "2026-10-25T11:00:00Z")], {"t2": T2})
    snap = s3.snap()
    assert snap["tracks"][1] == ["t2", "Two", "Album", ["A"], 180000, "2020"]
    assert snap["listens"][1] == ["2026-10-25T11:00:00Z", 1, 180000, "s2", "playlist", None, None]
    assert snap["newest_played_at"] == "2026-10-25T11:00:00Z"
    assert s3.last_put["ContentEncoding"] == "gzip"
    assert s3.last_put["Metadata"]["newest-played-at"] == "2026-10-25T11:00:00Z"


def test_append_reuses_known_tracks_and_skips_duplicates():
    s3 = FakeS3(BASE)
    snapshot.append(s3, "b", [listen("t1", "2026-10-25T10:00:00Z"), listen("t1", "2026-10-25T12:00:00Z")], {})
    snap = s3.snap()
    assert len(snap["tracks"]) == 1
    assert [l[0] for l in snap["listens"]] == ["2026-10-25T10:00:00Z", "2026-10-25T12:00:00Z"]


def test_append_retries_when_someone_else_wrote_first():
    s3 = FakeS3(BASE, conflicts=2)
    assert snapshot.append(s3, "b", [listen("t2", "2026-10-25T11:00:00Z")], {"t2": T2})
    assert len(s3.snap()["listens"]) == 2


def test_append_without_a_snapshot_does_nothing():
    s3 = FakeS3(None)
    assert snapshot.append(s3, "b", [listen("t2", "2026-10-25T11:00:00Z")], {"t2": T2}) is False
    assert s3.puts == 0


def test_append_with_unknown_track_uses_placeholder():
    s3 = FakeS3(BASE)
    snapshot.append(s3, "b", [listen("zz", "2026-10-25T11:00:00Z")], {})
    assert s3.snap()["tracks"][1][1] == "Unknown track"


def test_rows_match_full_build_format():
    assert snapshot.listen_row({"played_at": "x", "duration_played_ms": 5.0, "activity_manual": "run"}, 3) == [
        "x", 3, 5, None, None, "run", None,
    ]


class RecentDynamo:
    """Answers the range query and track lookups refresh_recent makes; scanning fails the test."""

    def __init__(self, listens, tracks=()):
        self.listens = listens
        self.tracks = {t["track_id"]: t for t in tracks}
        self.ranges = []

    def get_listens_in_range(self, start, end):
        self.ranges.append((start, end))
        return [l for l in self.listens if start <= l["played_at"] <= end]

    def batch_get_tracks(self, ids):
        return {i: self.tracks[i] for i in ids if i in self.tracks}

    def scan_all_listens(self):
        raise AssertionError("refresh_recent must not scan")

    scan_all_tracks = scan_all_listens


NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
OLD = {
    "v": 1, "generated_at": "x", "newest_played_at": "2026-10-25T10:00:00Z",
    "tracks": [["t1", "One", "Album", ["A"], 200000, "2020"], ["t3", *snapshot.PLACEHOLDER]],
    "listens": [
        ["2020-01-01T00:00:00Z", 0, 200000, "old", None, None, None],
        ["2026-10-25T10:00:00Z", 0, 200000, "s1", "album", None, None],
    ],
}


def test_refresh_recent_rereads_only_the_recent_window():
    tagged = {**listen("t1", "2026-10-25T10:00:00Z"), "activity_manual": "running"}
    dynamo = RecentDynamo([tagged, listen("t2", "2026-10-26T09:00:00Z")], [T2])
    snap = snapshot.refresh_recent(dynamo, json.loads(json.dumps(OLD)), days=7, now=NOW)
    assert dynamo.ranges[0][0] == "2026-10-19T12:00:00"
    assert [r[0] for r in snap["listens"]] == [
        "2020-01-01T00:00:00Z", "2026-10-25T10:00:00Z", "2026-10-26T09:00:00Z",
    ]
    assert snap["listens"][0][3] == "old"  # outside the window: untouched
    assert snap["listens"][1][5] == "running"  # tag edit picked up
    assert snap["tracks"][snap["listens"][2][1]] == ["t2", "Two", "Album", ["A"], 180000, "2020"]
    assert snap["newest_played_at"] == "2026-10-26T09:00:00Z"
    assert snap["generated_at"] == NOW.isoformat()


def test_refresh_recent_fills_in_placeholder_tracks():
    t3 = {**T2, "track_id": "t3", "name": "Three"}
    dynamo = RecentDynamo([listen("t3", "2026-10-26T09:00:00Z")], [t3])
    snap = snapshot.refresh_recent(dynamo, json.loads(json.dumps(OLD)), days=7, now=NOW)
    assert snap["tracks"][1][:2] == ["t3", "Three"]
    assert len(snap["tracks"]) == 2


def test_refresh_falls_back_to_full_build_without_a_snapshot():
    s3 = FakeS3()

    class Full(RecentDynamo):
        def scan_all_tracks(self):
            return [T2]

        def scan_all_listens(self):
            return [listen("t2", "2026-10-26T09:00:00Z")]

    snap = snapshot.refresh(s3, "b", Full([]))
    assert s3.snap()["listens"] == snap["listens"] == [["2026-10-26T09:00:00Z", 0, 180000, "s2", "playlist", None, None]]


def test_refresh_writes_conditionally_and_retries_on_conflict():
    s3 = FakeS3(OLD, conflicts=1)
    snapshot.refresh(s3, "b", RecentDynamo([listen("t2", "2026-10-26T09:00:00Z")], [T2]), now=NOW)
    assert s3.puts == 1
    assert s3.last_put["IfMatch"] == '"v1"'
    assert len(s3.snap()["listens"]) == 2  # the old 2026-10-25 row left the window and isn't in dynamo
