"""Integration tests for the video_status queue-stage functions in db.py.

Requires a reachable Postgres with schema.sql applied (POSTGRES_HOST/PORT/DB/USER/PASSWORD env
vars, same as the running service -- e.g. run inside the ingest-worker container, or against the
docker-compose 'pipeline' profile's postgres-projects). Each test creates its own raw_events rows
(det_id prefixed 'pytest-') and cleans them up afterward so it doesn't depend on / interfere with
whatever else is in the table.
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import db  # noqa: E402


def _insert_row(camera="pytest-cam", objects="car", crop_status="done", video_status="new",
                 video_attempt_count=0, video_status_changed_at=None):
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, video_status, video_attempt_count)
        VALUES (%s, 'pytest-zone', %s, now(), now(), %s, true, true, %s, %s, %s)
        RETURNING id
        """,
        (camera, objects, det_id, crop_status, video_status, video_attempt_count),
        fetch=True,
    )
    event_id = rows[0]["id"]
    if video_status_changed_at is not None:
        db._execute(
            "UPDATE yard_stats.raw_events SET video_status_changed_at = %s WHERE id = %s",
            (video_status_changed_at, event_id),
        )
    return event_id


def _cleanup(*event_ids):
    db._execute(
        "DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)",
        (list(event_ids),),
    )


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def test_claim_video_batch_only_claims_crop_done_new_or_retry(conn_ok):
    eligible = _insert_row(crop_status="done", video_status="new")
    not_cropped_yet = _insert_row(crop_status="new", video_status="new")
    already_done = _insert_row(crop_status="done", video_status="done")
    try:
        claimed_ids = {row["id"] for row in db.claim_video_batch(limit=10)}
        assert eligible in claimed_ids
        assert not_cropped_yet not in claimed_ids
        assert already_done not in claimed_ids
    finally:
        _cleanup(eligible, not_cropped_yet, already_done)


def test_claim_video_batch_respects_limit(conn_ok):
    ids = [_insert_row(crop_status="done", video_status="new") for _ in range(3)]
    try:
        claimed = db.claim_video_batch(limit=2)
        assert len(claimed) == 2
    finally:
        _cleanup(*ids)


def test_claim_video_batch_marks_processing(conn_ok):
    event_id = _insert_row(crop_status="done", video_status="new")
    try:
        claimed = db.claim_video_batch(limit=10)
        assert any(r["id"] == event_id for r in claimed)
        row = db.get_raw_event(event_id)
        assert row["video_status"] == "processing"
    finally:
        _cleanup(event_id)


def test_mark_video_done_sets_path_and_status(conn_ok):
    event_id = _insert_row(crop_status="done", video_status="processing")
    try:
        db.mark_video_done(event_id, "/data/video/2026/07/15/car-1-123.mp4")
        row = db.get_raw_event(event_id)
        assert row["video_status"] == "done"
        assert row["video_path"] == "/data/video/2026/07/15/car-1-123.mp4"
        assert row["has_video"] is True
    finally:
        _cleanup(event_id)


def test_mark_video_retry_or_failed_retries_below_cap(conn_ok):
    event_id = _insert_row(crop_status="done", video_status="processing", video_attempt_count=0)
    try:
        db.mark_video_retry_or_failed(event_id, max_attempts=5)
        row = db.get_raw_event(event_id)
        assert row["video_status"] == "retry"
        assert row["video_attempt_count"] == 1
    finally:
        _cleanup(event_id)


def test_mark_video_retry_or_failed_fails_at_cap(conn_ok):
    event_id = _insert_row(crop_status="done", video_status="processing", video_attempt_count=4)
    try:
        db.mark_video_retry_or_failed(event_id, max_attempts=5)
        row = db.get_raw_event(event_id)
        assert row["video_status"] == "failed"
        assert row["video_attempt_count"] == 5
    finally:
        _cleanup(event_id)


def test_reap_stale_video_processing_only_reaps_past_stale_minutes(conn_ok, monkeypatch):
    monkeypatch.setattr(db.config, "STALE_MINUTES", 5)
    stale = _insert_row(crop_status="done", video_status="processing")
    fresh = _insert_row(crop_status="done", video_status="processing")
    db._execute(
        "UPDATE yard_stats.raw_events SET video_status_changed_at = now() - interval '10 minutes' WHERE id = %s",
        (stale,),
    )
    try:
        db.reap_stale_video_processing()
        assert db.get_raw_event(stale)["video_status"] == "retry"
        assert db.get_raw_event(fresh)["video_status"] == "processing"
    finally:
        _cleanup(stale, fresh)


def test_count_video_in_progress(conn_ok):
    a = _insert_row(crop_status="done", video_status="processing")
    b = _insert_row(crop_status="done", video_status="new")
    try:
        before = db.count_video_in_progress()
        assert before >= 1
    finally:
        _cleanup(a, b)
