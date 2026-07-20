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
