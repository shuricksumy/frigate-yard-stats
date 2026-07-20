import logging
import time

import requests
import yaml

import config
import db
import profile_config

logger = logging.getLogger(__name__)


def load_profile(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _chat_request(chat_path: str, prompt: str, crop_image_base64: str, timeout: float) -> dict:
    headers = {}
    if config.LLAMA_PROXY_TOKEN:
        headers["Authorization"] = f"Bearer {config.LLAMA_PROXY_TOKEN}"
    resp = requests.post(
        f"{config.LLAMA_PROXY_BASE_URL}{chat_path}",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{crop_image_base64}"},
                        },
                    ],
                }
            ],
            "temperature": 0,
        },
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def parse_sighting_response(response: dict, row: dict) -> dict:
    # No JSON parsing, no per-type branching -- the whole chat response is the sighting's
    # description verbatim. Whatever profiles.yaml's event_prompt asked the model to mention
    # (color, plate, breed, clothing, whatever) is already in that text; there's nothing left to
    # extract into separate columns in this universal model.
    return {
        "raw_event_id": row["id"],
        "object_label": row.get("objects"),
        "description": response["choices"][0]["message"]["content"],
    }


def _embed_text(text: str | None) -> list[float] | None:
    # An embedding failure shouldn't lose an already-computed sighting -- same decision made for
    # n8n's Call Embedding Model nodes (continueErrorOutput, falls back to null). Never raises;
    # the sighting still gets inserted, just not semantically searchable.
    if not text:
        return None
    try:
        resp = requests.post(
            f"{config.LLAMA_PROXY_BASE_URL}{config.LLAMA_PROXY_EMBED_PATH}",
            json={"input": text},
            timeout=config.AI_STAGE_EMBED_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        embedding = resp.json()["data"][0]["embedding"]
        if len(embedding) != config.EMBEDDING_DIMENSIONS:
            logger.warning(
                "Embedding call returned %d dims, expected %d (wrong model loaded at "
                "LLAMA_PROXY_EMBED_PATH?), storing sighting without one",
                len(embedding),
                config.EMBEDDING_DIMENSIONS,
            )
            return None
        return embedding
    except Exception:
        logger.warning("Embedding call failed, storing sighting without one", exc_info=True)
        return None


def run_embedding_backfill(limit: int) -> dict:
    # POST /embeddings/backfill's confirm=true path -- fills in the embedding column for
    # sightings that existed before semantic search did (or came from a run that didn't attach
    # one). Deliberately independent of AI_EVENTS_STAGE_ENABLED/process_claimed_event -- this only
    # ever re-embeds each sighting's own already-stored description, never re-runs the VLM. Covers
    # both event-level and visit-level sightings now -- one universal shape, one backfill loop
    # each, no more vehicle/person split to run twice.
    if not config.LLAMA_PROXY_BASE_URL:
        raise RuntimeError("LLAMA_PROXY_BASE_URL is not configured")

    result = {"sightings_processed": 0, "sightings_updated": 0, "visit_sightings_processed": 0, "visit_sightings_updated": 0}

    for row in db.get_sightings_missing_embedding(limit):
        result["sightings_processed"] += 1
        embedding = _embed_text(row["description"])
        if embedding is not None:
            db.update_sighting_embedding(row["id"], embedding)
            result["sightings_updated"] += 1

    for row in db.get_visit_sightings_missing_embedding(limit):
        result["visit_sightings_processed"] += 1
        embedding = _embed_text(row["description"])
        if embedding is not None:
            db.update_visit_sighting_embedding(row["id"], embedding)
            result["visit_sightings_updated"] += 1

    return result


def process_claimed_event(row: dict, profile: dict) -> None:
    event_id = row["id"]
    type_config = profile.get("object_types", {}).get(row.get("objects"))
    if type_config is None:
        # Shouldn't happen -- run_once only ever asks claim_ai_batch for mapped types -- but guard
        # rather than crash the poll loop on an unexpected row.
        logger.warning("Claimed raw_event id=%s has unmapped object type %r, skipping", event_id, row.get("objects"))
        return
    timeout = type_config.get("timeout_seconds", config.AI_STAGE_DEFAULT_TIMEOUT_SECONDS)

    try:
        response = _chat_request(type_config["chat_path"], type_config["event_prompt"], row["crop_image_base64"], timeout)
        fields = parse_sighting_response(response, row)
        embedding = _embed_text(fields["description"])
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
        label for label in profile.get("object_types", {})
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
    logger.info(
        "ai_worker starting: object_types=%s parallel_limit=%s stale_minutes=%s max_attempts=%s "
        "poll_interval=%ss llama_proxy_base_url=%s",
        list(profile.get("object_types", {}).keys()), config.AI_STAGE_PARALLEL_LIMIT,
        config.AI_STAGE_STALE_MINUTES, config.AI_STAGE_MAX_ATTEMPTS,
        config.AI_STAGE_POLL_INTERVAL_SECONDS, config.LLAMA_PROXY_BASE_URL,
    )
    while True:
        try:
            run_once(profile)
        except Exception:
            logger.exception("ai_worker poll iteration failed")
        time.sleep(config.AI_STAGE_POLL_INTERVAL_SECONDS)
