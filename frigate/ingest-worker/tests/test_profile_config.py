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


# ---- store_video / store_video_visits claim filters ----
#
# These gate a whole poll thread (main.py) *and* narrow a claim query (claim_video_batch/
# claim_visit_video_batch) -- unlike the AI-stage flags, they apply to any Frigate label by
# default, so the filter must be an include-or-exclude split, never a plain include-list checked
# against every "known" label (see profile_config.py's own docstring).

def test_claim_filter_returns_no_filter_when_base_enabled_and_no_overrides(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO", True)
    assert profile_config.store_video_claim_filter(None) == (None, None)
    assert profile_config.store_video_claim_filter({"object_types": {"car": {}}}) == (None, None)


def test_claim_filter_excludes_type_that_opts_out_when_base_enabled(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO", True)
    profile = {"object_types": {"person": {"store_video": False}}}
    assert profile_config.store_video_claim_filter(profile) == (None, ["person"])


def test_claim_filter_includes_only_types_that_opt_in_when_base_disabled(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO", False)
    profile = {"object_types": {"car": {"store_video": True}, "person": {}}}
    assert profile_config.store_video_claim_filter(profile) == (["car"], None)


def test_claim_filter_returns_empty_include_list_when_base_disabled_and_nothing_opts_in(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO", False)
    assert profile_config.store_video_claim_filter(None) == ([], None)
    assert profile_config.store_video_claim_filter({"object_types": {"car": {}}}) == ([], None)


def test_claim_filter_respects_defaults_section_as_the_base(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO", False)
    profile = {"defaults": {"store_video": True}, "object_types": {"person": {"store_video": False}}}
    assert profile_config.store_video_claim_filter(profile) == (None, ["person"])


def test_store_video_visits_claim_filter_uses_its_own_key(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_VISITS", False)
    profile = {"object_types": {"car": {"store_video_visits": True}}}
    assert profile_config.store_video_visits_claim_filter(profile) == (["car"], None)


def test_any_store_video_enabled_true_when_base_enabled(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO", True)
    assert profile_config.any_store_video_enabled(None) is True


def test_any_store_video_enabled_true_via_per_type_opt_in(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO", False)
    profile = {"object_types": {"car": {"store_video": True}}}
    assert profile_config.any_store_video_enabled(profile) is True


def test_any_store_video_enabled_false_when_nothing_enables_it(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO", False)
    assert profile_config.any_store_video_enabled(None) is False
    assert profile_config.any_store_video_enabled({"object_types": {"car": {}}}) is False


def test_any_store_video_visits_enabled_via_per_type_opt_in(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_VISITS", False)
    assert profile_config.any_store_video_visits_enabled({"object_types": {"car": {"store_video_visits": True}}}) is True
