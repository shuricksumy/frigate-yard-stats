"""Tests for the alert AI stage (AI_ALERTS_ENABLED): db.py's visits.alert_ai_status queue
functions (claim_alert_ai_batch/complete_visit_sighting/fail_alert_ai_event/
get_visit_alert_sighting) and alert_ai_worker.py's parsing/processing logic.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Additionally requires pgvector (pgvector/pgvector:pg16), same as
test_semantic_search.py.
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import alert_ai_worker  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402

PROFILE = {
    "object_types": {
        "car": {
            "chat_path": "/vehicle-slot/v1/chat/completions",
            "event_prompt": "vehicle event prompt", "alert_prompt": "vehicle alert prompt",
        },
        "person": {
            "chat_path": "/person-slot/v1/chat/completions",
            "event_prompt": "person event prompt", "alert_prompt": "person alert prompt",
        },
    },
}


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def _insert_event(objects="car", camera="pytest-alert-cam"):
    det_id = f"pytest-alert-{uuid.uuid4()}"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status)
        VALUES (%s, 'z', %s, now(), now(), %s, true, true, 'done', 'new')
        RETURNING id, det_id
        """,
        (camera, objects, det_id), fetch=True,
    )
    return rows[0]["id"], rows[0]["det_id"]


def _make_visit(objects="car", camera="pytest-alert-cam", alert_ai_status="new"):
    event_id, det_id = _insert_event(objects=objects, camera=camera)
    visit_id = db.record_visit({
        "camera": camera, "zone": "z", "objects": objects,
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id],
    })
    db._execute(
        "UPDATE yard_stats.visits SET alert_ai_status = %s WHERE id = %s",
        (alert_ai_status, visit_id),
    )
    return visit_id, event_id


def _cleanup_visit(visit_id, *event_ids):
    db._execute("DELETE FROM yard_stats.visit_sightings WHERE visit_id = %s", (visit_id,))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(event_ids),))
    db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


# ---- db.get_raw_events_for_visit ----

def test_get_raw_events_for_visit_returns_every_linked_event(conn_ok):
    camera = f"pytest-alert-visitevents-{uuid.uuid4()}"
    car_id, car_det = _insert_event(objects="car", camera=camera)
    person_id, person_det = _insert_event(objects="person", camera=camera)
    visit_id = db.record_visit({
        "camera": camera, "zone": "z", "objects": "car,person",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [car_det, person_det],
    })
    try:
        rows = db.get_raw_events_for_visit(visit_id)
        assert {r["det_id"] for r in rows} == {car_det, person_det}
        assert {r["objects"] for r in rows} == {"car", "person"}
        for r in rows:
            assert set(r.keys()) >= {"id", "det_id", "objects", "start_ts", "end_ts"}
    finally:
        _cleanup_visit(visit_id, car_id, person_id)


def test_get_raw_events_for_visit_orders_by_objects_then_start_ts(conn_ok):
    camera = f"pytest-alert-visitevents-{uuid.uuid4()}"
    car1_id, car1_det = _insert_event(objects="car", camera=camera)
    car2_id, car2_det = _insert_event(objects="car", camera=camera)
    person_id, person_det = _insert_event(objects="person", camera=camera)
    visit_id = db.record_visit({
        "camera": camera, "zone": "z", "objects": "car,person",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [car1_det, car2_det, person_det],
    })
    try:
        rows = db.get_raw_events_for_visit(visit_id)
        assert [r["objects"] for r in rows] == ["car", "car", "person"]
    finally:
        _cleanup_visit(visit_id, car1_id, car2_id, person_id)


def test_get_raw_events_for_visit_empty_for_visit_with_no_linked_events(conn_ok):
    visit_id = db.record_visit({
        "camera": f"pytest-alert-visitevents-{uuid.uuid4()}", "zone": "z", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": ["nonexistent-det-id"],
    })
    try:
        assert db.get_raw_events_for_visit(visit_id) == []
    finally:
        db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


# ---- run_once (per-type ai_alerts_enabled filtering, see profile_config.py) ----

