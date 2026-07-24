"""Launches the real ingest-worker FastAPI app for the demo recording, with the per-object-type
feature flags (normally only settable via profiles.yaml + main.py's apply_profile_defaults, which
this script doesn't run) forced on directly on the config module so the Admin dashboard's Health
panel reflects what the seeded dataset actually represents."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

config.AI_EVENTS_STAGE_ENABLED = True
config.AI_ALERTS_ENABLED = True
config.STORE_VIDEO = True
config.STORE_VIDEO_ALERTS = True
config.VISIT_THUMB_CROP_ENABLED = True
config.TELEGRAM_EVENTS_MODE = "all"
config.TELEGRAM_ALERTS_MODE = "all"

import uvicorn

import api
import mqtt_ingest

# Simulates a real Frigate MQTT heartbeat for the Admin dashboard's "Frigate health" card -- there's
# no real MQTT broker/Frigate instance in this demo setup, so without this the card would just show
# "unknown (no heartbeat received yet)" and empty stats. mqtt_ingest._handle_stats_message/
# _handle_available_message are plain functions over module state (no real MQTT message object
# needed beyond a .payload attribute), so calling them directly here is enough.
mqtt_ingest._handle_available_message(type("Msg", (), {"payload": b"online"})())
mqtt_ingest._handle_stats_message(type("Msg", (), {"payload": b"""
{
  "cameras": {
    "driveway": {"camera_fps": 5.0, "detection_fps": 14.2, "detection_enabled": true},
    "street": {"camera_fps": 5.0, "detection_fps": 11.8, "detection_enabled": true},
    "backyard": {"camera_fps": 5.0, "detection_fps": 9.6, "detection_enabled": true},
    "front_door": {"camera_fps": 5.0, "detection_fps": 12.4, "detection_enabled": true}
  },
  "detectors": {"coral": {"inference_speed": 19.4}},
  "cpu_usages": {"frigate.full_system": {"cpu": "18.3", "mem": "62.1"}},
  "gpu_usages": {"amd-vaapi": {"gpu": "34.5%", "mem": "41.0%"}}
}
"""})())

if __name__ == "__main__":
    uvicorn.run(api.app, host="127.0.0.1", port=8911)
