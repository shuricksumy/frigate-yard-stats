"""Unit tests for mqtt_ingest.parse_review_payload -- no Postgres/MQTT required, pure payload
parsing. Sample payload shape confirmed live against production Frigate's /api/review (frigate/
reviews carries the same {type, before, after} envelope, "after" being the review/alert segment).
"""
import json
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import mqtt_ingest  # noqa: E402


def _payload(msg_type="end", detections=None, objects=None, zones=None, thumb_time=None):
    return json.dumps({
        "type": msg_type,
        "before": {},
        "after": {
            "camera": "outside",
            "start_time": 1784198451.155298,
            "end_time": 1784198470.65966,
            "severity": "alert",
            "data": {
                "detections": detections if detections is not None else [
                    "1784198409.05586-34ion3", "1784198459.85577-dcle6n",
                ],
                "objects": objects if objects is not None else ["truck", "car"],
                "zones": zones if zones is not None else ["yard", "yard_car_zone"],
                "thumb_time": thumb_time if thumb_time is not None else 1784198455.5,
            },
        },
    }).encode()


def test_parse_review_payload_extracts_fields():
    result = mqtt_ingest.parse_review_payload(_payload())
    assert result["type"] == "end"
    assert result["camera"] == "outside"
    assert result["start_time"] == 1784198451.155298
    assert result["end_time"] == 1784198470.65966
    assert result["zone"] == "yard,yard_car_zone"
    assert result["objects"] == "truck,car"
    assert result["det_ids"] == ["1784198409.05586-34ion3", "1784198459.85577-dcle6n"]
    assert result["thumb_time"] == 1784198455.5


def test_parse_review_payload_handles_missing_data():
    payload = json.dumps({"type": "new", "after": {"camera": "outside2"}}).encode()
    result = mqtt_ingest.parse_review_payload(payload)
    assert result["type"] == "new"
    assert result["camera"] == "outside2"
    assert result["zone"] == ""
    assert result["objects"] == ""
    assert result["det_ids"] == []
    assert result["thumb_time"] is None


def test_on_message_routes_reviews_topic_without_touching_events_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(mqtt_ingest, "_handle_review_message", lambda msg: calls.append("review"))
    monkeypatch.setattr(mqtt_ingest, "_handle_event_message", lambda msg: calls.append("event"))

    import config

    class _Msg:
        topic = config.MQTT_REVIEWS_TOPIC

    mqtt_ingest._on_message(None, None, _Msg())
    assert calls == ["review"]


def test_on_message_routes_events_topic(monkeypatch):
    calls = []
    monkeypatch.setattr(mqtt_ingest, "_handle_review_message", lambda msg: calls.append("review"))
    monkeypatch.setattr(mqtt_ingest, "_handle_event_message", lambda msg: calls.append("event"))

    import config

    class _Msg:
        topic = config.MQTT_TOPIC

    mqtt_ingest._on_message(None, None, _Msg())
    assert calls == ["event"]
