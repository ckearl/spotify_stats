"""
DynamoDB client for all table operations.

Handles CRUD for Tracks, Listens, DeviceHistory, and DeviceMappings tables.
"""

import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from config import (
    DEVICE_HISTORY_TTL_SECONDS,
    DYNAMODB_REGION,
    GSI_PLAYED_AT,
    GSI_SESSION,
    GSI_TRACK_PLAYED_AT,
    MAX_RETRIES,
    PLAYED_AT_PARTITION_KEY,
    RETRY_BASE_DELAY_S,
    TABLE_DEVICE_HISTORY,
    TABLE_DEVICE_MAPPINGS,
    TABLE_LISTENS,
    TABLE_PLAYBACK_STATE,
    TABLE_TRACKS,
)
from utils.logger import get_logger

log = get_logger(__name__)


def _float_to_decimal(obj: Any) -> Any:
    """Recursively convert floats to Decimals for DynamoDB compatibility."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _float_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_float_to_decimal(i) for i in obj]
    return obj


def _decimal_to_float(obj: Any) -> Any:
    """Recursively convert Decimals back to floats for JSON compatibility."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_float(i) for i in obj]
    return obj


class DynamoClient:
    """Provides operations for all four DynamoDB tables."""

    def __init__(self):
        self._resource = boto3.resource("dynamodb", region_name=DYNAMODB_REGION)
        self._tracks = self._resource.Table(TABLE_TRACKS)
        self._listens = self._resource.Table(TABLE_LISTENS)
        self._device_history = self._resource.Table(TABLE_DEVICE_HISTORY)
        self._device_mappings = self._resource.Table(TABLE_DEVICE_MAPPINGS)
        self._playback_state = self._resource.Table(TABLE_PLAYBACK_STATE)

    # ------------------------------------------------------------------
    # Retry helper for throttling
    # ------------------------------------------------------------------

    @staticmethod
    def _retry_on_throttle(func, *args, **kwargs):
        """Retry a DynamoDB operation with exponential backoff on throttling."""
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code == "ProvisionedThroughputExceededException":
                    delay = RETRY_BASE_DELAY_S * (2 ** attempt)
                    log.warning("DynamoDB throttled, retrying in %ds", delay)
                    time.sleep(delay)
                else:
                    raise
        # Final attempt without catching
        return func(*args, **kwargs)

    # ==================================================================
    # Tracks table
    # ==================================================================

    def get_track(self, track_id: str) -> dict | None:
        """Get a single track by ID. Returns None if not found."""
        resp = self._retry_on_throttle(
            self._tracks.get_item, Key={"track_id": track_id}
        )
        item = resp.get("Item")
        return _decimal_to_float(item) if item else None

    def batch_get_tracks(self, track_ids: list[str]) -> dict[str, dict]:
        """Get multiple tracks by ID. Returns dict of track_id -> track data."""
        result: dict[str, dict] = {}
        # DynamoDB batch_get_item supports max 100 keys
        for i in range(0, len(track_ids), 100):
            batch = track_ids[i : i + 100]
            keys = [{"track_id": tid} for tid in batch]
            resp = self._retry_on_throttle(
                self._resource.batch_get_item,
                RequestItems={TABLE_TRACKS: {"Keys": keys}},
            )
            for item in resp.get("Responses", {}).get(TABLE_TRACKS, []):
                item = _decimal_to_float(item)
                result[item["track_id"]] = item

            # Handle unprocessed keys
            unprocessed = resp.get("UnprocessedKeys", {}).get(TABLE_TRACKS)
            if unprocessed:
                log.warning(
                    "Unprocessed keys in batch_get_tracks: %d",
                    len(unprocessed.get("Keys", [])),
                )

        return result

    def update_track_genres(self, track_id: str, genres: list[str]) -> None:
        """Update the genres field on a track."""
        self._retry_on_throttle(
            self._tracks.update_item,
            Key={"track_id": track_id},
            UpdateExpression="SET genres = :g",
            ExpressionAttributeValues={":g": genres},
        )

    def scan_all_tracks(self) -> list[dict]:
        """Scan the entire Tracks table. Use sparingly."""
        items = []
        resp = self._retry_on_throttle(self._tracks.scan)
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = self._retry_on_throttle(
                self._tracks.scan,
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
        return [_decimal_to_float(item) for item in items]

    def put_track(self, track_data: dict) -> None:
        """Insert a single track into the Tracks table."""
        item = _float_to_decimal(track_data)
        self._retry_on_throttle(self._tracks.put_item, Item=item)

    def batch_put_tracks(self, tracks: list[dict]) -> None:
        """Batch write tracks to the Tracks table (25 per batch)."""
        for i in range(0, len(tracks), 25):
            batch = tracks[i : i + 25]
            with self._tracks.batch_writer() as writer:
                for track in batch:
                    writer.put_item(Item=_float_to_decimal(track))
        log.info("Batch wrote %d tracks", len(tracks))

    # ==================================================================
    # Listens table
    # ==================================================================

    def check_listen_exists(self, track_id: str, played_at: str) -> bool:
        """Check if a listen already exists using the TrackPlayedAtIndex GSI."""
        resp = self._retry_on_throttle(
            self._listens.query,
            IndexName=GSI_TRACK_PLAYED_AT,
            KeyConditionExpression="track_id = :tid AND played_at = :pa",
            ExpressionAttributeValues={
                ":tid": track_id,
                ":pa": played_at,
            },
            Limit=1,
            Select="COUNT",
        )
        return resp.get("Count", 0) > 0

    def batch_check_listens_exist(
        self, pairs: list[tuple[str, str]]
    ) -> set[tuple[str, str]]:
        """Check which (track_id, played_at) pairs already exist.

        Returns set of existing pairs.
        """
        existing: set[tuple[str, str]] = set()
        for track_id, played_at in pairs:
            if self.check_listen_exists(track_id, played_at):
                existing.add((track_id, played_at))
        return existing

    def listen_exists_near(self, track_id: str, played_at: str, window_s: int = 90) -> bool:
        """Check for a listen of this track within window_s of played_at.

        The playback poller and the recently-played sync can both see the same
        play with slightly different timestamps; this catches that overlap.
        """
        from datetime import timedelta

        from dateutil.parser import isoparse

        ts = isoparse(played_at)
        # Stored played_at strings mix "Z" and "+00:00" suffixes; normalize the bounds.
        fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S")
        resp = self._retry_on_throttle(
            self._listens.query,
            IndexName=GSI_TRACK_PLAYED_AT,
            KeyConditionExpression="track_id = :tid AND played_at BETWEEN :lo AND :hi",
            ExpressionAttributeValues={
                ":tid": track_id,
                ":lo": fmt(ts - timedelta(seconds=window_s)),
                ":hi": fmt(ts + timedelta(seconds=window_s)) + "~",
            },
            Limit=1,
            Select="COUNT",
        )
        return resp.get("Count", 0) > 0

    def put_listen(self, listen_data: dict) -> None:
        """Insert a single listen. Generates listen_id if not present."""
        if "listen_id" not in listen_data:
            listen_data["listen_id"] = str(uuid.uuid4())
        # Add fixed partition key for PlayedAtIndex GSI
        listen_data["partition_key"] = PLAYED_AT_PARTITION_KEY
        item = _float_to_decimal(listen_data)
        self._retry_on_throttle(self._listens.put_item, Item=item)

    def batch_put_listens(self, listens: list[dict]) -> None:
        """Batch write listens to the Listens table (25 per batch)."""
        for listen in listens:
            if "listen_id" not in listen:
                listen["listen_id"] = str(uuid.uuid4())
            listen["partition_key"] = PLAYED_AT_PARTITION_KEY

        for i in range(0, len(listens), 25):
            batch = listens[i : i + 25]
            with self._listens.batch_writer() as writer:
                for listen in batch:
                    writer.put_item(Item=_float_to_decimal(listen))
        log.info("Batch wrote %d listens", len(listens))

    def get_listens_by_session(self, session_id: str) -> list[dict]:
        """Get all listens in a session using the SessionIndex GSI."""
        items: list[dict] = []
        resp = self._retry_on_throttle(
            self._listens.query,
            IndexName=GSI_SESSION,
            KeyConditionExpression="session_id = :sid",
            ExpressionAttributeValues={":sid": session_id},
        )
        items.extend(resp.get("Items", []))

        # Handle pagination
        while "LastEvaluatedKey" in resp:
            resp = self._retry_on_throttle(
                self._listens.query,
                IndexName=GSI_SESSION,
                KeyConditionExpression="session_id = :sid",
                ExpressionAttributeValues={":sid": session_id},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))

        return [_decimal_to_float(item) for item in items]

    def get_listens_in_range(
        self, start_time: str, end_time: str
    ) -> list[dict]:
        """Get all listens in a time range using the PlayedAtIndex GSI."""
        items: list[dict] = []
        resp = self._retry_on_throttle(
            self._listens.query,
            IndexName=GSI_PLAYED_AT,
            KeyConditionExpression=(
                "partition_key = :pk AND played_at BETWEEN :start AND :end"
            ),
            ExpressionAttributeValues={
                ":pk": PLAYED_AT_PARTITION_KEY,
                ":start": start_time,
                ":end": end_time,
            },
        )
        items.extend(resp.get("Items", []))

        while "LastEvaluatedKey" in resp:
            resp = self._retry_on_throttle(
                self._listens.query,
                IndexName=GSI_PLAYED_AT,
                KeyConditionExpression=(
                    "partition_key = :pk AND played_at BETWEEN :start AND :end"
                ),
                ExpressionAttributeValues={
                    ":pk": PLAYED_AT_PARTITION_KEY,
                    ":start": start_time,
                    ":end": end_time,
                },
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))

        return [_decimal_to_float(item) for item in items]

    def scan_all_listens(self) -> list[dict]:
        """Scan the entire Listens table. Use sparingly."""
        items = []
        resp = self._retry_on_throttle(self._listens.scan)
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = self._retry_on_throttle(
                self._listens.scan,
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
        return [_decimal_to_float(item) for item in items]

    def get_recent_listens(self, limit: int = 10) -> list[dict]:
        """Get the most recent listens (for cross-sync session continuity).

        Queries PlayedAtIndex GSI in reverse order.
        """
        resp = self._retry_on_throttle(
            self._listens.query,
            IndexName=GSI_PLAYED_AT,
            KeyConditionExpression="partition_key = :pk",
            ExpressionAttributeValues={":pk": PLAYED_AT_PARTITION_KEY},
            ScanIndexForward=False,
            Limit=limit,
        )
        items = resp.get("Items", [])
        return [_decimal_to_float(item) for item in items]

    def update_listen_duration(self, listen_id: str, duration_played_ms: int) -> None:
        """Set a listen's duration_played_ms (e.g. to the export's real listening time)."""
        self._retry_on_throttle(
            self._listens.update_item,
            Key={"listen_id": listen_id},
            UpdateExpression="SET duration_played_ms = :d",
            ExpressionAttributeValues={":d": int(duration_played_ms)},
            ConditionExpression="attribute_exists(listen_id)",
        )

    def update_listen_tags(self, listen_id: str, updates: dict) -> bool:
        """Update manual tag fields on a listen.

        Args:
            listen_id: The listen to update.
            updates: Dict of field names to new values. Supported fields:
                activity_manual, location_manual, device_custom_name.

        Returns:
            True if updated, False if listen_id not found.
        """
        allowed_fields = {"activity_manual", "location_manual", "device_custom_name"}
        filtered = {k: v for k, v in updates.items() if k in allowed_fields and v is not None}
        if not filtered:
            return False

        expr_parts = []
        attr_names = {}
        attr_values = {}
        for i, (key, val) in enumerate(filtered.items()):
            placeholder = f"#f{i}"
            value_placeholder = f":v{i}"
            expr_parts.append(f"{placeholder} = {value_placeholder}")
            attr_names[placeholder] = key
            attr_values[value_placeholder] = val

        update_expr = "SET " + ", ".join(expr_parts)

        try:
            self._retry_on_throttle(
                self._listens.update_item,
                Key={"listen_id": listen_id},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=attr_names,
                ExpressionAttributeValues=_float_to_decimal(attr_values),
                ConditionExpression="attribute_exists(listen_id)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    # ==================================================================
    # DeviceHistory table
    # ==================================================================

    def put_device_history(self, record: dict) -> None:
        """Store a device polling record with TTL.

        DeviceHistory table uses:
          PK: device_key = "DEVICE" (fixed, enables range queries on SK)
          SK: timestamp (ISO 8601 UTC)
        """
        now = datetime.now(timezone.utc)
        record.setdefault("timestamp", now.isoformat())
        record["device_key"] = "DEVICE"
        record["ttl"] = int(now.timestamp()) + DEVICE_HISTORY_TTL_SECONDS
        self._retry_on_throttle(
            self._device_history.put_item, Item=_float_to_decimal(record)
        )
        log.debug("Stored device history: %s", record.get("device_spotify_name"))

    def get_recent_device(
        self, before_timestamp: str, buffer_minutes: int = 15
    ) -> dict | None:
        """Find the most recent device poll within buffer_minutes before a timestamp.

        Args:
            before_timestamp: ISO 8601 UTC timestamp.
            buffer_minutes: How far back to look.

        Returns:
            Device record dict or None.
        """
        from datetime import timedelta

        from dateutil.parser import isoparse

        ts = isoparse(before_timestamp)
        start = (ts - timedelta(minutes=buffer_minutes)).isoformat()

        resp = self._retry_on_throttle(
            self._device_history.query,
            KeyConditionExpression=(
                "device_key = :dk AND #ts BETWEEN :start AND :end_ts"
            ),
            ExpressionAttributeNames={"#ts": "timestamp"},
            ExpressionAttributeValues={
                ":dk": "DEVICE",
                ":start": start,
                ":end_ts": before_timestamp,
            },
            ScanIndexForward=False,
            Limit=1,
        )

        items = resp.get("Items", [])
        if not items:
            return None
        return _decimal_to_float(items[0])

    # ==================================================================
    # PlaybackState table (one item: what the poller is currently tracking)
    # ==================================================================

    def get_playback_state(self) -> dict | None:
        resp = self._retry_on_throttle(self._playback_state.get_item, Key={"id": "current"})
        item = resp.get("Item")
        if not item:
            return None
        item = _decimal_to_float(item)
        item.pop("id", None)
        for key in ("duration_ms", "last_progress_ms", "listened_ms"):
            item[key] = int(item.get(key) or 0)
        return item

    def save_playback_state(self, state: dict | None) -> None:
        if state is None:
            self._retry_on_throttle(self._playback_state.delete_item, Key={"id": "current"})
            return
        self._retry_on_throttle(
            self._playback_state.put_item, Item=_float_to_decimal({**state, "id": "current"})
        )

    # ==================================================================
    # DeviceMappings table
    # ==================================================================

    def get_device_mapping(self, spotify_device_name: str) -> str | None:
        """Look up custom name for a Spotify device. Returns None if unmapped."""
        resp = self._retry_on_throttle(
            self._device_mappings.get_item,
            Key={"spotify_device_name": spotify_device_name},
        )
        item = resp.get("Item")
        return item.get("custom_name") if item else None

    def put_device_mapping(
        self, spotify_device_name: str, custom_name: str
    ) -> None:
        """Create or update a device name mapping."""
        self._retry_on_throttle(
            self._device_mappings.put_item,
            Item={
                "spotify_device_name": spotify_device_name,
                "custom_name": custom_name,
            },
        )
        log.info(
            "Device mapping saved: %s -> %s", spotify_device_name, custom_name
        )
