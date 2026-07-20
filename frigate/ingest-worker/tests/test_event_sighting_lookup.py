"""Integration tests for db.get_sighting_for_event (GET /events/{id}'s sighting field) and
db.list_events' q text search across the AI analysis result.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes.
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import db  # noqa: E402


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def _insert_event(camera, objects="car", crop_status="done", ai_status="done"):
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64)
        VALUES (%s, 'z', %s, now(), now(), %s, true, true, %s, %s, 'ZmFrZQ==')
        RETURNING id
        """,
        (camera, objects, det_id, crop_status, ai_status), fetch=True,
    )
    return rows[0]["id"]


def _cleanup_event(event_id):
    db._execute("DELETE FROM yard_stats.sightings WHERE raw_event_id = %s", (event_id,))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = %s", (event_id,))


def test_get_sighting_for_event_returns_none_when_absent(conn_ok):
    event_id = _insert_event(camera="pytest-cam", ai_status="new")
    try:
        assert db.get_sighting_for_event(event_id) is None
    finally:
        _cleanup_event(event_id)


def test_get_sighting_for_event_returns_row_when_present(conn_ok):
    event_id = _insert_event(camera="pytest-cam")
    try:
        db.complete_sighting(event_id, "car", "red sedan, roof rack, plate ABC123")
        sighting = db.get_sighting_for_event(event_id)
        assert sighting is not None
        assert sighting["object_label"] == "car"
        assert sighting["description"] == "red sedan, roof rack, plate ABC123"
    finally:
        _cleanup_event(event_id)


def test_get_sighting_for_event_works_for_any_object_label(conn_ok):
    event_id = _insert_event(camera="pytest-cam", objects="person")
    try:
        db.complete_sighting(event_id, "person", "wearing a red hoodie")
        sighting = db.get_sighting_for_event(event_id)
        assert sighting is not None
        assert sighting["description"] == "wearing a red hoodie"
    finally:
        _cleanup_event(event_id)


def test_list_events_q_matches_sighting_description(conn_ok):
    camera = f"pytest-q-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera)
    try:
        db.complete_sighting(event_id, "car", "silver Honda CR-V, dented rear bumper")
        matches = db.list_events(camera=camera, start=None, end=None, q="dented")
        assert {r["id"] for r in matches} == {event_id}

        no_matches = db.list_events(camera=camera, start=None, end=None, q="nonexistent-term-xyz")
        assert no_matches == []
    finally:
        _cleanup_event(event_id)


def test_list_events_q_matches_any_object_label(conn_ok):
    # No more vehicle-vs-person split to worry about -- q matches any sighting's description
    # regardless of object_label.
    camera = f"pytest-q-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera, objects="person")
    try:
        db.complete_sighting(event_id, "person", "carrying a blue umbrella")
        matches = db.list_events(camera=camera, start=None, end=None, q="umbrella")
        assert {r["id"] for r in matches} == {event_id}
    finally:
        _cleanup_event(event_id)


def test_list_events_q_does_not_duplicate_rows(conn_ok):
    # Guards against the LEFT JOIN to sightings fanning out a single raw_event into multiple
    # result rows (SELECT DISTINCT in _build_events_query).
    camera = f"pytest-q-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera)
    try:
        db.complete_sighting(event_id, "car", "blue hatchback")
        matches = db.list_events(camera=camera, start=None, end=None, q="blue")
        assert len(matches) == 1
    finally:
        _cleanup_event(event_id)
