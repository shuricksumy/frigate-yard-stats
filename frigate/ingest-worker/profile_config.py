"""Per-object-type setting resolution over profiles.yaml, with config.py as the fallback default.

Four settings can be overridden per Frigate object label (car/truck/person/dog/...) directly in
profiles.yaml's object_types.<label> entry: telegram_events_mode, telegram_alerts_mode,
ai_events_stage_enabled, ai_alerts_enabled. Every resolver here follows the same shape -- a type's
own override wins when present, otherwise the global env-var default from config.py applies. This
lets one type (e.g. a low-priority `dog`) opt out of Telegram/AI-stage participation, or opt IN
despite the global default being off, without a pipeline-wide switch -- every type that doesn't
set an override keeps behaving exactly as it did before this existed.

Every function here is a pure lookup over a plain dict -- no I/O, no caching -- so callers (crop_
worker.py, video_worker.py, mqtt_ingest.py, visit_thumb_worker.py, alert_video_worker.py, ai_worker.
py, alert_ai_worker.py, main.py) pass in whatever profile they already loaded once at startup
(ai_worker.load_profile). A missing/None profile or object_label is treated the same as "no
override for this type" -- every resolver falls back to the global default rather than raising.
"""
import config


def _type_config(profile: dict | None, object_label: str | None) -> dict:
    if not profile:
        return {}
    return profile.get("object_types", {}).get(object_label) or {}


def telegram_events_mode(profile: dict | None, object_label: str | None) -> str:
    return _type_config(profile, object_label).get("telegram_events_mode", config.TELEGRAM_EVENTS_MODE)


def telegram_alerts_mode(profile: dict | None, object_label: str | None) -> str:
    return _type_config(profile, object_label).get("telegram_alerts_mode", config.TELEGRAM_ALERTS_MODE)


def ai_events_stage_enabled(profile: dict | None, object_label: str | None) -> bool:
    return _type_config(profile, object_label).get("ai_events_stage_enabled", config.AI_EVENTS_STAGE_ENABLED)


def ai_alerts_enabled(profile: dict | None, object_label: str | None) -> bool:
    return _type_config(profile, object_label).get("ai_alerts_enabled", config.AI_ALERTS_ENABLED)


def any_ai_events_stage_enabled(profile: dict | None) -> bool:
    # Gates whether ai_worker's whole poll thread starts at all (main.py) -- true if the global
    # default is on, or at least one object type opts in despite the global default being off.
    if config.AI_EVENTS_STAGE_ENABLED:
        return True
    if not profile:
        return False
    return any(t.get("ai_events_stage_enabled") for t in profile.get("object_types", {}).values())


def any_ai_alerts_enabled(profile: dict | None) -> bool:
    if config.AI_ALERTS_ENABLED:
        return True
    if not profile:
        return False
    return any(t.get("ai_alerts_enabled") for t in profile.get("object_types", {}).values())
