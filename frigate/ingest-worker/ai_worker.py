import json
import logging
import re
import time

import requests
import yaml

import config
import db
import report

logger = logging.getLogger(__name__)

_JSON_BLOB_RE = re.compile(r"\{.*\}", re.DOTALL)
_PLATE_TOKEN_RE = re.compile(r"[A-Z0-9-]{4,14}")


def load_profile(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _sanitize_plate(text: str | None) -> str | None:
    # Ported verbatim from n8n/metadata-processor.json's "Parse VLM Responses (Vehicle)" node --
    # defensive against the model ignoring the "ONLY the plate's characters" instruction and
    # writing a narrative explanation into plate_text instead (seen in real data). A clean short
    # token passes through as-is; anything with whitespace/newlines or over 15 chars gets the most
    # plate-like token pulled out of it instead.
    if not text:
        return None
    trimmed = str(text).strip()
    if not trimmed:
        return None
    if not re.search(r"\s", trimmed) and len(trimmed) <= 15:
        return trimmed
    tokens = _PLATE_TOKEN_RE.findall(trimmed)
    return tokens[-1] if tokens else trimmed


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


def parse_vehicle_response(response: dict, row: dict) -> dict:
    text = response["choices"][0]["message"]["content"]
    match = _JSON_BLOB_RE.search(text)
    parsed = json.loads(match.group(0)) if match else {}
    return {
        "raw_event_id": row["id"],
        "color": parsed.get("color"),
        "body_type": parsed.get("body_type"),
        "make_guess": parsed.get("make"),
        "make_confidence": parsed.get("make_confidence"),
        "model_guess": parsed.get("model"),
        "model_confidence": parsed.get("model_confidence"),
        "notable_features": parsed.get("notable_features"),
        "plate_text_llm": _sanitize_plate(parsed.get("plate_text")),
        "plate_text_frigate": row.get("sub_label"),
        "plate_confidence": None,
        "notes": None,
    }


def parse_person_response(response: dict, row: dict) -> dict:
    return {
        "raw_event_id": row["id"],
        "description": response["choices"][0]["message"]["content"],
        "notes": None,
    }


def _embed_text(text: str | None) -> list[float] | None:
    # An embedding failure shouldn't lose an already-computed sighting -- same decision made for
    # n8n's Call Embedding Model (Vehicle)/(Person) nodes (continueErrorOutput, falls back to
    # null). Never raises; the sighting still gets inserted, just not semantically searchable.
    if not text:
        return None
    try:
        resp = requests.post(
            f"{config.LLAMA_PROXY_BASE_URL}{config.LLAMA_PROXY_EMBED_PATH}",
            json={"input": text},
            timeout=config.AI_STAGE_EMBED_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception:
        logger.warning("Embedding call failed, storing sighting without one", exc_info=True)
        return None


def process_claimed_event(row: dict, profile: dict) -> None:
    event_id = row["id"]
    mapping = profile.get("object_types", {}).get(row.get("objects"))
    if mapping is None:
        # Shouldn't happen -- run_once only ever asks claim_ai_batch for mapped types -- but guard
        # rather than crash the poll loop on an unexpected row.
        logger.warning("Claimed raw_event id=%s has unmapped object type %r, skipping", event_id, row.get("objects"))
        return
    sighting_type = mapping["sighting_type"]
    type_config = profile[sighting_type]
    timeout = type_config.get("timeout_seconds", config.AI_STAGE_DEFAULT_TIMEOUT_SECONDS)

    try:
        response = _chat_request(type_config["chat_path"], type_config["prompt"], row["crop_image_base64"], timeout)
        if sighting_type == "vehicle":
            fields = parse_vehicle_response(response, row)
            # Reuses report._vehicle_summary rather than a third copy of the same combination
            # logic (n8n's Build Embedding Text (Vehicle) node is the second) -- same one-line
            # description a human reads in the alerts report is what gets embedded here too.
            embedding = _embed_text(report._vehicle_summary(fields))
            db.complete_vehicle_sighting(
                fields["raw_event_id"], fields["color"], fields["body_type"], fields["make_guess"],
                fields["make_confidence"], fields["model_guess"], fields["model_confidence"],
                fields["notable_features"], fields["plate_text_llm"], fields["plate_text_frigate"],
                fields["plate_confidence"], fields["notes"], embedding,
            )
        else:
            fields = parse_person_response(response, row)
            embedding = _embed_text(report._person_summary(fields))
            db.complete_person_sighting(fields["raw_event_id"], fields["description"], fields["notes"], embedding)
        logger.info("AI analysis done for raw_event id=%s sighting_type=%s", event_id, sighting_type)

    except Exception:
        logger.exception("AI analysis failed for raw_event id=%s det_id=%s", event_id, row.get("det_id"))
        db.fail_ai_event(event_id, config.AI_STAGE_MAX_ATTEMPTS)


def run_once(profile: dict) -> None:
    # object_types keys are exactly the mapped types (see profiles.yaml's own comment) -- a type
    # with no entry is never included here, so claim_ai_batch is simply never asked for it, and
    # ai_status stays 'new' for those rows indefinitely rather than erroring.
    object_types = list(profile.get("object_types", {}).keys())
    events = db.claim_ai_batch(
        object_types, config.AI_STAGE_PARALLEL_LIMIT, config.AI_STAGE_STALE_MINUTES,
        max_age_hours=config.AI_STAGE_MAX_AGE_HOURS,
    )
    for row in events:
        process_claimed_event(row, profile)


def run_forever() -> None:
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
