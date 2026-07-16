import logging
import time

import config
import crop
import db
import retention
import telegram

logger = logging.getLogger(__name__)


def process_claimed_event(row: dict) -> None:
    event_id = row["id"]
    # Frigate is still finalizing the event/clip right after the "end" event fires -- give it a
    # head start before the first attempt (mirrors video_worker's VIDEO_INITIAL_WAIT_SECONDS).
    # Only wait on a genuinely fresh claim, not every retry pass through this row --
    # crop_attempt_count == 0 means this is the first attempt.
    if row.get("crop_attempt_count", 0) == 0:
        time.sleep(config.CROP_INITIAL_WAIT_SECONDS)
    try:
        result = crop.crop_event(row)
        db.mark_crop_done(event_id, result["crop_image_base64"], result["sub_label"], result["score"])
        logger.info("Cropped raw_event id=%s det_id=%s", event_id, row.get("det_id"))

        # Photo-first Telegram notification -- runs regardless of STORE_VIDEO (photo-only is a
        # valid steady state; video_worker sends a reply video later if video storage is on).
        # Never allowed to fail the crop stage -- telegram.py itself doesn't raise, but wrap
        # anyway (belt and suspenders, same spirit as the n8n workflow's onError branches).
        try:
            message_id = telegram.send_photo(result["crop_image_base64"], telegram.build_caption(row))
            if message_id is not None:
                db.set_telegram_photo_message_id(event_id, message_id)
        except Exception:
            logger.warning("Telegram photo send raised unexpectedly for raw_event id=%s", event_id, exc_info=True)

    except Exception:
        logger.exception("Crop failed for raw_event id=%s det_id=%s", event_id, row.get("det_id"))
        db.mark_crop_failed(event_id)


def run_once() -> None:
    retention.maybe_run_retention()

    db.reap_stale_processing()
    in_progress = db.count_in_progress()
    available_capacity = max(0, config.PARALLEL_LIMIT - in_progress)
    if available_capacity <= 0:
        return

    for row in db.claim_next_batch(available_capacity):
        process_claimed_event(row)


def run_forever() -> None:
    logger.info(
        "crop_worker starting: parallel_limit=%s stale_minutes=%s max_attempts=%s initial_wait=%ss "
        "poll_interval=%ss retention_months=%s retention_check_interval=%ss",
        config.PARALLEL_LIMIT, config.STALE_MINUTES, config.MAX_ATTEMPTS, config.CROP_INITIAL_WAIT_SECONDS,
        config.POLL_INTERVAL_SECONDS, config.RETENTION_MONTHS, config.RETENTION_CHECK_INTERVAL_SECONDS,
    )
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("crop_worker poll iteration failed")
        time.sleep(config.POLL_INTERVAL_SECONDS)
