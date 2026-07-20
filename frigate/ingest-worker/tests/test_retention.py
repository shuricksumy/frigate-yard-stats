"""Tests for db.py's retention/purge functions: run_retention_cleanup (the scheduled sweep),
purge_older_than (the full ad-hoc purge, POST /retention/purge's only_media=false), and
purge_media_older_than (the media-only ad-hoc purge, only_media=true, the default).

Confirmed live that both run_retention_cleanup and purge_older_than raised
psycopg2.errors.ForeignKeyViolation deleting visits while a still-linked raw_event's visit_id
pointed at them (raw_events.visit_id references visits(id), the opposite direction from the
delete order), and would have hit a second violation from visit_sightings once any visit with an
alert-stage sighting reached its cutoff. Both fixed by nulling raw_events.visit_id for affected
rows before deleting visits, and deleting visit_sightings before visits, same child-before-parent
shape the rest of this file already used for sightings before raw_events.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Additionally requires pgvector, same as test_semantic_search.py.
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


def _old_ts(days=100):
    return datetime.now(timezone.utc) - timedelta(days=days)


def _make_old_visit_with_everything(days_old=100, camera="pytest-retention-cam"):
    # An old raw_event + its visit, both carrying stored media, plus an alert-stage sighting on
    # the visit -- the exact combination that triggers both FK bugs this file guards against.
    det_id = f"pytest-retention-{uuid.uuid4()}"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64, video_path)
        VALUES (%s, 'z', 'car', %s, %s, %s, true, true, 'done', 'done', 'ZmFrZQ==', '/data/video/fake.mp4')
        RETURNING id
        """,
        (camera, _old_ts(days_old), _old_ts(days_old), det_id), fetch=True,
    )
    event_id = rows[0]["id"]
    visit_id = db.record_visit({
        "camera": camera, "zone": "z", "objects": "car",
        "start_time": _old_ts(days_old).timestamp(), "end_time": _old_ts(days_old).timestamp(),
        "det_ids": [det_id],
    })
    db._execute(
        "UPDATE yard_stats.visits SET start_ts = %s, thumb_crop_status = 'done', "
        "crop_image_base64 = 'ZmFrZQ==', preview_gif_base64 = 'ZmFrZQ==', "
        "video_path = '/data/video-alerts/fake.mp4' WHERE id = %s",
        (_old_ts(days_old), visit_id),
    )
    db.complete_visit_sighting(visit_id, "car", "red sedan")
    return event_id, visit_id


def _cleanup(event_id, visit_id):
    db._execute("DELETE FROM yard_stats.visit_sightings WHERE visit_id = %s", (visit_id,))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = %s", (event_id,))
    db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


# ---- purge_older_than (only_media=false) -- the FK-ordering fix ----

def test_purge_older_than_deletes_visit_with_linked_raw_event_and_alert_sighting(conn_ok):
    event_id, visit_id = _make_old_visit_with_everything()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        preview = db.purge_older_than(cutoff, execute=False)
        assert preview["visit_sightings"] >= 1
        assert preview["visits"] >= 1
        assert preview["raw_events"] >= 1

        result = db.purge_older_than(cutoff, execute=True)  # must not raise ForeignKeyViolation
        assert result["video_files"] >= 2  # one raw_event clip + one visit clip

        assert db.get_visit(visit_id) is None
        assert db.get_raw_event(event_id) is None
    finally:
        _cleanup(event_id, visit_id)  # no-ops if already purged


def test_purge_older_than_leaves_recent_rows_untouched(conn_ok):
    event_id, visit_id = _make_old_visit_with_everything(days_old=1)
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        db.purge_older_than(cutoff, execute=True)
        assert db.get_visit(visit_id) is not None
        assert db.get_raw_event(event_id) is not None
    finally:
        _cleanup(event_id, visit_id)


# ---- run_retention_cleanup -- the scheduled-sweep counterpart of the same fix ----

def test_run_retention_cleanup_deletes_old_visit_with_linked_raw_event(conn_ok):
    event_id, visit_id = _make_old_visit_with_everything(days_old=400)
    try:
        db.run_retention_cleanup(retention_months=12)  # must not raise ForeignKeyViolation
        assert db.get_visit(visit_id) is None
        assert db.get_raw_event(event_id) is None
    finally:
        _cleanup(event_id, visit_id)


# ---- purge_media_older_than (only_media=true, the default) ----

def test_purge_media_older_than_preview_matches_execute_counts(conn_ok):
    event_id, visit_id = _make_old_visit_with_everything()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        preview = db.purge_media_older_than(cutoff, execute=False)
        assert preview["raw_events_video_files"] >= 1
        assert preview["raw_events_images"] >= 1
        assert preview["visits_video_files"] >= 1
        assert preview["visits_images_or_gifs"] >= 1
        assert "video_files_deleted" not in preview  # dry run never reports an action taken

        result = db.purge_media_older_than(cutoff, execute=True)
        assert result["raw_events_video_files"] == preview["raw_events_video_files"]
        # The fixture's paths don't exist on disk in this test env -- _delete_video_files treats a
        # missing file as already-gone (FileNotFoundError is caught, not counted), same as
        # production behavior for a path left over from before VIDEO_STORAGE_PATH existed. This
        # only exercises that the key is reported, not real file I/O.
        assert result["video_files_deleted"] == 0
    finally:
        _cleanup(event_id, visit_id)


