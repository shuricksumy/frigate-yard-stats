"""Integration tests for db.record_visit -- populates visits + links raw_events.visit_id/
reconciled from a parsed frigate/reviews payload (see test_mqtt_ingest_review.py for the parsing
side). Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's
module docstring for setup notes. Only run against a local/throwaway Postgres.
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402


def _insert_raw_event(det_id: str) -> int:
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot)
        VALUES ('pytest-cam', 'pytest-zone', 'car', now(), now(), %s, true, true)
        RETURNING id
        """,
        (det_id,), fetch=True,
    )
    return rows[0]["id"]


def _review(det_ids: list[str], thumb_time: float | None = None) -> dict:
    return {
        "camera": "pytest-cam",
        "zone": "yard,yard_car_zone",
        "objects": "car,truck",
        "start_time": 1784198451.155298,
        "end_time": 1784198470.65966,
        "det_ids": det_ids,
        "thumb_time": thumb_time,
    }


def _cleanup_raw_events(*ids):
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(ids),))


def _cleanup_visit(visit_id):
    if visit_id is not None:
        db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def test_record_visit_inserts_visit_row(conn_ok):
    visit_id = db.record_visit(_review(det_ids=[]))
    try:
        rows = db._execute("SELECT * FROM yard_stats.visits WHERE id = %s", (visit_id,), fetch=True)
        assert rows[0]["zone"] == "yard,yard_car_zone"
        assert rows[0]["objects"] == "car,truck"
        assert rows[0]["cameras"] == "pytest-cam"
        assert rows[0]["camera_count"] == 1
    finally:
        _cleanup_visit(visit_id)


def test_record_visit_links_matching_raw_events(conn_ok):
    det_id_a = f"pytest-{uuid.uuid4()}"
    det_id_b = f"pytest-{uuid.uuid4()}"
    raw_id_a = _insert_raw_event(det_id_a)
    raw_id_b = _insert_raw_event(det_id_b)
    visit_id = None
    try:
        visit_id = db.record_visit(_review(det_ids=[det_id_a, det_id_b]))
        rows = db._execute(
            "SELECT id, visit_id, reconciled FROM yard_stats.raw_events WHERE id = ANY(%s) ORDER BY id",
            ([raw_id_a, raw_id_b],), fetch=True,
        )
        assert {r["visit_id"] for r in rows} == {visit_id}
        assert all(r["reconciled"] for r in rows)
    finally:
        _cleanup_raw_events(raw_id_a, raw_id_b)
        _cleanup_visit(visit_id)


