import logging
import time

import config
import crop
import db
import retention

logger = logging.getLogger(__name__)


def process_claimed_event(row: dict) -> None:
    event_id = row["id"]
    try:
        result = crop.crop_event(row)
        db.mark_crop_done(event_id, result["crop_image_base64"], result["sub_label"], result["score"])
        logger.info("Cropped raw_event id=%s det_id=%s", event_id, row.get("det_id"))
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
        "crop_worker starting: parallel_limit=%s stale_minutes=%s max_attempts=%s poll_interval=%ss "
        "retention_months=%s retention_check_interval=%ss",
        config.PARALLEL_LIMIT, config.STALE_MINUTES, config.MAX_ATTEMPTS, config.POLL_INTERVAL_SECONDS,
        config.RETENTION_MONTHS, config.RETENTION_CHECK_INTERVAL_SECONDS,
    )
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("crop_worker poll iteration failed")
        time.sleep(config.POLL_INTERVAL_SECONDS)
