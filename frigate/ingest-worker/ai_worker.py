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


def _llama_proxy_chat_request(type_config: dict, prompt: str, crop_image_base64: str, timeout: float) -> dict:
    # The original, still-default shape: llama_slot_proxy speaks an OpenAI-compatible
    # chat-completions API with no "model" field at all -- the slot is selected entirely by
    # chat_path (one URL path segment per model), not a body field.
    headers = {}
    if config.LLAMA_PROXY_TOKEN:
        headers["Authorization"] = f"Bearer {config.LLAMA_PROXY_TOKEN}"
    resp = requests.post(
        f"{config.LLAMA_PROXY_BASE_URL}{type_config['chat_path']}",
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


def _openai_chat_request(type_config: dict, prompt: str, crop_image_base64: str, timeout: float) -> dict:
    # Same request/response shape llama_slot_proxy already speaks (it's deliberately
    # OpenAI-compatible) -- the two real differences are the base URL/auth and that OpenAI needs a
    # "model" field in the body instead of selecting the model via the URL path.
    headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
    resp = requests.post(
        f"{config.OPENAI_BASE_URL}/v1/chat/completions",
        json={
            "model": type_config["model"],
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


def _anthropic_chat_request(type_config: dict, prompt: str, crop_image_base64: str, timeout: float) -> dict:
    # Claude's Messages API -- a genuinely different shape from the other two providers: auth is
    # x-api-key + anthropic-version headers (not Authorization: Bearer), images are a "source"
    # block instead of a data-URI image_url, and max_tokens is required (there's no server-side
    # default the way OpenAI/llama_slot_proxy have one).
    headers = {
        "x-api-key": config.ANTHROPIC_API_KEY,
        "anthropic-version": config.ANTHROPIC_VERSION,
    }
    resp = requests.post(
        f"{config.ANTHROPIC_BASE_URL}/v1/messages",
        json={
            "model": type_config["model"],
            "max_tokens": type_config.get("max_tokens", config.AI_STAGE_DEFAULT_MAX_TOKENS),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": crop_image_base64,
                            },
                        },
                    ],
                }
            ],
        },
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _chat_request(type_config: dict, prompt: str, crop_image_base64: str, timeout: float) -> dict:
    # Dispatches on this type's own `provider` (profiles.yaml, per object type -- see
    # profiles.yaml.example) -- "llama_proxy" (the default, unchanged behavior) if the key is
    # omitted entirely, so an existing deployment's profiles.yaml needs no edit to keep working.
    provider = type_config.get("provider", "llama_proxy")
    if provider == "openai":
        return _openai_chat_request(type_config, prompt, crop_image_base64, timeout)
    if provider == "anthropic":
        return _anthropic_chat_request(type_config, prompt, crop_image_base64, timeout)
    return _llama_proxy_chat_request(type_config, prompt, crop_image_base64, timeout)


def _extract_response_text(response: dict, type_config: dict | None) -> str:
    # Claude's response shape (content[0].text) differs from the OpenAI-compatible shape
    # llama_slot_proxy and OpenAI itself both use (choices[0].message.content) -- type_config is
    # optional so existing callers/tests that only ever dealt with the OpenAI-compatible shape
    # keep working unchanged.
    if (type_config or {}).get("provider") == "anthropic":
        return response["content"][0]["text"]
    return response["choices"][0]["message"]["content"]


def parse_sighting_response(response: dict, row: dict, type_config: dict | None = None) -> dict:
    # No JSON parsing, no per-type branching -- the whole chat response is the sighting's
    # description verbatim. Whatever profiles.yaml's event_prompt asked the model to mention
    # (color, plate, breed, clothing, whatever) is already in that text; there's nothing left to
    # extract into separate columns in this universal model.
    return {
        "raw_event_id": row["id"],
        "object_label": row.get("objects"),
        "description": _extract_response_text(response, type_config),
    }


def _embed_request(text: str, timeout: float) -> dict:
    # config.EMBEDDING_PROVIDER is independent of whichever provider(s) profiles.yaml routes chat
    # calls to -- Claude has no embeddings endpoint at all, so a deployment using
    # `provider: anthropic` for chat still needs this set to "llama_proxy" (default) or "openai"
    # for semantic search/backfill to work.
    if config.EMBEDDING_PROVIDER == "openai":
        headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
        resp = requests.post(
            f"{config.OPENAI_BASE_URL}/v1/embeddings",
            json={"model": config.OPENAI_EMBED_MODEL, "input": text},
            headers=headers,
            timeout=timeout,
        )
    else:
        headers = {}
        if config.LLAMA_PROXY_TOKEN:
            headers["Authorization"] = f"Bearer {config.LLAMA_PROXY_TOKEN}"
        resp = requests.post(
            f"{config.LLAMA_PROXY_BASE_URL}{config.LLAMA_PROXY_EMBED_PATH}",
            json={"input": text},
            headers=headers,
            timeout=timeout,
        )
    resp.raise_for_status()
    return resp.json()


def _embed_text(text: str | None) -> list[float] | None:
    # An embedding failure shouldn't lose an already-computed sighting -- same decision made for
    # n8n's Call Embedding Model nodes (continueErrorOutput, falls back to null). Never raises;
    # the sighting still gets inserted, just not semantically searchable.
    if not text:
        return None
    try:
        embedding = _embed_request(text, config.AI_STAGE_EMBED_TIMEOUT_SECONDS)["data"][0]["embedding"]
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


def embed_query_text(text: str) -> list[float]:
    """Embeds arbitrary free-text (the web UI Search tab's own query, not a stored sighting) via
    the same embedding backend _embed_text uses. Raises on any failure -- unlike _embed_text's
    "fine, store the sighting without one" fallback, a search request has nothing useful to do
    with a missing vector, so the caller (api.py's POST /search) turns this into a real error
    response instead of silently returning empty results."""
    if not text or not text.strip():
        raise ValueError("query text must not be empty")
    embedding = _embed_request(text, config.AI_STAGE_EMBED_TIMEOUT_SECONDS)["data"][0]["embedding"]
    if len(embedding) != config.EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"embedding backend returned {len(embedding)} dims, expected {config.EMBEDDING_DIMENSIONS} "
            "(wrong model loaded at LLAMA_PROXY_EMBED_PATH?)"
        )
    return embedding


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
        response = _chat_request(type_config, type_config["event_prompt"], row["crop_image_base64"], timeout)
        fields = parse_sighting_response(response, row, type_config)
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
