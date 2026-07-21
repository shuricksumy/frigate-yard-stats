"""Integration tests for db.list_events'/db._build_events_query's remaining filters --
object_type, crop_status, ai_status, video_status, the start/end time window, and camera actually
*excluding* another camera's rows (not just scoping a test to one camera, which every other test
file already does implicitly) -- plus one test combining several filters at once. has_media
tests / event_id / visit_id / q already have their own dedicated files (test_list_events_has_media.
py, test_event_sighting_lookup.py, test_visit_connected_events.py).

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


@pytest.fixture
def three_distinct_events(conn_ok):
    # camera_a/camera_b are both unique per test run -- lets the camera filter be tested as an
    # actual excluder (camera_b's row must never show up when filtering to camera_a) rather than
    # just a scoping mechanism that happens to return only this fixture's own rows.
    camera_a = f"pytest-filters-a-{uuid.uuid4()}"
    camera_b = f"pytest-filters-b-{uuid.uuid4()}"
    now = datetime.now(timezone.utc)

    def insert(camera, objects, crop_status, ai_status, video_status, start_ts, with_image=True):
        return db._execute(
            """
            INSERT INTO yard_stats.raw_events
                (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
                 crop_status, ai_status, video_status, crop_image_base64, video_path)
            VALUES (%s, 'z', %s, %s, %s, %s, true, true, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                camera, objects, start_ts, start_ts, f"pytest-{uuid.uuid4()}",
                crop_status, ai_status, video_status,
                "ZmFrZQ==" if with_image else None,
                None if with_image else "/data/video/fake.mp4",
            ),
            fetch=True,
        )[0]["id"]

    # car, camera_a, fully done, 2 hours ago
    car_done = insert(camera_a, "car", "done", "done", "done", now - timedelta(hours=2))
    # person, camera_a, still queued for AI/video, 1 hour ago
    person_queued = insert(camera_a, "person", "done", "new", "skipped", now - timedelta(hours=1))
    # car, camera_b, crop still retrying (no image yet -- give it a video instead so has_media's
    # default doesn't hide it), right now
    car_retry_other_camera = insert(
        camera_b, "car", "retry", "done", "done", now, with_image=False,
    )

    ids = (car_done, person_queued, car_retry_other_camera)
    yield camera_a, camera_b, now, ids
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(ids),))


def test_object_type_filters_to_matching_types_only(three_distinct_events):
    camera_a, camera_b, now, (car_done, person_queued, car_retry_other_camera) = three_distinct_events
    ids = {r["id"] for r in db.list_events(object_type="car", start=None, end=None, has_media=False)}
    assert car_done in ids
    assert car_retry_other_camera in ids
    assert person_queued not in ids


def test_object_type_accepts_comma_separated_list(three_distinct_events):
    camera_a, camera_b, now, (car_done, person_queued, car_retry_other_camera) = three_distinct_events
    ids = {
        r["id"] for r in db.list_events(
            object_type="car,person", camera=camera_a, start=None, end=None, has_media=False,
        )
    }
    assert ids == {car_done, person_queued}


def test_crop_status_filter(three_distinct_events):
    camera_a, camera_b, now, (car_done, person_queued, car_retry_other_camera) = three_distinct_events
    ids = {r["id"] for r in db.list_events(crop_status="retry", start=None, end=None, has_media=False)}
    assert ids == {car_retry_other_camera}


def test_ai_status_filter(three_distinct_events):
    camera_a, camera_b, now, (car_done, person_queued, car_retry_other_camera) = three_distinct_events
    ids = {r["id"] for r in db.list_events(ai_status="new", start=None, end=None, has_media=False)}
    assert ids == {person_queued}


def test_video_status_filter(three_distinct_events):
    camera_a, camera_b, now, (car_done, person_queued, car_retry_other_camera) = three_distinct_events
    ids = {r["id"] for r in db.list_events(video_status="skipped", start=None, end=None, has_media=False)}
    assert ids == {person_queued}


def test_camera_filter_excludes_other_cameras_rows(three_distinct_events):
    # Confirms camera actually narrows results (not just "happens to scope a test fixture") --
    # camera_b's own row must never appear when filtering to camera_a.
    camera_a, camera_b, now, (car_done, person_queued, car_retry_other_camera) = three_distinct_events
    ids = {r["id"] for r in db.list_events(camera=camera_a, start=None, end=None, has_media=False)}
    assert ids == {car_done, person_queued}
    assert car_retry_other_camera not in ids


def test_start_end_time_window_filter(three_distinct_events):
    camera_a, camera_b, now, (car_done, person_queued, car_retry_other_camera) = three_distinct_events
    # Window covers only "1 hour ago" (person_queued) -- excludes the 2-hours-ago row (before
    # start) and the "now" row (after end).
    ids = {
        r["id"] for r in db.list_events(
            start=now - timedelta(minutes=90), end=now - timedelta(minutes=30), has_media=False,
        )
    }
    assert person_queued in ids
    assert car_done not in ids
    assert car_retry_other_camera not in ids


def test_combined_object_type_camera_and_ai_status_filters(three_distinct_events):
    # All three filters must compose with AND semantics -- only the one row matching every
    # condition at once should come back.
    camera_a, camera_b, now, (car_done, person_queued, car_retry_other_camera) = three_distinct_events
    ids = {
        r["id"] for r in db.list_events(
            object_type="car", camera=camera_a, ai_status="done", start=None, end=None, has_media=False,
        )
    }
    assert ids == {car_done}


def test_count_events_matches_list_events_filter_set(three_distinct_events):
    # count_events shares _build_events_query with list_events -- confirms the two can't drift
    # apart for a realistic multi-filter combination.
    camera_a, camera_b, now, (car_done, person_queued, car_retry_other_camera) = three_distinct_events
    filters = dict(object_type="car,person", camera=camera_a, start=None, end=None, has_media=False)
    assert db.count_events(**filters) == len(db.list_events(**filters))
