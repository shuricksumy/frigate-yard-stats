"""Unit tests confirming crop_worker.py/visit_thumb_worker.py/video_worker.py/alert_video_worker.py
actually resolve and thread through the per-object-type overrides profile_config.py exposes, rather
than continuing to read config.* globals directly. No DB/network required -- db.*/crop.* calls are
monkeypatched.
"""
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import alert_video_worker  # noqa: E402
import config  # noqa: E402
import crop_worker  # noqa: E402
import db  # noqa: E402
import video_worker  # noqa: E402
import visit_thumb_worker  # noqa: E402


# ---- crop_worker.process_claimed_event resolves per-type crop settings ----

def test_process_claimed_event_resolves_per_type_crop_settings(monkeypatch):
    profile = {
        "object_types": {
            "car": {
                "crop_disabled": True, "crop_frame_offset_pct": 0.9,
                "crop_padding_pct": 0.05, "frigate_snapshot_enabled": False,
            },
        },
    }
    captured = {}

    def fake_crop_event(row, **kwargs):
        captured.update(kwargs)
        return {"crop_image_base64": "b64", "sub_label": None, "score": None}
    monkeypatch.setattr(crop_worker.crop, "crop_event", fake_crop_event)
    monkeypatch.setattr(crop_worker.db, "mark_crop_done", lambda *a, **k: None)
    monkeypatch.setattr(crop_worker.telegram, "send_photo", lambda *a, **k: None)

    row = {"id": 1, "objects": "car", "crop_attempt_count": 1, "det_id": "d1"}
    crop_worker.process_claimed_event(row, profile)

    assert captured == {
        "frigate_snapshot_enabled": False,
        "crop_disabled": True,
        "crop_frame_offset_pct": 0.9,
        "crop_padding_pct": 0.05,
    }


def test_process_claimed_event_falls_back_to_global_config_with_no_profile(monkeypatch):
    monkeypatch.setattr(config, "CROP_DISABLED", True)
    monkeypatch.setattr(config, "FRIGATE_SNAPSHOT_ENABLED", False)
    monkeypatch.setattr(config, "CROP_FRAME_OFFSET_PCT", 0.3)
    monkeypatch.setattr(config, "CROP_PADDING_PCT", 0.1)
    captured = {}

    def fake_crop_event(row, **kwargs):
        captured.update(kwargs)
        return {"crop_image_base64": "b64", "sub_label": None, "score": None}
    monkeypatch.setattr(crop_worker.crop, "crop_event", fake_crop_event)
    monkeypatch.setattr(crop_worker.db, "mark_crop_done", lambda *a, **k: None)
    monkeypatch.setattr(crop_worker.telegram, "send_photo", lambda *a, **k: None)

    row = {"id": 1, "objects": "car", "crop_attempt_count": 1, "det_id": "d1"}
    crop_worker.process_claimed_event(row, None)

    assert captured == {
        "frigate_snapshot_enabled": False,
        "crop_disabled": True,
        "crop_frame_offset_pct": 0.3,
        "crop_padding_pct": 0.1,
    }


# ---- visit_thumb_worker.process_claimed_visit resolves per-type settings ----

def test_process_claimed_visit_resolves_per_type_settings(monkeypatch):
    profile = {
        "object_types": {
            "car": {
                "visit_preview_frame_percentages": [10, 40, 70, 95],
                "crop_disabled": True, "crop_padding_pct": 0.05,
            },
        },
    }
    monkeypatch.setattr(
        visit_thumb_worker.db, "get_representative_event_for_visit",
        lambda visit_id: {"objects": "car", "det_id": "d1"},
    )
    captured = {}

    def fake_build(visit, representative, **kwargs):
        captured.update(kwargs)
        return ("grid-b64", "gif-b64")
    monkeypatch.setattr(visit_thumb_worker.crop, "build_visit_preview", fake_build)
    monkeypatch.setattr(visit_thumb_worker.db, "mark_visit_thumb_crop_done", lambda *a, **k: None)

    visit = {"id": 5, "thumb_crop_attempt_count": 1, "cameras": "cam1", "thumb_time": None}
    visit_thumb_worker.process_claimed_visit(visit, profile)

    assert captured == {
        "frame_percentages": [10, 40, 70, 95],
        "crop_disabled": True,
        "crop_padding_pct": 0.05,
    }


# ---- video_worker.run_once / alert_video_worker.run_once / visit_thumb_worker.run_once gate the
# claim by object type and skip claiming entirely when nothing is enabled ----

