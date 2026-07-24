import json
import logging

import paho.mqtt.client as mqtt

import config
import db
import profile_config
import telegram

logger = logging.getLogger(__name__)

# Set once by start(profile) -- read by _handle_review_message to resolve the effective
# telegram_alerts_mode for a visit's representative event's object type. Module-level rather than
# threaded through every MQTT callback argument, since paho-mqtt's on_message signature is fixed
# and doesn't leave room for an extra parameter.
_profile: dict | None = None

# Latest frigate/stats snapshot (summarized, see summarize_stats) and frigate/available state --
# live current-state only, never persisted to Postgres (there's no historical value in "what was
# Frigate's CPU usage 3 days ago" the way there is for raw_events/visits). Plain module globals,
# same pattern _profile already uses -- paho-mqtt callbacks all run on the same network loop
# thread, so there's no concurrent-write race to guard against; a reader (api.py, from FastAPI's
# own thread) only ever sees a fully-formed dict or None, never a partial write, since Python
# attribute assignment for a plain variable is atomic.
_latest_stats: dict | None = None
_frigate_available: bool | None = None


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
        # Frigate's own per-review "best frame" choice -- confirmed live via MQTT to be present on
        # every review message (even type="new"), and distinct from start_time (which is just the
        # review's id/thumb_path filename). Content/score-dependent, not a fixed offset -- see
        # visit_thumb_worker.py.
        "thumb_time": data.get("thumb_time"),
    }


def summarize_stats(raw: dict) -> dict:
    # frigate/stats' raw payload also includes a cpu_usages entry per OS process Frigate's own
    # container is running (s6-supervise, nginx, go2rtc, ...), which is irrelevant noise for our
    # purposes and makes the payload much bigger than it needs to be for what the admin dashboard
    # actually wants to show -- trim to just the genuinely useful subset: per-camera fps/detection
    # health, detector (Coral/CPU) inference speed, Frigate's own overall process CPU/mem, and
    # whatever GPU is configured (key name varies by hardware -- amd-vaapi, nvidia, etc. -- so this
    # is passed through generically rather than hardcoding one vendor's key).
    cameras = {
        name: {
            "camera_fps": c.get("camera_fps"),
            "detection_fps": c.get("detection_fps"),
            "detection_enabled": c.get("detection_enabled"),
        }
        for name, c in (raw.get("cameras") or {}).items()
    }
    detectors = {
        name: {"inference_speed": d.get("inference_speed")}
        for name, d in (raw.get("detectors") or {}).items()
    }
    frigate_process = (raw.get("cpu_usages") or {}).get("frigate.full_system") or {}
    return {
        "cameras": cameras,
        "detectors": detectors,
        "cpu_percent": frigate_process.get("cpu"),
        "mem_percent": frigate_process.get("mem"),
        "gpu_usages": raw.get("gpu_usages") or {},
    }


def _on_connect(client, userdata, flags, rc):
    logger.info("Connected to MQTT broker %s:%s (rc=%s)", config.MQTT_HOST, config.MQTT_PORT, rc)
    client.subscribe(config.MQTT_TOPIC)
    client.subscribe(config.MQTT_REVIEWS_TOPIC)
    client.subscribe(config.MQTT_STATS_TOPIC)
    client.subscribe(config.MQTT_AVAILABLE_TOPIC)


def _on_message(client, userdata, msg):
    if msg.topic == config.MQTT_REVIEWS_TOPIC:
        _handle_review_message(msg)
        return
    if msg.topic == config.MQTT_STATS_TOPIC:
        _handle_stats_message(msg)
        return
    if msg.topic == config.MQTT_AVAILABLE_TOPIC:
        _handle_available_message(msg)
        return
    _handle_event_message(msg)


def _handle_stats_message(msg):
    global _latest_stats
    try:
        _latest_stats = summarize_stats(json.loads(msg.payload))
    except Exception:
        logger.exception("Failed to parse %s payload", config.MQTT_STATS_TOPIC)


def _handle_available_message(msg):
    # Plain text payload ("online"/"offline"), not JSON -- Frigate's own MQTT last-will/birth
    # message convention.
    global _frigate_available
    _frigate_available = msg.payload.decode("utf-8", errors="replace").strip() == "online"


def get_frigate_health() -> dict:
    return {"available": _frigate_available, "stats": _latest_stats}


