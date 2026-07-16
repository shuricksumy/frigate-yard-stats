import logging
import time

import config
import db
import video

logger = logging.getLogger(__name__)


def process_claimed_visit(visit: dict) -> None:
    visit_id = visit["id"]
    # Same head-start reasoning as video_worker.process_claimed_event -- Frigate may still be
    # finalizing the recording segment right after the review closes.
    if visit.get("video_attempt_count", 0) == 0:
        time.sleep(config.VIDEO_INITIAL_WAIT_SECONDS)

    # video.download_clip/build_clip_url only read start_ts/end_ts/camera/det_id off the row --
    # visits store the camera under "cameras" (singular value, per-camera-only grouping), so a
    # small adapter dict lets both flows share the exact same download/validation logic.
    clip_row = {
        "start_ts": visit["start_ts"], "end_ts": visit["end_ts"],
        "camera": visit["cameras"], "det_id": f"visit-{visit_id}",
    }
    try:
        content = video.download_clip(clip_row)
        path = video.store_visit_clip(visit, content)
        db.mark_visit_video_done(visit_id, path)
        logger.info("Stored visit video for visit id=%s camera=%s path=%s", visit_id, visit.get("cameras"), path)
    except Exception:
        logger.warning(
            "Visit video download not ready / failed for visit id=%s (attempt %s/%s)",
            visit_id, visit.get("video_attempt_count", 0) + 1, config.VIDEO_MAX_ATTEMPTS,
        )
        db.mark_visit_video_retry_or_failed(visit_id, config.VIDEO_MAX_ATTEMPTS)
        if visit.get("video_attempt_count", 0) + 1 < config.VIDEO_MAX_ATTEMPTS:
            time.sleep(config.VIDEO_RETRY_WAIT_SECONDS)


def run_once() -> None:
    db.reap_stale_visit_video_processing()
    in_progress = db.count_visit_video_in_progress()
    available_capacity = max(0, config.VIDEO_PARALLEL_LIMIT - in_progress)
    if available_capacity <= 0:
        return

    for visit in db.claim_visit_video_batch(available_capacity, config.VIDEO_MAX_AGE_HOURS):
        process_claimed_visit(visit)


def run_forever() -> None:
    logger.info(
        "alert_video_worker starting: parallel_limit=%s initial_wait=%ss min_valid_bytes=%s "
        "max_attempts=%s retry_wait=%ss max_age_hours=%s poll_interval=%ss",
        config.VIDEO_PARALLEL_LIMIT, config.VIDEO_INITIAL_WAIT_SECONDS, config.VIDEO_MIN_VALID_BYTES,
        config.VIDEO_MAX_ATTEMPTS, config.VIDEO_RETRY_WAIT_SECONDS, config.VIDEO_MAX_AGE_HOURS,
        config.POLL_INTERVAL_SECONDS,
    )
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("alert_video_worker poll iteration failed")
        time.sleep(config.POLL_INTERVAL_SECONDS)
