import logging

import ai_worker
import config
import db
import llm
import poll_loop

logger = logging.getLogger(__name__)


def build_summary_input(sightings: list[dict]) -> str:
    # One line per already-analyzed event, chronological (get_sightings_for_visit orders by
    # start_ts) -- the LLM synthesizes across these, it doesn't re-describe any single image.
    return "\n".join(
        f"{s['object_label']}: {s['description']}" for s in sightings if s.get("description")
    )


def process_claimed_visit(row: dict, visit_summary_config: dict) -> None:
    visit_id = row["id"]
    text = build_summary_input(db.get_sightings_for_visit(visit_id))
    if not text:
        # Every linked event settled with no real sighting text to summarize (e.g. all
        # skipped/failed) -- nothing to synthesize, terminal rather than retried forever.
        db.mark_visit_summary_skipped(visit_id)
        logger.info("Visit summary skipped for visit id=%s (no sighting text to summarize)", visit_id)
        return

    timeout = visit_summary_config.get("timeout_seconds", config.AI_STAGE_DEFAULT_TIMEOUT_SECONDS)
    try:
        prompt = f"{visit_summary_config['prompt']}\n\n{text}"
        response = llm.chat_request(visit_summary_config, prompt, [], timeout)
        summary = llm.extract_response_text(response, visit_summary_config)
        embedding = llm.embed_text(summary)
        db.complete_visit_summary(visit_id, summary, embedding)
        logger.info("Visit summary done for visit id=%s", visit_id)
    except Exception:
        logger.exception("Visit summary failed for visit id=%s", visit_id)
        db.fail_visit_summary(
            visit_id, visit_summary_config.get("max_attempts", config.AI_STAGE_MAX_ATTEMPTS)
        )


def run_once(profile: dict) -> None:
    visit_summary_config = profile.get("visit_summary") or {}
    if not visit_summary_config.get("enabled"):
        return
    visits = db.claim_visit_summary_batch(
        visit_summary_config.get("parallel_limit", config.AI_STAGE_PARALLEL_LIMIT),
        visit_summary_config.get("stale_minutes", config.AI_STAGE_STALE_MINUTES),
        max_age_hours=visit_summary_config.get("max_age_hours", config.AI_STAGE_MAX_AGE_HOURS),
    )
    for row in visits:
        process_claimed_visit(row, visit_summary_config)


def run_forever(profile: dict | None = None) -> None:
    if profile is None:
        profile = ai_worker.load_profile(config.AI_STAGE_PROFILE_PATH)
    visit_summary_config = profile.get("visit_summary") or {}
    poll_interval = visit_summary_config.get("poll_interval_seconds", config.AI_STAGE_POLL_INTERVAL_SECONDS)
    poll_loop.run_forever(
        "visit_summary_worker",
        lambda: run_once(profile),
        poll_interval,
        {
            "enabled": visit_summary_config.get("enabled", False),
            "parallel_limit": visit_summary_config.get("parallel_limit", config.AI_STAGE_PARALLEL_LIMIT),
            "stale_minutes": visit_summary_config.get("stale_minutes", config.AI_STAGE_STALE_MINUTES),
            "max_attempts": visit_summary_config.get("max_attempts", config.AI_STAGE_MAX_ATTEMPTS),
            "poll_interval": f"{poll_interval}s",
            "provider": visit_summary_config.get("provider", "llama_proxy"),
        },
    )
