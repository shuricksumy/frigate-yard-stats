"""Tests for mqtt_ingest.py's has_snapshot filter -- a tracked-object lifecycle Frigate never gave
a snapshot to can never be cropped/stored on video/AI-analyzed regardless of retries (has_snapshot
is Frigate's own final answer by the time we act on it, since we only ever process the "end"
message, never "new"/"update" -- see mqtt_ingest._handle_event_message's own comment), so such an
event is never even inserted into raw_events at all. Confirmed live in production this was the
overwhelming majority of MQTT traffic on a busy camera (~98% of one camera's "car" detections)
with zero analytical value -- each such row never gets an image, video, or AI description, ever.
Unit tests only (monkeypatches db.insert_raw_event) -- no Postgres required.
"""
import json
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import mqtt_ingest  # noqa: E402


class _Msg:
    def __init__(self, payload: bytes):
        self.payload = payload


def _event_payload(has_snapshot: bool) -> bytes:
    return json.dumps({
        "type": "end",
        "after": {
            "camera": "outside", "label": "car", "id": "det-1",
            "start_time": 1784198451.0, "end_time": 1784198470.0,
            "current_zones": [], "has_clip": True, "has_snapshot": has_snapshot,
        },
    }).encode()


def test_event_never_inserted_when_no_snapshot(monkeypatch):
    calls = []
    monkeypatch.setattr(mqtt_ingest.db, "insert_raw_event", lambda event, *a, **k: calls.append(event))

    mqtt_ingest._handle_event_message(_Msg(_event_payload(has_snapshot=False)))

    assert calls == []


def test_event_inserted_when_snapshot_present(monkeypatch):
    calls = []
    monkeypatch.setattr(mqtt_ingest.db, "insert_raw_event", lambda event, *a, **k: calls.append(event))

    mqtt_ingest._handle_event_message(_Msg(_event_payload(has_snapshot=True)))

    assert len(calls) == 1
