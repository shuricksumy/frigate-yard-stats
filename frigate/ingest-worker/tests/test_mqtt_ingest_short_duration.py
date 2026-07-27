"""Tests for mqtt_ingest.py's min_event_duration_seconds filter -- a tracked-object lifecycle
shorter than the configured threshold is never inserted into raw_events at all, the same way and
for the same reason as the has_snapshot=false filter (see test_mqtt_ingest_no_snapshot.py):
confirmed live that Frigate's tracker can repeatedly lose/re-acquire a stationary object (foot
traffic occluding a parked car, motion/glare flicker) as a brand-new det_id every few seconds, each
one an independent 1-3 second lifecycle for what's really the same physical, unmoving object.
Unit tests only (monkeypatches db.insert_raw_event) -- no Postgres required.
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


def _event_payload(start_time: float, end_time: float, label: str = "car") -> bytes:
    return json.dumps({
        "type": "end",
        "after": {
            "camera": "outside", "label": label, "id": "det-1",
            "start_time": start_time, "end_time": end_time,
            "current_zones": [], "has_clip": True, "has_snapshot": True,
        },
    }).encode()


def test_short_event_never_inserted_when_below_threshold(monkeypatch):
    monkeypatch.setattr(config, "MIN_EVENT_DURATION_SECONDS", 3)
    calls = []
    monkeypatch.setattr(mqtt_ingest.db, "insert_raw_event", lambda event, *a, **k: calls.append(event))

    mqtt_ingest._handle_event_message(_Msg(_event_payload(1000.0, 1001.5)))  # 1.5s < 3s

    assert calls == []


def test_event_at_or_above_threshold_is_inserted(monkeypatch):
    monkeypatch.setattr(config, "MIN_EVENT_DURATION_SECONDS", 3)
    calls = []
    monkeypatch.setattr(mqtt_ingest.db, "insert_raw_event", lambda event, *a, **k: calls.append(event))

    mqtt_ingest._handle_event_message(_Msg(_event_payload(1000.0, 1003.0)))  # exactly 3s, not < 3s

    assert len(calls) == 1


def test_filter_disabled_by_default(monkeypatch):
    monkeypatch.setattr(config, "MIN_EVENT_DURATION_SECONDS", 0)
    calls = []
    monkeypatch.setattr(mqtt_ingest.db, "insert_raw_event", lambda event, *a, **k: calls.append(event))

    mqtt_ingest._handle_event_message(_Msg(_event_payload(1000.0, 1000.5)))  # 0.5s, but filter off

    assert len(calls) == 1


def test_per_type_override_only_filters_that_type(monkeypatch):
    monkeypatch.setattr(config, "MIN_EVENT_DURATION_SECONDS", 0)
    mqtt_ingest._profile = {"object_types": {"car": {"min_event_duration_seconds": 3}}}
    calls = []
    monkeypatch.setattr(mqtt_ingest.db, "insert_raw_event", lambda event, *a, **k: calls.append(event))

    try:
        mqtt_ingest._handle_event_message(_Msg(_event_payload(1000.0, 1001.0, label="car")))
        assert calls == []  # car: filtered (1s < 3s override)

        mqtt_ingest._handle_event_message(_Msg(_event_payload(1000.0, 1001.0, label="person")))
        assert len(calls) == 1  # person: no override, global default (0) applies -- not filtered
    finally:
        mqtt_ingest._profile = None
