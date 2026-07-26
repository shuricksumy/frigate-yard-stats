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
    box: list[float], max_dimension: int | None,
    crop_disabled: bool | None = None, crop_padding_pct: float | None = None,
) -> str:
    if crop_disabled is None:
        crop_disabled = config.CROP_DISABLED
    if crop_padding_pct is None:
        crop_padding_pct = config.CROP_PADDING_PCT
    # max_dimension=None means no scale filter at all -- the crop stays at whatever resolution the
    # record stream naturally gives it (used for the full-resolution storage copy; the AI-facing
    # copy is produced afterward via scale_image_base64 instead of a second ffmpeg scale here).
    scale_filter = (
        f"scale='min({max_dimension},iw)':'min({max_dimension},ih)':"
        "force_original_aspect_ratio=decrease"
    ) if max_dimension is not None else None
    if crop_disabled:
        # box is unused in this mode -- no validation needed, since it never affects the result
        # (the frame is scaled down but never cropped to a region).
        return scale_filter or "null"
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
    return f"{crop_filter},{scale_filter}" if scale_filter else crop_filter


def crop_and_scale(
    clip_url: str, timestamp_offset: float, box: list[float],
    crop_disabled: bool | None = None, crop_padding_pct: float | None = None,
    max_dimension: int | None = None,
) -> str:
    # max_dimension=None (the default) -- no scale filter, the crop is left at native record-stream
    # resolution. Callers that want the original AI-facing capped size pass config.MAX_CROP_DIMENSION
    # (or a per-type override) explicitly.
    vf = _build_vf_filter(box, max_dimension, crop_disabled, crop_padding_pct)

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


def crop_event(
    raw_event: dict,
    crop_disabled: bool | None = None,
    crop_frame_offset_pct: float | None = None,
    crop_padding_pct: float | None = None,
    ai_image_max_dimension: int | None = None,
) -> dict:
    # sub_label/score come from this same Frigate API fetch (not the live MQTT "end" payload)
    # because LPR/sub_label resolution can settle after the event first fires -- this is the
    # settled, final read. Captured here rather than re-fetched later so the AI-processing
    # stage (n8n) never needs to call Frigate's API at all.
    #
    # A genuine ffmpeg seek+crop from the record stream, via the event-id-scoped clip endpoint
    # (/api/events/{det_id}/clip.mp4) -- NOT the continuous-recording start/end endpoint
    # video.build_clip_url uses for visits, which is what caused the old visit-preview grid's
    # abandoned unpredictable-padding bugs (see CLAUDE.md). This endpoint is confirmed far more
    # durable (observed surviving over an hour vs. ~36 minutes for the continuous-recording one).
    # One seek produces one full-resolution crop; the AI-facing (and DB-stored) copy is a cheap
    # scale_image_base64 downscale of that same crop, not a second seek/fetch. This is the same
    # capture path the alert stage used to use on its own (crop_event_high_res, now folded in here
    # since that was its only other caller) -- there is no longer a separate Frigate-snapshot image
    # source to choose between (see FRIGATE_SNAPSHOT_ENABLED's removal in CLAUDE.md).
    if crop_frame_offset_pct is None:
        crop_frame_offset_pct = config.CROP_FRAME_OFFSET_PCT
    det_id = raw_event["det_id"]
    event = fetch_frigate_event(det_id)
    data = event.get("data") or {}
    box = compute_full_res_box(event)
    offset = compute_frame_offset_seconds(
        raw_event["start_ts"], raw_event["end_ts"], crop_frame_offset_pct,
    )
    clip_url = f"{config.FRIGATE_API_BASE}/api/events/{det_id}/clip.mp4"
    full_res_image_base64 = crop_and_scale(clip_url, offset, box, crop_disabled, crop_padding_pct)
    ai_image_base64 = scale_image_base64(
        full_res_image_base64, ai_image_max_dimension or config.MAX_CROP_DIMENSION,
    )
    return {
        "crop_image_base64": ai_image_base64,
        # Never stored in Postgres -- only written to disk (event_images.store_event_image) when
        # STORE_EVENT_IMAGES resolves true for this event's own object type.
        "full_res_image_base64": full_res_image_base64,
        "sub_label": event.get("sub_label"),
        "score": data.get("score"),
    }
