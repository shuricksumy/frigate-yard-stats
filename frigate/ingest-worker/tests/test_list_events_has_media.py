"""Integration tests for db.list_events' has_media filter (default true -- hides rows with
neither a stored crop image nor a stored video, e.g. crop_status='skipped' or not yet 'done')
and its event_id exact-match filter.

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


@pytest.fixture
def three_events(conn_ok):
    # A unique camera name per test run scopes list_events(camera=...) to just these rows, so
    # this doesn't depend on / interfere with whatever else is in the table.
    camera = f"pytest-has-media-{uuid.uuid4()}"

    def insert(crop_image_base64, video_path):
        return db._execute(
            """
            INSERT INTO yard_stats.raw_events
                (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
                 crop_status, crop_image_base64, video_path)
            VALUES (%s, 'z', 'car', now(), now(), %s, true, true, 'done', %s, %s)
            RETURNING id
            """,
            (camera, f"pytest-{uuid.uuid4()}", crop_image_base64, video_path), fetch=True,
        )[0]["id"]

    image_only_id = insert("ZmFrZQ==", None)
    # Not reachable via the real pipeline today (claim_video_batch only claims crop_status='done'
    # rows, so a video always implies a crop image already exists) -- inserted directly to prove
    # the OR logic itself, defensively, in case that invariant ever changes.
    video_only_id = insert(None, "/data/video/fake.mp4")
    neither_id = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot, crop_status)
        VALUES (%s, 'z', 'car', now(), now(), %s, false, false, 'skipped')
        RETURNING id
        """,
        (camera, f"pytest-{uuid.uuid4()}"), fetch=True,
    )[0]["id"]

    yield camera, image_only_id, video_only_id, neither_id
    db._execute(
        "DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)",
        ([image_only_id, video_only_id, neither_id],),
    )


def test_has_media_default_true_hides_rows_with_neither(three_events):
    camera, image_only_id, video_only_id, neither_id = three_events
    rows = db.list_events(camera=camera, start=None, end=None)
    ids = {r["id"] for r in rows}
    assert image_only_id in ids
    assert video_only_id in ids
    assert neither_id not in ids


def test_has_media_false_shows_everything(three_events):
    camera, image_only_id, video_only_id, neither_id = three_events
    rows = db.list_events(camera=camera, start=None, end=None, has_media=False)
    ids = {r["id"] for r in rows}
    assert image_only_id in ids
    assert video_only_id in ids
    assert neither_id in ids


def test_has_image_and_has_video_fields_are_independent(three_events):
    camera, image_only_id, video_only_id, neither_id = three_events
    rows = {r["id"]: r for r in db.list_events(camera=camera, start=None, end=None, has_media=False)}
    assert rows[image_only_id]["has_image"] is True
    assert rows[image_only_id]["has_video"] is False
    assert rows[video_only_id]["has_image"] is False
    assert rows[video_only_id]["has_video"] is True
    assert rows[neither_id]["has_image"] is False
    assert rows[neither_id]["has_video"] is False


def test_event_id_filter_exact_matches_ignoring_other_filters(three_events):
    camera, image_only_id, video_only_id, neither_id = three_events
    # event_id should surface even a has_media=false-only row without passing has_media=false --
    # matches the API layer's behavior of an id search overriding every other filter.
    rows = db.list_events(camera=None, start=None, end=None, event_id=neither_id)
    ids = {r["id"] for r in rows}
    assert ids == {neither_id}
