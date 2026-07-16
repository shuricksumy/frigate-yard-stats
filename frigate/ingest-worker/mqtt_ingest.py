import json
import logging

import paho.mqtt.client as mqtt

import config
import db

logger = logging.getLogger(__name__)


def parse_payload(raw_payload: bytes) -> dict:
    # Same fields as the (now superseded) raw-event-logger.json's "Parse Event Payload" node.
    # No label filtering -- records every Frigate object type (car, truck, person, dog, ...).
    payload = json.loads(raw_payload)
    after = payload.get("after") or {}
    return {
        "type": payload.get("type"),
        "camera": after.get("camera"),
        "zone": ",".join(after.get("current_zones") or []),
        "objects": after.get("label"),
        "det_id": after.get("id"),
        "start_time": after.get("start_time"),
        "end_time": after.get("end_time"),
        "has_clip": bool(after.get("has_clip", False)),
        "has_snapshot": bool(after.get("has_snapshot", False)),
    }


def parse_review_payload(raw_payload: bytes) -> dict:
    # frigate/reviews -- same {type, before, after} envelope as frigate/events, but "after" is a
    # review/alert segment (Frigate's own tracker grouping multiple det_ids into one real-world
    # activity), not a single tracked object. data.detections is the list of det_ids this segment
    # bundles together -- confirmed live against production Frigate's /api/review.
    payload = json.loads(raw_payload)
    after = payload.get("after") or {}
    data = after.get("data") or {}
    return {
        "type": payload.get("type"),
        "camera": after.get("camera"),
        "zone": ",".join(data.get("zones") or []),
        "objects": ",".join(data.get("objects") or []),
        "start_time": after.get("start_time"),
        "end_time": after.get("end_time"),
        "det_ids": data.get("detections") or [],
    }


def _on_connect(client, userdata, flags, rc):
    logger.info("Connected to MQTT broker %s:%s (rc=%s)", config.MQTT_HOST, config.MQTT_PORT, rc)
    client.subscribe(config.MQTT_TOPIC)
    client.subscribe(config.MQTT_REVIEWS_TOPIC)


def _on_message(client, userdata, msg):
    if msg.topic == config.MQTT_REVIEWS_TOPIC:
        _handle_review_message(msg)
        return
    _handle_event_message(msg)


def _handle_event_message(msg):
    try:
        event = parse_payload(msg.payload)
    except Exception:
        logger.exception("Failed to parse %s payload", config.MQTT_TOPIC)
        return

    if event["type"] != "end":
        return

    try:
        db.insert_raw_event(event)
        logger.info(
            "Ingested raw_event camera=%s objects=%s det_id=%s",
            event["camera"], event["objects"], event["det_id"],
        )
    except Exception:
        logger.exception("Failed to insert raw_event for det_id=%s", event.get("det_id"))


def _handle_review_message(msg):
    try:
        review = parse_review_payload(msg.payload)
    except Exception:
        logger.exception("Failed to parse %s payload", config.MQTT_REVIEWS_TOPIC)
        return

    if review["type"] != "end":
        return

    try:
        visit_id = db.record_visit(review)
        logger.info(
            "Recorded visit id=%s camera=%s det_ids=%s",
            visit_id, review["camera"], review["det_ids"],
        )
    except Exception:
        logger.exception("Failed to record visit for camera=%s", review.get("camera"))


def start() -> mqtt.Client:
    client = mqtt.Client()
    if config.MQTT_USERNAME:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.connect(config.MQTT_HOST, config.MQTT_PORT)
    client.loop_start()
    return client