def _handle_event_message(msg):
    try:
        event = parse_payload(msg.payload)
    except Exception:
        logger.exception("Failed to parse %s payload", config.MQTT_TOPIC)
        return

    if event["type"] != "end":
        return

    if config.CAMERAS and event["camera"] not in config.CAMERAS:
        return

    # has_snapshot=false on an "end" message is Frigate's own final, terminal answer for this
    # det_id -- we only ever act on "end" (see above), never "new"/"update", so there's no race
    # where a snapshot might still arrive later for the same det_id. A tracked-object lifecycle
    # that never got one can never be cropped, stored on video, or AI-analyzed regardless of
    # retries (db.insert_raw_event would immediately mark it crop_status/video_status/ai_status=
    # 'skipped' for exactly this reason -- see its own comment) -- confirmed live in production
    # this is the overwhelming majority of traffic on a busy camera (~98% of one camera's "car"
    # detections, ~14,000 rows) with zero analytical value (each skipped row never gets an image,
    # video, or AI description, ever) and confirmed via profile_config/Frigate tuning that it's
    # tracker-confidence noise (borderline detections that don't sustain long enough to become a
    # real event), not a timing gap retrying would fix. Filtering here means it's never written to
    # Postgres at all, rather than inserted and immediately marked terminal -- a raw_events row
    # with no snapshot has no image/video/AI value and was never queryable for anything a caller
    # couldn't already get from Frigate's own event history directly.
    if not event["has_snapshot"]:
        logger.debug(
            "Skipping raw_event with no snapshot camera=%s objects=%s det_id=%s",
            event["camera"], event["objects"], event["det_id"],
        )
        return

    try:
        db.insert_raw_event(event, _profile)
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

    if config.CAMERAS and review["camera"] not in config.CAMERAS:
        return

    try:
        visit_id = db.record_visit(review, _profile)
        logger.info(
            "Recorded visit id=%s camera=%s det_ids=%s",
            visit_id, review["camera"], review["det_ids"],
        )
    except Exception:
        logger.exception("Failed to record visit for camera=%s", review.get("camera"))
        return

    if visit_id is None:
        return

    # Resolved against the representative event's own single object label (not review["objects"],
    # which can be a comma-joined multi-type list) -- same single-type-per-visit convention
    # claim_alert_ai_batch already uses. Fetched once here and reused for both the thumb-crop
    # defer decision below and the Telegram mode resolution, rather than two separate lookups.
    representative = db.get_representative_event_for_visit(visit_id)
    object_label = representative.get("objects") if representative else None

    # If a thumb-crop re-crop attempt is going to happen for this visit, defer the summary send
    # entirely to visit_thumb_worker -- it fires once the re-crop resolves (done -> the well-timed
    # high-res image, or failed -> falls back to the representative event's own crop), rather than
    # immediately here with whatever the representative event's crop looks like right now. Only
    # skips the immediate send when a deferred one is actually guaranteed to happen later.
    if not db.visit_thumb_crop_will_be_attempted(review, _profile, object_label):
        try:
            mode = profile_config.telegram_alerts_mode(_profile, object_label)
            if mode in ("image", "all"):
                image_base64 = representative.get("crop_image_base64") if representative else None
                message_id = telegram.send_visit_summary(
                    review["camera"], review["objects"], len(review["det_ids"]) or 1,
                    image_base64=image_base64, mode=mode,
                )
                if message_id is not None:
                    # Durable reply-threading target, same idea as raw_events.telegram_photo_message_id
                    # -- lets alert_video_worker's later video send reply onto this message once the
                    # visit's clip (STORE_VIDEO_ALERTS) finishes downloading.
                    db.set_visit_telegram_photo_message_id(visit_id, message_id)
        except Exception:
            # Never let a Telegram hiccup take down the MQTT message handler -- same belt-and-
            # suspenders wrapping as video_worker's send_video call.
            logger.warning("Telegram visit summary send raised unexpectedly for visit id=%s", visit_id, exc_info=True)


def start(profile: dict | None = None) -> mqtt.Client:
    global _profile
    _profile = profile
    client = mqtt.Client()
    if config.MQTT_USERNAME:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.connect(config.MQTT_HOST, config.MQTT_PORT)
    client.loop_start()
    return client
