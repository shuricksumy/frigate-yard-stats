import logging
import time

import config
import crop
import db

logger = logging.getLogger(__name__)


def process_claimed_visit(visit: dict) -> None:
    visit_id = visit["id"]
    # Same head-start reasoning as video_worker.process_claimed_event / alert_video_worker's own
    # wait -- Frigate may still be finalizing the continuous-recording segment right after the
    # review closes.
    if visit.get("thumb_crop_attempt_count", 0) == 0:
        time.sleep(config.VISIT_THUMB_CROP_INITIAL_WAIT_SECONDS)

    try:
        representative = db.get_representative_event_for_visit(visit_id)
        if representative is None or not representative.get("det_id"):
            raise ValueError(f"No representative raw_event with det_id for visit id={visit_id}")

        crop_image_base64 = crop.crop_visit_thumbnail(visit, representative)
        db.mark_visit_thumb_crop_done(visit_id, crop_image_base64)
        logger.info(
            "Cropped visit thumbnail for visit id=%s camera=%s thumb_time=%s",
            visit_id, visit.get("cameras"), visit.get("thumb_time"),
        )
    except Exception:
        logger.warning(
            "Visit thumbnail crop failed for visit id=%s (attempt %s/%s)",
            visit_id, visit.get("thumb_crop_attempt_count", 0) + 1, config.VISIT_THUMB_CROP_MAX_ATTEMPTS,
            exc_info=True,
        )
        db.mark_visit_thumb_crop_retry_or_failed(visit_id, config.VISIT_THUMB_CROP_MAX_ATTEMPTS)
        if visit.get("thumb_crop_attempt_count", 0) + 1 < config.VISIT_THUMB_CROP_MAX_ATTEMPTS:
            time.sleep(config.VISIT_THUMB_CROP_RETRY_WAIT_SECONDS)


def run_once() -> None:
    db.reap_stale_visit_thumb_crop_processing()
    in_progress = db.count_visit_thumb_crop_in_progress()
    available_capacity = max(0, config.VISIT_THUMB_CROP_PARALLEL_LIMIT - in_progress)
    if available_capacity <= 0:
        return

    for visit in db.claim_visit_thumb_crop_batch(available_capacity):
        process_claimed_visit(visit)


def run_forever() -> None:
    logger.info(
        "visit_thumb_worker starting: parallel_limit=%s initial_wait=%ss max_attempts=%s "
        "retry_wait=%ss poll_interval=%ss",
        config.VISIT_THUMB_CROP_PARALLEL_LIMIT, config.VISIT_THUMB_CROP_INITIAL_WAIT_SECONDS,
        config.VISIT_THUMB_CROP_MAX_ATTEMPTS, config.VISIT_THUMB_CROP_RETRY_WAIT_SECONDS,
        config.POLL_INTERVAL_SECONDS,
    )
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("visit_thumb_worker poll iteration failed")
        time.sleep(config.POLL_INTERVAL_SECONDS)
