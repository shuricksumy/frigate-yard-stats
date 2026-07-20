import json
import logging
import time

import ai_worker
import config
import db
import report

logger = logging.getLogger(__name__)


def parse_alert_vehicle_response(response: dict, row: dict) -> dict:
    # Same shape as ai_worker.parse_vehicle_response, but alert_prompt asks for one extra field
    # ("notes" -- a short description of what changed across the grid's 4 frames) that
    # event_prompt never does, and there's no plate_text_frigate equivalent (Frigate's own LPR
    # read is per-event, this is per-visit).
    text = response["choices"][0]["message"]["content"]
    match = ai_worker._JSON_BLOB_RE.search(text)
    parsed = json.loads(match.group(0)) if match else {}
    return {
        "visit_id": row["id"],
        "color": parsed.get("color"),
        "body_type": parsed.get("body_type"),
        "make_guess": parsed.get("make"),
        "make_confidence": parsed.get("make_confidence"),
        "model_guess": parsed.get("model"),
        "model_confidence": parsed.get("model_confidence"),
        "notable_features": parsed.get("notable_features"),
        "plate_text_llm": ai_worker._sanitize_plate(parsed.get("plate_text")),
        "plate_confidence": None,
        "notes": parsed.get("notes"),
    }


def parse_alert_person_response(response: dict, row: dict) -> dict:
    return {
        "visit_id": row["id"],
        "description": response["choices"][0]["message"]["content"],
        "notes": None,
    }


def process_claimed_visit(row: dict, profile: dict) -> None:
    visit_id = row["id"]
    mapping = profile.get("object_types", {}).get(row.get("objects"))
    if mapping is None:
        # Shouldn't happen -- run_once only ever asks claim_alert_ai_batch for mapped types -- but
        # guard rather than crash the poll loop on an unexpected row.
        logger.warning("Claimed visit id=%s has unmapped representative object type %r, skipping", visit_id, row.get("objects"))
        return
    sighting_type = mapping["sighting_type"]
    type_config = profile[sighting_type]
    timeout = type_config.get("timeout_seconds", config.AI_STAGE_DEFAULT_TIMEOUT_SECONDS)

    try:
        response = ai_worker._chat_request(
            type_config["chat_path"], type_config["alert_prompt"], row["crop_image_base64"], timeout,
        )
        if sighting_type == "vehicle":
            fields = parse_alert_vehicle_response(response, row)
            embedding = ai_worker._embed_text(report._vehicle_summary(fields))
            db.complete_visit_vehicle_sighting(
                fields["visit_id"], fields["color"], fields["body_type"], fields["make_guess"],
                fields["make_confidence"], fields["model_guess"], fields["model_confidence"],
                fields["notable_features"], fields["plate_text_llm"], fields["plate_confidence"],
                fields["notes"], embedding,
            )
        else:
            fields = parse_alert_person_response(response, row)
            embedding = ai_worker._embed_text(report._person_summary(fields))
            db.complete_visit_person_sighting(fields["visit_id"], fields["description"], fields["notes"], embedding)
        logger.info("Alert AI analysis done for visit id=%s sighting_type=%s", visit_id, sighting_type)

    except Exception:
        logger.exception("Alert AI analysis failed for visit id=%s det_id=%s", visit_id, row.get("det_id"))
        db.fail_alert_ai_event(visit_id, config.AI_STAGE_MAX_ATTEMPTS)


def run_once(profile: dict) -> None:
    object_types = list(profile.get("object_types", {}).keys())
    visits = db.claim_alert_ai_batch(
        object_types, config.AI_STAGE_PARALLEL_LIMIT, config.AI_STAGE_STALE_MINUTES,
        max_age_hours=config.AI_STAGE_MAX_AGE_HOURS,
    )
    for row in visits:
        process_claimed_visit(row, profile)


def run_forever() -> None:
    profile = ai_worker.load_profile(config.AI_STAGE_PROFILE_PATH)
    logger.info(
        "alert_ai_worker starting: object_types=%s parallel_limit=%s stale_minutes=%s "
        "max_attempts=%s poll_interval=%ss llama_proxy_base_url=%s",
        list(profile.get("object_types", {}).keys()), config.AI_STAGE_PARALLEL_LIMIT,
        config.AI_STAGE_STALE_MINUTES, config.AI_STAGE_MAX_ATTEMPTS,
        config.AI_STAGE_POLL_INTERVAL_SECONDS, config.LLAMA_PROXY_BASE_URL,
    )
    while True:
        try:
            run_once(profile)
        except Exception:
            logger.exception("alert_ai_worker poll iteration failed")
        time.sleep(config.AI_STAGE_POLL_INTERVAL_SECONDS)
