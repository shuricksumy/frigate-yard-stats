"""Tests that claim_next_batch and claim_video_batch prioritize newest-eligible-first under a
backlog (more eligible rows than capacity) -- same deliberate priority inversion claim_ai_batch
already had. Confirmed necessary in production: crop is the first stage, so an oldest-first crop
queue meant fresh events waited behind a tens-of-thousands-deep backlog before ever becoming
croppable, which cascades to everything downstream (video, AI) since neither can start until
crop_status='done'.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Only run against a local/throwaway Postgres (both claim functions
mutate whatever real rows match their WHERE clause with a nonzero limit).
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import db  # noqa: E402


def _insert(crop_status, video_status, created_at_expr):
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        f"""
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, video_status, created_at)
        VALUES ('pytest-cam', 'pytest-zone', 'car', now(), now(), %s, true, true,
                %s, %s, {created_at_expr})
        RETURNING id
        """,
        (det_id, crop_status, video_status), fetch=True,
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


def test_claim_next_batch_prefers_newest_under_backlog(conn_ok):
    old_id = _insert(crop_status="new", video_status="new", created_at_expr="now() - interval '2 days'")
    new_id = _insert(crop_status="new", video_status="new", created_at_expr="now()")
    try:
        # Only capacity for one of the two eligible rows -- newest should win, oldest keeps waiting.
        claimed_ids = {r["id"] for r in db.claim_next_batch(limit=1)}
        assert claimed_ids == {new_id}
    finally:
        _cleanup(old_id, new_id)


def test_claim_video_batch_prefers_newest_under_backlog(conn_ok):
    old_id = _insert(crop_status="done", video_status="new", created_at_expr="now() - interval '2 days'")
    new_id = _insert(crop_status="done", video_status="new", created_at_expr="now()")
    try:
        claimed_ids = {r["id"] for r in db.claim_video_batch(limit=1)}
        assert claimed_ids == {new_id}
    finally:
        _cleanup(old_id, new_id)
