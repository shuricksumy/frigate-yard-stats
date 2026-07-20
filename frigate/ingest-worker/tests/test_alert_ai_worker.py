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


def _make_visit_with_grid_ready(objects="car", camera="pytest-alert-cam", alert_ai_status="new"):
    event_id, det_id = _insert_event(objects=objects, camera=camera)
    visit_id = db.record_visit({
        "camera": camera, "zone": "z", "objects": objects,
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id],
    })
    db._execute(
        "UPDATE yard_stats.visits SET thumb_crop_status = 'done', crop_image_base64 = %s, "
        "alert_ai_status = %s WHERE id = %s",
        ("ZmFrZQ==", alert_ai_status, visit_id),
    )
    return visit_id, event_id


def _cleanup_visit(visit_id, *event_ids):
    db._execute("DELETE FROM yard_stats.visit_sightings WHERE visit_id = %s", (visit_id,))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(event_ids),))
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

def test_claim_alert_ai_batch_claims_visit_with_ready_grid(conn_ok):
    visit_id, event_id = _make_visit_with_grid_ready()
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


def test_claim_alert_ai_batch_skips_visit_without_ready_grid(conn_ok):
    event_id, det_id = _insert_event()
    visit_id = db.record_visit({
        "camera": "pytest-alert-cam", "zone": "z", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id],
    })
    # thumb_crop_status stays at its default ('new'/'skipped') -- grid never marked done.
    try:
        claimed_ids = {r["id"] for r in db.claim_alert_ai_batch(["car"], parallel_limit=10, stale_minutes=5)}
        assert visit_id not in claimed_ids
    finally:
        _cleanup_visit(visit_id, event_id)


def test_claim_alert_ai_batch_respects_object_types_filter(conn_ok):
    visit_id, event_id = _make_visit_with_grid_ready(objects="person")
    try:
        claimed_ids = {r["id"] for r in db.claim_alert_ai_batch(["car"], parallel_limit=10, stale_minutes=5)}
        assert visit_id not in claimed_ids
        claimed_ids = {r["id"] for r in db.claim_alert_ai_batch(["person"], parallel_limit=10, stale_minutes=5)}
        assert visit_id in claimed_ids
    finally:
        _cleanup_visit(visit_id, event_id)


def test_claim_alert_ai_batch_respects_parallel_limit_via_in_progress_count(conn_ok):
    visit_id, event_id = _make_visit_with_grid_ready(alert_ai_status="processing")
    try:
        # capacity = parallel_limit(1) - in_progress(1) = 0
        claimed = db.claim_alert_ai_batch(["car"], parallel_limit=1, stale_minutes=5)
        assert claimed == []
    finally:
        _cleanup_visit(visit_id, event_id)


# ---- db.complete_visit_sighting ----

def test_complete_visit_sighting_marks_alert_ai_status_done(conn_ok):
    visit_id, event_id = _make_visit_with_grid_ready()
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
    visit_id, event_id = _make_visit_with_grid_ready(objects="person")
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
    visit_id, event_id = _make_visit_with_grid_ready()
    try:
        result = db.fail_alert_ai_event(visit_id, max_attempts=3)
        assert result["alert_ai_status"] == "retry"
        assert result["alert_ai_attempt_count"] == 1
    finally:
        _cleanup_visit(visit_id, event_id)


def test_fail_alert_ai_event_fails_at_cap(conn_ok):
    visit_id, event_id = _make_visit_with_grid_ready()
    try:
        db._execute("UPDATE yard_stats.visits SET alert_ai_attempt_count = 2 WHERE id = %s", (visit_id,))
        result = db.fail_alert_ai_event(visit_id, max_attempts=3)
        assert result["alert_ai_status"] == "failed"
        assert result["alert_ai_attempt_count"] == 3
    finally:
        _cleanup_visit(visit_id, event_id)


# ---- db.get_visit_alert_sighting ----

def test_get_visit_alert_sighting_returns_none_when_not_analyzed(conn_ok):
    visit_id, event_id = _make_visit_with_grid_ready()
    try:
        assert db.get_visit_alert_sighting(visit_id) is None
    finally:
        _cleanup_visit(visit_id, event_id)


def test_get_visit_alert_sighting_returns_result(conn_ok):
    visit_id, event_id = _make_visit_with_grid_ready()
    try:
        db.complete_visit_sighting(visit_id, "car", "red sedan")
        result = db.get_visit_alert_sighting(visit_id)
        assert result["object_label"] == "car"
        assert result["description"] == "red sedan"
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


# ---- alert_ai_worker.process_claimed_visit (mocked chat call, no real network) ----

def test_process_claimed_visit_success(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "orange suv, parked"}}]}

    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(alert_ai_worker.ai_worker.requests, "post", fake_post)
    inserted = []
    monkeypatch.setattr(db, "complete_visit_sighting", lambda *a, **k: inserted.append(a) or 1)
    failed = []
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: failed.append((a, k)))

    row = {"id": 9, "objects": "car", "crop_image_base64": "aGVsbG8=", "det_id": "d1"}
    alert_ai_worker.process_claimed_visit(row, PROFILE)

    assert len(inserted) == 1
    assert inserted[0][:3] == (9, "car", "orange suv, parked")
    assert not failed
    assert calls[0] == "http://llama.test/vehicle-slot/v1/chat/completions"


def test_process_claimed_visit_unmapped_object_type_is_skipped(monkeypatch):
    inserted = []
    monkeypatch.setattr(db, "complete_visit_sighting", lambda *a, **k: inserted.append(a))
    failed = []
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: failed.append((a, k)))

    row = {"id": 11, "objects": "dog", "crop_image_base64": "x", "det_id": "d2"}
    alert_ai_worker.process_claimed_visit(row, PROFILE)

    assert not inserted
    assert not failed  # unmapped type is a silent skip, not a failure


def test_process_claimed_visit_chat_failure_routes_to_fail_alert_ai_event(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")

    def _raise(*a, **k):
        raise ConnectionError("backend down")

    monkeypatch.setattr(alert_ai_worker.ai_worker.requests, "post", _raise)
    failed = []
    monkeypatch.setattr(db, "fail_alert_ai_event", lambda *a, **k: failed.append((a, k)))

    row = {"id": 13, "objects": "car", "crop_image_base64": "x", "det_id": "d3"}
    alert_ai_worker.process_claimed_visit(row, PROFILE)

    assert failed == [((13, config.AI_STAGE_MAX_ATTEMPTS), {})]
