import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from lambda_handlers import dashboard_data_handler as h

PASSWORD = "not-the-real-password"
PASSWORD_HASH = hashlib.sha256(PASSWORD.encode()).hexdigest()

TRACKS = [
    {"track_id": "t1", "name": "Dancing Days - Remaster", "album": "Houses of the Holy (Remaster)",
     "artists": ["Led Zeppelin"], "duration_ms": 223000.0, "release_date": "1973-03-28"},
    {"track_id": "t2", "name": "Never Played", "album": "Liked", "artists": ["Someone"],
     "duration_ms": 100000.0},
]
LISTENS = [
    {"listen_id": "a", "track_id": "t1", "played_at": "2026-06-01T10:00:00Z",
     "duration_played_ms": 223000.0, "session_id": "s1", "context_type": "album",
     "activity_inferred": True},
    {"listen_id": "b", "track_id": "gone", "played_at": "2026-06-02T10:00:00Z",
     "duration_played_ms": 1000.0, "activity_manual": "running"},
]


class FakeDynamo:
    scans = 0

    def scan_all_tracks(self):
        FakeDynamo.scans += 1
        return TRACKS

    def scan_all_listens(self):
        FakeDynamo.scans += 1
        return LISTENS

    def get_recent_listens(self, limit=10):
        return [LISTENS[-1]]

    def get_listens_in_range(self, start, end):
        return [l for l in LISTENS if start <= l["played_at"] <= end]

    def batch_get_tracks(self, ids):
        return {t["track_id"]: t for t in TRACKS if t["track_id"] in ids}


class FakeS3:
    def __init__(self, head=None):
        self.head = head
        self.body = None
        self.puts = []

    def get_object(self, Bucket, Key):
        if self.body is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        import io
        return {"Body": io.BytesIO(self.body), "ETag": '"e1"'}

    def head_object(self, Bucket, Key):
        if self.head is None:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"Metadata": self.head}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://s3.example/{Params['Key']}?exp={ExpiresIn}"


@pytest.fixture
def s3(monkeypatch):
    FakeDynamo.scans = 0
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.setenv("DASHBOARD_BUCKET", "bucket")
    monkeypatch.setattr(h, "DynamoClient", FakeDynamo)
    fake = FakeS3()
    monkeypatch.setattr(h, "s3", fake)
    return fake


def _event(password=PASSWORD, header="X-Dashboard-Password", query=None):
    headers = {header: password} if password is not None else {}
    return {"headers": headers, "queryStringParameters": query}


def _snapshot(fake):
    return json.loads(gzip.decompress(fake.puts[-1]["Body"]))


def test_rejects_missing_and_wrong_password(s3):
    assert h.handler(_event(password=None), None)["statusCode"] == 401
    assert h.handler(_event(password="nope"), None)["statusCode"] == 401
    assert s3.puts == []


def test_header_lookup_is_case_insensitive(s3):
    resp = h.handler(_event(header="x-dashboard-password"), None)
    assert resp["statusCode"] == 200


def test_builds_snapshot_when_missing(s3):
    resp = h.handler(_event(), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["url"].startswith("https://s3.example/data/snapshot.json")
    assert len(s3.puts) == 1
    assert s3.puts[0]["ContentEncoding"] == "gzip"
    snap = _snapshot(s3)
    assert snap["v"] == 1
    assert snap["newest_played_at"] == "2026-06-02T10:00:00Z"
    assert [t[0] for t in snap["tracks"]][:2] == ["t1", "t2"]
    assert snap["tracks"][0] == ["t1", "Dancing Days - Remaster", "Houses of the Holy (Remaster)",
                                 ["Led Zeppelin"], 223000, "1973-03-28"]
    first = snap["listens"][0]
    assert first == ["2026-06-01T10:00:00Z", 0, 223000, "s1", "album", None, None]


def test_serves_an_existing_snapshot_without_touching_dynamo(s3):
    # Old or not, an existing snapshot is served as-is: writers keep it current.
    s3.head = {
        "newest-played-at": "2026-01-01T00:00:00Z",
        "generated-at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
    }
    resp = h.handler(_event(), None)
    assert resp["statusCode"] == 200
    assert s3.puts == []
    assert FakeDynamo.scans == 0


def test_refresh_param_forces_rebuild(s3):
    s3.head = {"newest-played-at": "x", "generated-at": datetime.now(timezone.utc).isoformat()}
    h.handler(_event(query={"refresh": "1"}), None)
    assert len(s3.puts) == 1


def test_rebuild_of_an_existing_snapshot_refreshes_recent_days_without_scanning(s3):
    s3.head = {"newest-played-at": "x", "generated-at": datetime.now(timezone.utc).isoformat()}
    s3.body = gzip.compress(json.dumps({
        "v": 1, "generated_at": "x", "newest_played_at": "2020-01-01T00:00:00Z",
        "tracks": [["t1", "One", "Album", ["A"], 1, None]],
        "listens": [["2020-01-01T00:00:00Z", 0, 1, "s0", None, None, None]],
    }).encode())
    assert h.handler({"rebuild": True}, None)["statusCode"] == 200
    assert FakeDynamo.scans == 0
    assert s3.puts[0]["IfMatch"] == '"e1"'
    assert _snapshot(s3)["listens"][0][3] == "s0"


def test_scheduled_rebuild_needs_no_password(s3):
    s3.head = {"newest-played-at": "x", "generated-at": datetime.now(timezone.utc).isoformat()}
    resp = h.handler({"rebuild": True}, None)
    assert resp["statusCode"] == 200
    assert len(s3.puts) == 1


def test_rebuild_flag_in_an_api_request_is_ignored(s3):
    s3.head = {"newest-played-at": "x", "generated-at": datetime.now(timezone.utc).isoformat()}
    event = _event(password=None)
    event["queryStringParameters"] = {"rebuild": "true"}
    assert h.handler(event, None)["statusCode"] == 401
    assert s3.puts == []


def test_missing_track_gets_placeholder(s3):
    h.handler(_event(), None)
    snap = _snapshot(s3)
    listen = snap["listens"][1]
    track = snap["tracks"][listen[1]]
    assert track[0] == "gone"
    assert track[1] == "Unknown track"
    assert listen[5] == "running"


def test_presigned_url_is_regional_sigv4(monkeypatch):
    # A new bucket's global endpoint 307-redirects, which breaks the browser fetch.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    url = h.s3.generate_presigned_url(
        "get_object", Params={"Bucket": "bucket", "Key": h.SNAPSHOT_KEY}, ExpiresIn=60
    )
    assert url.startswith("https://bucket.s3.us-west-2.amazonaws.com/")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
