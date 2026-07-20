import logging
import time

import config
import db
import profile_config
import telegram
import video

logger = logging.getLogger(__name__)


def process_claimed_event(row: dict, profile: dict | None = None) -> None:
    event_id = row["id"]
    # Frigate is still finalizing the recording segment right after the "end" event fires --
    # give it a head start before the first attempt (mirrors the n8n workflow's "Wait 10s" ahead
    # of "Download Clip"). Only wait on a genuinely fresh claim, not every retry pass through
    # this row -- video_attempt_count == 0 means this is the first attempt.
    if row.get("video_attempt_count", 0) == 0:
        time.sleep(config.VIDEO_INITIAL_WAIT_SECONDS)

    try:
        content = video.download_clip(row)
        path = video.store_clip(row, content)
        db.mark_video_done(event_id, path)
        logger.info("Stored video for raw_event id=%s det_id=%s path=%s", event_id, row.get("det_id"), path)

        try:
            reply_to = row.get("telegram_photo_message_id")
            mode = profile_config.telegram_events_mode(profile, row.get("objects"))
            telegram.send_video(path, telegram.build_caption(row), reply_to_message_id=reply_to, mode=mode)
        except Exception:
            # telegram.py itself shouldn't raise, but never let a Telegram hiccup take down the
            # video poll loop -- belt and suspenders.
            logger.warning("Telegram video send raised unexpectedly for raw_event id=%s", event_id, exc_info=True)

    except Exception:
        logger.warning(
            "Video download not ready / failed for raw_event id=%s det_id=%s (attempt %s/%s)",
            event_id, row.get("det_id"), row.get("video_attempt_count", 0) + 1, config.VIDEO_MAX_ATTEMPTS,
        )
        db.mark_video_retry_or_failed(event_id, config.VIDEO_MAX_ATTEMPTS)
        if row.get("video_attempt_count", 0) + 1 < config.VIDEO_MAX_ATTEMPTS:
            time.sleep(config.VIDEO_RETRY_WAIT_SECONDS)


def run_once(profile: dict | None = None) -> None:
    db.reap_stale_video_processing()
    in_progress = db.count_video_in_progress()
    available_capacity = max(0, config.VIDEO_PARALLEL_LIMIT - in_progress)
    if available_capacity <= 0:
        return

    object_types, exclude_object_types = profile_config.store_video_claim_filter(profile)
    if object_types == []:
        # Base disabled, nothing opted in per-type -- nothing for this stage to do at all.
        return
    for row in db.claim_video_batch(
        available_capacity, config.VIDEO_MAX_AGE_HOURS,
        object_types=object_types, exclude_object_types=exclude_object_types,
    ):
        process_claimed_event(row, profile)


def run_forever(profile: dict | None = None) -> None:
    logger.info(
        "video_worker starting: parallel_limit=%s initial_wait=%ss min_valid_bytes=%s "
        "max_attempts=%s retry_wait=%ss max_age_hours=%s poll_interval=%ss",
        config.VIDEO_PARALLEL_LIMIT, config.VIDEO_INITIAL_WAIT_SECONDS, config.VIDEO_MIN_VALID_BYTES,
        config.VIDEO_MAX_ATTEMPTS, config.VIDEO_RETRY_WAIT_SECONDS, config.VIDEO_MAX_AGE_HOURS,
        config.POLL_INTERVAL_SECONDS,
    )
    while True:
        try:
            run_once(profile)
        except Exception:
            logger.exception("video_worker poll iteration failed")
        time.sleep(config.POLL_INTERVAL_SECONDS)