def test_run_once_excludes_type_that_opts_out_despite_global_default_on(monkeypatch):
    monkeypatch.setattr(config, "AI_ALERTS_ENABLED", True)
    profile = {
        "object_types": {
            "car": {**PROFILE["object_types"]["car"], "ai_alerts_enabled": False},
            "person": PROFILE["object_types"]["person"],
        },
    }
    captured = {}

    def fake_claim(object_types, *a, **k):
        captured["object_types"] = object_types
        return []

    monkeypatch.setattr(db, "claim_alert_ai_batch", fake_claim)
    alert_ai_worker.run_once(profile)
    assert captured["object_types"] == ["person"]


def test_run_once_includes_type_that_opts_in_despite_global_default_off(monkeypatch):
    monkeypatch.setattr(config, "AI_ALERTS_ENABLED", False)
    profile = {
        "object_types": {
            "car": {**PROFILE["object_types"]["car"], "ai_alerts_enabled": True},
            "person": PROFILE["object_types"]["person"],
        },
    }
    captured = {}

    def fake_claim(object_types, *a, **k):
        captured["object_types"] = object_types
        return []

    monkeypatch.setattr(db, "claim_alert_ai_batch", fake_claim)
    alert_ai_worker.run_once(profile)
    assert captured["object_types"] == ["car"]


# ---- db.claim_alert_ai_batch ----

def test_claim_alert_ai_batch_claims_visit_immediately(conn_ok):
    visit_id, event_id = _make_visit()
    try:
        claimed = db.claim_alert_ai_batch(["car"], parallel_limit=10, stale_minutes=5)
        claimed_ids = {r["id"] for r in claimed}
        assert visit_id in claimed_ids
        row = next(r for r in claimed if r["id"] == visit_id)
        assert row["objects"] == "car"  # from the representative event, not visits.objects
        updated = db.get_visit(visit_id)
        assert updated["alert_ai_status"] == "processing"
    finally:
        _cleanup_visit(visit_id, event_id)


def test_claim_alert_ai_batch_claims_visit_with_no_thumb_crop_stage_run(conn_ok):
    # No pre-built grid needed at all -- a visit is claimable as soon as it exists, since the
    # alert stage now gathers its own high-res crops directly at processing time.
    event_id, det_id = _insert_event()
    visit_id = db.record_visit({
        "camera": "pytest-alert-cam", "zone": "z", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id],
    })
    try:
        claimed_ids = {r["id"] for r in db.claim_alert_ai_batch(["car"], parallel_limit=10, stale_minutes=5)}
        assert visit_id in claimed_ids
    finally:
        _cleanup_visit(visit_id, event_id)


def test_claim_alert_ai_batch_respects_object_types_filter(conn_ok):
    visit_id, event_id = _make_visit(objects="person")
    try:
        claimed_ids = {r["id"] for r in db.claim_alert_ai_batch(["car"], parallel_limit=10, stale_minutes=5)}
        assert visit_id not in claimed_ids
        claimed_ids = {r["id"] for r in db.claim_alert_ai_batch(["person"], parallel_limit=10, stale_minutes=5)}
        assert visit_id in claimed_ids
    finally:
        _cleanup_visit(visit_id, event_id)


def test_claim_alert_ai_batch_respects_parallel_limit_via_in_progress_count(conn_ok):
    visit_id, event_id = _make_visit(alert_ai_status="processing")
    try:
        # capacity = parallel_limit(1) - in_progress(1) = 0
        claimed = db.claim_alert_ai_batch(["car"], parallel_limit=1, stale_minutes=5)
        assert claimed == []
    finally:
        _cleanup_visit(visit_id, event_id)


# ---- db.complete_visit_sighting ----

def test_complete_visit_sighting_marks_alert_ai_status_done(conn_ok):
    visit_id, event_id = _make_visit()
    try:
        db.complete_visit_sighting(visit_id, "car", "orange Dacia Duster, roof rails, pulled in and parked")
        updated = db.get_visit(visit_id)
        assert updated["alert_ai_status"] == "done"
        rows = db._execute(
            "SELECT object_label, description FROM yard_stats.visit_sightings WHERE visit_id = %s",
            (visit_id,), fetch=True,
        )
        assert rows[0]["object_label"] == "car"
        assert rows[0]["description"] == "orange Dacia Duster, roof rails, pulled in and parked"
    finally:
        _cleanup_visit(visit_id, event_id)


