"""Unit tests for config.apply_profile_defaults() -- the once-at-startup resolution of plain
technical tuning knobs (queue parallel limits, retry counts, timeouts, retention schedule) from
profiles.yaml's `defaults:` section. Unlike profile_config.py's per-object-type resolvers, these
settings have no per-row/per-type meaning, so they're applied once as module-level constant
overrides rather than looked up on every call. Pure unit tests, no DB/network required.
"""
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import config  # noqa: E402


def _reset(monkeypatch):
    # apply_profile_defaults mutates config.py's module attributes directly -- restore each one
    # to its current value afterward so tests don't leak state into each other/other test files
    # that import config once and reuse it (module caching).
    for attr in config._PROFILE_DEFAULTS_MAP:
        monkeypatch.setattr(config, attr, getattr(config, attr))


def test_apply_profile_defaults_overrides_from_defaults_section(monkeypatch):
    _reset(monkeypatch)
    config.apply_profile_defaults({"defaults": {
        "parallel_limit": 9,
        "retention_months": 3,
        "video_max_age_hours": 12.5,
        "ai_stage_default_timeout_seconds": 300,
    }})
    assert config.PARALLEL_LIMIT == 9
    assert config.RETENTION_MONTHS == 3
    assert config.VIDEO_MAX_AGE_HOURS == 12.5
    assert config.AI_STAGE_DEFAULT_TIMEOUT_SECONDS == 300


def test_apply_profile_defaults_leaves_unset_keys_at_hardcoded_default(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(config, "STALE_MINUTES", 5)
    config.apply_profile_defaults({"defaults": {"parallel_limit": 9}})
    assert config.PARALLEL_LIMIT == 9
    assert config.STALE_MINUTES == 5  # untouched -- not present in defaults:


def test_apply_profile_defaults_tolerates_none_profile(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(config, "PARALLEL_LIMIT", 2)
    config.apply_profile_defaults(None)
    assert config.PARALLEL_LIMIT == 2


def test_apply_profile_defaults_tolerates_missing_defaults_section(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(config, "PARALLEL_LIMIT", 2)
    config.apply_profile_defaults({"object_types": {"car": {}}})
    assert config.PARALLEL_LIMIT == 2


def test_apply_profile_defaults_ignores_object_types_level_values(monkeypatch):
    # These technical knobs have no per-object-type meaning -- a value set under object_types.
    # <label> (rather than defaults:) must never apply, even if it happens to use the same key
    # name a per-type setting might.
    _reset(monkeypatch)
    monkeypatch.setattr(config, "PARALLEL_LIMIT", 2)
    config.apply_profile_defaults({"object_types": {"car": {"parallel_limit": 99}}})
    assert config.PARALLEL_LIMIT == 2


def test_profile_defaults_map_keys_are_real_config_attributes():
    # Every mapped constant name must actually exist on the config module -- catches a typo'd
    # attribute name in _PROFILE_DEFAULTS_MAP that would otherwise silently no-op via setattr
    # creating a new, never-read attribute instead of overriding the real one.
    for attr in config._PROFILE_DEFAULTS_MAP:
        assert hasattr(config, attr), f"config.{attr} does not exist"
