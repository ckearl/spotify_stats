"""
Spotify API client with automatic token refresh and retry logic.
"""

import time
from typing import Any

import requests

from config import (
    MAX_RETRIES,
    RETRY_BASE_DELAY_S,
    SPOTIFY_API_BASE,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REFRESH_TOKEN,
    SPOTIFY_TOKEN_URL,
)
from utils.logger import get_logger

log = get_logger(__name__)


class SpotifyAuthError(Exception):
    """Raised when Spotify authentication fails (invalid/expired refresh token)."""


class SpotifyAPIError(Exception):
    """Raised when a Spotify API call fails after retries."""


class SpotifyClient:
    """Wrapper around the Spotify Web API with token management and retry."""

    def __init__(
        self,
        client_id: str = SPOTIFY_CLIENT_ID,
        client_secret: str = SPOTIFY_CLIENT_SECRET,
        refresh_token: str = SPOTIFY_REFRESH_TOKEN,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _refresh_access_token(self) -> None:
        """Exchange refresh token for a new access token."""
        log.debug("Refreshing Spotify access token")
        resp = requests.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            auth=(self._client_id, self._client_secret),
            timeout=10,
        )

        if resp.status_code != 200:
            log.error("Token refresh failed: status=%d", resp.status_code)
            raise SpotifyAuthError(
                f"Token refresh failed with status {resp.status_code}"
            )

        data = resp.json()
        self._access_token = data["access_token"]
        log.debug("Access token refreshed successfully")

    def _ensure_token(self) -> None:
        if self._access_token is None:
            self._refresh_access_token()

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    # ------------------------------------------------------------------
    # Retry helper
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Make an HTTP request with retry and token refresh on 401."""
        self._ensure_token()

        kwargs.setdefault("timeout", 15)

        for attempt in range(MAX_RETRIES):
            resp = requests.request(
                method, url, headers=self._headers, **kwargs
            )

            # Token expired mid-invocation — refresh once and retry
            if resp.status_code == 401 and attempt == 0:
                log.warning("Got 401, refreshing token and retrying")
                self._refresh_access_token()
                continue

            # Rate limited
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 2))
                log.warning("Rate limited, waiting %ds", retry_after)
                time.sleep(retry_after)
                continue

            # Success or client error (not retryable)
            if resp.status_code < 500:
                return resp

            # Server error — retry with backoff
            delay = RETRY_BASE_DELAY_S * (2 ** attempt)
            log.warning(
                "Server error %d on attempt %d, retrying in %ds",
                resp.status_code,
                attempt + 1,
                delay,
            )
            time.sleep(delay)

        raise SpotifyAPIError(
            f"{method} {url} failed after {MAX_RETRIES} retries "
            f"(last status={resp.status_code})"
        )

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    def get_recently_played(self, limit: int = 50) -> list[dict]:
        """Fetch recently played tracks.

        Returns list of play-history objects, each containing:
          - track: full track object
          - played_at: ISO 8601 timestamp
          - context: context object (nullable)
        """
        resp = self._request(
            "GET",
            f"{SPOTIFY_API_BASE}/me/player/recently-played",
            params={"limit": limit},
        )
        if resp.status_code != 200:
            log.error("get_recently_played failed: %d", resp.status_code)
            return []

        data = resp.json()
        items = data.get("items", [])
        log.info("Fetched %d recently played tracks", len(items))
        return items

    def get_liked_songs(self, limit: int = 50, offset: int = 0) -> dict:
        """Fetch a page of the user's liked/saved tracks.

        Args:
            limit: Number of tracks per page (max 50).
            offset: Starting index.

        Returns:
            Full response dict with 'items', 'total', 'next', etc.
        """
        resp = self._request(
            "GET",
            f"{SPOTIFY_API_BASE}/me/tracks",
            params={"limit": limit, "offset": offset},
        )
        if resp.status_code != 200:
            log.error("get_liked_songs failed: %d", resp.status_code)
            return {"items": [], "total": 0, "next": None}

        return resp.json()

    def get_currently_playing(self) -> dict | None:
        """Fetch currently playing track and device info.

        Returns the full response dict, or None if nothing is playing.
        """
        resp = self._request(
            "GET",
            f"{SPOTIFY_API_BASE}/me/player/currently-playing",
        )

        # 204 = nothing playing, 200 with empty body = nothing playing
        if resp.status_code == 204 or not resp.content:
            log.debug("Nothing currently playing")
            return None

        if resp.status_code != 200:
            log.error("get_currently_playing failed: %d", resp.status_code)
            return None

        data = resp.json()
        if not data.get("is_playing"):
            log.debug("Playback paused")
            return None

        return data

    def get_playback_state(self) -> dict | None:
        """Fetch the full player state, including the active device.

        Unlike currently-playing, /me/player includes `device`, and it sees
        librespot clients (spotify_player, spotifyd, Spotifast) whose plays
        never reach recently-played. Returns None when nothing is active.
        """
        resp = self._request("GET", f"{SPOTIFY_API_BASE}/me/player")
        if resp.status_code == 204 or not resp.content:
            return None
        if resp.status_code != 200:
            log.error("get_playback_state failed: %d", resp.status_code)
            return None
        return resp.json()

    def get_artists(self, artist_ids: list[str]) -> list[dict]:
        """Fetch full artist objects for a list of artist IDs.

        Handles batching (max 50 per request).
        Returns list of artist objects (skips None entries for invalid IDs).
        """
        artists: list[dict] = []
        for i in range(0, len(artist_ids), 50):
            batch = artist_ids[i : i + 50]
            resp = self._request(
                "GET",
                f"{SPOTIFY_API_BASE}/artists",
                params={"ids": ",".join(batch)},
            )
            if resp.status_code != 200:
                log.error("get_artists batch failed: %d", resp.status_code)
                continue
            for a in resp.json().get("artists", []):
                if a is not None:
                    artists.append(a)

        log.info("Fetched metadata for %d artists", len(artists))
        return artists

    def get_tracks(self, track_ids: list[str]) -> list[dict]:
        """Fetch full track objects for a list of track IDs.

        Handles batching (max 50 per request).
        Returns list of track objects (skips None entries for invalid IDs).
        """
        tracks: list[dict] = []
        for i in range(0, len(track_ids), 50):
            batch = track_ids[i : i + 50]
            resp = self._request(
                "GET",
                f"{SPOTIFY_API_BASE}/tracks",
                params={"ids": ",".join(batch)},
            )
            if resp.status_code != 200:
                log.error("get_tracks batch failed: %d", resp.status_code)
                continue
            for t in resp.json().get("tracks", []):
                if t is not None:
                    tracks.append(t)

        log.info("Fetched metadata for %d tracks", len(tracks))
        return tracks

    def get_audio_features(self, track_ids: list[str]) -> dict[str, dict]:
        """Fetch audio features for a list of track IDs.

        Handles batching (max 100 per request).
        Returns dict mapping track_id -> audio features dict.
        Gracefully handles tracks without audio features (returns empty dict for those).
        """
        features: dict[str, dict] = {}
        for i in range(0, len(track_ids), 100):
            batch = track_ids[i : i + 100]
            resp = self._request(
                "GET",
                f"{SPOTIFY_API_BASE}/audio-features",
                params={"ids": ",".join(batch)},
            )
            if resp.status_code == 403:
                log.warning(
                    "Audio features endpoint returned 403 — may be deprecated "
                    "for this app. Skipping audio features."
                )
                return features
            if resp.status_code != 200:
                log.error(
                    "get_audio_features batch failed: %d", resp.status_code
                )
                continue
            for af in resp.json().get("audio_features", []):
                if af is not None:
                    features[af["id"]] = {
                        "danceability": af.get("danceability"),
                        "energy": af.get("energy"),
                        "valence": af.get("valence"),
                        "tempo": af.get("tempo"),
                        "acousticness": af.get("acousticness"),
                        "instrumentalness": af.get("instrumentalness"),
                        "liveness": af.get("liveness"),
                        "speechiness": af.get("speechiness"),
                        "loudness": af.get("loudness"),
                        "key": af.get("key"),
                        "mode": af.get("mode"),
                        "time_signature": af.get("time_signature"),
                    }

        log.info("Fetched audio features for %d tracks", len(features))
        return features
