import logging
import time

import config
import crop
import db
import profile_config
import telegram

logger = logging.getLogger(__name__)


def _send_deferred_visit_summary(
    visit: dict, object_label: str | None = None,
    gif_base64: str | None = None, image_base64: str | None = None, profile: dict | None = None,
) -> None:
    # Fires the visit-summary Telegram message mqtt_ingest.py deliberately skipped sending
    # immediately (see visit_thumb_crop_will_be_attempted) -- this worker is the only place that
    # ever settles a visit's thumb_crop_status to a terminal state (done or failed), so it's the
    # only place that can know when it's finally time to send. Never raises, same
    # belt-and-suspenders reasoning as every other Telegram call site in this project.
    mode = profile_config.telegram_alerts_mode(profile, object_label)
    if mode not in ("image", "all"):
        return
    visit_id = visit["id"]
    try:
        event_count = db.count_events_for_visit(visit_id)
        message_id = telegram.send_visit_summary(
            visit.get("cameras"), visit.get("objects"), event_count,
            gif_base64=gif_base64, image_base64=image_base64, mode=mode,
        )
        if message_id is not None:
            db.set_visit_telegram_photo_message_id(visit_id, message_id)
    except Exception:
        logger.warning("Deferred Telegram visit summary send failed for visit id=%s", visit_id, exc_info=True)


def process_claimed_visit(visit: dict, profile: dict | None = None) -> None:
    visit_id = visit["id"]
    # Same head-start reasoning as video_worker.process_claimed_event / alert_video_worker's own
    # wait -- Frigate may still be finalizing the continuous-recording segment right after the
    # review closes.
    if visit.get("thumb_crop_attempt_count", 0) == 0:
        time.sleep(config.VISIT_THUMB_CROP_INITIAL_WAIT_SECONDS)

    representative = db.get_representative_event_for_visit(visit_id)
    # Resolved against the representative event's own single object label, same convention as
    # mqtt_ingest.py's immediate-send path and claim_alert_ai_batch.
    object_label = representative.get("objects") if representative else None
    try:
        if representative is None or not representative.get("det_id"):
            raise ValueError(f"No representative raw_event with det_id for visit id={visit_id}")

        crop_image_base64, preview_gif_base64 = crop.build_visit_preview(
            visit, representative,
            frame_percentages=profile_config.visit_preview_frame_percentages(profile, object_label),
            crop_disabled=profile_config.crop_disabled(profile, object_label),
            crop_padding_pct=profile_config.crop_padding_pct(profile, object_label),
        )
        db.mark_visit_thumb_crop_done(visit_id, crop_image_base64, preview_gif_base64)
        logger.info(
            "Cropped visit thumbnail for visit id=%s camera=%s thumb_time=%s",
            visit_id, visit.get("cameras"), visit.get("thumb_time"),
        )
        _send_deferred_visit_summary(visit, object_label, gif_base64=preview_gif_base64, profile=profile)
    except Exception:
        logger.warning(
            "Visit thumbnail crop failed for visit id=%s (attempt %s/%s)",
            visit_id, visit.get("thumb_crop_attempt_count", 0) + 1, config.VISIT_THUMB_CROP_MAX_ATTEMPTS,
            exc_info=True,
        )
        result = db.mark_visit_thumb_crop_retry_or_failed(visit_id, config.VISIT_THUMB_CROP_MAX_ATTEMPTS)
        if result["thumb_crop_status"] == "failed":
            # Terminal -- the re-crop will never succeed for this visit now, so send the deferred
            # summary anyway, falling back to the representative event's own crop (or text-only
            # if that's not ready either), rather than never notifying about this visit at all.
            fallback_image = representative.get("crop_image_base64") if representative else None
            _send_deferred_visit_summary(visit, object_label, image_base64=fallback_image, profile=profile)
        else:
            time.sleep(config.VISIT_THUMB_CROP_RETRY_WAIT_SECONDS)


def run_once(profile: dict | None = None) -> None:
    db.reap_stale_visit_thumb_crop_processing()
    in_progress = db.count_visit_thumb_crop_in_progress()
    available_capacity = max(0, config.VISIT_THUMB_CROP_PARALLEL_LIMIT - in_progress)
    if available_capacity <= 0:
        return

    object_types, exclude_object_types = profile_config.visit_thumb_crop_claim_filter(profile)
    if object_types == []:
        # Base disabled, nothing opted in per-type -- nothing for this stage to do at all.
        return
    for visit in db.claim_visit_thumb_crop_batch(
        available_capacity, object_types=object_types, exclude_object_types=exclude_object_types,
    ):
        process_claimed_visit(visit, profile)


def run_forever(profile: dict | None = None) -> None:
    logger.info(
        "visit_thumb_worker starting: parallel_limit=%s initial_wait=%ss max_attempts=%s "
        "retry_wait=%ss poll_interval=%ss",
        config.VISIT_THUMB_CROP_PARALLEL_LIMIT, config.VISIT_THUMB_CROP_INITIAL_WAIT_SECONDS,
        config.VISIT_THUMB_CROP_MAX_ATTEMPTS, config.VISIT_THUMB_CROP_RETRY_WAIT_SECONDS,
        config.POLL_INTERVAL_SECONDS,
    )
    while True:
        try:
            run_once(profile)
        except Exception:
            logger.exception("visit_thumb_worker poll iteration failed")
        time.sleep(config.POLL_INTERVAL_SECONDS)
