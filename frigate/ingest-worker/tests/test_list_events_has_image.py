"""Integration tests for db.list_events' has_image filter (default true -- hides rows with no
stored crop, e.g. crop_status='skipped' or not yet 'done').

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
def two_events(conn_ok):
    # A unique camera name per test run scopes list_events(camera=...) to just these two rows,
    # so this doesn't depend on / interfere with whatever else is in the table.
    camera = f"pytest-has-image-{uuid.uuid4()}"
    with_image_id = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, crop_image_base64)
        VALUES (%s, 'z', 'car', now(), now(), %s, true, true, 'done', 'ZmFrZQ==')
        RETURNING id
        """,
        (camera, f"pytest-{uuid.uuid4()}"), fetch=True,
    )[0]["id"]
    without_image_id = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot, crop_status)
        VALUES (%s, 'z', 'car', now(), now(), %s, false, false, 'skipped')
        RETURNING id
        """,
        (camera, f"pytest-{uuid.uuid4()}"), fetch=True,
    )[0]["id"]
    yield camera, with_image_id, without_image_id
    db._execute(
        "DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)",
        ([with_image_id, without_image_id],),
    )


def test_has_image_default_true_hides_imageless_rows(two_events):
    camera, with_image_id, without_image_id = two_events
    rows = db.list_events(camera=camera, start=None, end=None)
    ids = {r["id"] for r in rows}
    assert with_image_id in ids
    assert without_image_id not in ids


def test_has_image_false_shows_everything(two_events):
    camera, with_image_id, without_image_id = two_events
    rows = db.list_events(camera=camera, start=None, end=None, has_image=False)
    ids = {r["id"] for r in rows}
    assert with_image_id in ids
    assert without_image_id in ids


def test_has_image_field_reflects_crop_image_presence(two_events):
    camera, with_image_id, without_image_id = two_events
    rows = {r["id"]: r for r in db.list_events(camera=camera, start=None, end=None, has_image=False)}
    assert rows[with_image_id]["has_image"] is True
    assert rows[without_image_id]["has_image"] is False
