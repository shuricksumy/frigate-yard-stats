"""Tests for the Telegram visit-summary send being deferred from mqtt_ingest.py to
visit_thumb_worker.py whenever a thumb-crop re-crop attempt will actually happen for a visit --
so the eventual message uses the well-timed high-res thumb-crop (or a fallback once the re-crop
is guaranteed to never succeed) instead of whatever the representative event's own crop looks
like at the moment the review closes.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Only run against a local/throwaway Postgres.
"""
import json
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import mqtt_ingest  # noqa: E402
import visit_thumb_worker  # noqa: E402


def _review_payload(thumb_time):
    det_id = f"pytest-{uuid.uuid4()}"
    return json.dumps({
        "type": "end",
        "after": {
            "camera": "pytest-cam",
            "start_time": 1784198451.155298,
            "end_time": 1784198470.65966,
            "data": {
                "detections": [det_id],
                "objects": ["car"],
                "zones": ["pytest-zone"],
                "thumb_time": thumb_time,
            },
        },
    }).encode(), det_id


def _insert_raw_event(det_id, crop_image_base64=None):
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64)
        VALUES ('pytest-cam', 'pytest-zone', 'car', now(), now(), %s, true, true,
                'done', 'new', %s)
        RETURNING id
        """,
        (det_id, crop_image_base64), fetch=True,
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


def test_immediate_send_skipped_when_thumb_crop_will_be_attempted(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "all")
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_ENABLED", True)
    calls = []
    monkeypatch.setattr(mqtt_ingest.telegram, "send_visit_summary", lambda *a, **k: calls.append((a, k)) or 1)

    payload, det_id = _review_payload(thumb_time=1784198460.0)
    raw_id = _insert_raw_event(det_id)

    class _Msg:
        pass
    msg = _Msg()
    msg.payload = payload

    try:
        mqtt_ingest._handle_review_message(msg)
        assert calls == []
    finally:
        rows = db._execute(
            "SELECT visit_id FROM yard_stats.raw_events WHERE id = %s", (raw_id,), fetch=True,
        )
        visit_id = rows[0]["visit_id"] if rows else None
        _cleanup(raw_id, visit_id=visit_id)


def test_immediate_send_happens_when_thumb_crop_disabled(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "all")
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_ENABLED", False)
    calls = []
    monkeypatch.setattr(mqtt_ingest.telegram, "send_visit_summary", lambda *a, **k: calls.append((a, k)) or 1)

    payload, det_id = _review_payload(thumb_time=1784198460.0)
    raw_id = _insert_raw_event(det_id)

    class _Msg:
        pass
    msg = _Msg()
    msg.payload = payload

    try:
        mqtt_ingest._handle_review_message(msg)
        assert len(calls) == 1
    finally:
        rows = db._execute(
            "SELECT visit_id FROM yard_stats.raw_events WHERE id = %s", (raw_id,), fetch=True,
        )
        visit_id = rows[0]["visit_id"] if rows else None
        _cleanup(raw_id, visit_id=visit_id)


def test_worker_sends_deferred_summary_on_successful_crop(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "all")
    monkeypatch.setattr(visit_thumb_worker, "crop", type("C", (), {"build_visit_preview": staticmethod(lambda v, r: ("new-visit-crop", "new-visit-gif"))}))
    calls = []
    monkeypatch.setattr(
        visit_thumb_worker.telegram, "send_visit_summary",
        lambda camera, objects, count, gif_base64=None, image_base64=None: calls.append(gif_base64 or image_base64) or 1,
    )

    det_id = f"pytest-{uuid.uuid4()}"
    raw_id = _insert_raw_event(det_id, crop_image_base64="representative-crop")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id], "thumb_time": 1784198460.0,
    })
    visit = db.get_visit(visit_id)
    try:
        visit_thumb_worker.process_claimed_visit(dict(visit, thumb_crop_attempt_count=1))
        # Telegram now gets the animated GIF (sendAnimation), not the composite grid (crop_
        # image_base64) -- the grid is still what's stored/analyzed/shown elsewhere, just not
        # what's sent to Telegram once a GIF is available.
        assert calls == ["new-visit-gif"]
        row = db._execute(
            "SELECT thumb_crop_status FROM yard_stats.visits WHERE id = %s", (visit_id,), fetch=True,
        )[0]
        assert row["thumb_crop_status"] == "done"
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_worker_sends_fallback_summary_once_crop_permanently_fails(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "all")
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_MAX_ATTEMPTS", 1)

    def _boom(visit, representative):
        raise RuntimeError("clip not ready")
    monkeypatch.setattr(visit_thumb_worker, "crop", type("C", (), {"build_visit_preview": staticmethod(_boom)}))
    calls = []
    monkeypatch.setattr(
        visit_thumb_worker.telegram, "send_visit_summary",
        lambda camera, objects, count, gif_base64=None, image_base64=None: calls.append(gif_base64 or image_base64) or 1,
    )

    det_id = f"pytest-{uuid.uuid4()}"
    raw_id = _insert_raw_event(det_id, crop_image_base64="representative-crop")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id], "thumb_time": 1784198460.0,
    })
    visit = db.get_visit(visit_id)
    try:
        # attempt_count=1 already means the next failure hits max_attempts=1 -> terminal 'failed'.
        visit_thumb_worker.process_claimed_visit(dict(visit, thumb_crop_attempt_count=1))
        assert calls == ["representative-crop"]
        row = db._execute(
            "SELECT thumb_crop_status FROM yard_stats.visits WHERE id = %s", (visit_id,), fetch=True,
        )[0]
        assert row["thumb_crop_status"] == "failed"
    finally:
        _cleanup(raw_id, visit_id=visit_id)


def test_worker_does_not_send_summary_while_still_retrying(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "all")
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_INITIAL_WAIT_SECONDS", 0)

    def _boom(visit, representative):
        raise RuntimeError("clip not ready")
    monkeypatch.setattr(visit_thumb_worker, "crop", type("C", (), {"build_visit_preview": staticmethod(_boom)}))
    calls = []
    monkeypatch.setattr(
        visit_thumb_worker.telegram, "send_visit_summary",
        lambda camera, objects, count, gif_base64=None, image_base64=None: calls.append(gif_base64 or image_base64) or 1,
    )

    det_id = f"pytest-{uuid.uuid4()}"
    raw_id = _insert_raw_event(det_id, crop_image_base64="representative-crop")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id], "thumb_time": 1784198460.0,
    })
    visit = db.get_visit(visit_id)
    try:
        visit_thumb_worker.process_claimed_visit(dict(visit, thumb_crop_attempt_count=0))
        assert calls == []
        row = db._execute(
            "SELECT thumb_crop_status FROM yard_stats.visits WHERE id = %s", (visit_id,), fetch=True,
        )[0]
        assert row["thumb_crop_status"] == "retry"
    finally:
        _cleanup(raw_id, visit_id=visit_id)