def test_complete_visit_sighting_works_for_any_object_label(conn_ok):
    visit_id, event_id = _make_visit(objects="person")
    try:
        db.complete_visit_sighting(visit_id, "person", "walked to the door")
        updated = db.get_visit(visit_id)
        assert updated["alert_ai_status"] == "done"
        rows = db._execute(
            "SELECT description FROM yard_stats.visit_sightings WHERE visit_id = %s",
            (visit_id,), fetch=True,
        )
        assert rows[0]["description"] == "walked to the door"
    finally:
        _cleanup_visit(visit_id, event_id)


# ---- db.fail_alert_ai_event ----

def test_fail_alert_ai_event_retries_below_cap(conn_ok):
    visit_id, event_id = _make_visit()
    try:
        result = db.fail_alert_ai_event(visit_id, max_attempts=3)
        assert result["alert_ai_status"] == "retry"
        assert result["alert_ai_attempt_count"] == 1
    finally:
        _cleanup_visit(visit_id, event_id)


def test_fail_alert_ai_event_fails_at_cap(conn_ok):
    visit_id, event_id = _make_visit()
    try:
        db._execute("UPDATE yard_stats.visits SET alert_ai_attempt_count = 2 WHERE id = %s", (visit_id,))
        result = db.fail_alert_ai_event(visit_id, max_attempts=3)
        assert result["alert_ai_status"] == "failed"
        assert result["alert_ai_attempt_count"] == 3
    finally:
        _cleanup_visit(visit_id, event_id)


# ---- db.get_visit_alert_sighting ----

def test_get_visit_alert_sighting_returns_none_when_not_analyzed(conn_ok):
    visit_id, event_id = _make_visit()
    try:
        assert db.get_visit_alert_sighting(visit_id) is None
    finally:
        _cleanup_visit(visit_id, event_id)


def test_get_visit_alert_sighting_returns_result(conn_ok):
    visit_id, event_id = _make_visit()
    try:
        db.complete_visit_sighting(visit_id, "car", "red sedan")
        result = db.get_visit_alert_sighting(visit_id)
        assert result["object_label"] == "car"
        assert result["description"] == "red sedan"
    finally:
        _cleanup_visit(visit_id, event_id)


# ---- db.set_visit_alert_image_paths / get_visit_alert_image_paths ----

def test_get_visit_alert_image_paths_empty_before_any_stored(conn_ok):
    visit_id, event_id = _make_visit()
    try:
        assert db.get_visit_alert_image_paths(visit_id) == []
    finally:
        _cleanup_visit(visit_id, event_id)


def test_set_and_get_visit_alert_image_paths_round_trip(conn_ok):
    visit_id, event_id = _make_visit()
    try:
        db.set_visit_alert_image_paths(visit_id, ["/data/alert-images/a.jpg", "/data/alert-images/b.jpg"])
        assert db.get_visit_alert_image_paths(visit_id) == [
            "/data/alert-images/a.jpg", "/data/alert-images/b.jpg",
        ]
    finally:
        _cleanup_visit(visit_id, event_id)


def test_set_visit_alert_image_paths_empty_list_clears_to_null(conn_ok):
    visit_id, event_id = _make_visit()
    try:
        db.set_visit_alert_image_paths(visit_id, ["/data/alert-images/a.jpg"])
        db.set_visit_alert_image_paths(visit_id, [])
        assert db.get_visit_alert_image_paths(visit_id) == []
        row = db.get_visit(visit_id)
        assert row["alert_image_paths"] is None
    finally:
        _cleanup_visit(visit_id, event_id)


# ---- alert_ai_worker.parse_alert_sighting_response ----

def test_parse_alert_sighting_response_uses_raw_content_and_objects_label():
    response = {"choices": [{"message": {"content": "blue hatchback, drove past left to right"}}]}
    fields = alert_ai_worker.parse_alert_sighting_response(response, {"id": 5, "objects": "car"})
    assert fields == {"visit_id": 5, "object_label": "car", "description": "blue hatchback, drove past left to right"}


