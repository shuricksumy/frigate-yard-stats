"""Regression tests for a latent crash: a profiles.yaml section that EXISTS but is empty.

YAML parses a bare `object_types:` / `visit_summary:` line (nothing indented under it) as None,
not {}. `dict.get(key, {})` only falls back to its default when the key is ABSENT -- a
present-but-None value comes back as None, so `profile.get("visit_summary", {}).get("enabled")`
raised AttributeError. main.py's copy of that pattern crashed the container at startup; ai_worker's
crashed the AI stage per row; profile_config's crashed every per-object-type resolver.

Pure unit tests -- no DB, network, or real profiles.yaml needed.
"""
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import config  # noqa: E402
import profile_config  # noqa: E402

# What yaml.safe_load actually returns for:
#   object_types:
#   visit_summary:
#   defaults:
EMPTY_SECTIONS = {"object_types": None, "visit_summary": None, "defaults": None}


def test_object_types_config_normalizes_none_to_empty_dict():
    assert profile_config.object_types_config(EMPTY_SECTIONS) == {}
    assert profile_config.object_types_config(None) == {}
    assert profile_config.object_types_config({}) == {}


def test_type_config_survives_empty_object_types_section():
    assert profile_config._type_config(EMPTY_SECTIONS, "car") == {}


def test_defaults_config_survives_empty_defaults_section():
    assert profile_config._defaults_config(EMPTY_SECTIONS) == {}


def test_per_type_resolvers_fall_back_to_hardcoded_defaults():
    # Every resolver should degrade to config.py's hardcoded fallback, not raise.
    assert profile_config.telegram_events_mode(EMPTY_SECTIONS, "car") == config.TELEGRAM_EVENTS_MODE
    assert profile_config.telegram_alerts_mode(EMPTY_SECTIONS, "car") == config.TELEGRAM_ALERTS_MODE
    assert profile_config.ai_events_stage_enabled(EMPTY_SECTIONS, "car") == config.AI_EVENTS_STAGE_ENABLED
    assert profile_config.store_video_events_enabled(EMPTY_SECTIONS, "car") == config.STORE_VIDEO_EVENTS
    assert profile_config.store_video_alerts_enabled(EMPTY_SECTIONS, "car") == config.STORE_VIDEO_ALERTS
    assert profile_config.store_event_images(EMPTY_SECTIONS, "car") == config.STORE_EVENT_IMAGES
    assert profile_config.ai_image_max_dimension(EMPTY_SECTIONS, "car") == config.MAX_CROP_DIMENSION
    assert profile_config.min_event_duration_seconds(EMPTY_SECTIONS, "car") == 0


def test_claim_filters_survive_empty_object_types_section():
    # These iterate profile["object_types"] directly -- an empty section must read as "no per-type
    # overrides exist", not crash.
    assert profile_config.video_events_claim_filter(EMPTY_SECTIONS) is not None
    assert profile_config.video_alerts_claim_filter(EMPTY_SECTIONS) is not None
    assert profile_config.store_video_events_claim_filter(EMPTY_SECTIONS) is not None
    assert profile_config.store_video_alerts_claim_filter(EMPTY_SECTIONS) is not None
    assert profile_config._bool_override_labels(EMPTY_SECTIONS, "store_video_events") == ([], [])


def test_thread_start_gates_survive_empty_object_types_section():
    # main.py calls these to decide which worker threads to start -- an AttributeError here would
    # crash the container at startup, same class of failure as the visit_summary gate.
    for gate in (
        profile_config.any_ai_events_stage_enabled,
        profile_config.any_video_events_worker_needed,
        profile_config.any_video_alerts_worker_needed,
        profile_config.any_store_video_events_enabled,
        profile_config.any_store_video_alerts_enabled,
    ):
        assert isinstance(gate(EMPTY_SECTIONS), bool)


def test_flag_summary_survives_empty_sections():
    summary = profile_config.flag_summary(EMPTY_SECTIONS, "store_video_events", config.STORE_VIDEO_EVENTS)
    assert summary["value"] == config.STORE_VIDEO_EVENTS
    assert summary["source"] == "hardcoded"
    assert summary["overridden_for"] == []


def test_main_visit_summary_gate_survives_empty_section():
    # The exact expression main.py uses to decide whether to start the visit-summary thread --
    # `.get("visit_summary", {})` here would return None and raise on the chained .get.
    assert ((EMPTY_SECTIONS.get("visit_summary") or {}).get("enabled")) is None


def test_ai_worker_type_lookup_survives_empty_section():
    # The exact expression ai_worker.process_claimed_event uses to resolve a row's type config.
    assert (EMPTY_SECTIONS.get("object_types") or {}).get("car") is None
