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


def _insert_sighting(raw_event_id: int, object_label: str = "car", description: str = "silver") -> int:
    rows = db._execute(
        "INSERT INTO yard_stats.sightings (raw_event_id, object_label, description) VALUES (%s, %s, %s) RETURNING id",
        (raw_event_id, object_label, description), fetch=True,
    )
    return rows[0]["id"]


def _cleanup(*raw_event_ids, visit_id=None):
    db._execute("DELETE FROM yard_stats.sightings WHERE raw_event_id = ANY(%s)", (list(raw_event_ids),))
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


def test_get_visit_includes_has_video(conn_ok):
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [],
    })
    try:
        assert db.get_visit(visit_id)["has_video"] is False
        db.mark_visit_video_done(visit_id, "/data/video-alerts/2026/07/16/visit-car-1-x.mp4")
        row = db.get_visit(visit_id)
        assert row["has_video"] is True
        assert row["video_path"] == "/data/video-alerts/2026/07/16/visit-car-1-x.mp4"
    finally:
        _cleanup(visit_id=visit_id)


def test_get_visit_returns_none_for_unknown_id(conn_ok):
    assert db.get_visit(999999999) is None


def test_list_visits_has_image_true_from_visit_thumb_crop_even_without_representative_crop(conn_ok):
    # A visit's own thumb-crop (visit_thumb_worker.py) is a separate artifact from the
    # representative event's crop_image_base64 -- has_image should reflect either source being
    # available, not just the representative event's.
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64)
        VALUES ('pytest-cam', 'pytest-zone', 'car', now(), now(), %s, true, true, 'done', 'done', NULL)
        RETURNING id
        """,
        (det_id,), fetch=True,
    )
    raw_id = rows[0]["id"]
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id],
    })
    db.mark_visit_thumb_crop_done(visit_id, "ZmFrZS1qcGVn")
    try:
        rows = db.list_visits(start=None, end=None, limit=50, offset=0)
        match = next(r for r in rows if r["id"] == visit_id)
        assert match["has_thumb_crop"] is True
        assert match["thumb_crop_status"] == "done"
        assert match["has_image"] is True
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_list_visits_has_video_reflects_the_visit_not_the_representative_event(conn_ok):
    # Regression test: has_video/video_status must describe the VISIT's own video
    # (STORE_VIDEO_ALERTS/alert_video_worker.py), not the representative raw_event's -- those are
    # two entirely separate video flows/storage locations. Confirmed live in production this was
    # backwards: every visit had video_status='done' (a real downloaded clip) but list_visits
    # reported has_video=false because it was reading the representative event's video_path
    # (always NULL, since STORE_VIDEO was off) instead of the visit's own.
    det_id = f"pytest-{uuid.uuid4()}"
    raw_id = _insert_raw_event(det_id)
    # The representative raw_event has no video of its own (video_status default 'new', no path).
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id],
    })
    # Simulate alert_video_worker having successfully stored the visit's own clip.
    db.mark_visit_video_done(visit_id, "/data/video-alerts/2026/07/16/visit-car-1-x.mp4")
    try:
        rows = db.list_visits(start=None, end=None, limit=50, offset=0)
        match = next(r for r in rows if r["id"] == visit_id)
        assert match["has_video"] is True
        assert match["video_status"] == "done"
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_list_visits_q_matches_visit_by_representative_events_own_sighting(conn_ok):
    det_id = f"pytest-{uuid.uuid4()}"
    raw_id = _insert_raw_event(det_id, objects="car")
    _insert_sighting(raw_id, "car", "silver Passat")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    try:
        matching = db.list_visits(q="passat", start=None, end=None, limit=50, offset=0)
        assert any(r["id"] == visit_id for r in matching)

        non_matching = db.list_visits(q="camry", start=None, end=None, limit=50, offset=0)
        assert not any(r["id"] == visit_id for r in non_matching)
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_list_visits_q_matches_visit_by_a_non_representative_events_sighting(conn_ok):
    # Regression case this feature exists for: a visit grouping a car det_id (the earliest,
    # therefore representative) and a person det_id -- the search term only matches the person's
    # description, not the representative car's own sighting, so this must not be a plain per-row
    # filter on the representative alone (see list_visits' q comment).
    car_det = f"pytest-{uuid.uuid4()}"
    person_det = f"pytest-{uuid.uuid4()}"
    car_id = _insert_raw_event(car_det, "now() - interval '10 seconds'", objects="car")
    person_id = _insert_raw_event(person_det, "now()", objects="person")
    _insert_sighting(car_id, "car", "silver")
    _insert_sighting(person_id, "person", "wearing a bright green jacket")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car,person",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [car_det, person_det],
    })
    try:
        matching = db.list_visits(q="green jacket", start=None, end=None, limit=50, offset=0)
        assert any(r["id"] == visit_id for r in matching)
    finally:
        _cleanup(car_id, person_id, visit_id=visit_id)


def test_list_visits_q_is_case_insensitive_substring(conn_ok):
    det_id = f"pytest-{uuid.uuid4()}"
    raw_id = _insert_raw_event(det_id, objects="car")
    _insert_sighting(raw_id, "car", "has a roof rack")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    try:
        matching = db.list_visits(q="ROOF", start=None, end=None, limit=50, offset=0)
        assert any(r["id"] == visit_id for r in matching)
    finally:
        _cleanup(raw_id, visit_id=visit_id)