def test_video_worker_run_once_passes_resolved_object_types_to_claim(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO", False)
    monkeypatch.setattr(config, "VIDEO_PARALLEL_LIMIT", 5)
    monkeypatch.setattr(video_worker.db, "reap_stale_video_processing", lambda: None)
    monkeypatch.setattr(video_worker.db, "count_video_in_progress", lambda: 0)
    captured = {}

    def fake_claim(limit, max_age_hours=None, object_types=None, exclude_object_types=None):
        captured["object_types"] = object_types
        captured["exclude_object_types"] = exclude_object_types
        return []
    monkeypatch.setattr(video_worker.db, "claim_video_batch", fake_claim)

    profile = {"object_types": {"car": {"store_video": True}}}
    video_worker.run_once(profile)

    assert captured == {"object_types": ["car"], "exclude_object_types": None}


def test_video_worker_run_once_skips_claim_entirely_when_nothing_enabled(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO", False)
    monkeypatch.setattr(config, "VIDEO_PARALLEL_LIMIT", 5)
    monkeypatch.setattr(video_worker.db, "reap_stale_video_processing", lambda: None)
    monkeypatch.setattr(video_worker.db, "count_video_in_progress", lambda: 0)

    def fail_if_called(*a, **k):
        raise AssertionError("claim_video_batch should not be called when nothing opts in")
    monkeypatch.setattr(video_worker.db, "claim_video_batch", fail_if_called)

    video_worker.run_once(None)  # STORE_VIDEO false, no profile -- nothing enabled at all


def test_alert_video_worker_run_once_passes_resolved_object_types_to_claim(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_ALERTS", True)
    monkeypatch.setattr(config, "VIDEO_PARALLEL_LIMIT", 5)
    monkeypatch.setattr(alert_video_worker.db, "reap_stale_visit_video_processing", lambda: None)
    monkeypatch.setattr(alert_video_worker.db, "count_visit_video_in_progress", lambda: 0)
    captured = {}

    def fake_claim(limit, max_age_hours=None, object_types=None, exclude_object_types=None):
        captured["object_types"] = object_types
        captured["exclude_object_types"] = exclude_object_types
        return []
    monkeypatch.setattr(alert_video_worker.db, "claim_visit_video_batch", fake_claim)

    profile = {"object_types": {"person": {"store_video_alerts": False}}}
    alert_video_worker.run_once(profile)

    assert captured == {"object_types": None, "exclude_object_types": ["person"]}


def test_alert_video_worker_run_once_skips_claim_entirely_when_nothing_enabled(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_ALERTS", False)
    monkeypatch.setattr(config, "VIDEO_PARALLEL_LIMIT", 5)
    monkeypatch.setattr(alert_video_worker.db, "reap_stale_visit_video_processing", lambda: None)
    monkeypatch.setattr(alert_video_worker.db, "count_visit_video_in_progress", lambda: 0)

    def fail_if_called(*a, **k):
        raise AssertionError("claim_visit_video_batch should not be called when nothing opts in")
    monkeypatch.setattr(alert_video_worker.db, "claim_visit_video_batch", fail_if_called)

    alert_video_worker.run_once(None)  # STORE_VIDEO_ALERTS false, no profile -- nothing enabled


def test_visit_thumb_worker_run_once_passes_resolved_object_types_to_claim(monkeypatch):
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_ENABLED", True)
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_PARALLEL_LIMIT", 5)
    monkeypatch.setattr(visit_thumb_worker.db, "reap_stale_visit_thumb_crop_processing", lambda: None)
    monkeypatch.setattr(visit_thumb_worker.db, "count_visit_thumb_crop_in_progress", lambda: 0)
    captured = {}

    def fake_claim(limit, object_types=None, exclude_object_types=None):
        captured["object_types"] = object_types
        captured["exclude_object_types"] = exclude_object_types
        return []
    monkeypatch.setattr(visit_thumb_worker.db, "claim_visit_thumb_crop_batch", fake_claim)

    profile = {"object_types": {"dog": {"visit_thumb_crop_enabled": False}}}
    visit_thumb_worker.run_once(profile)

    assert captured == {"object_types": None, "exclude_object_types": ["dog"]}


def test_visit_thumb_worker_run_once_skips_claim_when_nothing_enabled(monkeypatch):
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_ENABLED", False)
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_PARALLEL_LIMIT", 5)
    monkeypatch.setattr(visit_thumb_worker.db, "reap_stale_visit_thumb_crop_processing", lambda: None)
    monkeypatch.setattr(visit_thumb_worker.db, "count_visit_thumb_crop_in_progress", lambda: 0)

    def fail_if_called(*a, **k):
        raise AssertionError("claim_visit_thumb_crop_batch should not be called when nothing opts in")
    monkeypatch.setattr(visit_thumb_worker.db, "claim_visit_thumb_crop_batch", fail_if_called)

    visit_thumb_worker.run_once(None)
