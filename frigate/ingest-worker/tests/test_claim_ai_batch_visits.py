"""Tests for claim_ai_batch's only_visit_representative option (POST /ai-queue/claim's
source=visits) -- skips analyzing duplicate det_ids a visit already grouped together, claiming
only the visit's earliest-linked raw_event (plus every raw_event never grouped into a visit at
all). Doesn't change ai_status semantics or completion -- purely a claim-time filter.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Only run against a local/throwaway Postgres.
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import db  # noqa: E402


def _insert(start_ts_expr="now()", objects="car", crop_image_base64=None):
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        f"""
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64)
        VALUES ('pytest-cam', 'pytest-zone', %s, {start_ts_expr}, {start_ts_expr}, %s, true, true,
                'done', 'new', %s)
        RETURNING id, det_id
        """,
        (objects, det_id, crop_image_base64), fetch=True,
    )
    return rows[0]["id"], rows[0]["det_id"]


def _cleanup(*raw_event_ids, visit_id=None):
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(raw_event_ids),))
    if visit_id is not None:
        db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def test_only_visit_representative_excludes_non_representative_grouped_event(conn_ok):
    older_id, older_det = _insert("now() - interval '10 seconds'")
    newer_id, newer_det = _insert("now()")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [older_det, newer_det],
    })
    try:
        claimed_ids = {
            r["id"] for r in db.claim_ai_batch(
                ["car"], parallel_limit=10, stale_minutes=5, only_visit_representative=True,
            )
        }
        assert older_id in claimed_ids
        assert newer_id not in claimed_ids
    finally:
        _cleanup(older_id, newer_id, visit_id=visit_id)


def test_only_visit_representative_still_claims_ungrouped_event(conn_ok):
    ungrouped_id, _ = _insert()
    try:
        claimed_ids = {
            r["id"] for r in db.claim_ai_batch(
                ["car"], parallel_limit=10, stale_minutes=5, only_visit_representative=True,
            )
        }
        assert ungrouped_id in claimed_ids
    finally:
        _cleanup(ungrouped_id)


def test_visits_only_excludes_ungrouped_event(conn_ok):
    # visits_only=true is the strict alerts-only mode -- confirmed needed in production: without
    # it, source=visits' default fallback still claims plain ungrouped raw_events, which an
    # alerts-scoped n8n workflow doesn't want at all.
    ungrouped_id, _ = _insert()
    try:
        claimed_ids = {
            r["id"] for r in db.claim_ai_batch(
                ["car"], parallel_limit=10, stale_minutes=5,
                only_visit_representative=True, visits_only=True,
            )
        }
        assert ungrouped_id not in claimed_ids
    finally:
        _cleanup(ungrouped_id)


def test_visits_only_still_claims_visit_representative(conn_ok):
    older_id, older_det = _insert("now() - interval '10 seconds'")
    newer_id, newer_det = _insert("now()")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [older_det, newer_det],
    })
    try:
        claimed_ids = {
            r["id"] for r in db.claim_ai_batch(
                ["car"], parallel_limit=10, stale_minutes=5,
                only_visit_representative=True, visits_only=True,
            )
        }
        assert older_id in claimed_ids
        assert newer_id not in claimed_ids
    finally:
        _cleanup(older_id, newer_id, visit_id=visit_id)


def test_require_thumb_crop_excludes_visit_whose_thumb_crop_is_not_done(conn_ok):
    raw_id, det_id = _insert(crop_image_base64="event-crop")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    # thumb_crop_status defaults to 'new' (VISIT_THUMB_CROP_ENABLED is off in tests, but this
    # simulates a visit whose re-crop simply hasn't finished yet).
    db._execute("UPDATE yard_stats.visits SET thumb_crop_status = 'new' WHERE id = %s", (visit_id,))
    try:
        claimed_ids = {
            r["id"] for r in db.claim_ai_batch(
                ["car"], parallel_limit=10, stale_minutes=5,
                only_visit_representative=True, require_thumb_crop=True,
            )
        }
        assert raw_id not in claimed_ids
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_require_thumb_crop_claims_visit_once_thumb_crop_is_done(conn_ok):
    raw_id, det_id = _insert(crop_image_base64="event-crop")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    db.mark_visit_thumb_crop_done(visit_id, "visit-crop")
    try:
        claimed = {
            r["id"]: r for r in db.claim_ai_batch(
                ["car"], parallel_limit=10, stale_minutes=5,
                only_visit_representative=True, require_thumb_crop=True,
            )
        }
        assert raw_id in claimed
        assert claimed[raw_id]["crop_image_base64"] == "visit-crop"
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_claim_opportunistically_prefers_visit_thumb_crop_when_already_done(conn_ok):
    # Independent of require_thumb_crop -- a plain source=visits claim should still prefer the
    # visit's own well-timed crop over the representative event's crop whenever it's already done
    # by claim time, at zero extra latency cost (this doesn't change which rows get claimed).
    raw_id, det_id = _insert(crop_image_base64="event-crop")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    db.mark_visit_thumb_crop_done(visit_id, "visit-crop")
    try:
        claimed = {
            r["id"]: r for r in db.claim_ai_batch(
                ["car"], parallel_limit=10, stale_minutes=5, only_visit_representative=True,
            )
        }
        assert claimed[raw_id]["crop_image_base64"] == "visit-crop"
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_claim_falls_back_to_event_crop_when_thumb_crop_not_done(conn_ok):
    raw_id, det_id = _insert(crop_image_base64="event-crop")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    try:
        claimed = {
            r["id"]: r for r in db.claim_ai_batch(
                ["car"], parallel_limit=10, stale_minutes=5, only_visit_representative=True,
            )
        }
        assert claimed[raw_id]["crop_image_base64"] == "event-crop"
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_source_events_never_overridden_by_visit_thumb_crop(conn_ok):
    # Plain source=events (only_visit_representative=False) must never substitute the visit's
    # crop -- overriding every duplicate det_id under one visit with the identical image would be
    # wasted/duplicate VLM analysis, not an improvement (see claim_ai_batch's comment).
    raw_id, det_id = _insert(crop_image_base64="event-crop")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    db.mark_visit_thumb_crop_done(visit_id, "visit-crop")
    try:
        claimed = {
            r["id"]: r for r in db.claim_ai_batch(["car"], parallel_limit=10, stale_minutes=5)
        }
        assert claimed[raw_id]["crop_image_base64"] == "event-crop"
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_only_visit_representative_claims_one_representative_per_distinct_object_type(conn_ok):
    # Regression test: the dedup used to partition by visit_id alone, so a visit grouping a car
    # det_id and a person det_id together (a real, confirmed-in-production case -- e.g. someone
    # getting out of their car) only ever got the earlier of the two analyzed, silently dropping
    # the other object type's sighting entirely. Partitioning by (visit_id, objects) instead keeps
    # same-type dedup (still just one analyzed event per repeated re-track of the same object) but
    # gives each distinct object type in the visit its own representative.
    car_id, car_det = _insert("now() - interval '10 seconds'", objects="car")
    person_id, person_det = _insert("now()", objects="person")
    # A second car det_id (re-track) should still collapse to just the one car representative.
    car_dup_id, car_dup_det = _insert("now() - interval '5 seconds'", objects="car")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car,person",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [car_det, person_det, car_dup_det],
    })
    try:
        claimed_ids = {
            r["id"] for r in db.claim_ai_batch(
                ["car", "person"], parallel_limit=10, stale_minutes=5,
                only_visit_representative=True,
            )
        }
        assert car_id in claimed_ids
        assert person_id in claimed_ids
        assert car_dup_id not in claimed_ids
    finally:
        _cleanup(car_id, person_id, car_dup_id, visit_id=visit_id)


def test_default_source_events_claims_every_grouped_event(conn_ok):
    older_id, older_det = _insert("now() - interval '10 seconds'")
    newer_id, newer_det = _insert("now()")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [older_det, newer_det],
    })
    try:
        claimed_ids = {
            r["id"] for r in db.claim_ai_batch(["car"], parallel_limit=10, stale_minutes=5)
        }
        assert older_id in claimed_ids
        assert newer_id in claimed_ids
    finally:
        _cleanup(older_id, newer_id, visit_id=visit_id)