def test_parse_alert_sighting_response_person():
    response = {"choices": [{"message": {"content": "wearing a red jacket, walking toward the door"}}]}
    fields = alert_ai_worker.parse_alert_sighting_response(response, {"id": 7, "objects": "person"})
    assert fields == {"visit_id": 7, "object_label": "person", "description": "wearing a red jacket, walking toward the door"}


# ---- alert_ai_worker._select_events_for_alert (pure selection logic, no DB/network) ----

def test_select_events_for_alert_one_representative_per_type_when_under_cap():
    events = [
        {"id": 1, "objects": "car", "start_ts": 10},
        {"id": 2, "objects": "person", "start_ts": 20},
    ]
    selected = alert_ai_worker._select_events_for_alert(events, max_images=4)
    assert [e["id"] for e in selected] == [1, 2]


def test_select_events_for_alert_truncates_representatives_when_over_cap():
    events = [
        {"id": 1, "objects": "car", "start_ts": 10},
        {"id": 2, "objects": "person", "start_ts": 20},
        {"id": 3, "objects": "dog", "start_ts": 30},
    ]
    selected = alert_ai_worker._select_events_for_alert(events, max_images=2)
    assert [e["id"] for e in selected] == [1, 2]  # earliest 2 representatives


def test_select_events_for_alert_fills_remaining_slots_from_same_type_retracks():
    events = [
        {"id": i, "objects": "car", "start_ts": i * 10}
        for i in range(1, 6)  # 5 re-tracks of the same real car
    ]
    selected = alert_ai_worker._select_events_for_alert(events, max_images=3)
    assert len(selected) == 3
    ids = [e["id"] for e in selected]
    assert 1 in ids  # the representative (earliest) is always included
    assert ids == sorted(ids)  # returned in chronological order


def test_select_events_for_alert_round_robins_across_multiple_noisy_types():
    events = [
        {"id": 1, "objects": "car", "start_ts": 10},
        {"id": 2, "objects": "car", "start_ts": 20},
        {"id": 3, "objects": "car", "start_ts": 30},
        {"id": 4, "objects": "person", "start_ts": 15},
        {"id": 5, "objects": "person", "start_ts": 25},
    ]
    selected = alert_ai_worker._select_events_for_alert(events, max_images=4)
    labels = [e["objects"] for e in selected]
    assert len(selected) == 4
    assert labels.count("car") >= 1
    assert labels.count("person") >= 1  # not all 4 slots hogged by one noisy type


def test_select_events_for_alert_empty_events_returns_empty():
    assert alert_ai_worker._select_events_for_alert([], max_images=4) == []


def test_select_events_for_alert_single_event_under_cap():
    events = [{"id": 1, "objects": "car", "start_ts": 10}]
    assert alert_ai_worker._select_events_for_alert(events, max_images=4) == events


# ---- alert_ai_worker._gather_alert_images (per-event failure tolerance) ----

def test_gather_alert_images_skips_individual_failures(monkeypatch):
    def fake_crop(event, **kwargs):
        if event["id"] == 2:
            raise RuntimeError("Frigate clip already rolled off")
        return f"crop-for-{event['id']}"

    monkeypatch.setattr(alert_ai_worker.crop, "crop_event_high_res", fake_crop)
    events = [{"id": 1, "det_id": "d1"}, {"id": 2, "det_id": "d2"}, {"id": 3, "det_id": "d3"}]
    gathered = alert_ai_worker._gather_alert_images(events)
    # (event, image) pairs, not a bare image list -- so alert_images.store_alert_images can name
    # each stored file after its own source event even when a middle one was skipped.
    assert gathered == [(events[0], "crop-for-1"), (events[2], "crop-for-3")]


def test_gather_alert_images_empty_when_all_fail(monkeypatch):
    def _boom(event, **kwargs):
        raise RuntimeError("gone")
    monkeypatch.setattr(alert_ai_worker.crop, "crop_event_high_res", _boom)
    images = alert_ai_worker._gather_alert_images([{"id": 1, "det_id": "d1"}])
    assert images == []