def test_record_visit_stores_thumb_time_and_starts_new_when_enabled(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_ENABLED", True)
    visit_id = db.record_visit(_review(det_ids=[], thumb_time=1784198455.5))
    try:
        row = db._execute(
            "SELECT thumb_time, thumb_crop_status FROM yard_stats.visits WHERE id = %s",
            (visit_id,), fetch=True,
        )[0]
        assert row["thumb_time"] == 1784198455.5
        assert row["thumb_crop_status"] == "new"
    finally:
        _cleanup_visit(visit_id)


def test_record_visit_skips_thumb_crop_when_disabled(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_ENABLED", False)
    visit_id = db.record_visit(_review(det_ids=[], thumb_time=1784198455.5))
    try:
        row = db._execute(
            "SELECT thumb_time, thumb_crop_status FROM yard_stats.visits WHERE id = %s",
            (visit_id,), fetch=True,
        )[0]
        assert row["thumb_crop_status"] == "skipped"
    finally:
        _cleanup_visit(visit_id)


def test_record_visit_still_starts_new_when_frigate_omits_thumb_time(conn_ok, monkeypatch):
    # crop.build_visit_preview samples frames proportionally across the visit's own clip duration
    # (start_ts/end_ts/cameras only) -- unlike its predecessor (crop_visit_thumbnail, which seeked
    # to thumb_time specifically and could never succeed without it), a missing thumb_time no
    # longer means the re-crop can't happen, so this must still start 'new', not 'skipped'.
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_ENABLED", True)
    visit_id = db.record_visit(_review(det_ids=[], thumb_time=None))
    try:
        row = db._execute(
            "SELECT thumb_crop_status FROM yard_stats.visits WHERE id = %s",
            (visit_id,), fetch=True,
        )[0]
        assert row["thumb_crop_status"] == "new"
    finally:
        _cleanup_visit(visit_id)


def test_record_visit_resolves_store_video_alerts_from_profile_defaults(conn_ok, monkeypatch):
    # Regression test: store_video_alerts has no env var backing at all (see config.py) -- a
    # deployment can only enable it via profiles.yaml. record_visit must resolve it through
    # profile_config, not a bare config.STORE_VIDEO_ALERTS read (which is always the hardcoded
    # False and can never see this profile-only override).
    monkeypatch.setattr(config, "STORE_VIDEO_ALERTS", False)
    profile = {"defaults": {"store_video_alerts": True}}
    visit_id = db.record_visit(_review(det_ids=[]), profile)
    try:
        row = db._execute(
            "SELECT video_status FROM yard_stats.visits WHERE id = %s", (visit_id,), fetch=True,
        )[0]
        assert row["video_status"] == "new"
    finally:
        _cleanup_visit(visit_id)


def test_record_visit_resolves_visit_thumb_crop_enabled_from_profile_defaults(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_ENABLED", False)
    profile = {"defaults": {"visit_thumb_crop_enabled": True}}
    visit_id = db.record_visit(_review(det_ids=[]), profile)
    try:
        row = db._execute(
            "SELECT thumb_crop_status FROM yard_stats.visits WHERE id = %s", (visit_id,), fetch=True,
        )[0]
        assert row["thumb_crop_status"] == "new"
    finally:
        _cleanup_visit(visit_id)


def test_record_visit_resolves_per_type_override_against_representative_det_id(conn_ok, monkeypatch):
    # The representative type is the earliest-linked raw_event's own objects (by start_ts, id) --
    # resolved here via det_ids, before the visit row (and its raw_events.visit_id link) exists.
    monkeypatch.setattr(config, "STORE_VIDEO_ALERTS", True)
    det_id_car = f"pytest-{uuid.uuid4()}"
    raw_id = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot)
        VALUES ('pytest-cam', 'pytest-zone', 'car', now(), now(), %s, true, true)
        RETURNING id
        """,
        (det_id_car,), fetch=True,
    )[0]["id"]
    profile = {"object_types": {"car": {"store_video_alerts": False}}}
    visit_id = None
    try:
        # Global default (True) would normally start 'new', but the representative event is a
        # 'car', and car explicitly opts out -- must start 'skipped'.
        visit_id = db.record_visit(_review(det_ids=[det_id_car]), profile)
        row = db._execute(
            "SELECT video_status FROM yard_stats.visits WHERE id = %s", (visit_id,), fetch=True,
        )[0]
        assert row["video_status"] == "skipped"
    finally:
        _cleanup_raw_events(raw_id)
        _cleanup_visit(visit_id)


def test_visit_thumb_crop_will_be_attempted_resolves_via_profile_config(monkeypatch):
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_ENABLED", False)
    profile = {"object_types": {"car": {"visit_thumb_crop_enabled": True}}}
    assert db.visit_thumb_crop_will_be_attempted({}, profile, "car") is True
    assert db.visit_thumb_crop_will_be_attempted({}, profile, "person") is False
    assert db.visit_thumb_crop_will_be_attempted({}, None, None) is False


def test_record_visit_does_not_touch_unrelated_raw_events(conn_ok):
    unrelated_det_id = f"pytest-{uuid.uuid4()}"
    unrelated_id = _insert_raw_event(unrelated_det_id)
    visit_id = None
    try:
        visit_id = db.record_visit(_review(det_ids=[f"pytest-{uuid.uuid4()}"]))
        row = db._execute(
            "SELECT visit_id, reconciled FROM yard_stats.raw_events WHERE id = %s",
            (unrelated_id,), fetch=True,
        )[0]
        assert row["visit_id"] is None
        assert row["reconciled"] is False
    finally:
        _cleanup_raw_events(unrelated_id)
        _cleanup_visit(visit_id)
