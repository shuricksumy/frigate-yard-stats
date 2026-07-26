import base64
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone

import requests

import config


def fetch_frigate_event(det_id: str) -> dict:
    resp = requests.get(f"{config.FRIGATE_API_BASE}/api/events/{det_id}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def compute_full_res_box(event: dict) -> list[float]:
    # region is Frigate's own padded, hysteresis-smoothed context area around the object --
    # box is just the tight detected-object box and produces an unusably tight crop (see the
    # `Scale Bbox to Full-Res` notes in the n8n processor workflows this replaces).
    data = event.get("data") or {}
    box = data.get("region") or data.get("box") or event.get("box")
    x, y, w, h = box
    x1, y1, x2, y2 = x, y, x + w, y + h
    return [
        x1 * config.RECORD_WIDTH, y1 * config.RECORD_HEIGHT,
        x2 * config.RECORD_WIDTH, y2 * config.RECORD_HEIGHT,
    ]


def compute_frame_offset_seconds(start_ts, end_ts, offset_pct: float = 0.5) -> float:
    # offset_pct=0.5 (config.CROP_FRAME_OFFSET_PCT's default) is the midpoint -- this project's
    # original fixed behavior, kept as the default since there's no universal offset that matches
    # Frigate's own per-event best-score frame choice (see config.py's comment).
    start = _as_datetime(start_ts)
    end = _as_datetime(end_ts)
    return (end - start).total_seconds() * offset_pct


def scale_image_base64(image_base64: str, max_dimension: int) -> str:
    # Same ffmpeg scale-filter approach crop_and_scale uses for MAX_CROP_DIMENSION, factored out
    # so report.py can shrink an already-cropped image further for inline previews without ever
    # touching the stored full-quality crop_image_base64.
    with tempfile.TemporaryDirectory() as tmp:
        src_path = os.path.join(tmp, "src.jpg")
        dst_path = os.path.join(tmp, "dst.jpg")
        with open(src_path, "wb") as f:
            f.write(base64.b64decode(image_base64))

        scale_filter = (
            f"scale='min({max_dimension},iw)':'min({max_dimension},ih)':"
            "force_original_aspect_ratio=decrease"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", src_path, "-vf", scale_filter, dst_path],
            check=True, capture_output=True,
        )

        with open(dst_path, "rb") as f:
            return base64.b64encode(f.read()).decode()


def _grab_frame(clip_url: str, timestamp_offset: float, frame_path: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(timestamp_offset), "-i", clip_url, "-frames:v", "1", frame_path],
        check=True, capture_output=True,
    )


# Fallback offset when the computed offset lands past the end of Frigate's saved clip -- always safely
# within any real clip, however short.
_FALLBACK_FRAME_OFFSET_SECONDS = 1.0


def _build_vf_filter(
    box: list[float], max_dimension: int,
    crop_disabled: bool | None = None, crop_padding_pct: float | None = None,
) -> str:
    if crop_disabled is None:
        crop_disabled = config.CROP_DISABLED
    if crop_padding_pct is None:
        crop_padding_pct = config.CROP_PADDING_PCT
    scale_filter = (
        f"scale='min({max_dimension},iw)':'min({max_dimension},ih)':"
        "force_original_aspect_ratio=decrease"
    )
    if crop_disabled:
        # box is unused in this mode -- no validation needed, since it never affects the result
        # (the frame is scaled down but never cropped to a region).
        return scale_filter
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        raise ValueError(f"Invalid box {box}: width={w}, height={h} must both be positive")
    pad_x, pad_y = w * crop_padding_pct, h * crop_padding_pct
    crop_x1 = max(0, x1 - pad_x)
    crop_y1 = max(0, y1 - pad_y)
    crop_x2 = min(config.RECORD_WIDTH, x2 + pad_x)
    crop_y2 = min(config.RECORD_HEIGHT, y2 + pad_y)
    crop_filter = f"crop={crop_x2 - crop_x1}:{crop_y2 - crop_y1}:{crop_x1}:{crop_y1}"
    return f"{crop_filter},{scale_filter}"


