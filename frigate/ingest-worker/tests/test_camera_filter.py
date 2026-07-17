"""Tests for config.CAMERAS -- an optional camera allow-list applied to both the events flow
(frigate/events) and the alerts flow (frigate/reviews) at ingest time in mqtt_ingest.py, so a
camera not on the list never gets a raw_events/visits row at all. Unit tests only (monkeypatches
db.insert_raw_event/db.record_visit) -- no Postgres required.
"""
import json
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import config  # noqa: E402
import mqtt_ingest  # noqa: E402


class _Msg:
    def __init__(self, payload: bytes):
        self.payload = payload


def _event_payload(camera: str) -> bytes:
    return json.dumps({
        "type": "end",
        "after": {
            "camera": camera, "label": "car", "id": "det-1",
            "start_time": 1784198451.0, "end_time": 1784198470.0,
            "current_zones": [], "has_clip": True, "has_snapshot": True,
        },
    }).encode()


def _review_payload(camera: str) -> bytes:
    return json.dumps({
        "type": "end",
        "after": {
            "camera": camera, "start_time": 1784198451.0, "end_time": 1784198470.0,
            "data": {"detections": ["det-1"], "objects": ["car"], "zones": ["yard"]},
        },
    }).encode()


def test_event_skipped_when_camera_not_in_allow_list(monkeypatch):
    monkeypatch.setattr(config, "CAMERAS", ["outside"])
    calls = []
    monkeypatch.setattr(mqtt_ingest.db, "insert_raw_event", lambda event: calls.append(event))

    mqtt_ingest._handle_event_message(_Msg(_event_payload("outside2")))

    assert calls == []


def test_event_processed_when_camera_in_allow_list(monkeypatch):
    monkeypatch.setattr(config, "CAMERAS", ["outside", "outside2"])
    calls = []
    monkeypatch.setattr(mqtt_ingest.db, "insert_raw_event", lambda event: calls.append(event))

    mqtt_ingest._handle_event_message(_Msg(_event_payload("outside2")))

    assert len(calls) == 1


def test_event_processed_when_allow_list_empty(monkeypatch):
    monkeypatch.setattr(config, "CAMERAS", [])
    calls = []
    monkeypatch.setattr(mqtt_ingest.db, "insert_raw_event", lambda event: calls.append(event))

    mqtt_ingest._handle_event_message(_Msg(_event_payload("any-camera")))

    assert len(calls) == 1


def test_review_skipped_when_camera_not_in_allow_list(monkeypatch):
    monkeypatch.setattr(config, "CAMERAS", ["outside"])
    calls = []
    monkeypatch.setattr(mqtt_ingest.db, "record_visit", lambda review: calls.append(review) or 1)

    mqtt_ingest._handle_review_message(_Msg(_review_payload("outside2")))

    assert calls == []


def test_review_processed_when_camera_in_allow_list(monkeypatch):
    monkeypatch.setattr(config, "CAMERAS", ["outside2"])
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "none")
    calls = []
    monkeypatch.setattr(mqtt_ingest.db, "record_visit", lambda review: calls.append(review) or 1)

    mqtt_ingest._handle_review_message(_Msg(_review_payload("outside2")))

    assert len(calls) == 1
