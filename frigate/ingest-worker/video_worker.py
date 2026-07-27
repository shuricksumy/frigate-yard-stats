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
    object_label = row.get("objects")
    # Frigate is still finalizing the recording segment right after the "end" event fires --
    # give it a head start before the first attempt (mirrors the n8n workflow's "Wait 10s" ahead
    # of "Download Clip"). Only wait on a genuinely fresh claim, not every retry pass through
    # this row -- video_attempt_count == 0 means this is the first attempt.
    if row.get("video_attempt_count", 0) == 0:
        time.sleep(config.VIDEO_INITIAL_WAIT_SECONDS)

    # This row was only claimed at all because EITHER storage is on OR Telegram wants a video for
    # this type (profile_config.video_events_claim_filter) -- resolve both here once, up front,
    # rather than re-deriving "why was I claimed" deeper in the function.
    store = profile_config.store_video_events_enabled(profile, object_label)
    mode = profile_config.telegram_events_mode(profile, object_label)
    reply_to = row.get("telegram_photo_message_id")
    filename = f"{object_label or 'event'}-{event_id}.mp4"

    try:
        # download_clip already returns the full clip as bytes in memory -- store_clip (below)
        # is a genuinely separate, optional step from here, not a precondition of sending to
        # Telegram; content itself is what gets sent either way, whether or not it's also
        # persisted to disk (see telegram._post_video's own comment).
        content = video.download_clip(row)

        if store:
            path = video.store_clip(row, content)
            db.mark_video_done(event_id, path)
            logger.info("Stored video for raw_event id=%s det_id=%s path=%s", event_id, row.get("det_id"), path)
            try:
                telegram.send_video(
                    content, filename, telegram.build_caption(row), reply_to_message_id=reply_to, mode=mode,
                )
            except Exception:
                # telegram.py itself shouldn't raise, but never let a Telegram hiccup take down
                # the video poll loop -- storage already succeeded, so a notification failure is
                # logged only, same as before send-without-store existed.
                logger.warning("Telegram video send raised unexpectedly for raw_event id=%s", event_id, exc_info=True)
        else:
            # Storage is off for this type -- the only reason this row was claimed at all is that
            # Telegram wants a video for it, so the send's own success/failure IS the outcome here
            # (unlike the storage branch above, where a Telegram hiccup doesn't affect an already-
            # successful video_status). A failed send with nothing stored is a genuine failure --
            # raise so the existing retry-or-fail-with-cap handling below applies, the same as a
            # download failure would.
            sent = telegram.send_video(
                content, filename, telegram.build_caption(row), reply_to_message_id=reply_to, mode=mode,
            )
            if not sent:
                raise RuntimeError(
                    f"Telegram video send failed for raw_event id={event_id} and storage is disabled"
                )
            db.mark_video_done(event_id, None)
            logger.info(
                "Sent video to Telegram (not stored) for raw_event id=%s det_id=%s", event_id, row.get("det_id"),
            )

    except Exception:
        logger.warning(
            "Video download/send not ready or failed for raw_event id=%s det_id=%s (attempt %s/%s)",
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

    # Eligible if EITHER storage is on OR Telegram wants a video for that type (send-without-
    # store) -- see profile_config.py's "send-to-Telegram-without-storing" section.
    object_types, exclude_object_types = profile_config.video_events_claim_filter(profile)
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
