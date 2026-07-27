"""Unit tests for profile_config.py -- per-object-type setting resolution over profiles.yaml with
config.py as the fallback default. Pure functions, no DB/network required.
"""
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import config  # noqa: E402
import profile_config  # noqa: E402

PROFILE = {
    "object_types": {
        "car": {
            "telegram_events_mode": "image",
            "ai_events_stage_enabled": False,
        },
        "dog": {
            "ai_events_stage_enabled": True,
        },
        "person": {},
    },
}


def test_telegram_events_mode_uses_type_override(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "none")
    assert profile_config.telegram_events_mode(PROFILE, "car") == "image"


def test_telegram_events_mode_falls_back_to_global_when_no_override(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "all")
    assert profile_config.telegram_events_mode(PROFILE, "person") == "all"
    assert profile_config.telegram_events_mode(PROFILE, "unmapped-label") == "all"


def test_telegram_alerts_mode_falls_back_to_global(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "video")
    assert profile_config.telegram_alerts_mode(PROFILE, "car") == "video"


def test_telegram_modes_tolerate_missing_or_none_profile(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "none")
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "all")
    assert profile_config.telegram_events_mode(None, "car") == "none"
    assert profile_config.telegram_events_mode({}, "car") == "none"
    assert profile_config.telegram_alerts_mode(None, None) == "all"


def test_ai_events_stage_enabled_type_override_can_disable_despite_global_on(monkeypatch):
    monkeypatch.setattr(config, "AI_EVENTS_STAGE_ENABLED", True)
    assert profile_config.ai_events_stage_enabled(PROFILE, "car") is False


def test_ai_events_stage_enabled_type_override_can_enable_despite_global_off(monkeypatch):
    monkeypatch.setattr(config, "AI_EVENTS_STAGE_ENABLED", False)
    assert profile_config.ai_events_stage_enabled(PROFILE, "dog") is True


def test_ai_events_stage_enabled_falls_back_to_global_when_no_override(monkeypatch):
    monkeypatch.setattr(config, "AI_EVENTS_STAGE_ENABLED", True)
    assert profile_config.ai_events_stage_enabled(PROFILE, "person") is True
    monkeypatch.setattr(config, "AI_EVENTS_STAGE_ENABLED", False)
    assert profile_config.ai_events_stage_enabled(PROFILE, "person") is False


def test_min_event_duration_seconds_falls_back_to_global_default(monkeypatch):
    monkeypatch.setattr(config, "MIN_EVENT_DURATION_SECONDS", 0)
    assert profile_config.min_event_duration_seconds(PROFILE, "person") == 0
    assert profile_config.min_event_duration_seconds(None, "anything") == 0
    assert profile_config.min_event_duration_seconds({}, "anything") == 0


def test_min_event_duration_seconds_type_override(monkeypatch):
    monkeypatch.setattr(config, "MIN_EVENT_DURATION_SECONDS", 0)
    profile = {"object_types": {"car": {"min_event_duration_seconds": 3}}}
    assert profile_config.min_event_duration_seconds(profile, "car") == 3
    assert profile_config.min_event_duration_seconds(profile, "person") == 0


def test_min_event_duration_seconds_defaults_section(monkeypatch):
    monkeypatch.setattr(config, "MIN_EVENT_DURATION_SECONDS", 0)
    profile = {"defaults": {"min_event_duration_seconds": 3}}
    assert profile_config.min_event_duration_seconds(profile, "car") == 3
    assert profile_config.min_event_duration_seconds(profile, "person") == 3


def test_any_ai_events_stage_enabled_true_when_global_on(monkeypatch):
    monkeypatch.setattr(config, "AI_EVENTS_STAGE_ENABLED", True)
    assert profile_config.any_ai_events_stage_enabled({}) is True
    assert profile_config.any_ai_events_stage_enabled(None) is True


def test_any_ai_events_stage_enabled_true_when_any_type_opts_in(monkeypatch):
    monkeypatch.setattr(config, "AI_EVENTS_STAGE_ENABLED", False)
    # PROFILE's "dog" entry opts in (ai_events_stage_enabled: True) despite the global default
    # being off here and "car" explicitly opting out -- one type opting in is enough.
    assert profile_config.any_ai_events_stage_enabled(PROFILE) is True
    profile_with_no_opt_in = {"object_types": {"car": {"ai_events_stage_enabled": False}, "person": {}}}
    assert profile_config.any_ai_events_stage_enabled(profile_with_no_opt_in) is False


