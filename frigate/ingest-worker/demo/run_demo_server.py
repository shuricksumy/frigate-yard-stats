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

if __name__ == "__main__":
    uvicorn.run(api.app, host="127.0.0.1", port=8911)
