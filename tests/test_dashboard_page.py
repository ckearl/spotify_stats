import io

import pytest
from botocore.exceptions import ClientError

from lambda_handlers import dashboard_page_handler as h


class FakeS3:
    def __init__(self, body=None):
        self.body = body
        self.keys = []

    def get_object(self, Bucket, Key):
        self.keys.append(Key)
        if self.body is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.body)}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_BUCKET", "bucket")


def test_serves_index_html_for_any_dashboard_path(monkeypatch):
    fake = FakeS3(b"<!doctype html><title>x</title>")
    monkeypatch.setattr(h, "s3", fake)
    resp = h.handler({"path": "/dashboard/artist/led-zeppelin"}, None)
    assert resp["statusCode"] == 200
    assert resp["headers"]["Content-Type"] == "text/html; charset=utf-8"
    assert resp["headers"]["Cache-Control"] == "no-cache"
    assert resp["body"].startswith("<!doctype html>")
    assert fake.keys == ["site/index.html"]


def test_returns_503_when_not_deployed(monkeypatch):
    monkeypatch.setattr(h, "s3", FakeS3(None))
    resp = h.handler({"path": "/dashboard"}, None)
    assert resp["statusCode"] == 503
