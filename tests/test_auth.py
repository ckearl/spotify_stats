import hashlib
import importlib
import json

import pytest

from utils import auth

PASSWORD = "not-the-real-password"
HASH = hashlib.sha256(PASSWORD.encode()).hexdigest()

API_HANDLERS = [
    ("lambda_handlers.sync_handler", "POST"),
    ("lambda_handlers.tag_listen_handler", "POST"),
    ("lambda_handlers.tag_session_handler", "POST"),
    ("lambda_handlers.tag_range_handler", "POST"),
    ("lambda_handlers.map_device_handler", "POST"),
    ("lambda_handlers.stats_handler", "GET"),
    ("lambda_handlers.iphone_widget_handler", "GET"),
    ("lambda_handlers.dashboard_data_handler", "GET"),
]


@pytest.fixture(autouse=True)
def password_hash(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", HASH)
    monkeypatch.setenv("DASHBOARD_BUCKET", "bucket")


def api_event(method="GET", password=None, header="X-Dashboard-Password"):
    return {"httpMethod": method, "headers": {header: password} if password else {}, "body": "{}"}


def test_checks_the_password_header_case_insensitively():
    assert auth.authorized(api_event(password=PASSWORD))
    assert auth.authorized(api_event(password=PASSWORD, header="x-dashboard-password"))
    assert not auth.authorized(api_event(password="wrong"))
    assert not auth.authorized(api_event())
    assert not auth.authorized({"httpMethod": "GET", "headers": None})


def test_fails_closed_without_a_configured_hash(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD_HASH")
    assert not auth.authorized(api_event(password=PASSWORD))


@pytest.mark.parametrize("module,method", API_HANDLERS)
def test_every_api_endpoint_rejects_requests_without_the_password(module, method):
    handler = importlib.import_module(module).handler
    for event in (api_event(method), api_event(method, password="wrong")):
        resp = handler(event, None)
        assert resp["statusCode"] == 401
        assert json.loads(resp["body"]) == {"error": "unauthorized"}


def test_scheduled_sync_needs_no_password(monkeypatch):
    from lambda_handlers import sync_handler

    class NoPlays:
        def get_recently_played(self, limit):
            return []

    monkeypatch.setattr(sync_handler, "SpotifyClient", NoPlays)
    monkeypatch.setattr(sync_handler, "DynamoClient", lambda: None)
    resp = sync_handler.handler({"source": "aws.events"}, None)
    assert resp["statusCode"] == 200
