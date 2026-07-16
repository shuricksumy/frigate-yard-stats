"""Tests for claim_video_batch's max_age_hours option -- lets a backlog of very old events (whose
clip has very likely already rolled off Frigate's continuous-recording buffer, a much shorter
retention window than the event-scoped clip crop.py reads from) age out of the video queue instead
of burning download attempts on it. Mirrors claim_ai_batch's existing max_age_hours.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Only run against a local/throwaway Postgres (see that docstring for
why -- claim_video_batch mutates whatever real rows match its WHERE clause).
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import db  # noqa: E402


def _insert(created_at_expr: str) -> int:
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        f"""
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, video_status, created_at)
        VALUES ('pytest-cam', 'pytest-zone', 'car', now(), now(), %s, true, true,
                'done', 'new', {created_at_expr})
        RETURNING id
        """,
        (det_id,), fetch=True,
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


def test_max_age_hours_none_claims_old_row(conn_ok):
    old_id = _insert("now() - interval '3 days'")
    try:
        claimed_ids = {r["id"] for r in db.claim_video_batch(limit=10, max_age_hours=None)}
        assert old_id in claimed_ids
    finally:
        _cleanup(old_id)


def test_max_age_hours_excludes_row_older_than_cutoff(conn_ok):
    old_id = _insert("now() - interval '3 days'")
    try:
        claimed_ids = {r["id"] for r in db.claim_video_batch(limit=10, max_age_hours=6)}
        assert old_id not in claimed_ids
    finally:
        _cleanup(old_id)


def test_max_age_hours_still_claims_row_within_cutoff(conn_ok):
    fresh_id = _insert("now() - interval '1 hour'")
    try:
        claimed_ids = {r["id"] for r in db.claim_video_batch(limit=10, max_age_hours=6)}
        assert fresh_id in claimed_ids
    finally:
        _cleanup(fresh_id)
