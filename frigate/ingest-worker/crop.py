import base64
import os
import subprocess
import tempfile

import requests

import config


def fetch_frigate_event(det_id: str) -> dict:
    resp = requests.get(f"{config.FRIGATE_API_BASE}/api/events/{det_id}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def scale_image_base64(image_base64: str, max_dimension: int) -> str:
    # ffmpeg scale-filter downscale, shared by crop_event's AI-facing copy and report.py's inline
    # preview thumbnails -- never touches the stored full-quality copy it's derived from.
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


def fetch_frigate_snapshot_base64(det_id: str) -> str:
    # Frigate's own already-rendered best-detection-score frame -- no ffmpeg involved at all, just
    # the raw JPEG bytes Frigate itself already produced. This is deliberately the ONLY image
    # source crop_event uses (see its own comment below) -- a seeked frame from a record-stream
    # clip at a computed offset can land on a materially different, less representative moment than
    # whatever Frigate itself judged as this event's best frame, and there is no way to sync our own
    # seek to Frigate's actual choice (Frigate never exposes that timestamp anywhere in its API --
    # confirmed directly: not in the event JSON, not in the snapshot's own response headers, not in
    # EXIF; see CLAUDE.md's "Cropping" section). The resolution/overlay trade-off this accepts
    # (Frigate's fixed, lower detect-stream resolution, with a burned-in bbox/label/timestamp
    # overlay this Frigate version's API has no way to suppress) is worth it for actually matching
    # what Frigate itself considered the event's defining moment.
    resp = requests.get(f"{config.FRIGATE_API_BASE}/api/events/{det_id}/snapshot.jpg", timeout=10)
    resp.raise_for_status()
    return base64.b64encode(resp.content).decode()


def crop_event(raw_event: dict, ai_image_max_dimension: int | None = None) -> dict:
    # sub_label/score come from this same Frigate API fetch (not the live MQTT "end" payload)
    # because LPR/sub_label resolution can settle after the event first fires -- this is the
    # settled, final read. Captured here rather than re-fetched later so the AI-processing
    # stage (n8n) never needs to call Frigate's API at all.
    #
    # Uses ONLY fetch_frigate_snapshot_base64 -- never a seek+crop grabbed from a record-stream
    # clip at a computed offset. A prior change (commit b1cd068) switched every event over to an
    # unconditional record-stream seek, which was a real regression: the seeked frame can be a
    # materially different, less representative moment than the one Frigate itself already chose
    # and rendered as this event's own snapshot -- confirmed directly against production traffic.
    # The record-stream seek+crop primitives this project used to have (crop_and_scale/
    # _build_vf_filter/compute_full_res_box/compute_frame_offset_seconds, plus the per-object-type
    # crop_disabled/crop_frame_offset_pct/crop_padding_pct settings that configured them) have since
    # been removed entirely -- once every event uses Frigate's own snapshot exclusively, there is no
    # region-crop math left to configure, so keeping those settings around any longer would just be
    # dead, misleading config. ai_image_max_dimension still applies: the snapshot is scaled down to
    # fit the AI/DB-stored size limit, same as every other image this project sends to a VLM.
    det_id = raw_event["det_id"]
    event = fetch_frigate_event(det_id)
    data = event.get("data") or {}
    snapshot_base64 = fetch_frigate_snapshot_base64(det_id)
    ai_image_base64 = scale_image_base64(
        snapshot_base64, ai_image_max_dimension or config.MAX_CROP_DIMENSION,
    )
    return {
        "crop_image_base64": ai_image_base64,
        # Never stored in Postgres -- only written to disk (event_images.store_event_image) when
        # STORE_EVENT_IMAGES resolves true for this event's own object type. Frigate's snapshot has
        # no separate higher-resolution version to persist here -- this is the same unscaled bytes
        # fetch_frigate_snapshot_base64 already returned.
        "full_res_image_base64": snapshot_base64,
        "sub_label": event.get("sub_label"),
        "score": data.get("score"),
    }
