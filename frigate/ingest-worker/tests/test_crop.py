"""Unit tests for crop.py.

crop_event uses ONLY Frigate's own best-detection-score snapshot (fetch_frigate_snapshot_base64) --
the record-stream seek+crop primitives this project used to have (crop_and_scale/_build_vf_filter/
compute_full_res_box/compute_frame_offset_seconds, plus the crop_disabled/crop_frame_offset_pct/
crop_padding_pct settings that configured them) have been removed entirely; see crop_event's own
comment and CLAUDE.md's "Cropping" section for why.
"""
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import config  # noqa: E402
import crop  # noqa: E402


# ---- crop_event: Frigate's own best-moment snapshot, ONLY -- never a record-stream seek ----

def test_crop_event_uses_frigate_snapshot_not_record_stream_seek(monkeypatch):
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {
        "data": {"region": [0.1, 0.1, 0.2, 0.2], "score": 0.5}, "sub_label": None,
    })
    captured = {}

    def fake_fetch_snapshot(det_id):
        captured["det_id"] = det_id
        return "snapshot-base64"
    monkeypatch.setattr(crop, "fetch_frigate_snapshot_base64", fake_fetch_snapshot)
    monkeypatch.setattr(crop, "scale_image_base64", lambda image_base64, max_dimension: f"ai-{image_base64}")

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    result = crop.crop_event(raw_event)

    assert result == {
        "crop_image_base64": "ai-snapshot-base64",
        "full_res_image_base64": "snapshot-base64",
        "sub_label": None,
        "score": 0.5,
    }
    assert captured["det_id"] == "abc123"


def test_crop_event_scales_the_snapshot_down_for_the_ai_facing_copy(monkeypatch):
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {
        "data": {"region": [0.1, 0.1, 0.2, 0.2], "score": None}, "sub_label": None,
    })
    monkeypatch.setattr(crop, "fetch_frigate_snapshot_base64", lambda det_id: "snapshot-base64")
    captured = {}

    def fake_scale(image_base64, max_dimension):
        captured["image_base64"] = image_base64
        captured["max_dimension"] = max_dimension
        return "ai-crop-base64"
    monkeypatch.setattr(crop, "scale_image_base64", fake_scale)

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    result = crop.crop_event(raw_event, ai_image_max_dimension=640)

    assert result["crop_image_base64"] == "ai-crop-base64"
    assert result["full_res_image_base64"] == "snapshot-base64"
    assert captured == {"image_base64": "snapshot-base64", "max_dimension": 640}


def test_crop_event_ai_image_max_dimension_falls_back_to_global_config(monkeypatch):
    monkeypatch.setattr(config, "MAX_CROP_DIMENSION", 999)
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {
        "data": {"region": [0.1, 0.1, 0.2, 0.2]}, "sub_label": None,
    })
    monkeypatch.setattr(crop, "fetch_frigate_snapshot_base64", lambda det_id: "snapshot-base64")
    captured = {}
    monkeypatch.setattr(crop, "scale_image_base64", lambda image_base64, max_dimension: captured.setdefault("max_dimension", max_dimension) or "ai-crop-base64")

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    crop.crop_event(raw_event)

    assert captured["max_dimension"] == 999


def test_crop_event_calls_frigate_event_only_once(monkeypatch):
    # crop_event must fetch the Frigate event exactly once and reuse it for its own sub_label/score
    # fields -- not fetch it twice.
    call_count = {"n": 0}

    def counting_fetch(det_id):
        call_count["n"] += 1
        return {"data": {"region": [0.1, 0.1, 0.2, 0.2], "score": 0.5}, "sub_label": None}
    monkeypatch.setattr(crop, "fetch_frigate_event", counting_fetch)
    monkeypatch.setattr(crop, "fetch_frigate_snapshot_base64", lambda det_id: "snapshot-base64")
    monkeypatch.setattr(crop, "scale_image_base64", lambda *a, **k: "ai-crop-base64")

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    crop.crop_event(raw_event)

    assert call_count["n"] == 1


# ---- fetch_frigate_snapshot_base64 ----

def test_fetch_frigate_snapshot_base64_hits_the_snapshot_endpoint(monkeypatch):
    captured = {}

    class _Resp:
        content = b"fake-jpeg-bytes"

        def raise_for_status(self):
            pass

    def fake_get(url, timeout=None):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(crop.requests, "get", fake_get)
    result = crop.fetch_frigate_snapshot_base64("abc123")

    assert captured["url"] == f"{config.FRIGATE_API_BASE}/api/events/abc123/snapshot.jpg"
    import base64
    assert base64.b64decode(result) == b"fake-jpeg-bytes"
