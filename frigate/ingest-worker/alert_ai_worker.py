import logging
import time

import ai_worker
import config
import db

logger = logging.getLogger(__name__)


def parse_alert_sighting_response(response: dict, row: dict) -> dict:
    # Same shape as ai_worker.parse_sighting_response, just keyed by visit_id instead of
    # raw_event_id -- no JSON parsing, no per-type branching. alert_prompt already asks the model
    # to cover both static attributes and what changed across the grid's 4 frames in one flowing
    # answer, so the whole chat response is the description verbatim, same as the event-level path.
    return {
        "visit_id": row["id"],
        "object_label": row.get("objects"),
        "description": response["choices"][0]["message"]["content"],
    }


def process_claimed_visit(row: dict, profile: dict) -> None:
    visit_id = row["id"]
    type_config = profile.get("object_types", {}).get(row.get("objects"))
    if type_config is None:
        # Shouldn't happen -- run_once only ever asks claim_alert_ai_batch for mapped types -- but
        # guard rather than crash the poll loop on an unexpected row.
        logger.warning("Claimed visit id=%s has unmapped representative object type %r, skipping", visit_id, row.get("objects"))
        return
    timeout = type_config.get("timeout_seconds", config.AI_STAGE_DEFAULT_TIMEOUT_SECONDS)

    try:
        response = ai_worker._chat_request(
            type_config["chat_path"], type_config["alert_prompt"], row["crop_image_base64"], timeout,
        )
        fields = parse_alert_sighting_response(response, row)
        embedding = ai_worker._embed_text(fields["description"])
        db.complete_visit_sighting(fields["visit_id"], fields["object_label"], fields["description"], embedding)
        logger.info("Alert AI analysis done for visit id=%s object_label=%s", visit_id, fields["object_label"])

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
