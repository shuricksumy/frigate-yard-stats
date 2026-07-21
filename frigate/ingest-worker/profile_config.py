"""Per-object-type setting resolution, entirely within profiles.yaml.

Every setting resolved here is deliberately NOT an env var -- these are all settings you'd
realistically want different per Frigate object type, so profiles.yaml is the one place to
configure them rather than splitting them across .env and here. Two tiers, checked in order: a
type's own entry under object_types.<label> (highest), then a profile-wide `defaults` section
(common values applied to every type that doesn't set its own). If neither tier sets a given key,
resolution falls through to a plain Python constant in config.py -- a hardcoded last-resort default
matching this project's original behavior, not a third configurable tier (there's no env var
backing it). Every resolver here follows this same shape -- no I/O, no caching -- so callers
(crop.py, crop_worker.py, video_worker.py, alert_video_worker.py, visit_thumb_worker.py,
mqtt_ingest.py, ai_worker.py, alert_ai_worker.py, main.py) pass in whatever profile they already
loaded once at startup (ai_worker.load_profile). A missing/None profile or object_label is treated
the same as "no override for this type" -- every resolver falls back to the hardcoded default
rather than raising.

Two families of settings:
  - Plain per-row settings (telegram_events_mode/telegram_alerts_mode/ai_events_stage_enabled/
    ai_alerts_enabled/crop_disabled/crop_frame_offset_pct/crop_padding_pct/
    frigate_snapshot_enabled/visit_preview_frame_percentages) -- resolved fresh for whatever row is
    currently being processed, since the worker that owns that row already claims every type
    regardless (crop_worker, visit_thumb_worker) or already knows which types to ask an existing
    object_types-aware claim function for (ai_worker/alert_ai_worker).
  - store_video/store_video_alerts/visit_thumb_crop_enabled -- these gate whether their whole poll
    thread starts at all (main.py) *and* which rows their claim function is even allowed to look at
    (claim_video_batch/claim_visit_video_batch/claim_visit_thumb_crop_batch), since unlike the AI
    stage these apply to any Frigate label by default, not just ones with a profiles.yaml prompt
    entry. Their *_claim_filter functions return an include-or-exclude label list (never a plain
    include-list checked against every "known" label) specifically so a label that isn't mentioned
    anywhere -- not even in the cosmetic-only OBJECT_TYPES env var -- still inherits the plain
    global default instead of being silently dropped.
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


def telegram_events_mode(profile: dict | None, object_label: str | None) -> str:
    return _resolve(profile, object_label, "telegram_events_mode", config.TELEGRAM_EVENTS_MODE)


def telegram_alerts_mode(profile: dict | None, object_label: str | None) -> str:
    return _resolve(profile, object_label, "telegram_alerts_mode", config.TELEGRAM_ALERTS_MODE)


def ai_events_stage_enabled(profile: dict | None, object_label: str | None) -> bool:
    return _resolve(profile, object_label, "ai_events_stage_enabled", config.AI_EVENTS_STAGE_ENABLED)


def ai_alerts_enabled(profile: dict | None, object_label: str | None) -> bool:
    return _resolve(profile, object_label, "ai_alerts_enabled", config.AI_ALERTS_ENABLED)


def crop_disabled(profile: dict | None, object_label: str | None) -> bool:
    return _resolve(profile, object_label, "crop_disabled", config.CROP_DISABLED)


def crop_frame_offset_pct(profile: dict | None, object_label: str | None) -> float:
    return _resolve(profile, object_label, "crop_frame_offset_pct", config.CROP_FRAME_OFFSET_PCT)


def crop_padding_pct(profile: dict | None, object_label: str | None) -> float:
    return _resolve(profile, object_label, "crop_padding_pct", config.CROP_PADDING_PCT)


def frigate_snapshot_enabled(profile: dict | None, object_label: str | None) -> bool:
    return _resolve(profile, object_label, "frigate_snapshot_enabled", config.FRIGATE_SNAPSHOT_ENABLED)


def visit_preview_frame_percentages(profile: dict | None, object_label: str | None) -> list[float]:
    return _resolve(
        profile, object_label, "visit_preview_frame_percentages", config.VISIT_PREVIEW_FRAME_PERCENTAGES,
    )


def store_video_enabled(profile: dict | None, object_label: str | None) -> bool:
    # Plain per-label resolution (type override -> defaults -> hardcoded fallback), for callers
    # that already know the one row/type they're deciding for (e.g. insert_raw_event, choosing a
    # freshly-ingested row's *initial* video_status) -- as opposed to store_video_claim_filter
    # below, which builds an include/exclude filter for a claim query spanning many rows/types at
    # once.
    return _resolve(profile, object_label, "store_video", config.STORE_VIDEO)


def store_video_alerts_enabled(profile: dict | None, object_label: str | None) -> bool:
    return _resolve(profile, object_label, "store_video_alerts", config.STORE_VIDEO_ALERTS)


def visit_thumb_crop_enabled(profile: dict | None, object_label: str | None) -> bool:
    return _resolve(profile, object_label, "visit_thumb_crop_enabled", config.VISIT_THUMB_CROP_ENABLED)


def any_ai_events_stage_enabled(profile: dict | None) -> bool:
    # Gates whether ai_worker's whole poll thread starts at all (main.py) -- true if the effective
    # base (profile-wide `defaults`, else config.py's hardcoded fallback) is on, or at least one
    # object type opts in despite that base being off.
    if _resolve(profile, None, "ai_events_stage_enabled", config.AI_EVENTS_STAGE_ENABLED):
        return True
    if not profile:
        return False
    return any(t.get("ai_events_stage_enabled") for t in profile.get("object_types", {}).values())


def any_ai_alerts_enabled(profile: dict | None) -> bool:
    if _resolve(profile, None, "ai_alerts_enabled", config.AI_ALERTS_ENABLED):
        return True
    if not profile:
        return False
    return any(t.get("ai_alerts_enabled") for t in profile.get("object_types", {}).values())


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


def store_video_claim_filter(profile: dict | None) -> tuple[list[str] | None, list[str] | None]:
    return _claim_filter(profile, "store_video", config.STORE_VIDEO)


def store_video_alerts_claim_filter(profile: dict | None) -> tuple[list[str] | None, list[str] | None]:
    return _claim_filter(profile, "store_video_alerts", config.STORE_VIDEO_ALERTS)


def visit_thumb_crop_claim_filter(profile: dict | None) -> tuple[list[str] | None, list[str] | None]:
    return _claim_filter(profile, "visit_thumb_crop_enabled", config.VISIT_THUMB_CROP_ENABLED)


def _any_enabled(object_types: list[str] | None) -> bool:
    # object_types is None whenever the base is enabled (unfiltered, or exclude-filtered -- either
    # way at least the unlisted labels are still enabled); it's a concrete (possibly empty) list
    # only when the base is disabled and per-type opt-ins are the sole source of anything enabled.
    return object_types is None or len(object_types) > 0


def any_store_video_enabled(profile: dict | None) -> bool:
    object_types, _ = store_video_claim_filter(profile)
    return _any_enabled(object_types)


def any_store_video_alerts_enabled(profile: dict | None) -> bool:
    object_types, _ = store_video_alerts_claim_filter(profile)
    return _any_enabled(object_types)


def any_visit_thumb_crop_enabled(profile: dict | None) -> bool:
    object_types, _ = visit_thumb_crop_claim_filter(profile)
    return _any_enabled(object_types)
