"""Integration tests for db.insert_raw_event's crop_status/video_status branching.

Requires a reachable Postgres with schema.sql applied (same requirements as
test_db_video_queue.py -- see that file's module docstring).
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


def _event(has_snapshot: bool, has_clip: bool = True) -> dict:
    det_id = f"pytest-{uuid.uuid4()}"
    return {
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784154175.0, "end_time": 1784154177.0, "det_id": det_id,
        "has_clip": has_clip, "has_snapshot": has_snapshot,
    }


def _fetch_by_det_id(det_id: str) -> dict:
    rows = db._execute(
        "SELECT * FROM yard_stats.raw_events WHERE det_id = %s", (det_id,), fetch=True,
    )
    return rows[0]


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def test_insert_raw_event_has_snapshot_true_starts_crop_new(conn_ok):
    event = _event(has_snapshot=True)
    db.insert_raw_event(event)
    try:
        row = _fetch_by_det_id(event["det_id"])
        assert row["crop_status"] == "new"
        assert row["ai_status"] == "new"
    finally:
        db._execute("DELETE FROM yard_stats.raw_events WHERE det_id = %s", (event["det_id"],))


def test_insert_raw_event_has_snapshot_false_starts_crop_skipped(conn_ok):
    event = _event(has_snapshot=False)
    db.insert_raw_event(event)
    try:
        row = _fetch_by_det_id(event["det_id"])
        assert row["crop_status"] == "skipped"
        # ai_status gets the identical treatment -- a row that can never get a crop can never
        # satisfy claim_ai_batch's crop_status='done' requirement either, so it must not sit at
        # ai_status='new' forever (see insert_raw_event's own comment).
        assert row["ai_status"] == "skipped"
    finally:
        db._execute("DELETE FROM yard_stats.raw_events WHERE det_id = %s", (event["det_id"],))


def test_insert_raw_event_crop_skipped_rows_excluded_from_claim_where_clause(conn_ok):
    # Doesn't call claim_next_batch directly -- with a high enough limit it would claim (mutate)
    # unrelated real rows in whatever DB the suite runs against. Checks the same condition
    # claim_next_batch's WHERE clause uses instead, read-only.
    event = _event(has_snapshot=False)
    db.insert_raw_event(event)
    try:
        matches = db._execute(
            """
            SELECT id FROM yard_stats.raw_events
            WHERE det_id = %s AND has_snapshot = true AND crop_status IN ('new', 'retry')
            """,
            (event["det_id"],), fetch=True,
        )
        assert matches == []
    finally:
        db._execute("DELETE FROM yard_stats.raw_events WHERE det_id = %s", (event["det_id"],))


def test_insert_raw_event_ai_skipped_rows_excluded_from_claim_where_clause(conn_ok):
    # Same read-only check as the crop-stage test above, for claim_ai_batch's own WHERE clause --
    # confirms a row that starts ai_status='skipped' was never claimable to begin with, not just
    # that it happens not to be 'new'/'retry' by coincidence.
    event = _event(has_snapshot=False)
    db.insert_raw_event(event)
    try:
        matches = db._execute(
            """
            SELECT id FROM yard_stats.raw_events
            WHERE det_id = %s AND crop_status = 'done' AND ai_status IN ('new', 'retry')
            """,
            (event["det_id"],), fetch=True,
        )
        assert matches == []
    finally:
        db._execute("DELETE FROM yard_stats.raw_events WHERE det_id = %s", (event["det_id"],))


def test_insert_raw_event_resolves_store_video_from_profile_defaults(conn_ok, monkeypatch):
    # Regression test: store_video has no env var backing at all (see config.py) -- a deployment
    # can only enable it via profiles.yaml. insert_raw_event must resolve it through
    # profile_config, not a bare config.STORE_VIDEO read (which is always the hardcoded False and
    # can never see this profile-only override) -- confirmed live in production: a deployment with
    # store_video_alerts: true in profiles.yaml's defaults: still got video_status='skipped' on
    # every new visit until this was fixed.
    monkeypatch.setattr(config, "STORE_VIDEO", False)
    profile = {"defaults": {"store_video": True}}
    event = _event(has_snapshot=True)
    db.insert_raw_event(event, profile)
    try:
        row = _fetch_by_det_id(event["det_id"])
        assert row["video_status"] == "new"
    finally:
        db._execute("DELETE FROM yard_stats.raw_events WHERE det_id = %s", (event["det_id"],))


def test_insert_raw_event_resolves_store_video_per_type_override(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO", True)
    profile = {"object_types": {"car": {"store_video": False}}}
    event = _event(has_snapshot=True)  # objects="car"
    db.insert_raw_event(event, profile)
    try:
        row = _fetch_by_det_id(event["det_id"])
        assert row["video_status"] == "skipped"
    finally:
        db._execute("DELETE FROM yard_stats.raw_events WHERE det_id = %s", (event["det_id"],))


def test_insert_raw_event_falls_back_to_hardcoded_default_with_no_profile(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO", True)
    event = _event(has_snapshot=True)
    db.insert_raw_event(event, None)
    try:
        row = _fetch_by_det_id(event["det_id"])
        assert row["video_status"] == "new"
    finally:
        db._execute("DELETE FROM yard_stats.raw_events WHERE det_id = %s", (event["det_id"],))


def test_ensure_schema_backfills_stale_ai_new_rows_with_skipped_crop(conn_ok):
    # Simulates a row inserted under the old code (before ai_status='skipped' existed) -- crop_
    # status='skipped' but ai_status left at 'new' forever. ensure_schema's backfill UPDATE should
    # catch these on every startup, not just for newly-inserted rows.
    event = _event(has_snapshot=False)
    db.insert_raw_event(event)
    db._execute(
        "UPDATE yard_stats.raw_events SET ai_status = 'new' WHERE det_id = %s", (event["det_id"],),
    )
    try:
        row = _fetch_by_det_id(event["det_id"])
        assert row["ai_status"] == "new"  # confirm the simulated pre-fix state took effect

        db.ensure_schema()

        row = _fetch_by_det_id(event["det_id"])
        assert row["ai_status"] == "skipped"
    finally:
        db._execute("DELETE FROM yard_stats.raw_events WHERE det_id = %s", (event["det_id"],))
