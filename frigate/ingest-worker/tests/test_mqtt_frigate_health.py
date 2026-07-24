"""Tests for mqtt_ingest.py's frigate/stats + frigate/available handling -- Frigate's own system
health heartbeat, kept in memory only (never persisted to Postgres, since there's no historical
value in "what was Frigate's CPU usage 3 days ago" the way there is for raw_events/visits) and
surfaced via GET /status for the admin dashboard's "Frigate health" panel. Unit tests only
(no MQTT broker, no Postgres) -- exercises the parsing/summarizing/state-update logic directly.
"""
import json
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import mqtt_ingest  # noqa: E402


class _Msg:
    def __init__(self, payload: bytes):
        self.payload = payload


# A trimmed-down but structurally real frigate/stats payload -- confirmed live against production
# to have this shape (per-camera fps/detection_fps, detectors.<name>.inference_speed, a
# cpu_usages.frigate.full_system entry alongside many other per-PID entries we don't care about,
# and vendor-specific gpu_usages keys).
_RAW_STATS = {
    "cameras": {
        "outside": {"camera_fps": 5.0, "detection_fps": 13.9, "detection_enabled": True},
        "outside2": {"camera_fps": 5.0, "detection_fps": 15.8, "detection_enabled": True},
    },
    "detectors": {"coral": {"inference_speed": 20.81, "detection_start": 123.456, "pid": 753}},
    "cpu_usages": {
        "frigate.full_system": {"cpu": "42.1", "mem": "74.9"},
        "32": {"cpu": "0.0", "cpu_average": "0", "mem": "0.0", "cmdline": "s6-supervise frigate"},
    },
    "gpu_usages": {"amd-vaapi": {"gpu": "98.33%", "mem": "102.92%"}},
}


def test_summarize_stats_extracts_relevant_fields():
    summary = mqtt_ingest.summarize_stats(_RAW_STATS)
    assert summary["cameras"] == {
        "outside": {"camera_fps": 5.0, "detection_fps": 13.9, "detection_enabled": True},
        "outside2": {"camera_fps": 5.0, "detection_fps": 15.8, "detection_enabled": True},
    }
    assert summary["detectors"] == {"coral": {"inference_speed": 20.81}}
    assert summary["cpu_percent"] == "42.1"
    assert summary["mem_percent"] == "74.9"
    assert summary["gpu_usages"] == {"amd-vaapi": {"gpu": "98.33%", "mem": "102.92%"}}


def test_summarize_stats_drops_per_process_cpu_usage_noise():
    # The per-PID s6-supervise/nginx/go2rtc entries are irrelevant noise -- only
    # cpu_usages.frigate.full_system should surface, flattened into cpu_percent/mem_percent.
    summary = mqtt_ingest.summarize_stats(_RAW_STATS)
    assert "32" not in json.dumps(summary)
    assert "cmdline" not in json.dumps(summary)


def test_summarize_stats_handles_missing_sections():
    assert mqtt_ingest.summarize_stats({}) == {
        "cameras": {}, "detectors": {}, "cpu_percent": None, "mem_percent": None, "gpu_usages": {},
    }


def test_handle_stats_message_updates_latest_stats(monkeypatch):
    monkeypatch.setattr(mqtt_ingest, "_latest_stats", None)
    mqtt_ingest._handle_stats_message(_Msg(json.dumps(_RAW_STATS).encode()))
    health = mqtt_ingest.get_frigate_health()
    assert health["stats"]["cameras"]["outside"]["detection_fps"] == 13.9


def test_handle_stats_message_malformed_payload_does_not_raise(monkeypatch):
    monkeypatch.setattr(mqtt_ingest, "_latest_stats", None)
    mqtt_ingest._handle_stats_message(_Msg(b"not json"))
    assert mqtt_ingest.get_frigate_health()["stats"] is None


def test_handle_available_message_online(monkeypatch):
    monkeypatch.setattr(mqtt_ingest, "_frigate_available", None)
    mqtt_ingest._handle_available_message(_Msg(b"online"))
    assert mqtt_ingest.get_frigate_health()["available"] is True


def test_handle_available_message_offline(monkeypatch):
    monkeypatch.setattr(mqtt_ingest, "_frigate_available", None)
    mqtt_ingest._handle_available_message(_Msg(b"offline"))
    assert mqtt_ingest.get_frigate_health()["available"] is False
