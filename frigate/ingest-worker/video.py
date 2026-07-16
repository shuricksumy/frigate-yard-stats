import os
from datetime import datetime, timezone

import requests

import config


class ClipNotReadyError(Exception):
    """Raised when Frigate's clip endpoint returns a too-small/placeholder response -- the
    recording segment likely isn't finalized yet. Callers should treat this the same as any
    other retryable failure (video_status -> 'retry', capped by VIDEO_MAX_ATTEMPTS)."""


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def build_clip_url(row: dict) -> str:
    # Same -5s/+5s padding around the event window as the n8n workflow's "Extract & Filter" code
    # node (clipUrl), against Frigate's own REST clip endpoint (not the Frigate-event-id endpoint
    # crop.py uses -- this one takes a camera + epoch-second start/end window directly).
    start_ts = int(_as_datetime(row["start_ts"]).timestamp()) - 5
    end_ts = int(_as_datetime(row["end_ts"]).timestamp()) + 5
    camera = row["camera"]
    return f"{config.FRIGATE_API_BASE}/api/{camera}/start/{start_ts}/end/{end_ts}/clip.mp4"


def download_clip(row: dict) -> bytes:
    url = build_clip_url(row)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    content = resp.content
    if len(content) <= config.VIDEO_MIN_VALID_BYTES:
        raise ClipNotReadyError(
            f"Clip response too small ({len(content)} bytes <= {config.VIDEO_MIN_VALID_BYTES}) "
            f"for det_id={row.get('det_id')} -- likely not finalized yet"
        )
    return content


def _primary_object_type(row: dict) -> str:
    objects = (row.get("objects") or "").strip()
    if not objects:
        return "event"
    # `objects` is a free-text, possibly comma-joined label string (see mqtt_ingest.parse_payload
    # -- it's actually a single Frigate label per row today, but be defensive either way) --
    # take the first token for the filename.
    return objects.split(",")[0].strip() or "event"


def store_clip(row: dict, content: bytes) -> str:
    start = _as_datetime(row["start_ts"])
    day_dir = os.path.join(
        config.VIDEO_STORAGE_PATH,
        f"{start:%Y}", f"{start:%m}", f"{start:%d}",
    )
    os.makedirs(day_dir, exist_ok=True)

    object_type = _primary_object_type(row)
    filename = f"{object_type}-{row['id']}-{int(start.timestamp())}.mp4"
    path = os.path.join(day_dir, filename)
    with open(path, "wb") as f:
        f.write(content)
    return path