def crop_and_scale(
    clip_url: str, timestamp_offset: float, box: list[float],
    crop_disabled: bool | None = None, crop_padding_pct: float | None = None,
) -> str:
    vf = _build_vf_filter(box, config.MAX_CROP_DIMENSION, crop_disabled, crop_padding_pct)

    with tempfile.TemporaryDirectory() as tmp:
        frame_path = os.path.join(tmp, "frame.jpg")
        _grab_frame(clip_url, timestamp_offset, frame_path)
        if not os.path.exists(frame_path):
            # Frigate's saved clip for a long-lived tracked object can be much shorter than the
            # event's own logical start/end span (confirmed in production: a ~20-minute stationary
            # car produced a clip only ~7 minutes long) -- ffmpeg exits 0 with no output when -ss
            # seeks past the actual end of the file rather than raising, so this can't be caught
            # via the subprocess's exit code. Falling back to a small fixed offset near the start
            # is always within an actual saved clip, however much its tail got truncated.
            _grab_frame(clip_url, _FALLBACK_FRAME_OFFSET_SECONDS, frame_path)

        crop_path = os.path.join(tmp, "crop.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-i", frame_path, "-vf", vf, crop_path],
            check=True, capture_output=True,
        )

        with open(crop_path, "rb") as f:
            return base64.b64encode(f.read()).decode()


def fetch_frigate_snapshot_base64(det_id: str) -> str:
    # Frigate's own already-rendered best-detection-score frame -- no ffmpeg involved at all, just
    # the raw JPEG bytes Frigate itself already produced. See FRIGATE_SNAPSHOT_ENABLED's comment in
    # config.py for the resolution/overlay trade-off this accepts in exchange for better framing.
    resp = requests.get(f"{config.FRIGATE_API_BASE}/api/events/{det_id}/snapshot.jpg", timeout=10)
    resp.raise_for_status()
    return base64.b64encode(resp.content).decode()


def crop_event_high_res(
    raw_event: dict,
    crop_frame_offset_pct: float | None = None,
    crop_disabled: bool | None = None,
    crop_padding_pct: float | None = None,
    event: dict | None = None,
) -> str:
    # A genuine ffmpeg seek+crop+scale from the record stream, via the event-id-scoped clip
    # endpoint (/api/events/{det_id}/clip.mp4) -- NOT the continuous-recording start/end endpoint
    # video.build_clip_url uses for visits, which is what caused the old visit-preview grid's
    # abandoned unpredictable-padding bugs (see CLAUDE.md). This endpoint is confirmed far more
    # durable (observed surviving over an hour vs. ~36 minutes for the continuous-recording one),
    # so it's safe to call once per selected event when gathering high-res images for the alert
    # stage, regardless of whether FRIGATE_SNAPSHOT_ENABLED is on for that same event's own
    # (low-res) events-stage analysis. Factored out of crop_event so both callers share one
    # implementation. `event` lets a caller that already fetched the Frigate event (crop_event's
    # own non-snapshot branch) pass it through instead of fetching it a second time; omitted, it's
    # fetched fresh (the shape every other caller, e.g. alert_ai_worker, actually needs).
    if crop_frame_offset_pct is None:
        crop_frame_offset_pct = config.CROP_FRAME_OFFSET_PCT
    det_id = raw_event["det_id"]
    if event is None:
        event = fetch_frigate_event(det_id)
    box = compute_full_res_box(event)
    offset = compute_frame_offset_seconds(
        raw_event["start_ts"], raw_event["end_ts"], crop_frame_offset_pct,
    )
    clip_url = f"{config.FRIGATE_API_BASE}/api/events/{det_id}/clip.mp4"
    return crop_and_scale(clip_url, offset, box, crop_disabled, crop_padding_pct)


def crop_event(
    raw_event: dict,
    frigate_snapshot_enabled: bool | None = None,
    crop_disabled: bool | None = None,
    crop_frame_offset_pct: float | None = None,
    crop_padding_pct: float | None = None,
) -> dict:
    # sub_label/score come from this same Frigate API fetch (not the live MQTT "end" payload)
    # because LPR/sub_label resolution can settle after the event first fires -- this is the
    # settled, final read. Captured here rather than re-fetched later so the AI-processing
    # stage (n8n) never needs to call Frigate's API at all.
    if frigate_snapshot_enabled is None:
        frigate_snapshot_enabled = config.FRIGATE_SNAPSHOT_ENABLED
    det_id = raw_event["det_id"]
    event = fetch_frigate_event(det_id)
    data = event.get("data") or {}
    if frigate_snapshot_enabled:
        crop_image_base64 = fetch_frigate_snapshot_base64(det_id)
    else:
        crop_image_base64 = crop_event_high_res(
            raw_event, crop_frame_offset_pct, crop_disabled, crop_padding_pct, event=event,
        )
    return {
        "crop_image_base64": crop_image_base64,
        "sub_label": event.get("sub_label"),
        "score": data.get("score"),
    }
