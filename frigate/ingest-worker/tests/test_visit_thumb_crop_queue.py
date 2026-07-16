"""Tests for the visits table's thumb-crop queue (claim_visit_thumb_crop_batch/
mark_visit_thumb_crop_done/mark_visit_thumb_crop_retry_or_failed) -- same shape/mechanics as the
visit video queue (claim_visit_video_batch), just for the re-crop at Frigate's own review
thumb_time. See visit_thumb_worker.py.

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


def _insert_visit(thumb_crop_status="new", start_ts_expr="now()", thumb_time=1784198455.5):
    rows = db._execute(
        f"""
        INSERT INTO yard_stats.visits
            (zone, objects, start_ts, end_ts, cameras, camera_count, thumb_crop_status, thumb_time)
        VALUES ('pytest-zone', 'car', {start_ts_expr}, {start_ts_expr}, 'pytest-cam', 1, %s, %s)
        RETURNING id
        """,
        (thumb_crop_status, thumb_time), fetch=True,
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


def test_claim_visit_thumb_crop_batch_claims_new_row(conn_ok):
    visit_id = _insert_visit(thumb_crop_status="new")
    try:
        claimed_ids = {r["id"] for r in db.claim_visit_thumb_crop_batch(limit=10)}
        assert visit_id in claimed_ids
    finally:
        _cleanup(visit_id)


def test_claim_visit_thumb_crop_batch_excludes_skipped_row(conn_ok):
    visit_id = _insert_visit(thumb_crop_status="skipped")
    try:
        claimed_ids = {r["id"] for r in db.claim_visit_thumb_crop_batch(limit=10)}
        assert visit_id not in claimed_ids
    finally:
        _cleanup(visit_id)


def test_claim_visit_thumb_crop_batch_prefers_newest_under_backlog(conn_ok):
    old_id = _insert_visit(thumb_crop_status="new", start_ts_expr="now() - interval '2 days'")
    new_id = _insert_visit(thumb_crop_status="new", start_ts_expr="now()")
    try:
        claimed_ids = {r["id"] for r in db.claim_visit_thumb_crop_batch(limit=1)}
        assert claimed_ids == {new_id}
    finally:
        _cleanup(old_id, new_id)


def test_mark_visit_thumb_crop_done_sets_image_and_status(conn_ok):
    visit_id = _insert_visit(thumb_crop_status="processing")
    try:
        db.mark_visit_thumb_crop_done(visit_id, "ZmFrZS1qcGVn")
        row = db._execute(
            "SELECT thumb_crop_status, crop_image_base64 FROM yard_stats.visits WHERE id = %s",
            (visit_id,), fetch=True,
        )[0]
        assert row["thumb_crop_status"] == "done"
        assert row["crop_image_base64"] == "ZmFrZS1qcGVn"
    finally:
        _cleanup(visit_id)


def test_mark_visit_thumb_crop_retry_or_failed_retries_below_cap(conn_ok):
    visit_id = _insert_visit(thumb_crop_status="processing")
    try:
        db.mark_visit_thumb_crop_retry_or_failed(visit_id, max_attempts=3)
        row = db._execute(
            "SELECT thumb_crop_status, thumb_crop_attempt_count FROM yard_stats.visits WHERE id = %s",
            (visit_id,), fetch=True,
        )[0]
        assert row["thumb_crop_status"] == "retry"
        assert row["thumb_crop_attempt_count"] == 1
    finally:
        _cleanup(visit_id)


def test_mark_visit_thumb_crop_retry_or_failed_fails_at_cap(conn_ok):
    visit_id = _insert_visit(thumb_crop_status="processing")
    db._execute("UPDATE yard_stats.visits SET thumb_crop_attempt_count = 2 WHERE id = %s", (visit_id,))
    try:
        db.mark_visit_thumb_crop_retry_or_failed(visit_id, max_attempts=3)
        row = db._execute(
            "SELECT thumb_crop_status, thumb_crop_attempt_count FROM yard_stats.visits WHERE id = %s",
            (visit_id,), fetch=True,
        )[0]
        assert row["thumb_crop_status"] == "failed"
        assert row["thumb_crop_attempt_count"] == 3
    finally:
        _cleanup(visit_id)


def test_reap_stale_visit_thumb_crop_processing(conn_ok):
    visit_id = _insert_visit(thumb_crop_status="processing")
    db._execute(
        """
        UPDATE yard_stats.visits
        SET thumb_crop_status_changed_at = now() - interval '10 minutes'
        WHERE id = %s
        """,
        (visit_id,),
    )
    try:
        db.reap_stale_visit_thumb_crop_processing()
        row = db._execute(
            "SELECT thumb_crop_status FROM yard_stats.visits WHERE id = %s",
            (visit_id,), fetch=True,
        )[0]
        assert row["thumb_crop_status"] == "retry"
    finally:
        _cleanup(visit_id)


def test_count_visit_thumb_crop_in_progress(conn_ok):
    visit_id = _insert_visit(thumb_crop_status="processing")
    try:
        assert db.count_visit_thumb_crop_in_progress() >= 1
    finally:
        _cleanup(visit_id)
