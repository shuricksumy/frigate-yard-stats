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
            "ai_alerts_enabled": True,
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


def test_ai_alerts_enabled_type_override_and_fallback(monkeypatch):
    monkeypatch.setattr(config, "AI_ALERTS_ENABLED", False)
    assert profile_config.ai_alerts_enabled(PROFILE, "dog") is True
    assert profile_config.ai_alerts_enabled(PROFILE, "person") is False


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


def test_any_ai_alerts_enabled_true_when_any_type_opts_in(monkeypatch):
    monkeypatch.setattr(config, "AI_ALERTS_ENABLED", False)
    assert profile_config.any_ai_alerts_enabled(PROFILE) is True  # dog opts in


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

CROP_PROFILE = {
    "defaults": {"crop_padding_pct": 0.3},
    "object_types": {
        "car": {"crop_disabled": True, "crop_frame_offset_pct": 0.9, "frigate_snapshot_enabled": False},
        "person": {},
    },
}


def test_crop_disabled_uses_type_override_and_falls_back_to_global(monkeypatch):
    monkeypatch.setattr(config, "CROP_DISABLED", False)
    assert profile_config.crop_disabled(CROP_PROFILE, "car") is True
    assert profile_config.crop_disabled(CROP_PROFILE, "person") is False
    assert profile_config.crop_disabled(None, "car") is False


def test_crop_frame_offset_pct_uses_type_override_and_falls_back_to_global(monkeypatch):
    monkeypatch.setattr(config, "CROP_FRAME_OFFSET_PCT", 0.5)
    assert profile_config.crop_frame_offset_pct(CROP_PROFILE, "car") == 0.9
    assert profile_config.crop_frame_offset_pct(CROP_PROFILE, "person") == 0.5


def test_crop_padding_pct_uses_defaults_section_when_no_type_override(monkeypatch):
    monkeypatch.setattr(config, "CROP_PADDING_PCT", 0.2)
    # Neither "car" nor "person" sets crop_padding_pct of their own -- both inherit the `defaults`
    # section's 0.3, not the global 0.2.
    assert profile_config.crop_padding_pct(CROP_PROFILE, "car") == 0.3
    assert profile_config.crop_padding_pct(CROP_PROFILE, "person") == 0.3
    assert profile_config.crop_padding_pct(None, "car") == 0.2


def test_frigate_snapshot_enabled_uses_type_override_and_falls_back_to_global(monkeypatch):
    monkeypatch.setattr(config, "FRIGATE_SNAPSHOT_ENABLED", True)
    assert profile_config.frigate_snapshot_enabled(CROP_PROFILE, "car") is False
    assert profile_config.frigate_snapshot_enabled(CROP_PROFILE, "person") is True


def test_visit_preview_frame_percentages_uses_type_override_and_falls_back_to_global(monkeypatch):
    monkeypatch.setattr(config, "VISIT_PREVIEW_FRAME_PERCENTAGES", [0, 25, 50, 100])
    profile = {"object_types": {"car": {"visit_preview_frame_percentages": [5, 35, 65, 90]}}}
    assert profile_config.visit_preview_frame_percentages(profile, "car") == [5, 35, 65, 90]
    assert profile_config.visit_preview_frame_percentages(profile, "person") == [0, 25, 50, 100]


# ---- store_video / store_video_alerts / visit_thumb_crop_enabled claim filters ----
#
# These gate a whole poll thread (main.py) *and* narrow a claim query (claim_video_batch/
# claim_visit_video_batch/claim_visit_thumb_crop_batch) -- unlike the AI-stage flags, they apply to
# any Frigate label by default, so the filter must be an include-or-exclude split, never a plain
# include-list checked against every "known" label (see profile_config.py's own docstring).

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


def test_store_video_alerts_and_visit_thumb_crop_claim_filters_use_their_own_keys(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_ALERTS", False)
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_ENABLED", True)
    profile = {
        "object_types": {
            "car": {"store_video_alerts": True},
            "dog": {"visit_thumb_crop_enabled": False},
        },
    }
    assert profile_config.store_video_alerts_claim_filter(profile) == (["car"], None)
    assert profile_config.visit_thumb_crop_claim_filter(profile) == (None, ["dog"])


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


def test_any_store_video_alerts_and_visit_thumb_crop_enabled(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_ALERTS", False)
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_ENABLED", False)
    assert profile_config.any_store_video_alerts_enabled({"object_types": {"car": {"store_video_alerts": True}}}) is True
    assert profile_config.any_visit_thumb_crop_enabled({"object_types": {"car": {}}}) is False
