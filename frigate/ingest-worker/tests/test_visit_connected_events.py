"""Tests for db.list_events'/db.count_events' visit_id filter -- lets the web UI's visit lightbox
show every raw_event a visit grouped together (its "Connected events" strip), not just the
deduped AI-analyzed sighting(s) db.get_sightings_for_visit already returns.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

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


def _insert_event(camera, has_media=False):
    det_id = f"pytest-connected-{uuid.uuid4()}"
    crop_image_base64 = "ZmFrZQ==" if has_media else None
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64)
        VALUES (%s, 'z', 'car', now(), now(), %s, true, true, 'new', 'new', %s)
        RETURNING id, det_id
        """,
        (camera, det_id, crop_image_base64), fetch=True,
    )
    return rows[0]["id"], rows[0]["det_id"]


def _cleanup(event_ids, visit_id=None):
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (event_ids,))
    if visit_id is not None:
        db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


def test_list_events_by_visit_id_returns_only_linked_events(conn_ok):
    camera = f"pytest-connected-{uuid.uuid4()}"
    id_a, det_a = _insert_event(camera)
    id_b, det_b = _insert_event(camera)
    id_unrelated, _ = _insert_event(camera)  # never linked to the visit below
    visit_id = db.record_visit({
        "camera": camera, "zone": "z", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_a, det_b],
    })
    try:
        results = db.list_events(visit_id=visit_id)
        assert {r["id"] for r in results} == {id_a, id_b}
        assert db.count_events(visit_id=visit_id) == 2
    finally:
        _cleanup([id_a, id_b, id_unrelated], visit_id=visit_id)


def test_list_events_by_visit_id_ignores_has_media_default(conn_ok):
    # has_media defaults to True everywhere else -- visit_id must bypass that, same reasoning as
    # event_id, since "every event this visit grouped" should include ones still awaiting crop.
    camera = f"pytest-connected-{uuid.uuid4()}"
    event_id, det_id = _insert_event(camera, has_media=False)
    visit_id = db.record_visit({
        "camera": camera, "zone": "z", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id],
    })
    try:
        results = db.list_events(visit_id=visit_id)  # has_media not passed -- defaults to True
        assert [r["id"] for r in results] == [event_id]
    finally:
        _cleanup([event_id], visit_id=visit_id)


def test_list_events_by_visit_id_finds_events_regardless_of_age(conn_ok):
    # The actual bypass of the start/end window lives at the API layer (GET /events skips
    # resolving one at all when visit_id is given, same as it already does for event_id) --
    # list_events itself still applies whatever start/end it's explicitly passed, it just isn't
    # passed any when called this way. This confirms a very old connected event is still found
    # when no window is passed, matching how the API actually calls this.
    camera = f"pytest-connected-{uuid.uuid4()}"
    old_ts = datetime.now(timezone.utc) - timedelta(days=400)
    event_id, det_id = _insert_event(camera, has_media=True)
    db._execute("UPDATE yard_stats.raw_events SET start_ts = %s, end_ts = %s WHERE id = %s", (old_ts, old_ts, event_id))
    visit_id = db.record_visit({
        "camera": camera, "zone": "z", "objects": "car",
        "start_time": old_ts.timestamp(), "end_time": old_ts.timestamp(),
        "det_ids": [det_id],
    })
    try:
        results = db.list_events(visit_id=visit_id)
        assert [r["id"] for r in results] == [event_id]
    finally:
        _cleanup([event_id], visit_id=visit_id)


def test_list_events_by_visit_id_excludes_other_visits(conn_ok):
    camera = f"pytest-connected-{uuid.uuid4()}"
    id_a, det_a = _insert_event(camera)
    id_b, det_b = _insert_event(camera)
    visit_a = db.record_visit({
        "camera": camera, "zone": "z", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_a],
    })
    visit_b = db.record_visit({
        "camera": camera, "zone": "z", "objects": "car",
        "start_time": 1784198551.0, "end_time": 1784198570.0, "det_ids": [det_b],
    })
    try:
        results = db.list_events(visit_id=visit_a)
        assert [r["id"] for r in results] == [id_a]
    finally:
        _cleanup([id_a, id_b], visit_id=visit_a)
        db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_b,))
