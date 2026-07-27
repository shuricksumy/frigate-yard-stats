"""Per-object-type setting resolution, entirely within profiles.yaml.

Every setting resolved here is deliberately NOT an env var -- these are all settings you'd
realistically want different per Frigate object type, so profiles.yaml is the one place to
configure them rather than splitting them across .env and here. Two tiers, checked in order: a
type's own entry under object_types.<label> (highest), then a profile-wide `defaults` section
(common values applied to every type that doesn't set its own). If neither tier sets a given key,
resolution falls through to a plain Python constant in config.py -- a hardcoded last-resort default
matching this project's original behavior, not a third configurable tier (there's no env var
backing it). Every resolver here follows this same shape -- no I/O, no caching -- so callers
(crop.py, crop_worker.py, video_worker.py, alert_video_worker.py,
mqtt_ingest.py, ai_worker.py, main.py) pass in whatever profile they already
loaded once at startup (ai_worker.load_profile). A missing/None profile or object_label is treated
the same as "no override for this type" -- every resolver falls back to the hardcoded default
rather than raising.

Two families of settings:
  - Plain per-row settings (telegram_events_mode/telegram_alerts_mode/ai_events_stage_enabled/
    ai_image_max_dimension/store_event_images) -- resolved fresh for whatever row is currently
    being processed, since the worker that owns that row already claims every type regardless
    (crop_worker) or already knows which types to ask an existing object_types-aware claim
    function for (ai_worker). (crop_disabled/crop_frame_offset_pct/crop_padding_pct used to live
    here too, configuring a region-crop/seek computed from the record-stream clip -- removed
    entirely once crop_event switched to using Frigate's own snapshot exclusively, which has no
    region-crop math left to configure; see crop.py's own comment and CLAUDE.md's "Cropping"
    section.)
  - store_video_events/store_video_alerts (events vs. alerts flow -- store_video_alerts has always
    gated per-VISIT video storage, not anything alert-AI-specific) -- these gate whether their
    whole poll thread starts at all (main.py) *and* which rows their claim function is even
    allowed to look at (claim_video_batch/claim_visit_video_batch), since unlike the AI stage
    these apply to any Frigate label by default, not just ones with a profiles.yaml prompt entry.
    Their *_claim_filter functions return an include-or-exclude label list (never a plain
    include-list checked against every "known" label) specifically so a label that isn't mentioned
    anywhere -- not even in the cosmetic-only OBJECT_TYPES env var -- still inherits the plain
    global default instead of being silently dropped. video_events_needed/video_alerts_needed and
    their own *_claim_filter siblings go one step further -- a row/type is eligible even with
    storage off, as long as that type's Telegram mode wants a video (send-without-store).
"""
import config


def _type_config(profile: dict | None, object_label: str | None) -> dict:
    if not profile:
        return {}
    return profile.get("object_types", {}).get(object_label) or {}


def _defaults_config(profile: dict | None) -> dict:
    if not profile:
        return {}
    return profile.get("defaults") or {}


def _resolve(profile: dict | None, object_label: str | None, key: str, global_default):
    type_cfg = _type_config(profile, object_label)
    if key in type_cfg:
        return type_cfg[key]
    defaults_cfg = _defaults_config(profile)
    if key in defaults_cfg:
        return defaults_cfg[key]
    return global_default


def flag_summary(profile: dict | None, key: str, global_default) -> dict:
    # Used by the admin dashboard (/admin/overview) to show a per-object-type-resolvable setting's
    # actual effective value -- previously that endpoint only ever showed config.py's hardcoded
    # fallback constant, never parsing profiles.yaml at all, which meant a deployment with e.g.
    # `defaults: {ai_events_stage_enabled: true}` still saw "off" on the dashboard (confirmed live:
    # a production instance with several of these genuinely on showed every one as off/none).
    # Resolved with object_label=None -- the "effective base" every type falls back to unless it
    # sets its own override -- plus which tier that came from (`defaults:` vs the hardcoded
    # fallback; there's no "type" tier here since no specific type is being asked about) and which
    # object types (if any) override it to a *different* value, so a real per-type split doesn't
    # silently collapse into one summary number.
    value = _resolve(profile, None, key, global_default)
    source = "defaults" if key in _defaults_config(profile) else "hardcoded"
    overridden_for = sorted(
        label for label, type_cfg in (profile or {}).get("object_types", {}).items()
        if key in (type_cfg or {}) and type_cfg[key] != value
    )
    return {"value": value, "source": source, "overridden_for": overridden_for}


def telegram_events_mode(profile: dict | None, object_label: str | None) -> str:
    return _resolve(profile, object_label, "telegram_events_mode", config.TELEGRAM_EVENTS_MODE)