# ---- alert_ai_worker._resolve_alert_type_config ----

def test_resolve_alert_type_config_falls_back_to_plain_provider_when_alert_keys_absent():
    type_config = {"provider": "llama_proxy", "chat_path": "/x", "model": "m"}
    resolved = alert_ai_worker._resolve_alert_type_config(type_config)
    assert resolved["provider"] == "llama_proxy"
    assert resolved["chat_path"] == "/x"


def test_resolve_alert_type_config_prefers_alert_specific_overrides():
    type_config = {
        "provider": "llama_proxy", "chat_path": "/local", "model": "local-model",
        "alert_provider": "openai", "alert_model": "gpt-4o", "alert_chat_path": "/unused",
    }
    resolved = alert_ai_worker._resolve_alert_type_config(type_config)
    assert resolved["provider"] == "openai"
    assert resolved["model"] == "gpt-4o"
    assert resolved["chat_path"] == "/unused"


# ---- alert_ai_worker.process_claimed_visit (mocked chat call + crop, no real network/DB) ----

def test_process_claimed_visit_success(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(config, "ALERT_AI_INITIAL_WAIT_SECONDS", 0)
    monkeypatch.setattr(db, "get_raw_events_for_visit", lambda visit_id: [
        {"id": 100, "det_id": "d1", "objects": "car", "start_ts": 10, "end_ts": 20},
    ])
    monkeypatch.setattr(alert_ai_worker.crop, "crop_event_high_res", lambda event, **k: "high-res-base64")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "orange suv, parked"}}]}

    calls = []

    def fake_post(url, json=None, **kwargs):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(alert_ai_worker.ai_worker.requests, "post", fake_post)
    inserted = []
    monkeypatch.setattr(db, "complete_visit_sighting", lambda *a, **k: inserted.append(a) or 1)
    failed = []
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: failed.append((a, k)))

    row = {"id": 9, "objects": "car", "det_id": "d1", "alert_ai_attempt_count": 0}
    alert_ai_worker.process_claimed_visit(row, PROFILE)

    assert len(inserted) == 1
    assert inserted[0][:3] == (9, "car", "orange suv, parked")
    assert not failed
    assert calls[0] == "http://llama.test/vehicle-slot/v1/chat/completions"


def test_process_claimed_visit_stores_images_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(config, "ALERT_AI_INITIAL_WAIT_SECONDS", 0)
    monkeypatch.setattr(db, "get_raw_events_for_visit", lambda visit_id: [
        {"id": 100, "det_id": "d1", "objects": "car", "start_ts": 10, "end_ts": 20},
    ])
    monkeypatch.setattr(alert_ai_worker.crop, "crop_event_high_res", lambda event, **k: "high-res-base64")
    monkeypatch.setattr(alert_ai_worker.ai_worker, "_chat_request", lambda *a, **k: {"choices": [{"message": {"content": "orange suv"}}]})
    monkeypatch.setattr(db, "complete_visit_sighting", lambda *a, **k: 1)
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: None)

    stored = {}
    monkeypatch.setattr(
        alert_ai_worker.alert_images, "store_alert_images",
        lambda visit, events, images: stored.update(visit=visit, events=events, images=images) or ["p1"],
    )
    recorded = []
    monkeypatch.setattr(db, "set_visit_alert_image_paths", lambda visit_id, paths: recorded.append((visit_id, paths)))

    profile = {
        "object_types": {
            "car": {**PROFILE["object_types"]["car"], "store_alert_images": True},
        },
    }
    row = {"id": 9, "objects": "car", "det_id": "d1", "alert_ai_attempt_count": 0, "cameras": "outside", "start_ts": 10}
    alert_ai_worker.process_claimed_visit(row, profile)

    assert stored["images"] == ["high-res-base64"]
    assert stored["events"] == [{"id": 100, "det_id": "d1", "objects": "car", "start_ts": 10, "end_ts": 20}]
    assert recorded == [(9, ["p1"])]