def test_any_ai_events_stage_enabled_false_when_nothing_enables_it(monkeypatch):
    monkeypatch.setattr(config, "AI_EVENTS_STAGE_ENABLED", False)
    assert profile_config.any_ai_events_stage_enabled({"object_types": {"car": {}}}) is False
    assert profile_config.any_ai_events_stage_enabled(None) is False


# ---- profile-wide `defaults` section (common override tier, between per-type and global) ----

def test_defaults_section_applies_to_type_with_no_own_override(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "none")
    profile = {"defaults": {"telegram_events_mode": "all"}, "object_types": {"person": {}}}
    assert profile_config.telegram_events_mode(profile, "person") == "all"
    assert profile_config.telegram_events_mode(profile, "unmapped-label") == "all"


def test_type_level_override_wins_over_defaults_section(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "none")
    profile = {
        "defaults": {"telegram_events_mode": "all"},
        "object_types": {"car": {"telegram_events_mode": "image"}},
    }
    assert profile_config.telegram_events_mode(profile, "car") == "image"
    assert profile_config.telegram_events_mode(profile, "person") == "all"


def test_any_ai_events_stage_enabled_true_via_defaults_section(monkeypatch):
    monkeypatch.setattr(config, "AI_EVENTS_STAGE_ENABLED", False)
    profile = {"defaults": {"ai_events_stage_enabled": True}}
    assert profile_config.any_ai_events_stage_enabled(profile) is True
    assert profile_config.ai_events_stage_enabled(profile, "anything") is True


# ---- new plain per-row crop-family resolvers ----
# crop_disabled/crop_frame_offset_pct/crop_padding_pct used to be resolved here too, configuring a
# region-crop/seek from the record-stream clip -- removed entirely once crop_event switched to
# using Frigate's own snapshot exclusively (see crop.py's own comment and CLAUDE.md's "Cropping"
# section). ai_image_max_dimension is the only one of this family left.

CROP_PROFILE = {
    "object_types": {
        "car": {"ai_image_max_dimension": 640},
        "person": {},
    },
}


def test_ai_image_max_dimension_uses_type_override_and_falls_back_to_global(monkeypatch):
    monkeypatch.setattr(config, "MAX_CROP_DIMENSION", 1280)
    assert profile_config.ai_image_max_dimension(CROP_PROFILE, "car") == 640
    assert profile_config.ai_image_max_dimension(CROP_PROFILE, "person") == 1280
    assert profile_config.ai_image_max_dimension(None, "car") == 1280


def test_store_event_images_uses_type_override_and_falls_back_to_global(monkeypatch):
    monkeypatch.setattr(config, "STORE_EVENT_IMAGES", False)
    profile = {"object_types": {"car": {"store_event_images": True}, "person": {}}}
    assert profile_config.store_event_images(profile, "car") is True
    assert profile_config.store_event_images(profile, "person") is False
    assert profile_config.store_event_images(None, "car") is False


# ---- store_video_events / store_video_alerts claim filters ----
#
# These gate a whole poll thread (main.py) *and* narrow a claim query (claim_video_batch/
# claim_visit_video_batch) -- unlike the AI-stage flags, they apply to any Frigate label by
# default, so the filter must be an include-or-exclude split, never a plain include-list checked
# against every "known" label (see profile_config.py's own docstring).

