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


def _on_connect(client, userdata, flags, rc):
    logger.info("Connected to MQTT broker %s:%s (rc=%s)", config.MQTT_HOST, config.MQTT_PORT, rc)
    client.subscribe(config.MQTT_TOPIC)


def _on_message(client, userdata, msg):
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


def start() -> mqtt.Client:
    client = mqtt.Client()
    if config.MQTT_USERNAME:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.connect(config.MQTT_HOST, config.MQTT_PORT)
    client.loop_start()
    return client