def test_process_claimed_visit_does_not_store_images_by_default(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(config, "ALERT_AI_INITIAL_WAIT_SECONDS", 0)
    monkeypatch.setattr(db, "get_raw_events_for_visit", lambda visit_id: [
        {"id": 100, "det_id": "d1", "objects": "car", "start_ts": 10, "end_ts": 20},
    ])
    monkeypatch.setattr(alert_ai_worker.crop, "crop_event_high_res", lambda event, **k: "high-res-base64")
    monkeypatch.setattr(alert_ai_worker.ai_worker, "_chat_request", lambda *a, **k: {"choices": [{"message": {"content": "orange suv"}}]})
    monkeypatch.setattr(db, "complete_visit_sighting", lambda *a, **k: 1)
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: None)

    store_calls = []
    monkeypatch.setattr(alert_ai_worker.alert_images, "store_alert_images", lambda *a, **k: store_calls.append(1))
    record_calls = []
    monkeypatch.setattr(db, "set_visit_alert_image_paths", lambda *a, **k: record_calls.append(1))

    row = {"id": 9, "objects": "car", "det_id": "d1", "alert_ai_attempt_count": 0, "cameras": "outside", "start_ts": 10}
    alert_ai_worker.process_claimed_visit(row, PROFILE)  # PROFILE has no store_alert_images key

    assert not store_calls
    assert not record_calls


def test_process_claimed_visit_storage_failure_is_non_fatal(monkeypatch):
    # A disk-write failure (full disk, permissions) shouldn't take down an AI analysis that already
    # has its images in hand and is about to (or already did) succeed.
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(config, "ALERT_AI_INITIAL_WAIT_SECONDS", 0)
    monkeypatch.setattr(db, "get_raw_events_for_visit", lambda visit_id: [
        {"id": 100, "det_id": "d1", "objects": "car", "start_ts": 10, "end_ts": 20},
    ])
    monkeypatch.setattr(alert_ai_worker.crop, "crop_event_high_res", lambda event, **k: "high-res-base64")
    monkeypatch.setattr(alert_ai_worker.ai_worker, "_chat_request", lambda *a, **k: {"choices": [{"message": {"content": "orange suv"}}]})
    inserted = []
    monkeypatch.setattr(db, "complete_visit_sighting", lambda *a, **k: inserted.append(1) or 1)
    failed = []
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: failed.append(1))

    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(alert_ai_worker.alert_images, "store_alert_images", _boom)

    profile = {
        "object_types": {
            "car": {**PROFILE["object_types"]["car"], "store_alert_images": True},
        },
    }
    row = {"id": 9, "objects": "car", "det_id": "d1", "alert_ai_attempt_count": 0, "cameras": "outside", "start_ts": 10}
    alert_ai_worker.process_claimed_visit(row, profile)

    assert inserted == [1]  # analysis still completed
    assert not failed


def test_process_claimed_visit_sends_one_image_per_selected_event(monkeypatch):
    # Multi-image sending only actually happens on a hosted provider (llama_proxy only ever sends
    # the first image, by design -- see test_process_claimed_visit_success for that path), so this
    # exercises the openai branch specifically.
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "ALERT_AI_INITIAL_WAIT_SECONDS", 0)
    monkeypatch.setattr(config, "ALERT_AI_MAX_IMAGES", 4)
    monkeypatch.setattr(db, "get_raw_events_for_visit", lambda visit_id: [
        {"id": 100, "det_id": "d1", "objects": "car", "start_ts": 10, "end_ts": 12},
        {"id": 101, "det_id": "d2", "objects": "car", "start_ts": 30, "end_ts": 32},
    ])
    monkeypatch.setattr(
        alert_ai_worker.crop, "crop_event_high_res",
        lambda event, **k: f"crop-for-{event['id']}",
    )
    captured = {}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_post(url, json=None, **kwargs):
        if url.endswith("/v1/chat/completions"):
            captured["json"] = json
            return _Resp({"choices": [{"message": {"content": "orange suv, pulled in and parked"}}]})
        return _Resp({"data": [{"embedding": [0.1, 0.2]}]})

    monkeypatch.setattr(alert_ai_worker.ai_worker.requests, "post", fake_post)
    monkeypatch.setattr(db, "complete_visit_sighting", lambda *a, **k: 1)
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: None)

    profile = {
        "object_types": {
            "car": {"provider": "openai", "model": "gpt-4o", "alert_prompt": "describe the series"},
        },
    }
    row = {"id": 9, "objects": "car", "det_id": "d1", "alert_ai_attempt_count": 0}
    alert_ai_worker.process_claimed_visit(row, profile)

    content = captured["json"]["messages"][0]["content"]
    image_urls = [b["image_url"]["url"] for b in content[1:]]
    assert image_urls == [
        "data:image/jpeg;base64,crop-for-100", "data:image/jpeg;base64,crop-for-101",
    ]


