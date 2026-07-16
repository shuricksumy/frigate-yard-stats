"""Integration tests for db.list_visits -- the comparison view alongside list_events: one row per
Frigate review/alert segment (visit) instead of one per raw_event, so duplicate det_ids from
tracker re-ID/label flicker collapse into a single row.

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


def _insert_raw_event(det_id: str, start_ts_expr: str = "now()", objects: str = "car") -> int:
    rows = db._execute(
        f"""
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status)
        VALUES ('pytest-cam', 'pytest-zone', %s, {start_ts_expr}, {start_ts_expr}, %s, true, true,
                'done', 'done')
        RETURNING id
        """,
        (objects, det_id), fetch=True,
    )
    return rows[0]["id"]


def _cleanup(*raw_event_ids, visit_id=None):
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(raw_event_ids),))
    if visit_id is not None:
        db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def test_list_visits_groups_and_picks_earliest_as_representative(conn_ok):
    det_id_older = f"pytest-{uuid.uuid4()}"
    det_id_newer = f"pytest-{uuid.uuid4()}"
    raw_id_older = _insert_raw_event(det_id_older, "now() - interval '10 seconds'")
    raw_id_newer = _insert_raw_event(det_id_newer, "now()")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id_older, det_id_newer],
    })
    try:
        rows = db.list_visits(start=None, end=None, limit=50, offset=0)
        match = next(r for r in rows if r["id"] == visit_id)
        assert match["event_count"] == 2
        assert match["representative_event_id"] == raw_id_older
    finally:
        _cleanup(raw_id_older, raw_id_newer, visit_id=visit_id)


def test_list_visits_object_type_filter(conn_ok):
    det_id = f"pytest-{uuid.uuid4()}"
    raw_id = _insert_raw_event(det_id, objects="truck")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "truck",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id],
    })
    try:
        matching = db.list_visits(object_type="truck", start=None, end=None, limit=50, offset=0)
        assert any(r["id"] == visit_id for r in matching)

        non_matching = db.list_visits(object_type="person", start=None, end=None, limit=50, offset=0)
        assert not any(r["id"] == visit_id for r in non_matching)
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_list_visits_excludes_unlinked_raw_events(conn_ok):
    # A raw_event with no visit_id must never surface as (or be folded into) a visit row.
    det_id = f"pytest-{uuid.uuid4()}"
    raw_id = _insert_raw_event(det_id)
    try:
        rows = db.list_visits(start=None, end=None, limit=200, offset=0)
        assert not any(r["representative_event_id"] == raw_id for r in rows)
    finally:
        _cleanup(raw_id)