def telegram_alerts_mode(profile: dict | None, object_label: str | None) -> str:
    return _resolve(profile, object_label, "telegram_alerts_mode", config.TELEGRAM_ALERTS_MODE)


def ai_events_stage_enabled(profile: dict | None, object_label: str | None) -> bool:
    return _resolve(profile, object_label, "ai_events_stage_enabled", config.AI_EVENTS_STAGE_ENABLED)


def min_event_duration_seconds(profile: dict | None, object_label: str | None) -> float:
    # Ingest-time filter (mqtt_ingest._handle_event_message) -- a tracked-object lifecycle shorter
    # than this is never inserted into raw_events at all. See config.py's own comment on
    # MIN_EVENT_DURATION_SECONDS for why (repeated tracker re-detections of one stationary object,
    # confirmed live). 0 (the hardcoded fallback) means no filtering.
    return _resolve(
        profile, object_label, "min_event_duration_seconds", config.MIN_EVENT_DURATION_SECONDS
    )


def ai_image_max_dimension(profile: dict | None, object_label: str | None) -> int:
    # The AI-facing (and DB-stored) image size -- a downscale of the same full-resolution crop
    # crop.crop_event always builds. Per-object-type resolvable since a plate-heavy vehicle prompt
    # may want more resolution than a person/dog prompt; the full-resolution copy written to disk
    # (store_event_images below) is never capped by this.
    return _resolve(profile, object_label, "ai_image_max_dimension", config.MAX_CROP_DIMENSION)


def store_event_images(profile: dict | None, object_label: str | None) -> bool:
    # Plain per-row resolution, same shape as ai_image_max_dimension above -- gates a synchronous
    # side effect inside the existing crop_worker thread (persisting the full-resolution crop to
    # disk), not a separate poll thread/claim query.
    return _resolve(profile, object_label, "store_event_images", config.STORE_EVENT_IMAGES)


def store_video_events_enabled(profile: dict | None, object_label: str | None) -> bool:
    # Plain per-label resolution (type override -> defaults -> hardcoded fallback), for callers
    # that already know the one row/type they're deciding for (e.g. insert_raw_event, choosing a
    # freshly-ingested row's *initial* video_status) -- as opposed to store_video_events_claim_filter
    # below, which builds an include/exclude filter for a claim query spanning many rows/types at
    # once.
    return _resolve(profile, object_label, "store_video_events", config.STORE_VIDEO_EVENTS)


def store_video_alerts_enabled(profile: dict | None, object_label: str | None) -> bool:
    # This has always gated per-VISIT video storage (frigate/reviews), independent of
    # store_video_events_enabled above (per-raw_event clips, frigate/events).
    return _resolve(profile, object_label, "store_video_alerts", config.STORE_VIDEO_ALERTS)


def any_ai_events_stage_enabled(profile: dict | None) -> bool:
    # Gates whether ai_worker's whole poll thread starts at all (main.py) -- true if the effective
    # base (profile-wide `defaults`, else config.py's hardcoded fallback) is on, or at least one
    # object type opts in despite that base being off.
    if _resolve(profile, None, "ai_events_stage_enabled", config.AI_EVENTS_STAGE_ENABLED):
        return True
    if not profile:
        return False
    return any(t.get("ai_events_stage_enabled") for t in profile.get("object_types", {}).values())


def _bool_override_labels(profile: dict | None, key: str) -> tuple[list[str], list[str]]:
    # Every object type that explicitly sets `key` one way or the other -- split into
    # (true_labels, false_labels). Deliberately not an enumeration of every "known" label (config.
    # OBJECT_TYPES or otherwise); a label that never sets this key at all falls through to the
    # effective base in _claim_filter below, whatever that base is.
    if not profile:
        return [], []
    true_labels, false_labels = [], []
    for label, type_cfg in profile.get("object_types", {}).items():
        if key in type_cfg:
            (true_labels if type_cfg[key] else false_labels).append(label)
    return true_labels, false_labels


def _claim_filter(profile: dict | None, key: str, global_default: bool) -> tuple[list[str] | None, list[str] | None]:
    # Returns (object_types, exclude_object_types) for a claim query -- at most one of the two is
    # non-None. If the effective base (defaults section, else the global default) is enabled, only
    # the explicit per-type opt-outs need excluding (or nothing at all, i.e. (None, None), the
    # exact unfiltered query this project ran before per-type overrides existed). If the base is
    # disabled, only the explicit per-type opt-ins are eligible -- object_types can legitimately be
    # an empty list here (nothing opts in at all), which the caller must treat as "claim nothing",
    # not as "no filter".
    base = _resolve(profile, None, key, global_default)
    true_labels, false_labels = _bool_override_labels(profile, key)
    if base:
        return (None, false_labels) if false_labels else (None, None)
    return (true_labels, None)


