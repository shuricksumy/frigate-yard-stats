"""Tests for the visit-video Telegram reply-threading plumbing: set_visit_telegram_photo_message_id
(persists the visit-summary message id so alert_video_worker's later video send can reply to it)
and count_events_for_visit (used to build that video's caption). See alert_video_worker.py /
mqtt_ingest.py's review handler.

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


def _insert_visit():
    rows = db._execute(
        """
        INSERT INTO yard_stats.visits (zone, objects, start_ts, end_ts, cameras, camera_count)
        VALUES ('pytest-zone', 'car', now(), now(), 'pytest-cam', 1)
        RETURNING id
        """,
        fetch=True,
    )
    return rows[0]["id"]


def _insert_raw_event(visit_id: int | None):
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot, visit_id)
        VALUES ('pytest-cam', 'pytest-zone', 'car', now(), now(), %s, true, true, %s)
        RETURNING id
        """,
        (det_id, visit_id), fetch=True,
    )
    return rows[0]["id"]


def _cleanup(visit_id, *raw_event_ids):
    if raw_event_ids:
        db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(raw_event_ids),))
    db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def test_set_visit_telegram_photo_message_id(conn_ok):
    visit_id = _insert_visit()
    try:
        db.set_visit_telegram_photo_message_id(visit_id, 4242)
        row = db._execute(
            "SELECT telegram_photo_message_id FROM yard_stats.visits WHERE id = %s",
            (visit_id,), fetch=True,
        )[0]
        assert row["telegram_photo_message_id"] == 4242
    finally:
        _cleanup(visit_id)


def test_count_events_for_visit(conn_ok):
    visit_id = _insert_visit()
    raw_id_a = _insert_raw_event(visit_id)
    raw_id_b = _insert_raw_event(visit_id)
    unrelated_id = _insert_raw_event(None)
    try:
        assert db.count_events_for_visit(visit_id) == 2
    finally:
        _cleanup(visit_id, raw_id_a, raw_id_b, unrelated_id)


def test_count_events_for_visit_zero_when_none_linked(conn_ok):
    visit_id = _insert_visit()
    try:
        assert db.count_events_for_visit(visit_id) == 0
    finally:
        _cleanup(visit_id)