def test_claim_filter_returns_no_filter_when_base_enabled_and_no_overrides(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", True)
    assert profile_config.store_video_events_claim_filter(None) == (None, None)
    assert profile_config.store_video_events_claim_filter({"object_types": {"car": {}}}) == (None, None)


def test_claim_filter_excludes_type_that_opts_out_when_base_enabled(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", True)
    profile = {"object_types": {"person": {"store_video_events": False}}}
    assert profile_config.store_video_events_claim_filter(profile) == (None, ["person"])


def test_claim_filter_includes_only_types_that_opt_in_when_base_disabled(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", False)
    profile = {"object_types": {"car": {"store_video_events": True}, "person": {}}}
    assert profile_config.store_video_events_claim_filter(profile) == (["car"], None)


def test_claim_filter_returns_empty_include_list_when_base_disabled_and_nothing_opts_in(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", False)
    assert profile_config.store_video_events_claim_filter(None) == ([], None)
    assert profile_config.store_video_events_claim_filter({"object_types": {"car": {}}}) == ([], None)


def test_claim_filter_respects_defaults_section_as_the_base(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", False)
    profile = {"defaults": {"store_video_events": True}, "object_types": {"person": {"store_video_events": False}}}
    assert profile_config.store_video_events_claim_filter(profile) == (None, ["person"])


def test_store_video_alerts_claim_filter_uses_its_own_key(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_ALERTS", False)
    profile = {"object_types": {"car": {"store_video_alerts": True}}}
    assert profile_config.store_video_alerts_claim_filter(profile) == (["car"], None)


def test_any_store_video_events_enabled_true_when_base_enabled(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", True)
    assert profile_config.any_store_video_events_enabled(None) is True


def test_any_store_video_events_enabled_true_via_per_type_opt_in(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", False)
    profile = {"object_types": {"car": {"store_video_events": True}}}
    assert profile_config.any_store_video_events_enabled(profile) is True


def test_any_store_video_events_enabled_false_when_nothing_enables_it(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", False)
    assert profile_config.any_store_video_events_enabled(None) is False
    assert profile_config.any_store_video_events_enabled({"object_types": {"car": {}}}) is False


def test_any_store_video_alerts_enabled_via_per_type_opt_in(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_ALERTS", False)
    assert profile_config.any_store_video_alerts_enabled({"object_types": {"car": {"store_video_alerts": True}}}) is True


# ---- send-to-Telegram-without-storing: video_events_needed/video_alerts_needed and their own
# claim filters -- eligible if storage is on OR Telegram wants a video, not storage alone ----

def test_video_events_needed_true_when_storage_enabled_and_telegram_off(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", True)
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "none")
    assert profile_config.video_events_needed(None, "car") is True


def test_video_events_needed_true_when_storage_off_but_telegram_wants_video(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", False)
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "video")
    assert profile_config.video_events_needed(None, "car") is True


def test_video_events_needed_true_when_telegram_mode_is_all(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", False)
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "all")
    assert profile_config.video_events_needed(None, "car") is True


def test_video_events_needed_false_when_neither_wants_it(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", False)
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "image")  # image, not video/all
    assert profile_config.video_events_needed(None, "car") is False


def test_video_events_needed_respects_per_type_overrides(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", False)
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "none")
    profile = {"object_types": {"car": {"telegram_events_mode": "video"}}}
    assert profile_config.video_events_needed(profile, "car") is True
    assert profile_config.video_events_needed(profile, "person") is False


def test_video_alerts_needed_mirrors_events_for_the_alerts_flow(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_ALERTS", False)
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "video")
    assert profile_config.video_alerts_needed(None, "car") is True


def test_video_events_claim_filter_widens_beyond_storage_alone(monkeypatch):
    # car: storage off, but Telegram wants video for it -- still eligible. person: neither wants
    # it, and the global base itself is off -- not eligible. This can't be expressed by the
    # storage-only store_video_events_claim_filter at all (it would return [] for car).
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", False)
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "none")
    profile = {"object_types": {"car": {"telegram_events_mode": "video"}, "person": {}}}
    assert profile_config.video_events_claim_filter(profile) == (["car"], None)


def test_video_events_claim_filter_excludes_type_that_wants_neither_despite_enabled_base(monkeypatch):
    # Base combined is enabled (storage on globally), but "dog" explicitly opts out of storage
    # AND doesn't want a Telegram video either -- genuinely not eligible, unlike a type that opts
    # out of storage but still wants Telegram video (which should stay eligible).
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", True)
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "none")
    profile = {"object_types": {"dog": {"store_video_events": False, "telegram_events_mode": "none"}}}
    assert profile_config.video_events_claim_filter(profile) == (None, ["dog"])


def test_video_events_claim_filter_keeps_type_eligible_via_telegram_despite_storage_opt_out(monkeypatch):
    # Same setup as above, but "dog" still wants a Telegram video despite opting out of storage --
    # must NOT be excluded, since the combined (storage OR telegram) value still matches the base.
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", True)
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "none")
    profile = {"object_types": {"dog": {"store_video_events": False, "telegram_events_mode": "video"}}}
    assert profile_config.video_events_claim_filter(profile) == (None, None)


def test_any_video_events_worker_needed_true_via_telegram_alone(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", False)
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "video")
    assert profile_config.any_video_events_worker_needed(None) is True


def test_any_video_events_worker_needed_false_when_neither_wants_it(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", False)
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "none")
    assert profile_config.any_video_events_worker_needed(None) is False


def test_any_video_alerts_worker_needed_true_via_telegram_alone(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_ALERTS", False)
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "all")
    assert profile_config.any_video_alerts_worker_needed(None) is True
