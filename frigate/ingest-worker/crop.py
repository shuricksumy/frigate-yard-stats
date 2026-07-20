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


def _probe_duration_seconds(clip_url: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", clip_url],
        check=True, capture_output=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


# Small buffer off both ends of the clip so a percentage-boundary offset (e.g. the default 0%/100%)
# doesn't land on a garbled encoder edge frame (same idea as _DURATION_SAFETY_MARGIN_SECONDS used
# to be). Applied on top of whatever VISIT_PREVIEW_FRAME_PERCENTAGES is configured to -- harmless
# even if that's already tuned to avoid the exact edges (e.g. "5,35,65,90").
_VISIT_PREVIEW_EDGE_MARGIN_SECONDS = 0.3

# Guards against Frigate's continuous-recording clip endpoint returning a not-yet-finalized/
# placeholder clip -- confirmed live in production: a visit whose nominal window (end_ts+5 -
# (start_ts-5)) was ~44.6s got back a clip only ~3.9s long (an ~11x shortfall, not the few-seconds
# segment-boundary jitter this design is meant to tolerate). Sampling percentages of THAT duration
# produced 4 frames all crammed within the same ~3.9s window instead of spanning the visit's real
# ~34.6s span -- silently wrong, not caught by anything, since this function (unlike its
# predecessor) has no duration-vs-window check at all otherwise. video.download_clip has its own
# analogous guard (VIDEO_MIN_VALID_BYTES) for the exact same "not ready yet" condition on the
# byte-size axis; this is the duration-axis equivalent for this function specifically. Only used
# by _panels_from_clip (the already-downloaded-video path) -- a locally stored file's duration is
# trustworthy to probe since there's no race left to guard against by the time it's on disk.
_MIN_DURATION_RATIO = 0.5


def _panels_from_clip(clip_url: str, visit: dict, tmp: str, frame_percentages: list[float] | None = None) -> list[str]:
    # Used when a visit video is already downloaded (alert_video_worker) -- a single known-good
    # file, so probing its real duration and sampling proportionally across it is trustworthy (no
    # race left to guard against once the whole clip is sitting on disk). Returns the 4 raw
    # (un-cropped, un-scaled) frame paths -- the caller applies whatever size it needs each of them
    # at (a smaller one for the grid, a full-size one for the GIF), since both are just different
    # crops of the same underlying moments.
    duration = _probe_duration_seconds(clip_url)

    requested_span = (
        (int(_as_datetime(visit["end_ts"]).timestamp()) + 5)
        - (int(_as_datetime(visit["start_ts"]).timestamp()) - 5)
    )
    if duration < requested_span * _MIN_DURATION_RATIO:
        raise ValueError(
            f"Clip duration {duration:.3f}s is far shorter than the requested window "
            f"{requested_span:.3f}s for visit id={visit.get('id')} -- Frigate likely hasn't "
            f"finished writing this recording segment yet"
        )

    if frame_percentages is None:
        frame_percentages = config.VISIT_PREVIEW_FRAME_PERCENTAGES
    usable = max(duration - 2 * _VISIT_PREVIEW_EDGE_MARGIN_SECONDS, 0.0)
    offsets = [
        _VISIT_PREVIEW_EDGE_MARGIN_SECONDS + (pct / 100) * usable
        for pct in frame_percentages
    ]

    raw_paths = []
    for i, offset in enumerate(offsets):
        raw_path = os.path.join(tmp, f"raw_{i}.jpg")
        _grab_frame(clip_url, offset, raw_path)
        if not os.path.exists(raw_path):
            # Same ffmpeg silent-empty-output behavior crop_and_scale guards against.
            _grab_frame(clip_url, _FALLBACK_FRAME_OFFSET_SECONDS, raw_path)
        raw_paths.append(raw_path)
    return raw_paths


def _grab_frame_near_timestamp(camera: str, timestamp: float, frame_path: str) -> bool:
    # One independent Frigate request per sampled moment (used when no visit video is downloaded
    # yet) instead of one whole-visit-span request -- Frigate only durably retains recording
    # segments actually covered by an alert/detection/motion tag (record.continuous.days: 0 means
    # a bare continuous segment survives only a very short-lived rolling buffer before Frigate's
    # own cleanup purges it -- confirmed live: the identical URL returned a full clip, then a
    # near-empty one, only 5 seconds apart). A single request spanning the whole visit fails as
    # one unit if any part of that window was never tagged for longer retention; asking
    # per-moment means a gap at one percentage point doesn't take the other three down with it.
    # build_clip_url's own -5s/+5s padding gives a small window around `timestamp` for free, so
    # passing the same instant as both start_ts and end_ts is enough -- the target moment then
    # sits ~5s into the returned clip.
    window_row = {"start_ts": timestamp, "end_ts": timestamp, "camera": camera}
    clip_url = video.build_clip_url(window_row)
    _grab_frame(clip_url, 5.0, frame_path)
    if not os.path.exists(frame_path):
        _grab_frame(clip_url, _FALLBACK_FRAME_OFFSET_SECONDS, frame_path)
    return os.path.exists(frame_path)


def _panels_from_independent_timestamps(visit: dict, tmp: str, frame_percentages: list[float] | None = None) -> list[str]:
    # Returns the 4 raw (un-cropped, un-scaled) frame paths, already deduped (a gap reuses a
    # neighbor's raw path -- see below) -- same contract as _panels_from_clip, so the caller can
    # apply whatever size it needs each of them at.
    if frame_percentages is None:
        frame_percentages = config.VISIT_PREVIEW_FRAME_PERCENTAGES
    start_epoch = _as_datetime(visit["start_ts"]).timestamp()
    end_epoch = _as_datetime(visit["end_ts"]).timestamp()
    camera = visit["cameras"]
    timestamps = [
        start_epoch + (pct / 100) * (end_epoch - start_epoch)
        for pct in frame_percentages
    ]

    raw_paths: list[str | None] = [None] * len(timestamps)
    for i, ts in enumerate(timestamps):
        raw_path = os.path.join(tmp, f"raw_{i}.jpg")
        if _grab_frame_near_timestamp(camera, ts, raw_path):
            raw_paths[i] = raw_path

    if not any(raw_paths):
        raise ValueError(
            f"Could not grab a frame at any of the {len(timestamps)} sampled moments for visit "
            f"id={visit.get('id')} -- Frigate has no retained footage anywhere in this visit's span"
        )

    # A gap at one sampled moment (no retained footage right there -- see above) reuses the
    # nearest earlier successful frame rather than failing the whole grid over one missing
    # percentage point; a leading gap (before any success) borrows the first success instead.
    last_good = next(p for p in raw_paths if p is not None)
    filled_paths = []
    for p in raw_paths:
        if p is not None:
            last_good = p
        filled_paths.append(last_good)
    return filled_paths


def build_visit_preview(
    visit: dict, representative_event: dict,
    frame_percentages: list[float] | None = None,
    crop_disabled: bool | None = None, crop_padding_pct: float | None = None,
) -> tuple[str, str]:
    # Returns (grid_image_base64, preview_gif_base64). Frigate's own thumb_time turned out
    # unreliable as a seek target -- its continuous-recording clip endpoint pads an unpredictable
    # amount of extra footage onto EITHER edge of the requested window, not consistently the same
    # one request to request (confirmed live: one visit had extra footage prepended before the
    # start, another had it appended after the end -- no single fixed anchor point is correct for
    # both). Rather than chasing one precise "best moment" against a moving target, this samples
    # config.VISIT_PREVIEW_FRAME_PERCENTAGES (default 0/25/50/100, deployment-tunable -- e.g.
    # "5,35,65,90" to stay a bit clear of both edges) proportionally across the visit's own span,
    # combined into one composite grid image (guaranteed single-image, so any VLM handles it,
    # unlike sending several separate images which depends on the specific backend/model actually
    # supporting multi-image prompts) plus a separate animated GIF for human preview only (a
    # chat-completion vision API decodes an image_url as a single static frame, so an actual GIF
    # would never convey the animation to a model -- there's no point sending it one).
    det_id = representative_event["det_id"]
    event = fetch_frigate_event(det_id)
    box = compute_full_res_box(event)

    # Each grid panel scaled to half MAX_CROP_DIMENSION so the assembled 2x2 grid lands near
    # MAX_CROP_DIMENSION overall, not 4x it. The GIF is a different artifact -- it plays one frame
    # at a time rather than combining all 4 into one image, so it has no reason to share that
    # half-size constraint -- each of its frames is scaled to the SAME full MAX_CROP_DIMENSION a
    # normal single-event crop_image_base64 uses, not shrunk further for file-size's sake.
    panel_vf = _build_vf_filter(box, config.MAX_CROP_DIMENSION // 2, crop_disabled, crop_padding_pct)
    gif_frame_vf = _build_vf_filter(box, config.MAX_CROP_DIMENSION, crop_disabled, crop_padding_pct)

    with tempfile.TemporaryDirectory() as tmp:
        # Prefer a visit video alert_video_worker has already downloaded -- one known-good file,
        # cheaper than 4 separate requests. Otherwise (or if that file turns out to be a short/bad
        # clip too -- alert_video_worker only validates byte size via VIDEO_MIN_VALID_BYTES, which
        # at typical settings is far below what even a genuinely short few-second clip weighs, so a
        # bad download can pass that check and get stored as "done") fall through to sampling each
        # of the 4 moments independently (see _panels_from_independent_timestamps) instead of
        # treating a bad local file as a permanent dead end -- video_path never changes once set,
        # so failing here on every retry would otherwise never recover.
        local_video_path = visit.get("video_path")
        raw_paths = None
        if local_video_path and os.path.isfile(local_video_path):
            try:
                raw_paths = _panels_from_clip(local_video_path, visit, tmp, frame_percentages)
            except ValueError:
                pass
        if raw_paths is None:
            raw_paths = _panels_from_independent_timestamps(visit, tmp, frame_percentages)

        # Both the grid panels and the GIF frames are just different-sized crops of the same 4
        # raw moments -- built here, once, from the same source frames.
        panel_paths = []
        gif_frame_paths = []
        for i, raw_path in enumerate(raw_paths):
            panel_path = os.path.join(tmp, f"panel_{i}.jpg")
            subprocess.run(
                ["ffmpeg", "-y", "-i", raw_path, "-vf", panel_vf, panel_path],
                check=True, capture_output=True,
            )
            panel_paths.append(panel_path)

            gif_frame_path = os.path.join(tmp, f"gif_frame_{i}.jpg")
            subprocess.run(
                ["ffmpeg", "-y", "-i", raw_path, "-vf", gif_frame_vf, gif_frame_path],
                check=True, capture_output=True,
            )
            gif_frame_paths.append(gif_frame_path)

        grid_path = os.path.join(tmp, "grid.jpg")
        grid_inputs = []
        for p in panel_paths:
            grid_inputs += ["-i", p]
        subprocess.run(
            ["ffmpeg", "-y", *grid_inputs, "-filter_complex",
             "[0:v][1:v]hstack=2[top];[2:v][3:v]hstack=2[bot];[top][bot]vstack=2[out]",
             "-map", "[out]", grid_path],
            check=True, capture_output=True,
        )
        with open(grid_path, "rb") as f:
            grid_base64 = base64.b64encode(f.read()).decode()

        # Animated GIF from the full-size frames above, played as a slideshow -- human preview
        # only, never sent to the VLM (see docstring above). Palette generation keeps GIF's
        # 256-color limit from looking muddy against real photos.
        gif_path = os.path.join(tmp, "preview.gif")
        subprocess.run(
            ["ffmpeg", "-y", "-framerate", "1.5", "-i", os.path.join(tmp, "gif_frame_%d.jpg"),
             "-vf", "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
             "-loop", "0", gif_path],
            check=True, capture_output=True,
        )
        with open(gif_path, "rb") as f:
            gif_base64 = base64.b64encode(f.read()).decode()

    return grid_base64, gif_base64


def fetch_frigate_snapshot_base64(det_id: str) -> str:
    # Frigate's own already-rendered best-detection-score frame -- no ffmpeg involved at all, just
    # the raw JPEG bytes Frigate itself already produced. See FRIGATE_SNAPSHOT_ENABLED's comment in
    # config.py for the resolution/overlay trade-off this accepts in exchange for better framing.
    resp = requests.get(f"{config.FRIGATE_API_BASE}/api/events/{det_id}/snapshot.jpg", timeout=10)
    resp.raise_for_status()
    return base64.b64encode(resp.content).decode()


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
    if crop_frame_offset_pct is None:
        crop_frame_offset_pct = config.CROP_FRAME_OFFSET_PCT
    det_id = raw_event["det_id"]
    event = fetch_frigate_event(det_id)
    data = event.get("data") or {}
    if frigate_snapshot_enabled:
        crop_image_base64 = fetch_frigate_snapshot_base64(det_id)
    else:
        box = compute_full_res_box(event)
        offset = compute_frame_offset_seconds(
            raw_event["start_ts"], raw_event["end_ts"], crop_frame_offset_pct,
        )
        clip_url = f"{config.FRIGATE_API_BASE}/api/events/{det_id}/clip.mp4"
        crop_image_base64 = crop_and_scale(clip_url, offset, box, crop_disabled, crop_padding_pct)
    return {
        "crop_image_base64": crop_image_base64,
        "sub_label": event.get("sub_label"),
        "score": data.get("score"),
    }
