import logging
import threading

import uvicorn

import ai_worker
import alert_ai_worker
import alert_video_worker
import config
import crop_worker
import db
import mqtt_ingest
import profile_config
import video_worker
import visit_thumb_worker
from api import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main():
    db.ensure_schema()
    # Loaded once here and threaded through to every worker that needs to resolve a per-object-type
    # Telegram mode or AI-stage enable flag (profile_config.py) -- avoids each thread re-reading
    # profiles.yaml off disk independently.
    profile = ai_worker.load_profile(config.AI_STAGE_PROFILE_PATH)
    # Applies profiles.yaml's `defaults:` section to config.py's technical-tuning constants (queue
    # parallel limits, retry counts, timeouts, retention schedule -- see config.py's own comment)
    # once, before any worker starts -- these have no per-object-type meaning, unlike the settings
    # profile_config.py resolves per-call.
    config.apply_profile_defaults(profile)
    mqtt_ingest.start(profile)
    # The pipeline itself (MQTT ingest + crop poll loop) runs regardless of the API -- it's a
    # background thread so uvicorn can own the main thread for the admin/test API below.
    threading.Thread(target=crop_worker.run_forever, args=(profile,), name="crop_worker", daemon=True).start()
    # Only spins up a thread at all when video storage is turned on for at least one object type --
    # no polling overhead when nothing needs it, rather than a thread that runs and no-ops forever.
    # Checks profile_config.any_store_video_enabled rather than the bare global flag -- a
    # profiles.yaml per-type override can start this thread even when the global default is off.
    if profile_config.any_store_video_enabled(profile):
        threading.Thread(target=video_worker.run_forever, args=(profile,), name="video_worker", daemon=True).start()
    # Independent switch, independent thread -- STORE_VIDEO_ALERTS can be on/off regardless of
    # STORE_VIDEO, so the two flows can be A/B'd separately. Same per-type-override-can-start-it
    # check as above.
    if profile_config.any_store_video_alerts_enabled(profile):
        threading.Thread(target=alert_video_worker.run_forever, args=(profile,), name="alert_video_worker", daemon=True).start()
    # Independent switch, independent thread -- same reasoning as STORE_VIDEO_ALERTS above.
    if profile_config.any_visit_thumb_crop_enabled(profile):
        threading.Thread(target=visit_thumb_worker.run_forever, args=(profile,), name="visit_thumb_worker", daemon=True).start()
    # Alternative to n8n/metadata-processor.json (see CLAUDE.md) -- off by default so a fresh
    # deploy still needs n8n for the AI stage until this is deliberately opted into. Checks
    # profile_config.any_ai_events_stage_enabled rather than the bare global flag -- a profiles.yaml
    # per-type override can start this thread even when the global default is off, as long as at
    # least one object type opts in.
    if profile_config.any_ai_events_stage_enabled(profile):
        threading.Thread(target=ai_worker.run_forever, args=(profile,), name="ai_worker", daemon=True).start()
    # Independent switch, independent thread, independent queue (visits.alert_ai_status) -- can
    # run alongside or instead of AI_EVENTS_STAGE_ENABLED, same "A/B independently" precedent as
    # STORE_VIDEO_ALERTS/TELEGRAM_ALERTS_MODE. Same per-type-override-can-start-it-anyway check.
    if profile_config.any_ai_alerts_enabled(profile):
        threading.Thread(target=alert_ai_worker.run_forever, args=(profile,), name="alert_ai_worker", daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=config.API_PORT)


if __name__ == "__main__":
    main()
