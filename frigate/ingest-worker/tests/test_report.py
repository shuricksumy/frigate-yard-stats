"""Tests for db.get_report_data's source param -- source="visits" (the alerts-report workflow)
dedups the same way POST /ai-queue/claim's source=visits does: only the sighting for a visit's
earliest-linked raw_event, plus every sighting whose raw_event was never grouped into a visit,
so one real-world visit spanning several det_ids (re-track, label flicker) shows up once in the
report instead of once per det_id.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Only run against a local/throwaway Postgres.
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


def _insert_raw_event(start_ts_expr="now()", objects="car", crop_image_base64=None):
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        f"""
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64)
        VALUES ('pytest-cam', 'pytest-zone', %s, {start_ts_expr}, {start_ts_expr}, %s, true, true,
                'done', 'done', %s)
        RETURNING id, det_id
        """,
        (objects, det_id, crop_image_base64), fetch=True,
    )
    return rows[0]["id"], rows[0]["det_id"]


def _insert_vehicle_sighting(raw_event_id: int) -> int:
    rows = db._execute(
        """
        INSERT INTO yard_stats.vehicle_sightings (raw_event_id, color)
        VALUES (%s, 'red')
        RETURNING id
        """,
        (raw_event_id,), fetch=True,
    )
    return rows[0]["id"]


def _insert_person_sighting(raw_event_id: int) -> int:
    rows = db._execute(
        """
        INSERT INTO yard_stats.person_sightings (raw_event_id, description)
        VALUES (%s, 'dark jacket')
        RETURNING id
        """,
        (raw_event_id,), fetch=True,
    )
    return rows[0]["id"]


def _cleanup(*raw_event_ids, visit_id=None):
    db._execute(
        "DELETE FROM yard_stats.vehicle_sightings WHERE raw_event_id = ANY(%s)",
        (list(raw_event_ids),),
    )
    db._execute(
        "DELETE FROM yard_stats.person_sightings WHERE raw_event_id = ANY(%s)",
        (list(raw_event_ids),),
    )
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(raw_event_ids),))
    if visit_id is not None:
        db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def _window():
    now = datetime.now(timezone.utc)
    return now - timedelta(hours=1), now + timedelta(hours=1)


def test_get_report_data_orders_newest_first(conn_ok):
    older_id, older_det = _insert_raw_event("now() - interval '10 seconds'")
    newer_id, newer_det = _insert_raw_event("now()")
    _insert_vehicle_sighting(older_id)
    _insert_vehicle_sighting(newer_id)
    try:
        start, end = _window()
        data = db.get_report_data(start, end)
        raw_event_ids = [v["raw_event_id"] for v in data["vehicles"]]
        assert raw_event_ids.index(newer_id) < raw_event_ids.index(older_id)
    finally:
        _cleanup(older_id, newer_id)


def test_default_source_events_includes_every_grouped_sighting(conn_ok):
    older_id, older_det = _insert_raw_event("now() - interval '10 seconds'")
    newer_id, newer_det = _insert_raw_event("now()")
    _insert_vehicle_sighting(older_id)
    _insert_vehicle_sighting(newer_id)
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [older_det, newer_det],
    })
    try:
        start, end = _window()
        data = db.get_report_data(start, end)
        raw_event_ids = {v["raw_event_id"] for v in data["vehicles"]}
        assert older_id in raw_event_ids
        assert newer_id in raw_event_ids
    finally:
        _cleanup(older_id, newer_id, visit_id=visit_id)


def test_source_visits_dedups_to_representative_sighting(conn_ok):
    older_id, older_det = _insert_raw_event("now() - interval '10 seconds'")
    newer_id, newer_det = _insert_raw_event("now()")
    _insert_vehicle_sighting(older_id)
    _insert_vehicle_sighting(newer_id)
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [older_det, newer_det],
    })
    try:
        start, end = _window()
        data = db.get_report_data(start, end, source="visits")
        raw_event_ids = {v["raw_event_id"] for v in data["vehicles"]}
        assert older_id in raw_event_ids
        assert newer_id not in raw_event_ids
    finally:
        _cleanup(older_id, newer_id, visit_id=visit_id)


def test_source_visits_still_includes_ungrouped_sighting(conn_ok):
    ungrouped_id, _ = _insert_raw_event()
    _insert_vehicle_sighting(ungrouped_id)
    try:
        start, end = _window()
        data = db.get_report_data(start, end, source="visits")
        raw_event_ids = {v["raw_event_id"] for v in data["vehicles"]}
        assert ungrouped_id in raw_event_ids
    finally:
        _cleanup(ungrouped_id)


def test_source_visits_prefers_visit_thumb_crop_when_done(conn_ok):
    # Reports run well after the fact (a scheduled window), so unlike the AI queue there's no
    # latency cost to always preferring the visit's own well-timed re-crop here.
    raw_id, det_id = _insert_raw_event(crop_image_base64="representative-crop")
    _insert_vehicle_sighting(raw_id)
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    db.mark_visit_thumb_crop_done(visit_id, "visit-crop")
    try:
        start, end = _window()
        data = db.get_report_data(start, end, source="visits")
        match = next(v for v in data["vehicles"] if v["raw_event_id"] == raw_id)
        assert match["crop_image_base64"] == "visit-crop"
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_source_visits_falls_back_to_representative_crop_when_thumb_crop_not_done(conn_ok):
    raw_id, det_id = _insert_raw_event(crop_image_base64="representative-crop")
    _insert_vehicle_sighting(raw_id)
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    try:
        start, end = _window()
        data = db.get_report_data(start, end, source="visits")
        match = next(v for v in data["vehicles"] if v["raw_event_id"] == raw_id)
        assert match["crop_image_base64"] == "representative-crop"
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_source_visits_includes_sighting_per_distinct_object_type(conn_ok):
    # Regression test: the dedup used to partition by visit_id alone, so a visit grouping a car
    # and a person together only ever surfaced one of the two sightings in the report -- the other
    # object type's already-analyzed sighting silently never showed up anywhere. Partitioning by
    # (visit_id, objects) keeps same-type dedup (a re-tracked duplicate of the same object still
    # collapses to one) while surfacing one sighting per distinct object type.
    car_id, car_det = _insert_raw_event("now() - interval '10 seconds'", objects="car")
    person_id, person_det = _insert_raw_event("now()", objects="person")
    car_dup_id, car_dup_det = _insert_raw_event("now() - interval '5 seconds'", objects="car")
    _insert_vehicle_sighting(car_id)
    _insert_person_sighting(person_id)
    _insert_vehicle_sighting(car_dup_id)
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car,person",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [car_det, person_det, car_dup_det],
    })
    try:
        start, end = _window()
        data = db.get_report_data(start, end, source="visits")
        vehicle_ids = {v["raw_event_id"] for v in data["vehicles"]}
        person_ids = {p["raw_event_id"] for p in data["persons"]}
        assert vehicle_ids == {car_id}
        assert person_ids == {person_id}
    finally:
        _cleanup(car_id, person_id, car_dup_id, visit_id=visit_id)


def test_source_visits_includes_preview_gif_when_done(conn_ok):
    raw_id, det_id = _insert_raw_event(crop_image_base64="representative-crop")
    _insert_vehicle_sighting(raw_id)
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    db.mark_visit_thumb_crop_done(visit_id, "visit-crop", "visit-gif")
    try:
        start, end = _window()
        data = db.get_report_data(start, end, source="visits")
        match = next(v for v in data["vehicles"] if v["raw_event_id"] == raw_id)
        assert match["preview_gif_base64"] == "visit-gif"
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_source_visits_preview_gif_null_when_not_done(conn_ok):
    ungrouped_id, _ = _insert_raw_event()
    _insert_vehicle_sighting(ungrouped_id)
    try:
        start, end = _window()
        data = db.get_report_data(start, end, source="visits")
        match = next(v for v in data["vehicles"] if v["raw_event_id"] == ungrouped_id)
        assert match["preview_gif_base64"] is None
    finally:
        _cleanup(ungrouped_id)


def test_include_preview_false_drops_gif_but_keeps_visit_crop(conn_ok):
    # A lightweight-report opt-out (e.g. n8n wanting a smaller payload) -- only the GIF is
    # dropped; the visit's own composite grid crop (crop_image_expr, unrelated to gif_image_expr)
    # still comes through exactly as it does with include_preview=True.
    raw_id, det_id = _insert_raw_event(crop_image_base64="representative-crop")
    _insert_vehicle_sighting(raw_id)
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    db.mark_visit_thumb_crop_done(visit_id, "visit-crop", "visit-gif")
    try:
        start, end = _window()
        data = db.get_report_data(start, end, source="visits", include_preview=False)
        match = next(v for v in data["vehicles"] if v["raw_event_id"] == raw_id)
        assert match["preview_gif_base64"] is None
        assert match["crop_image_base64"] == "visit-crop"
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_source_events_never_includes_preview_gif(conn_ok):
    # source="events" (the default) never applies the visit-crop/GIF preference at all -- matches
    # the crop preference's own scoping decision (only source=visits substitutes either artifact).
    raw_id, det_id = _insert_raw_event(crop_image_base64="representative-crop")
    _insert_vehicle_sighting(raw_id)
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    db.mark_visit_thumb_crop_done(visit_id, "visit-crop", "visit-gif")
    try:
        start, end = _window()
        data = db.get_report_data(start, end)
        match = next(v for v in data["vehicles"] if v["raw_event_id"] == raw_id)
        assert match["preview_gif_base64"] is None
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_source_events_never_uses_visit_thumb_crop(conn_ok):
    # source="events" (the default) never applies the visit-crop preference at all -- matches
    # claim_ai_batch's own scoping decision (only source=visits substitutes the crop).
    raw_id, det_id = _insert_raw_event(crop_image_base64="representative-crop")
    _insert_vehicle_sighting(raw_id)
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    db.mark_visit_thumb_crop_done(visit_id, "visit-crop")
    try:
        start, end = _window()
        data = db.get_report_data(start, end)
        match = next(v for v in data["vehicles"] if v["raw_event_id"] == raw_id)
        assert match["crop_image_base64"] == "representative-crop"
    finally:
        _cleanup(raw_id, visit_id=visit_id)
