import os
import subprocess
import tempfile
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
    # Epoch seconds (stable, sortable, matches the event's start_ts exactly) plus a human-readable
    # UTC timestamp alongside it -- the epoch alone isn't recognizable at a glance in a directory
    # listing.
    filename = f"{object_type}-{row['id']}-{int(start.timestamp())}-{start:%Y%m%dT%H%M%SZ}.mp4"
    path = os.path.join(day_dir, filename)
    with open(path, "wb") as f:
        f.write(content)
    return path


def store_visit_clip(visit: dict, content: bytes) -> str:
    # Mirrors store_clip, but under VIDEO_STORAGE_PATH_ALERTS -- a genuinely separate storage
    # location (own bind mount), not a subfolder of VIDEO_STORAGE_PATH, so the two flows' disk
    # usage can be measured/managed independently. "visit-" filename prefix so a visit's
    # whole-span clip is never confused with a per-event clip that happens to share the same
    # numeric id (visit ids and raw_event ids are independent sequences) -- see
    # alert_video_worker.py.
    start = _as_datetime(visit["start_ts"])
    day_dir = os.path.join(
        config.VIDEO_STORAGE_PATH_ALERTS,
        f"{start:%Y}", f"{start:%m}", f"{start:%d}",
    )
    os.makedirs(day_dir, exist_ok=True)

    object_type = _primary_object_type(visit)
    filename = f"visit-{object_type}-{visit['id']}-{int(start.timestamp())}-{start:%Y%m%dT%H%M%SZ}.mp4"
    path = os.path.join(day_dir, filename)
    with open(path, "wb") as f:
        f.write(content)
    return path


def extract_frame_jpeg(video_path: str, max_dimension: int | None = None) -> bytes:
    # Fallback for events that have a stored clip but no crop_image_base64 -- shouldn't happen
    # today (claim_video_batch only ever claims crop_status='done' rows, so a video always implies
    # a crop image already exists), but the web UI's thumbnail/image endpoints use this defensively
    # so a future change to that invariant doesn't leave those events with nothing to show.
    # 0.1s in, not frame 0 -- some encoders' very first frame is black/incomplete.
    with tempfile.TemporaryDirectory() as tmp:
        frame_path = os.path.join(tmp, "frame.jpg")
        vf = []
        if max_dimension is not None:
            vf.append(f"scale='min({max_dimension},iw)':'min({max_dimension},ih)':force_original_aspect_ratio=decrease")
        cmd = ["ffmpeg", "-y", "-ss", "0.1", "-i", video_path, "-frames:v", "1"]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd.append(frame_path)
        subprocess.run(cmd, check=True, capture_output=True)
        with open(frame_path, "rb") as f:
            return f.read()
