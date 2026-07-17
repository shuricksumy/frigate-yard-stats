import base64
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone

import requests

import config
import video


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


def crop_and_scale(clip_url: str, timestamp_offset: float, box: list[float]) -> str:
    scale_filter = (
        f"scale='min({config.MAX_CROP_DIMENSION},iw)':'min({config.MAX_CROP_DIMENSION},ih)':"
        "force_original_aspect_ratio=decrease"
    )
    if config.CROP_DISABLED:
        # box is unused in this mode -- no validation needed, since it never affects the result
        # (crop_image_base64 becomes the full original frame, just scaled down).
        vf = scale_filter
    else:
        x1, y1, x2, y2 = box
        w, h = x2 - x1, y2 - y1
        if w <= 0 or h <= 0:
            raise ValueError(f"Invalid box {box}: width={w}, height={h} must both be positive")
        pad_x, pad_y = w * config.CROP_PADDING_PCT, h * config.CROP_PADDING_PCT
        crop_x1 = max(0, x1 - pad_x)
        crop_y1 = max(0, y1 - pad_y)
        crop_x2 = min(config.RECORD_WIDTH, x2 + pad_x)
        crop_y2 = min(config.RECORD_HEIGHT, y2 + pad_y)
        crop_filter = f"crop={crop_x2 - crop_x1}:{crop_y2 - crop_y1}:{crop_x1}:{crop_y1}"
        vf = f"{crop_filter},{scale_filter}"

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


def _probe_duration_seconds(clip_url: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", clip_url],
        check=True, capture_output=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


# Internal buffer against landing right at the very tail of whatever clip Frigate actually
# returned -- not deployment-tunable (see crop_visit_thumbnail below for why), same idea as
# _FALLBACK_FRAME_OFFSET_SECONDS above.
_DURATION_SAFETY_MARGIN_SECONDS = 0.5


def crop_visit_thumbnail(visit: dict, representative_event: dict) -> str:
    # visit["thumb_time"] is Frigate's own per-review "best frame" choice (see
    # mqtt_ingest.parse_review_payload) -- an absolute epoch timestamp, unlike
    # CROP_FRAME_OFFSET_PCT which is a percentage of one raw_event's own start/end span. It can
    # legitimately fall outside the representative event's own narrow window (Frigate picks it
    # over the whole review, which can span multiple det_ids) -- so this fetches the same
    # visit-scoped continuous-recording clip alert_video_worker.py downloads (video.build_clip_url,
    # camera + start/end with the same -5s/+5s padding), not the representative event's own
    # /api/events/{det_id}/clip.mp4 endpoint crop_event uses. The representative event's own
    # region/box is still used for spatial framing -- Frigate's review payload has no box/region
    # of its own, only individual tracked-object events do.
    det_id = representative_event["det_id"]
    event = fetch_frigate_event(det_id)
    box = compute_full_res_box(event)

    clip_row = {"start_ts": visit["start_ts"], "end_ts": visit["end_ts"], "camera": visit["cameras"]}
    clip_url = video.build_clip_url(clip_row)

    # Anchored from the clip's own END, not its assumed start. Confirmed live in production that
    # Frigate's continuous-recording clip endpoint can silently prepend extra lead-in footage
    # before the requested start_ts-5 -- one real visit asked for a 15.2s window (start_ts-5 to
    # end_ts+5) and got back 21.3s, ~6.1s more than requested. Seeking from an assumed
    # clip-start-equals-start_ts-5 landed on an empty stretch of parking lot; the actual thumb_time
    # frame (confirmed by pulling Frigate's own review thumbnail, which does show the moment) was
    # ~11.3s into the file, not ~5.2s -- exactly matching where this end-anchored formula below
    # places it. This lines up with Frigate storing continuous recording in fixed-length segments
    # and building an arbitrary clip by concatenating whole segments that cover the request --
    # snapping the start backward to a segment boundary, while the end reliably lines up with what
    # was actually asked for (confirmed: measured duration was within ~0.1s of the requested
    # end_ts+5 in that same case). So compute the offset from the far side instead: how far
    # thumb_time sits before the requested end, subtracted from the clip's real (ffprobe-measured)
    # duration.
    duration = _probe_duration_seconds(clip_url)
    end_padding_epoch = int(_as_datetime(visit["end_ts"]).timestamp()) + 5
    offset_from_end = end_padding_epoch - visit["thumb_time"]
    # VISIT_THUMB_CROP_OFFSET_ADJUST_SECONDS (default 0) shifts the seek target relative to
    # thumb_time -- positive moves later/forward, negative moves earlier. Exists for any leftover
    # sub-second drift once the end-anchoring above is applied (e.g. keyframe spacing during the
    # seek) -- tune it by comparing a few real crops against what thumb_time should show.
    offset = duration - offset_from_end + config.VISIT_THUMB_CROP_OFFSET_ADJUST_SECONDS

    # A genuine recording gap (confirmed separately in production: record.continuous.days=0 meant
    # a 13s request came back only ~4.06s long) can still land the computed offset outside the
    # clip's real bounds in either direction -- before its start (offset < 0) or within
    # _DURATION_SAFETY_MARGIN_SECONDS of its end. Either way, fail explicitly instead of letting
    # ffmpeg silently clamp to a nearby frame and return a plausible-looking but wrong-moment crop
    # -- this routes through the normal retry-then-fallback path (visit_thumb_worker.py ->
    # mark_visit_thumb_crop_retry_or_failed). _DURATION_SAFETY_MARGIN_SECONDS is a small fixed
    # buffer for encoder/keyframe edge cases right at a clip's tail, not deployment-tunable -- it
    # does NOT compensate for a real gap (no margin value would have; that gap was ~1.8s).
    if offset < 0 or offset >= duration - _DURATION_SAFETY_MARGIN_SECONDS:
        raise ValueError(
            f"thumb_time-derived offset {offset:.3f}s falls outside the actual clip's bounds "
            f"(duration {duration:.3f}s) for visit id={visit.get('id')} -- "
            f"Frigate likely has a recording gap for this window"
        )

    return crop_and_scale(clip_url, offset, box)


def crop_event(raw_event: dict) -> dict:
    # sub_label/score come from this same Frigate API fetch (not the live MQTT "end" payload)
    # because LPR/sub_label resolution can settle after the event first fires -- this is the
    # settled, final read. Captured here rather than re-fetched later so the AI-processing
    # stage (n8n) never needs to call Frigate's API at all.
    det_id = raw_event["det_id"]
    event = fetch_frigate_event(det_id)
    data = event.get("data") or {}
    box = compute_full_res_box(event)
    offset = compute_frame_offset_seconds(
        raw_event["start_ts"], raw_event["end_ts"], config.CROP_FRAME_OFFSET_PCT,
    )
    clip_url = f"{config.FRIGATE_API_BASE}/api/events/{det_id}/clip.mp4"
    crop_image_base64 = crop_and_scale(clip_url, offset, box)
    return {
        "crop_image_base64": crop_image_base64,
        "sub_label": event.get("sub_label"),
        "score": data.get("score"),
    }
