"""Tests for the visits table's video queue (claim_visit_video_batch/mark_visit_video_done/
mark_visit_video_retry_or_failed) -- same shape/mechanics as raw_events' video queue
(claim_video_batch), just against visits: one clip per visit's whole start_ts->end_ts span,
independent of any per-event video. See alert_video_worker.py.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Only run against a local/throwaway Postgres.
"""
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import db  # noqa: E402


def _insert_visit(video_status="new", start_ts_expr="now()"):
    rows = db._execute(
        f"""
        INSERT INTO yard_stats.visits (zone, objects, start_ts, end_ts, cameras, camera_count, video_status)
        VALUES ('pytest-zone', 'car', {start_ts_expr}, {start_ts_expr}, 'pytest-cam', 1, %s)
        RETURNING id
        """,
        (video_status,), fetch=True,
    )
    return rows[0]["id"]


def _cleanup(*visit_ids):
    db._execute("DELETE FROM yard_stats.visits WHERE id = ANY(%s)", (list(visit_ids),))


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def test_claim_visit_video_batch_claims_new_row(conn_ok):
    visit_id = _insert_visit(video_status="new")
    try:
        claimed_ids = {r["id"] for r in db.claim_visit_video_batch(limit=10)}
        assert visit_id in claimed_ids
    finally:
        _cleanup(visit_id)


def test_claim_visit_video_batch_excludes_skipped_row(conn_ok):
    visit_id = _insert_visit(video_status="skipped")
    try:
        claimed_ids = {r["id"] for r in db.claim_visit_video_batch(limit=10)}
        assert visit_id not in claimed_ids
    finally:
        _cleanup(visit_id)


def test_claim_visit_video_batch_prefers_newest_under_backlog(conn_ok):
    old_id = _insert_visit(video_status="new", start_ts_expr="now() - interval '2 days'")
    new_id = _insert_visit(video_status="new", start_ts_expr="now()")
    try:
        claimed_ids = {r["id"] for r in db.claim_visit_video_batch(limit=1)}
        assert claimed_ids == {new_id}
    finally:
        _cleanup(old_id, new_id)


def test_mark_visit_video_done_sets_path_and_status(conn_ok):
    visit_id = _insert_visit(video_status="processing")
    try:
        db.mark_visit_video_done(visit_id, "/data/video/visits/2026/07/16/visit-car-1-x.mp4")
        row = db._execute(
            "SELECT video_status, video_path FROM yard_stats.visits WHERE id = %s",
            (visit_id,), fetch=True,
        )[0]
        assert row["video_status"] == "done"
        assert row["video_path"] == "/data/video/visits/2026/07/16/visit-car-1-x.mp4"
    finally:
        _cleanup(visit_id)


def test_mark_visit_video_retry_or_failed_retries_below_cap(conn_ok):
    visit_id = _insert_visit(video_status="processing")
    try:
        db.mark_visit_video_retry_or_failed(visit_id, max_attempts=5)
        row = db._execute(
            "SELECT video_status, video_attempt_count FROM yard_stats.visits WHERE id = %s",
            (visit_id,), fetch=True,
        )[0]
        assert row["video_status"] == "retry"
        assert row["video_attempt_count"] == 1
    finally:
        _cleanup(visit_id)


def test_mark_visit_video_retry_or_failed_fails_at_cap(conn_ok):
    visit_id = _insert_visit(video_status="processing")
    db._execute("UPDATE yard_stats.visits SET video_attempt_count = 4 WHERE id = %s", (visit_id,))
    try:
        db.mark_visit_video_retry_or_failed(visit_id, max_attempts=5)
        row = db._execute(
            "SELECT video_status, video_attempt_count FROM yard_stats.visits WHERE id = %s",
            (visit_id,), fetch=True,
        )[0]
        assert row["video_status"] == "failed"
        assert row["video_attempt_count"] == 5
    finally:
        _cleanup(visit_id)
