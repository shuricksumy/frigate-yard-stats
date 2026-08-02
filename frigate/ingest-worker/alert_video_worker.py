import logging
import time

import config
import db
import poll_loop
import profile_config
import telegram
import video
import video_worker

logger = logging.getLogger(__name__)


def process_claimed_visit(visit: dict, profile: dict | None = None) -> None:
    visit_id = visit["id"]
    # Same head-start reasoning as video_worker.process_claimed_event -- Frigate may still be
    # finalizing the recording segment right after the review closes.
    if visit.get("video_attempt_count", 0) == 0:
        time.sleep(config.VIDEO_INITIAL_WAIT_SECONDS)

    # Resolved against the representative event's own single object label, same convention as
    # mqtt_ingest.py/visit_thumb_worker.py -- needed up front for both the store-enabled check
    # below and the Telegram mode check, not just the latter.
    representative = db.get_representative_event_for_visit(visit_id)
    object_label = representative.get("objects") if representative else None
    # This visit was only claimed at all because EITHER storage is on OR Telegram wants a video
    # for this type (profile_config.video_alerts_claim_filter) -- resolve both here once, up
    # front, rather than re-deriving "why was I claimed" deeper in the function.
    store = profile_config.store_video_alerts_enabled(profile, object_label)
    mode = profile_config.telegram_alerts_mode(profile, object_label)
    reply_to = visit.get("telegram_photo_message_id")
    event_count = db.count_events_for_visit(visit_id)
    caption = telegram.build_visit_caption(visit.get("cameras"), visit.get("objects"), event_count)
    filename = f"visit-{object_label or 'event'}-{visit_id}.mp4"

    # video.download_clip/build_clip_url only read start_ts/end_ts/camera/det_id off the row --
    # visits store the camera under "cameras" (singular value, per-camera-only grouping), so a
    # small adapter dict lets both flows share the exact same download/validation logic.
    clip_row = {
        "start_ts": visit["start_ts"], "end_ts": visit["end_ts"],
        "camera": visit["cameras"], "det_id": f"visit-{visit_id}",
    }
    try:
        content = video.download_clip(clip_row)

        if store:
            path = video.store_visit_clip(visit, content)
            db.mark_visit_video_done(visit_id, path)
            logger.info(
                "Stored visit video for visit id=%s camera=%s path=%s", visit_id, visit.get("cameras"), path,
            )
            try:
                telegram.send_visit_video(content, filename, caption, reply_to_message_id=reply_to, mode=mode)
            except Exception:
                # telegram.py itself shouldn't raise, but never let a Telegram hiccup take down
                # the alert-video poll loop -- storage already succeeded, so a notification
                # failure is logged only, same as before send-without-store existed.
                logger.warning(
                    "Telegram visit video send raised unexpectedly for visit id=%s", visit_id, exc_info=True,
                )
        else:
            # Storage is off for this type -- the only reason this visit was claimed at all is
            # that Telegram wants a video for it, so the send's own success/failure IS the
            # outcome here (unlike the storage branch above). A failed send with nothing stored
            # is a genuine failure -- raise so the existing retry-or-fail-with-cap handling below
            # applies, the same as a download failure would.
            sent = telegram.send_visit_video(content, filename, caption, reply_to_message_id=reply_to, mode=mode)
            if not sent:
                raise RuntimeError(
                    f"Telegram visit video send failed for visit id={visit_id} and storage is disabled"
                )
            db.mark_visit_video_done(visit_id, None)
            logger.info(
                "Sent visit video to Telegram (not stored) for visit id=%s camera=%s", visit_id, visit.get("cameras"),
            )

    except Exception:
        logger.warning(
            "Visit video download/send not ready or failed for visit id=%s (attempt %s/%s)",
            visit_id, visit.get("video_attempt_count", 0) + 1, config.VIDEO_MAX_ATTEMPTS,
        )
        db.mark_visit_video_retry_or_failed(visit_id, config.VIDEO_MAX_ATTEMPTS)
        if visit.get("video_attempt_count", 0) + 1 < config.VIDEO_MAX_ATTEMPTS:
            time.sleep(config.VIDEO_RETRY_WAIT_SECONDS)


def run_once(profile: dict | None = None) -> None:
    db.reap_stale_visit_video_processing()
    in_progress = db.count_visit_video_in_progress()
    available_capacity = max(0, config.VIDEO_PARALLEL_LIMIT - in_progress)
    if available_capacity <= 0:
        return

    # Eligible if EITHER storage is on OR Telegram wants a video for that type (send-without-
    # store) -- see profile_config.py's "send-to-Telegram-without-storing" section.
    object_types, exclude_object_types = profile_config.video_alerts_claim_filter(profile)
    # An EMPTY list means "base disabled and nothing opted in per-type" -- nothing for this stage
    # to do at all. None means "no filter", i.e. every type is eligible, so the two must not be
    # conflated (a plain `if not object_types` would wrongly skip the unfiltered case).
    if object_types is not None and not object_types:
        return
    for visit in db.claim_visit_video_batch(
        available_capacity, config.VIDEO_MAX_AGE_HOURS,
        object_types=object_types, exclude_object_types=exclude_object_types,
    ):
        process_claimed_visit(visit, profile)


def run_forever(profile: dict | None = None) -> None:
    poll_loop.run_forever(
        "alert_video_worker",
        lambda: run_once(profile),
        config.POLL_INTERVAL_SECONDS,
        # Same tuning knobs as the events-flow video worker -- the two flows deliberately share
        # every VIDEO_* setting, only their on/off switch, storage location and thread are separate.
        video_worker._startup_settings(),
    )
