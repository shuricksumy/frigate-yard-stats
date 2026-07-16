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


def _insert(start_ts_expr="now()", objects="car"):
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        f"""
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status)
        VALUES ('pytest-cam', 'pytest-zone', %s, {start_ts_expr}, {start_ts_expr}, %s, true, true,
                'done', 'new')
        RETURNING id, det_id
        """,
        (objects, det_id), fetch=True,
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
