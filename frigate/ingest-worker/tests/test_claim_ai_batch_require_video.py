"""Tests for claim_ai_batch's require_video option -- lets a caller (n8n) additionally require a
stored video before claiming a row for AI processing, not just a crop image. An image is always
guaranteed regardless (crop_status='done' is required either way); require_video only narrows
further.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Only run against a local/throwaway Postgres (see test_claim_limits.py's
docstring for why).
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import db  # noqa: E402


def _insert(crop_status="done", ai_status="new", video_status="new", objects="car"):
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, video_status)
        VALUES ('pytest-cam', 'pytest-zone', %s, now(), now(), %s, true, true, %s, %s, %s)
        RETURNING id
        """,
        (objects, det_id, crop_status, ai_status, video_status),
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


def test_require_video_false_default_claims_image_only_row(conn_ok):
    image_only_id = _insert(crop_status="done", video_status="new")
    try:
        claimed_ids = {r["id"] for r in db.claim_ai_batch(["car"], parallel_limit=10, stale_minutes=5)}
        assert image_only_id in claimed_ids
    finally:
        _cleanup(image_only_id)


def test_require_video_true_excludes_image_only_row(conn_ok):
    image_only_id = _insert(crop_status="done", video_status="new")
    try:
        claimed_ids = {
            r["id"] for r in db.claim_ai_batch(["car"], parallel_limit=10, stale_minutes=5, require_video=True)
        }
        assert image_only_id not in claimed_ids
    finally:
        _cleanup(image_only_id)


def test_require_video_true_includes_row_with_video(conn_ok):
    both_id = _insert(crop_status="done", video_status="done")
    try:
        claimed_ids = {
            r["id"] for r in db.claim_ai_batch(["car"], parallel_limit=10, stale_minutes=5, require_video=True)
        }
        assert both_id in claimed_ids
    finally:
        _cleanup(both_id)
