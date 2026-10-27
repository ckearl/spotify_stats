"""
Get a fresh Spotify refresh token via the OAuth authorization-code flow.

Opens the Spotify consent page in your browser, catches the redirect on
http://127.0.0.1:7777/callback, exchanges the code, and writes the new
SPOTIFY_REFRESH_TOKEN into .env. The token itself is never printed.

Usage:
    python scripts/get_refresh_token.py
"""

import os
import re
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_TOKEN_URL  # noqa: E402

REDIRECT_URI = "http://127.0.0.1:7777/callback"
SCOPES = " ".join([
    "user-read-recently-played",
    "user-read-currently-playing",
    "user-read-playback-state",
    "user-library-read",  # needed by scripts/backfill_liked_songs.py
])
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        _CallbackHandler.result = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Done - you can close this tab and return to the terminal.")

    def log_message(self, *args):
        pass


def _write_env(token: str) -> None:
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    line = f"SPOTIFY_REFRESH_TOKEN={token}"
    if re.search(r"^SPOTIFY_REFRESH_TOKEN=.*$", text, flags=re.M):
        text = re.sub(r"^SPOTIFY_REFRESH_TOKEN=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    ENV_PATH.write_text(text)
    os.chmod(ENV_PATH, 0o600)


def main() -> None:
    state = secrets.token_urlsafe(16)
    auth_url = "https://accounts.spotify.com/authorize?" + urlencode({
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    })

    server = HTTPServer(("127.0.0.1", 7777), _CallbackHandler)
    print("Opening Spotify authorization in your browser...")
    print(f"If it doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    while not _CallbackHandler.result:
        server.handle_request()
    server.server_close()

    result = _CallbackHandler.result
    if result.get("state") != state:
        sys.exit("State mismatch - aborting.")
    if "error" in result:
        sys.exit(f"Authorization failed: {result['error']}")

    resp = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
        },
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=10,
    )
    if resp.status_code != 200:
        sys.exit(f"Token exchange failed: {resp.status_code} {resp.text}")

    _write_env(resp.json()["refresh_token"])
    print(f"New refresh token saved to {ENV_PATH}")


if __name__ == "__main__":
    main()