def test_purge_media_older_than_clears_media_but_keeps_rows_and_text(conn_ok):
    event_id, visit_id = _make_old_visit_with_everything()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        db.purge_media_older_than(cutoff, execute=True)

        event = db.get_raw_event(event_id)
        assert event is not None  # row kept
        assert event["video_path"] is None
        assert event["crop_image_base64"] is None
        assert event["ai_status"] == "done"  # queue-state/text metadata untouched

        visit = db.get_visit(visit_id)
        assert visit is not None
        assert visit["video_path"] is None
        assert visit["crop_image_base64"] is None
        assert visit["preview_gif_base64"] is None

        sighting = db.get_visit_alert_sighting(visit_id)
        assert sighting is not None  # alert-stage text analysis fully preserved
        assert sighting["object_label"] == "car"
        assert sighting["description"] == "red sedan"
    finally:
        _cleanup(event_id, visit_id)


def test_purge_media_older_than_never_touches_recent_rows(conn_ok):
    event_id, visit_id = _make_old_visit_with_everything(days_old=1)
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        db.purge_media_older_than(cutoff, execute=True)
        event = db.get_raw_event(event_id)
        assert event["crop_image_base64"] is not None
        assert event["video_path"] is not None
    finally:
        _cleanup(event_id, visit_id)


def test_purge_media_older_than_does_not_delete_sighting_rows(conn_ok):
    event_id, visit_id = _make_old_visit_with_everything()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        db.purge_media_older_than(cutoff, execute=True)
        # The row-deleting full purge counts these -- the media-only purge must never remove them.
        rows = db._execute(
            "SELECT count(*)::int AS c FROM yard_stats.visit_sightings WHERE visit_id = %s",
            (visit_id,), fetch=True,
        )
        assert rows[0]["c"] == 1
    finally:
        _cleanup(event_id, visit_id)


# ---- object_label filter (only ever scopes raw_events/sightings -- never visits/visit_sightings,
# since a visit can span multiple distinct object types with no single-type-safe way to decide it
# belongs to just one type's purge) ----

def _make_old_raw_event(days_old, objects, camera):
    det_id = f"pytest-retention-{uuid.uuid4()}"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64, video_path)
        VALUES (%s, 'z', %s, %s, %s, %s, true, true, 'done', 'done', 'ZmFrZQ==', '/data/video/fake.mp4')
        RETURNING id
        """,
        (camera, objects, _old_ts(days_old), _old_ts(days_old), det_id), fetch=True,
    )
    event_id = rows[0]["id"]
    db._execute(
        "INSERT INTO yard_stats.sightings (raw_event_id, object_label, description) VALUES (%s, %s, 'x')",
        (event_id, objects),
    )
    return event_id


def _cleanup_events(*event_ids):
    db._execute("DELETE FROM yard_stats.sightings WHERE raw_event_id = ANY(%s)", (list(event_ids),))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(event_ids),))


def test_purge_older_than_object_label_only_affects_matching_type(conn_ok):
    car_id = _make_old_raw_event(100, "car", "pytest-retention-label-cam")
    person_id = _make_old_raw_event(100, "person", "pytest-retention-label-cam")
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        preview = db.purge_older_than(cutoff, execute=False, object_label="car")
        assert preview["raw_events"] == 1
        assert preview["sightings"] == 1
        assert preview["visits"] == 0
        assert preview["visit_sightings"] == 0

        db.purge_older_than(cutoff, execute=True, object_label="car")
        assert db.get_raw_event(car_id) is None
        assert db.get_raw_event(person_id) is not None  # untouched -- different type
    finally:
        _cleanup_events(car_id, person_id)


def test_purge_older_than_object_label_never_touches_visits(conn_ok):
    event_id, visit_id = _make_old_visit_with_everything()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        db.purge_older_than(cutoff, execute=True, object_label="car")
        # The matching raw_event is gone (it's the type-scoped purge's whole point)...
        assert db.get_raw_event(event_id) is None
        # ...but the visit and its own alert sighting/media are completely untouched, even though
        # the visit's representative event was itself a "car".
        assert db.get_visit(visit_id) is not None
        assert db.get_visit_alert_sighting(visit_id) is not None
    finally:
        db._execute("DELETE FROM yard_stats.visit_sightings WHERE visit_id = %s", (visit_id,))
        db._execute("DELETE FROM yard_stats.raw_events WHERE id = %s", (event_id,))
        db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


def test_purge_media_older_than_object_label_only_affects_matching_type(conn_ok):
    car_id = _make_old_raw_event(100, "car", "pytest-retention-label-cam")
    person_id = _make_old_raw_event(100, "person", "pytest-retention-label-cam")
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        preview = db.purge_media_older_than(cutoff, execute=False, object_label="car")
        assert preview["raw_events_video_files"] == 1
        assert preview["raw_events_images"] == 1
        assert preview["visits_video_files"] == 0
        assert preview["visits_images_or_gifs"] == 0

        db.purge_media_older_than(cutoff, execute=True, object_label="car")
        car_event = db.get_raw_event(car_id)
        person_event = db.get_raw_event(person_id)
        assert car_event["crop_image_base64"] is None
        assert car_event["video_path"] is None
        assert person_event["crop_image_base64"] is not None  # untouched -- different type
        assert person_event["video_path"] is not None
    finally:
        _cleanup_events(car_id, person_id)
