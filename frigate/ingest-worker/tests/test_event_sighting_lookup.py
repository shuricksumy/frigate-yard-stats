"""Integration tests for db.get_vehicle_sighting_for_event / get_person_sighting_for_event
(GET /events/{id}'s vehicle_sighting/person_sighting fields) and db.list_events' q text search
across the AI analysis result.

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
    db._execute("DELETE FROM yard_stats.vehicle_sightings WHERE raw_event_id = %s", (event_id,))
    db._execute("DELETE FROM yard_stats.person_sightings WHERE raw_event_id = %s", (event_id,))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = %s", (event_id,))


def test_get_vehicle_sighting_for_event_returns_none_when_absent(conn_ok):
    event_id = _insert_event(camera="pytest-cam", ai_status="new")
    try:
        assert db.get_vehicle_sighting_for_event(event_id) is None
    finally:
        _cleanup_event(event_id)


def test_get_vehicle_sighting_for_event_returns_row_when_present(conn_ok):
    event_id = _insert_event(camera="pytest-cam")
    try:
        db.complete_vehicle_sighting(
            event_id, color="red", body_type="sedan", make_guess="Toyota",
            make_confidence="high", model_guess="Camry", model_confidence="medium",
            notable_features="roof rack", plate_text_llm="ABC123", plate_text_frigate=None,
            plate_confidence="high", notes=None,
        )
        sighting = db.get_vehicle_sighting_for_event(event_id)
        assert sighting is not None
        assert sighting["color"] == "red"
        assert sighting["plate_text_llm"] == "ABC123"
    finally:
        _cleanup_event(event_id)


def test_get_person_sighting_for_event_returns_row_when_present(conn_ok):
    event_id = _insert_event(camera="pytest-cam", objects="person")
    try:
        db.complete_person_sighting(event_id, description="wearing a red hoodie", notes=None)
        sighting = db.get_person_sighting_for_event(event_id)
        assert sighting is not None
        assert sighting["description"] == "wearing a red hoodie"
    finally:
        _cleanup_event(event_id)


def test_list_events_q_matches_vehicle_sighting_text(conn_ok):
    camera = f"pytest-q-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera)
    try:
        db.complete_vehicle_sighting(
            event_id, color="silver", body_type="suv", make_guess="Honda",
            make_confidence="high", model_guess="CR-V", model_confidence="high",
            notable_features="dented rear bumper", plate_text_llm=None,
            plate_text_frigate=None, plate_confidence=None, notes=None,
        )
        matches = db.list_events(camera=camera, start=None, end=None, q="dented")
        assert {r["id"] for r in matches} == {event_id}

        no_matches = db.list_events(camera=camera, start=None, end=None, q="nonexistent-term-xyz")
        assert no_matches == []
    finally:
        _cleanup_event(event_id)


def test_list_events_q_matches_person_sighting_description(conn_ok):
    camera = f"pytest-q-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera, objects="person")
    try:
        db.complete_person_sighting(event_id, description="carrying a blue umbrella", notes=None)
        matches = db.list_events(camera=camera, start=None, end=None, q="umbrella")
        assert {r["id"] for r in matches} == {event_id}
    finally:
        _cleanup_event(event_id)


def test_list_events_q_does_not_duplicate_rows(conn_ok):
    # Guards against the LEFT JOIN to both sighting tables fanning out a single raw_event into
    # multiple result rows.
    camera = f"pytest-q-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera)
    try:
        db.complete_vehicle_sighting(
            event_id, color="blue", body_type="hatchback", make_guess=None,
            make_confidence=None, model_guess=None, model_confidence=None,
            notable_features=None, plate_text_llm=None, plate_text_frigate=None,
            plate_confidence=None, notes=None,
        )
        matches = db.list_events(camera=camera, start=None, end=None, q="blue")
        assert len(matches) == 1
    finally:
        _cleanup_event(event_id)
