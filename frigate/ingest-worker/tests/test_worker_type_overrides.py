"""Unit tests confirming crop_worker.py/video_worker.py/alert_video_worker.py actually resolve and
thread through the per-object-type overrides profile_config.py exposes, rather than continuing to
read config.* globals directly. No DB/network required -- db.*/crop.* calls are monkeypatched.
"""
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import alert_video_worker  # noqa: E402
import config  # noqa: E402
import crop_worker  # noqa: E402
import video_worker  # noqa: E402


def _crop_event_result():
    return {"crop_image_base64": "b64", "full_res_image_base64": "full-b64", "sub_label": None, "score": None}


# ---- crop_worker.process_claimed_event resolves per-type crop settings ----

def test_process_claimed_event_resolves_per_type_crop_settings(monkeypatch):
    profile = {
        "object_types": {
            "car": {
                "crop_disabled": True, "crop_frame_offset_pct": 0.9,
                "crop_padding_pct": 0.05, "ai_image_max_dimension": 640,
            },
        },
    }
    captured = {}

    def fake_crop_event(row, **kwargs):
        captured.update(kwargs)
        return {"crop_image_base64": "b64", "full_res_image_base64": "full-b64", "sub_label": None, "score": None}
    monkeypatch.setattr(crop_worker.crop, "crop_event", fake_crop_event)
    monkeypatch.setattr(crop_worker.db, "mark_crop_done", lambda *a, **k: None)
    monkeypatch.setattr(crop_worker.telegram, "send_photo", lambda *a, **k: None)

    row = {"id": 1, "objects": "car", "crop_attempt_count": 1, "det_id": "d1"}
    crop_worker.process_claimed_event(row, profile)

    assert captured == {
        "ai_image_max_dimension": 640,
        "crop_disabled": True,
        "crop_frame_offset_pct": 0.9,
        "crop_padding_pct": 0.05,
    }


def test_process_claimed_event_falls_back_to_global_config_with_no_profile(monkeypatch):
    monkeypatch.setattr(config, "CROP_DISABLED", True)
    monkeypatch.setattr(config, "MAX_CROP_DIMENSION", 999)
    monkeypatch.setattr(config, "CROP_FRAME_OFFSET_PCT", 0.3)
    monkeypatch.setattr(config, "CROP_PADDING_PCT", 0.1)
    captured = {}

    def fake_crop_event(row, **kwargs):
        captured.update(kwargs)
        return {"crop_image_base64": "b64", "full_res_image_base64": "full-b64", "sub_label": None, "score": None}
    monkeypatch.setattr(crop_worker.crop, "crop_event", fake_crop_event)
    monkeypatch.setattr(crop_worker.db, "mark_crop_done", lambda *a, **k: None)
    monkeypatch.setattr(crop_worker.telegram, "send_photo", lambda *a, **k: None)

    row = {"id": 1, "objects": "car", "crop_attempt_count": 1, "det_id": "d1"}
    crop_worker.process_claimed_event(row, None)

    assert captured == {
        "ai_image_max_dimension": 999,
        "crop_disabled": True,
        "crop_frame_offset_pct": 0.3,
        "crop_padding_pct": 0.1,
    }


# ---- crop_worker.process_claimed_event's opt-in store_event_images side effect ----

def test_process_claimed_event_stores_image_when_enabled(monkeypatch):
    profile = {"object_types": {"car": {"store_event_images": True}}}
    monkeypatch.setattr(crop_worker.crop, "crop_event", lambda row, **k: _crop_event_result())
    monkeypatch.setattr(crop_worker.db, "mark_crop_done", lambda *a, **k: None)
    monkeypatch.setattr(crop_worker.telegram, "send_photo", lambda *a, **k: None)

    captured = {}

    def fake_store(row, image_base64):
        captured["image_base64"] = image_base64
        return "/data/event-images/car-1.jpg"
    monkeypatch.setattr(crop_worker.event_images, "store_event_image", fake_store)
    set_path_calls = []
    monkeypatch.setattr(crop_worker.db, "set_event_image_path", lambda event_id, path: set_path_calls.append((event_id, path)))

    row = {"id": 1, "objects": "car", "crop_attempt_count": 1, "det_id": "d1"}
    crop_worker.process_claimed_event(row, profile)

    assert captured["image_base64"] == "full-b64"
    assert set_path_calls == [(1, "/data/event-images/car-1.jpg")]


def test_process_claimed_event_does_not_store_image_by_default(monkeypatch):
    monkeypatch.setattr(crop_worker.crop, "crop_event", lambda row, **k: _crop_event_result())
    monkeypatch.setattr(crop_worker.db, "mark_crop_done", lambda *a, **k: None)
    monkeypatch.setattr(crop_worker.telegram, "send_photo", lambda *a, **k: None)

    def fail_if_called(*a, **k):
        raise AssertionError("store_event_image should not run when store_event_images is off")
    monkeypatch.setattr(crop_worker.event_images, "store_event_image", fail_if_called)

    row = {"id": 1, "objects": "car", "crop_attempt_count": 1, "det_id": "d1"}
    crop_worker.process_claimed_event(row, None)  # no profile -- STORE_EVENT_IMAGES defaults False


def test_process_claimed_event_storage_failure_is_non_fatal(monkeypatch):
    profile = {"object_types": {"car": {"store_event_images": True}}}
    monkeypatch.setattr(crop_worker.crop, "crop_event", lambda row, **k: _crop_event_result())
    mark_done_calls = []
    monkeypatch.setattr(crop_worker.db, "mark_crop_done", lambda *a, **k: mark_done_calls.append(a))
    monkeypatch.setattr(crop_worker.telegram, "send_photo", lambda *a, **k: None)

    def fail_to_store(row, image_base64):
        raise OSError("disk full")
    monkeypatch.setattr(crop_worker.event_images, "store_event_image", fail_to_store)

    mark_failed_calls = []
    monkeypatch.setattr(crop_worker.db, "mark_crop_failed", lambda event_id: mark_failed_calls.append(event_id))

    row = {"id": 1, "objects": "car", "crop_attempt_count": 1, "det_id": "d1"}
    crop_worker.process_claimed_event(row, profile)

    # The crop itself still succeeded (mark_crop_done was called) -- a storage failure must not be
    # mistaken for a crop failure.
    assert mark_done_calls
    assert mark_failed_calls == []


# ---- video_worker.run_once / alert_video_worker.run_once gate the claim by object type and skip
# claiming entirely when nothing is enabled ----

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
