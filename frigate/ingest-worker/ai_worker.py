import logging

import yaml

import config
import db
import llm
import poll_loop
import profile_config

logger = logging.getLogger(__name__)


def load_profile(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)



def parse_sighting_response(response: dict, row: dict, type_config: dict | None = None) -> dict:
    # No JSON parsing, no per-type branching -- the whole chat response is the sighting's
    # description verbatim. Whatever profiles.yaml's event_prompt asked the model to mention
    # (color, plate, breed, clothing, whatever) is already in that text; there's nothing left to
    # extract into separate columns in this universal model.
    return {
        "raw_event_id": row["id"],
        "object_label": row.get("objects"),
        "description": llm.extract_response_text(response, type_config),
    }


def run_embedding_backfill(limit: int) -> dict:
    # POST /embeddings/backfill's confirm=true path -- fills in the embedding column for
    # sightings that existed before semantic search did (or came from a run that didn't attach
    # one). Deliberately independent of AI_EVENTS_STAGE_ENABLED/process_claimed_event -- this only
    # ever re-embeds each sighting's own already-stored description, never re-runs the VLM. Covers
    # both event-level and visit-level sightings now -- one universal shape, one backfill loop
    # each, no more vehicle/person split to run twice.
    if config.EMBEDDING_PROVIDER == "openai":
        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not configured")
    elif not config.LLAMA_PROXY_BASE_URL:
        raise RuntimeError("LLAMA_PROXY_BASE_URL is not configured")

    result = {
        "sightings_processed": 0, "sightings_updated": 0,
        "visit_sightings_processed": 0, "visit_sightings_updated": 0,
        "visit_summaries_processed": 0, "visit_summaries_updated": 0,
    }

    for row in db.get_sightings_missing_embedding(limit):
        result["sightings_processed"] += 1
        embedding = llm.embed_text(row["description"])
        if embedding is not None:
            db.update_sighting_embedding(row["id"], embedding)
            result["sightings_updated"] += 1

    # visit_sightings backed the now-removed alert AI stage -- always empty going forward, but this
    # loop is kept as harmless dead code (nothing to process, same "deprecated, not removed" stance
    # schema.sql itself takes for that table).
    for row in db.get_visit_sightings_missing_embedding(limit):
        result["visit_sightings_processed"] += 1
        embedding = llm.embed_text(row["description"])
        if embedding is not None:
            db.update_visit_sighting_embedding(row["id"], embedding)
            result["visit_sightings_updated"] += 1

    for row in db.get_visit_summaries_missing_embedding(limit):
        result["visit_summaries_processed"] += 1
        embedding = llm.embed_text(row["summary"])
        if embedding is not None:
            db.update_visit_summary_embedding(row["id"], embedding)
            result["visit_summaries_updated"] += 1

    return result


def process_claimed_event(row: dict, profile: dict) -> None:
    event_id = row["id"]
    # profile_config.object_types is the one safe accessor for this section -- see its docstring
    # (a bare `object_types:` line in profiles.yaml parses to None, not {}).
    type_config = profile_config.object_types_config(profile).get(row.get("objects"))
    if type_config is None:
        # Shouldn't happen -- run_once only ever asks claim_ai_batch for mapped types -- but guard
        # rather than crash the poll loop on an unexpected row.
        logger.warning("Claimed raw_event id=%s has unmapped object type %r, skipping", event_id, row.get("objects"))
        return
    timeout = type_config.get("timeout_seconds", config.AI_STAGE_DEFAULT_TIMEOUT_SECONDS)

    try:
        response = llm.chat_request(type_config, type_config["event_prompt"], [row["crop_image_base64"]], timeout)
        fields = parse_sighting_response(response, row, type_config)
        embedding = llm.embed_text(fields["description"])
        db.complete_sighting(fields["raw_event_id"], fields["object_label"], fields["description"], embedding)
        logger.info("AI analysis done for raw_event id=%s object_label=%s", event_id, fields["object_label"])

    except Exception:
        logger.exception("AI analysis failed for raw_event id=%s det_id=%s", event_id, row.get("det_id"))
        db.fail_ai_event(event_id, config.AI_STAGE_MAX_ATTEMPTS)


def run_once(profile: dict) -> None:
    # object_types keys are exactly the mapped labels (see profiles.yaml's own comment) -- a label
    # with no entry is never included here, so claim_ai_batch is simply never asked for it, and
    # ai_status stays 'new' for those rows indefinitely rather than erroring. Further filtered by
    # each type's own effective ai_events_stage_enabled (profiles.yaml override, falling back to
    # the global AI_EVENTS_STAGE_ENABLED) -- a type can opt out of this stage (or opt in despite
    # the global default being off) without affecting any other type's participation.
    object_types = [
        label for label in profile_config.object_types_config(profile)
        if profile_config.ai_events_stage_enabled(profile, label)
    ]
    events = db.claim_ai_batch(
        object_types, config.AI_STAGE_PARALLEL_LIMIT, config.AI_STAGE_STALE_MINUTES,
        max_age_hours=config.AI_STAGE_MAX_AGE_HOURS,
    )
    for row in events:
        process_claimed_event(row, profile)


def run_forever(profile: dict | None = None) -> None:
    if profile is None:
        profile = load_profile(config.AI_STAGE_PROFILE_PATH)
    poll_loop.run_forever(
        "ai_worker",
        lambda: run_once(profile),
        config.AI_STAGE_POLL_INTERVAL_SECONDS,
        {
            "object_types": list(profile_config.object_types_config(profile).keys()),
            "parallel_limit": config.AI_STAGE_PARALLEL_LIMIT,
            "stale_minutes": config.AI_STAGE_STALE_MINUTES,
            "max_attempts": config.AI_STAGE_MAX_ATTEMPTS,
            "poll_interval": f"{config.AI_STAGE_POLL_INTERVAL_SECONDS}s",
            "llama_proxy_base_url": config.LLAMA_PROXY_BASE_URL,
        },
    )