def test_process_claimed_visit_routes_to_openai_provider(monkeypatch):
    # Confirms process_claimed_visit threads type_config through ai_worker._chat_request/
    # parse_alert_sighting_response the same way ai_worker.process_claimed_event does.
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "ALERT_AI_INITIAL_WAIT_SECONDS", 0)
    monkeypatch.setattr(db, "get_raw_events_for_visit", lambda visit_id: [
        {"id": 100, "det_id": "d7", "objects": "car", "start_ts": 10, "end_ts": 20},
    ])
    monkeypatch.setattr(alert_ai_worker.crop, "crop_event_high_res", lambda event, **k: "high-res-base64")
    captured = {}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_post(url, json=None, **kwargs):
        if url.endswith("/v1/chat/completions"):
            captured.update(url=url, json=json)
            return _Resp({"choices": [{"message": {"content": "orange suv, drove past"}}]})
        return _Resp({"data": [{"embedding": [0.1, 0.2]}]})

    monkeypatch.setattr(alert_ai_worker.ai_worker.requests, "post", fake_post)
    inserted = []
    monkeypatch.setattr(db, "complete_visit_sighting", lambda *a, **k: inserted.append(a) or 1)
    failed = []
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: failed.append((a, k)))

    profile = {
        "object_types": {
            "car": {"provider": "openai", "model": "gpt-4o", "alert_prompt": "describe the series"},
        },
    }
    row = {"id": 15, "objects": "car", "det_id": "d7", "alert_ai_attempt_count": 0}
    alert_ai_worker.process_claimed_visit(row, profile)

    assert not failed
    assert len(inserted) == 1
    assert inserted[0][:3] == (15, "car", "orange suv, drove past")
    assert captured["url"] == f"{config.OPENAI_BASE_URL}/v1/chat/completions"
    assert captured["json"]["model"] == "gpt-4o"


def test_process_claimed_visit_alert_provider_override_wins_over_plain_provider(monkeypatch):
    # A type whose event_prompt stays on llama_proxy but whose alert_prompt should route to a
    # hosted provider -- alert_provider/alert_model let that be expressed without a second
    # profiles.yaml section.
    monkeypatch.setattr(config, "ALERT_AI_INITIAL_WAIT_SECONDS", 0)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(db, "get_raw_events_for_visit", lambda visit_id: [
        {"id": 100, "det_id": "d1", "objects": "car", "start_ts": 10, "end_ts": 20},
    ])
    monkeypatch.setattr(alert_ai_worker.crop, "crop_event_high_res", lambda event, **k: "high-res-base64")
    captured = {}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_post(url, json=None, **kwargs):
        if url.endswith("/v1/messages"):
            captured.update(url=url, json=json)
            return _Resp({"content": [{"text": "red sedan, drove past"}]})
        return _Resp({"data": [{"embedding": [0.1, 0.2]}]})

    monkeypatch.setattr(alert_ai_worker.ai_worker.requests, "post", fake_post)
    monkeypatch.setattr(db, "complete_visit_sighting", lambda *a, **k: 1)
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: None)

    profile = {
        "object_types": {
            "car": {
                "provider": "llama_proxy", "chat_path": "/local", "event_prompt": "vehicle event prompt",
                "alert_prompt": "describe the series",
                "alert_provider": "anthropic", "alert_model": "claude-opus-4-8",
            },
        },
    }
    row = {"id": 9, "objects": "car", "det_id": "d1", "alert_ai_attempt_count": 0}
    alert_ai_worker.process_claimed_visit(row, profile)

    assert captured["url"] == f"{config.ANTHROPIC_BASE_URL}/v1/messages"
    assert captured["json"]["model"] == "claude-opus-4-8"