def store_video_events_claim_filter(profile: dict | None) -> tuple[list[str] | None, list[str] | None]:
    return _claim_filter(profile, "store_video_events", config.STORE_VIDEO_EVENTS)


def store_video_alerts_claim_filter(profile: dict | None) -> tuple[list[str] | None, list[str] | None]:
    return _claim_filter(profile, "store_video_alerts", config.STORE_VIDEO_ALERTS)


def _any_enabled(object_types: list[str] | None) -> bool:
    # object_types is None whenever the base is enabled (unfiltered, or exclude-filtered -- either
    # way at least the unlisted labels are still enabled); it's a concrete (possibly empty) list
    # only when the base is disabled and per-type opt-ins are the sole source of anything enabled.
    return object_types is None or len(object_types) > 0


def any_store_video_events_enabled(profile: dict | None) -> bool:
    object_types, _ = store_video_events_claim_filter(profile)
    return _any_enabled(object_types)


def any_store_video_alerts_enabled(profile: dict | None) -> bool:
    object_types, _ = store_video_alerts_claim_filter(profile)
    return _any_enabled(object_types)


# ---- send-to-Telegram-without-storing -----------------------------------------------------
#
# A raw_event/visit can be claimed by the video stage purely to be sent to Telegram, with no
# persistent storage at all, as long as that type's Telegram mode wants a video -- storage and
# Telegram delivery are independent axes (see video_worker.py/alert_video_worker.py). The plain
# store_video_events_enabled/store_video_alerts_enabled resolvers above answer "should THIS
# claimed row also be persisted to disk"; the functions below answer the broader "is there any
# reason to claim/process this row/type at all" (storage OR Telegram video), used for the initial
# video_status at ingest time (db.insert_raw_event/record_visit), the claim query's own eligibility
# filter, and whether the poll thread should start at all (main.py).

def video_events_needed(profile: dict | None, object_label: str | None) -> bool:
    return (
        store_video_events_enabled(profile, object_label)
        or telegram_events_mode(profile, object_label) in ("video", "all")
    )


def video_alerts_needed(profile: dict | None, object_label: str | None) -> bool:
    return (
        store_video_alerts_enabled(profile, object_label)
        or telegram_alerts_mode(profile, object_label) in ("video", "all")
    )


def _union_claim_filter(
    profile: dict | None, store_key: str, store_default: bool, mode_key: str, mode_default: str,
) -> tuple[list[str] | None, list[str] | None]:
    # Same (object_types, exclude_object_types) shape _claim_filter returns, but for a claim query
    # that needs "store_key resolves true OR mode_key resolves to video/all" per type -- a union of
    # two independently-resolved settings can't reuse _claim_filter directly (that only ever
    # combines ONE key's own two-tier resolution), so each explicitly-configured label's own
    # combined value is computed and compared against the combined global base instead.
    store_base = _resolve(profile, None, store_key, store_default)
    mode_base = _resolve(profile, None, mode_key, mode_default) in ("video", "all")
    base = store_base or mode_base
    true_labels, false_labels = [], []
    for label in (profile or {}).get("object_types", {}):
        store_val = _resolve(profile, label, store_key, store_default)
        mode_val = _resolve(profile, label, mode_key, mode_default) in ("video", "all")
        combined = store_val or mode_val
        if combined != base:
            (true_labels if combined else false_labels).append(label)
    if base:
        return (None, false_labels) if false_labels else (None, None)
    return (true_labels, None)


def video_events_claim_filter(profile: dict | None) -> tuple[list[str] | None, list[str] | None]:
    return _union_claim_filter(
        profile, "store_video_events", config.STORE_VIDEO_EVENTS,
        "telegram_events_mode", config.TELEGRAM_EVENTS_MODE,
    )


def video_alerts_claim_filter(profile: dict | None) -> tuple[list[str] | None, list[str] | None]:
    return _union_claim_filter(
        profile, "store_video_alerts", config.STORE_VIDEO_ALERTS,
        "telegram_alerts_mode", config.TELEGRAM_ALERTS_MODE,
    )


def any_video_events_worker_needed(profile: dict | None) -> bool:
    object_types, _ = video_events_claim_filter(profile)
    return _any_enabled(object_types)


def any_video_alerts_worker_needed(profile: dict | None) -> bool:
    object_types, _ = video_alerts_claim_filter(profile)
    return _any_enabled(object_types)
