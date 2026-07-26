import logging
import time

import ai_worker
import alert_images
import config
import crop
import db
import profile_config

logger = logging.getLogger(__name__)


def parse_alert_sighting_response(response: dict, row: dict, type_config: dict | None = None) -> dict:
    # Same shape as ai_worker.parse_sighting_response, just keyed by visit_id instead of
    # raw_event_id -- no JSON parsing, no per-type branching. alert_prompt already asks the model
    # to cover both static attributes and what changed across the gathered series of images in one
    # flowing answer, so the whole chat response is the description verbatim, same as the
    # event-level path.
    return {
        "visit_id": row["id"],
        "object_label": row.get("objects"),
        "description": ai_worker._extract_response_text(response, type_config),
    }


def _select_events_for_alert(events: list[dict], max_images: int) -> list[dict]:
    # Picks which of a visit's linked raw_events get a high-res crop built and sent to the VLM --
    # one representative (the earliest) per distinct object type first, so a visit spanning several
    # real types (e.g. a car and a person) always gets at least one image of each rather than
    # losing a whole type to whichever happened to sort first. If that alone already reaches
    # max_images (a visit spanning more distinct types than the cap -- rare but possible), the
    # earliest-starting representatives win. Otherwise, remaining slots are filled by round-robin
    # across types that have more than one linked event (tracker re-tracks/label flicker of the
    # SAME real object), each round taking the middle of what's left in that type's own bucket --
    # a simple, deterministic way to spread the extra images across the type's own timespan instead
    # of clustering them all near wherever the representative already came from.
    if not events:
        return []
    groups: dict[str, list[dict]] = {}
    for event in events:
        groups.setdefault(event["objects"], []).append(event)

    representatives = [group[0] for group in groups.values()]
    representatives.sort(key=lambda e: e["start_ts"])
    if len(representatives) >= max_images:
        return representatives[:max_images]

    remaining_slots = max_images - len(representatives)
    leftovers_by_type = {label: group[1:] for label, group in groups.items() if len(group) > 1}

    fill: list[dict] = []
    while remaining_slots > 0 and any(leftovers_by_type.values()):
        for label in list(leftovers_by_type.keys()):
            if remaining_slots <= 0:
                break
            bucket = leftovers_by_type[label]
            if not bucket:
                continue
            fill.append(bucket.pop(len(bucket) // 2))
            remaining_slots -= 1

    selected = representatives + fill
    selected.sort(key=lambda e: e["start_ts"])
    return selected


def _gather_alert_images(
    events: list[dict], crop_disabled: bool | None = None, crop_padding_pct: float | None = None,
    crop_frame_offset_pct: float | None = None,
) -> list[tuple[dict, str]]:
    # A single bad/expired linked event (Frigate's own event-id clip already rolled off, a
    # transient fetch error, ...) shouldn't fail the whole visit's alert analysis -- same "a gap
    # doesn't take the rest down with it" tolerance crop._panels_from_independent_timestamps
    # already established for the old grid's per-moment fetches. Only an empty result routes to a
    # real failure (see process_claimed_visit). Returns (event, image) pairs rather than a bare
    # image list -- a per-event failure means events/images can end up different lengths, and
    # alert_images.store_alert_images needs each image's own source event (for its filename) when
    # STORE_ALERT_IMAGES is on, not just the flat image list the VLM call itself needs.
    gathered = []
    for event in events:
        try:
            image = crop.crop_event_high_res(
                event, crop_frame_offset_pct=crop_frame_offset_pct,
                crop_disabled=crop_disabled, crop_padding_pct=crop_padding_pct,
            )
            gathered.append((event, image))
        except Exception:
            logger.warning(
                "Could not build a high-res crop for raw_event id=%s det_id=%s in alert analysis, skipping",
                event.get("id"), event.get("det_id"), exc_info=True,
            )
    return gathered


def _resolve_alert_type_config(type_config: dict) -> dict:
    # Optional alert_provider/alert_model/alert_chat_path keys let one object type point its
    # alert-stage analysis at a different provider than its own event-stage analysis (e.g.
    # event_prompt stays on the local llama_slot_proxy for cheap, frequent single-image analysis,
    # while alert_prompt routes to a hosted provider for the multi-image series) -- without this,
    # profiles.yaml's single provider/model/chat_path keys would force both stages onto the same
    # backend. Falls back to the plain keys when the alert_* ones are absent, so an existing
    # profiles.yaml needs no edit to keep working, same convention `provider` itself established.
    resolved = dict(type_config)
    if "alert_provider" in type_config:
        resolved["provider"] = type_config["alert_provider"]
    if "alert_model" in type_config:
        resolved["model"] = type_config["alert_model"]
    if "alert_chat_path" in type_config:
        resolved["chat_path"] = type_config["alert_chat_path"]
    return resolved


def process_claimed_visit(row: dict, profile: dict) -> None:
    visit_id = row["id"]
    type_config = profile.get("object_types", {}).get(row.get("objects"))
    if type_config is None:
        # Shouldn't happen -- run_once only ever asks claim_alert_ai_batch for mapped types -- but
        # guard rather than crash the poll loop on an unexpected row.
        logger.warning("Claimed visit id=%s has unmapped representative object type %r, skipping", visit_id, row.get("objects"))
        return
    if row.get("alert_ai_attempt_count", 0) == 0:
        # Same head-start reasoning as crop_worker/video_worker/visit_thumb_worker's own initial
        # waits -- a just-linked event's own clip may not be finalized on Frigate's side yet.
        # Applied once per claimed visit, not once per gathered image.
        time.sleep(config.ALERT_AI_INITIAL_WAIT_SECONDS)

    effective_config = _resolve_alert_type_config(type_config)
    timeout = effective_config.get("timeout_seconds", config.AI_STAGE_DEFAULT_TIMEOUT_SECONDS)
    object_label = row.get("objects")
    crop_disabled = profile_config.crop_disabled(profile, object_label)
    crop_padding_pct = profile_config.crop_padding_pct(profile, object_label)
    crop_frame_offset_pct = profile_config.alert_crop_frame_offset_pct(profile, object_label)

    try:
        linked_events = db.get_raw_events_for_visit(visit_id)
        selected = _select_events_for_alert(linked_events, config.ALERT_AI_MAX_IMAGES)
        gathered = _gather_alert_images(selected, crop_disabled, crop_padding_pct, crop_frame_offset_pct)
        if not gathered:
            raise ValueError(f"Could not gather any high-res images for visit id={visit_id}")
        gathered_events = [event for event, _image in gathered]
        images = [image for _event, image in gathered]

        if profile_config.store_alert_images(profile, object_label):
            # Best-effort, non-fatal -- a disk-write failure (full disk, permissions) shouldn't
            # take down an AI analysis that already has its images in hand. Deterministic
            # filenames (visit id + index + event id, not a timestamp) mean a later retry of this
            # same visit overwrites these files rather than accumulating duplicates.
            try:
                paths = alert_images.store_alert_images(row, gathered_events, images)
                db.set_visit_alert_image_paths(visit_id, paths)
            except Exception:
                logger.warning(
                    "Failed to persist alert images to disk for visit id=%s", visit_id, exc_info=True,
                )

        response = ai_worker._chat_request(effective_config, effective_config["alert_prompt"], images, timeout)
        fields = parse_alert_sighting_response(response, row, effective_config)
        embedding = ai_worker._embed_text(fields["description"])
        db.complete_visit_sighting(fields["visit_id"], fields["object_label"], fields["description"], embedding)
        logger.info(
            "Alert AI analysis done for visit id=%s object_label=%s image_count=%d",
            visit_id, fields["object_label"], len(images),
        )

    except Exception:
        logger.exception("Alert AI analysis failed for visit id=%s det_id=%s", visit_id, row.get("det_id"))
        db.fail_alert_ai_event(visit_id, config.AI_STAGE_MAX_ATTEMPTS)


def run_once(profile: dict) -> None:
    # Same per-type opt-out/opt-in filtering ai_worker.run_once applies, against
    # ai_alerts_enabled/AI_ALERTS_ENABLED instead of ai_events_stage_enabled/AI_EVENTS_STAGE_ENABLED.
    object_types = [
        label for label in profile.get("object_types", {})
        if profile_config.ai_alerts_enabled(profile, label)
    ]
    visits = db.claim_alert_ai_batch(
        object_types, config.AI_STAGE_PARALLEL_LIMIT, config.AI_STAGE_STALE_MINUTES,
        max_age_hours=config.AI_STAGE_MAX_AGE_HOURS,
    )
    for row in visits:
        process_claimed_visit(row, profile)


def run_forever(profile: dict | None = None) -> None:
    if profile is None:
        profile = ai_worker.load_profile(config.AI_STAGE_PROFILE_PATH)
    logger.info(
        "alert_ai_worker starting: object_types=%s parallel_limit=%s stale_minutes=%s "
        "max_attempts=%s poll_interval=%ss llama_proxy_base_url=%s",
        list(profile.get("object_types", {}).keys()), config.AI_STAGE_PARALLEL_LIMIT,
        config.AI_STAGE_STALE_MINUTES, config.AI_STAGE_MAX_ATTEMPTS,
        config.AI_STAGE_POLL_INTERVAL_SECONDS, config.LLAMA_PROXY_BASE_URL,
    )
    while True:
        try:
            run_once(profile)
        except Exception:
            logger.exception("alert_ai_worker poll iteration failed")
        time.sleep(config.AI_STAGE_POLL_INTERVAL_SECONDS)
