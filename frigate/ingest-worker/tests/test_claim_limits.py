"""Regression tests for the LIMIT-respecting fix in claim_next_batch/claim_video_batch/
claim_ai_batch.

Reproduced directly in psql: `UPDATE raw_events WHERE id IN (SELECT id FROM raw_events
... LIMIT n FOR UPDATE SKIP LOCKED)` -- a self-referencing subquery -- did not reliably cap the
UPDATE at n rows (3 eligible rows, LIMIT 2, claimed all 3). All three claim functions now use a
CTE instead, which fences the claimable-id computation to run exactly once.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Only run against a local/throwaway Postgres, never a shared or
production database (these tests insert rows matching every claim function's WHERE clause and
call the claim functions with a real, small limit).
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import db  # noqa: E402


def _insert(crop_status="new", ai_status="new", video_status="new", has_snapshot=True, objects="car"):
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, video_status)
        VALUES ('pytest-cam', 'pytest-zone', %s, now(), now(), %s, true, %s, %s, %s, %s)
        RETURNING id
        """,
        (objects, det_id, has_snapshot, crop_status, ai_status, video_status),
        fetch=True,
    )
    return rows[0]["id"]


def _cleanup(*ids):
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(ids),))


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def test_claim_next_batch_never_exceeds_limit(conn_ok):
    ids = [_insert(crop_status="new", has_snapshot=True) for _ in range(5)]
    try:
        claimed = db.claim_next_batch(limit=2)
        assert len(claimed) <= 2
    finally:
        _cleanup(*ids)


def test_claim_video_batch_never_exceeds_limit(conn_ok):
    ids = [_insert(crop_status="done", video_status="new") for _ in range(5)]
    try:
        claimed = db.claim_video_batch(limit=2)
        assert len(claimed) <= 2
    finally:
        _cleanup(*ids)


def test_claim_ai_batch_never_exceeds_available_capacity(conn_ok):
    ids = [_insert(crop_status="done", ai_status="new", objects="car") for _ in range(5)]
    try:
        claimed = db.claim_ai_batch(object_types=["car"], parallel_limit=2, stale_minutes=5)
        assert len(claimed) <= 2
    finally:
        _cleanup(*ids)
