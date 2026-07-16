"""Tests for db.get_sightings_for_visit -- the visit-scoped combined view GET
/visits/{id}/sightings uses, returning every sighting linked to a visit (one representative per
distinct object type, see claim_ai_batch's only_visit_representative comment) instead of just the
single representative event GET /events/{id} would return.

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


def _insert_raw_event(start_ts_expr="now()", objects="car"):
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        f"""
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status)
        VALUES ('pytest-cam', 'pytest-zone', %s, {start_ts_expr}, {start_ts_expr}, %s, true, true,
                'done', 'done')
        RETURNING id, det_id
        """,
        (objects, det_id), fetch=True,
    )
    return rows[0]["id"], rows[0]["det_id"]


def _insert_vehicle_sighting(raw_event_id: int) -> int:
    rows = db._execute(
        "INSERT INTO yard_stats.vehicle_sightings (raw_event_id, color) VALUES (%s, 'silver') RETURNING id",
        (raw_event_id,), fetch=True,
    )
    return rows[0]["id"]


def _insert_person_sighting(raw_event_id: int) -> int:
    rows = db._execute(
        "INSERT INTO yard_stats.person_sightings (raw_event_id, description) VALUES (%s, 'dark jacket') RETURNING id",
        (raw_event_id,), fetch=True,
    )
    return rows[0]["id"]


def _cleanup(*raw_event_ids, visit_id=None):
    db._execute("DELETE FROM yard_stats.vehicle_sightings WHERE raw_event_id = ANY(%s)", (list(raw_event_ids),))
    db._execute("DELETE FROM yard_stats.person_sightings WHERE raw_event_id = ANY(%s)", (list(raw_event_ids),))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(raw_event_ids),))
    if visit_id is not None:
        db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def test_returns_both_vehicle_and_person_sightings_for_one_visit(conn_ok):
    car_id, car_det = _insert_raw_event(objects="car")
    person_id, person_det = _insert_raw_event(objects="person")
    _insert_vehicle_sighting(car_id)
    _insert_person_sighting(person_id)
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car,person",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [car_det, person_det],
    })
    try:
        result = db.get_sightings_for_visit(visit_id)
        assert {v["raw_event_id"] for v in result["vehicles"]} == {car_id}
        assert {p["raw_event_id"] for p in result["persons"]} == {person_id}
    finally:
        _cleanup(car_id, person_id, visit_id=visit_id)


def test_returns_empty_lists_for_visit_with_no_sightings_yet(conn_ok):
    raw_id, det_id = _insert_raw_event()
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    try:
        result = db.get_sightings_for_visit(visit_id)
        assert result == {"vehicles": [], "persons": []}
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_does_not_include_sightings_from_other_visits(conn_ok):
    this_car_id, this_det = _insert_raw_event(objects="car")
    other_car_id, other_det = _insert_raw_event(objects="car")
    _insert_vehicle_sighting(this_car_id)
    _insert_vehicle_sighting(other_car_id)
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [this_det],
    })
    other_visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198551.0, "end_time": 1784198570.0, "det_ids": [other_det],
    })
    try:
        result = db.get_sightings_for_visit(visit_id)
        assert {v["raw_event_id"] for v in result["vehicles"]} == {this_car_id}
    finally:
        _cleanup(this_car_id, visit_id=visit_id)
        _cleanup(other_car_id, visit_id=other_visit_id)