def test_process_claimed_visit_unmapped_object_type_is_skipped(monkeypatch):
    inserted = []
    monkeypatch.setattr(db, "complete_visit_sighting", lambda *a, **k: inserted.append(a))
    failed = []
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: failed.append((a, k)))

    row = {"id": 11, "objects": "dog", "det_id": "d2"}
    alert_ai_worker.process_claimed_visit(row, PROFILE)

    assert not inserted
    assert not failed  # unmapped type is a silent skip, not a failure


def test_process_claimed_visit_chat_failure_routes_to_fail_alert_ai_event(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(config, "ALERT_AI_INITIAL_WAIT_SECONDS", 0)
    monkeypatch.setattr(db, "get_raw_events_for_visit", lambda visit_id: [
        {"id": 100, "det_id": "d3", "objects": "car", "start_ts": 10, "end_ts": 20},
    ])
    monkeypatch.setattr(alert_ai_worker.crop, "crop_event_high_res", lambda event, **k: "high-res-base64")

    def _raise(*a, **k):
        raise ConnectionError("backend down")

    monkeypatch.setattr(alert_ai_worker.ai_worker.requests, "post", _raise)
    failed = []
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: failed.append((a, k)))

    row = {"id": 13, "objects": "car", "det_id": "d3", "alert_ai_attempt_count": 0}
    alert_ai_worker.process_claimed_visit(row, PROFILE)

    assert failed == [((13, config.AI_STAGE_MAX_ATTEMPTS), {})]


def test_process_claimed_visit_fails_when_no_images_could_be_gathered(monkeypatch):
    # Every linked event's high-res crop fails (e.g. Frigate's clip already rolled off for all of
    # them) -- routes to fail_alert_ai_event rather than sending an empty-image chat request.
    monkeypatch.setattr(config, "ALERT_AI_INITIAL_WAIT_SECONDS", 0)
    monkeypatch.setattr(db, "get_raw_events_for_visit", lambda visit_id: [
        {"id": 100, "det_id": "d4", "objects": "car", "start_ts": 10, "end_ts": 20},
    ])

    def _boom(event, **k):
        raise RuntimeError("clip gone")
    monkeypatch.setattr(alert_ai_worker.crop, "crop_event_high_res", _boom)

    posted = []
    monkeypatch.setattr(alert_ai_worker.ai_worker.requests, "post", lambda *a, **k: posted.append(1))
    failed = []
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: failed.append((a, k)))

    row = {"id": 14, "objects": "car", "det_id": "d4", "alert_ai_attempt_count": 0}
    alert_ai_worker.process_claimed_visit(row, PROFILE)

    assert not posted  # never even reached the chat call
    assert failed == [((14, config.AI_STAGE_MAX_ATTEMPTS), {})]


def test_process_claimed_visit_skips_initial_wait_on_retry(monkeypatch):
    # attempt_count > 0 means this isn't the first try -- no need to re-apply the head-start wait.
    monkeypatch.setattr(config, "ALERT_AI_INITIAL_WAIT_SECONDS", 999)  # would time out the test if slept
    monkeypatch.setattr(db, "get_raw_events_for_visit", lambda visit_id: [
        {"id": 100, "det_id": "d5", "objects": "car", "start_ts": 10, "end_ts": 20},
    ])
    monkeypatch.setattr(alert_ai_worker.crop, "crop_event_high_res", lambda event, **k: "high-res-base64")
    monkeypatch.setattr(alert_ai_worker.ai_worker, "_chat_request", lambda *a, **k: {"choices": [{"message": {"content": "x"}}]})
    monkeypatch.setattr(db, "complete_visit_sighting", lambda *a, **k: 1)
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: None)

    row = {"id": 16, "objects": "car", "det_id": "d5", "alert_ai_attempt_count": 1}
    alert_ai_worker.process_claimed_visit(row, PROFILE)  # must not hang
