import logging
import time

import config
import crop
import db
import event_images
import poll_loop
import profile_config
import retention
import telegram

logger = logging.getLogger(__name__)


def process_claimed_event(row: dict, profile: dict | None = None) -> None:
    event_id = row["id"]
    # Frigate is still finalizing the event/clip right after the "end" event fires -- give it a
    # head start before the first attempt (mirrors video_worker's VIDEO_INITIAL_WAIT_SECONDS).
    # Only wait on a genuinely fresh claim, not every retry pass through this row --
    # crop_attempt_count == 0 means this is the first attempt.
    if row.get("crop_attempt_count", 0) == 0:
        time.sleep(config.CROP_INITIAL_WAIT_SECONDS)
    try:
        object_label = row.get("objects")
        result = crop.crop_event(
            row,
            ai_image_max_dimension=profile_config.ai_image_max_dimension(profile, object_label),
        )
        db.mark_crop_done(event_id, result["crop_image_base64"], result["sub_label"], result["score"])
        logger.info("Cropped raw_event id=%s det_id=%s", event_id, row.get("det_id"))

        if profile_config.store_event_images(profile, object_label):
            # Best-effort, non-fatal -- a disk-write failure (full disk, permissions) shouldn't
            # take down the crop stage. Deterministic filename (object type + event id, not a
            # timestamp) means a later retry of this same event overwrites the file rather than
            # accumulating duplicates.
            try:
                image_path = event_images.store_event_image(row, result["full_res_image_base64"])
                db.set_event_image_path(event_id, image_path)
            except Exception:
                logger.warning(
                    "Failed to persist event image to disk for raw_event id=%s", event_id, exc_info=True,
                )

        # Photo-first Telegram notification -- runs regardless of STORE_VIDEO_EVENTS (photo-only is a
        # valid steady state; video_worker sends a reply video later if video storage is on).
        # Sends full_res_image_base64 (Frigate's own unmodified snapshot), not the downscaled
        # crop_image_base64 stored in Postgres -- both are already in memory from the same crop
        # call above regardless of STORE_EVENT_IMAGES, so there's no extra cost to using the
        # better one here. Never allowed to fail the crop stage -- telegram.py itself doesn't
        # raise, but wrap anyway (belt and suspenders, same spirit as the n8n workflow's onError
        # branches).
        try:
            mode = profile_config.telegram_events_mode(profile, object_label)
            message_id = telegram.send_photo(result["full_res_image_base64"], telegram.build_caption(row), mode=mode)
            if message_id is not None:
                db.set_telegram_photo_message_id(event_id, message_id)
        except Exception:
            logger.warning("Telegram photo send raised unexpectedly for raw_event id=%s", event_id, exc_info=True)

    except Exception:
        logger.exception("Crop failed for raw_event id=%s det_id=%s", event_id, row.get("det_id"))
        db.mark_crop_failed(event_id)


def run_once(profile: dict | None = None) -> None:
    retention.maybe_run_retention()

    db.reap_stale_processing()
    in_progress = db.count_in_progress()
    available_capacity = max(0, config.PARALLEL_LIMIT - in_progress)
    if available_capacity <= 0:
        return

    for row in db.claim_next_batch(available_capacity):
        process_claimed_event(row, profile)


def run_forever(profile: dict | None = None) -> None:
    poll_loop.run_forever(
        "crop_worker",
        lambda: run_once(profile),
        config.POLL_INTERVAL_SECONDS,
        {
            "parallel_limit": config.PARALLEL_LIMIT,
            "stale_minutes": config.STALE_MINUTES,
            "max_attempts": config.MAX_ATTEMPTS,
            "initial_wait": f"{config.CROP_INITIAL_WAIT_SECONDS}s",
            "poll_interval": f"{config.POLL_INTERVAL_SECONDS}s",
            "retention_months": config.RETENTION_MONTHS,
            "retention_check_interval": f"{config.RETENTION_CHECK_INTERVAL_SECONDS}s",
        },
    )
