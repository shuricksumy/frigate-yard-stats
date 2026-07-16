import logging
import threading

import uvicorn

import alert_video_worker
import config
import crop_worker
import db
import mqtt_ingest
import video_worker
from api import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main():
    db.ensure_schema()
    mqtt_ingest.start()
    # The pipeline itself (MQTT ingest + crop poll loop) runs regardless of the API -- it's a
    # background thread so uvicorn can own the main thread for the admin/test API below.
    threading.Thread(target=crop_worker.run_forever, name="crop_worker", daemon=True).start()
    # Only spins up a thread at all when video storage is turned on -- no polling overhead when
    # STORE_VIDEO=false, rather than a thread that runs and no-ops forever.
    if config.STORE_VIDEO:
        threading.Thread(target=video_worker.run_forever, name="video_worker", daemon=True).start()
    # Independent switch, independent thread -- STORE_VIDEO_ALERTS can be on/off regardless of
    # STORE_VIDEO, so the two flows can be A/B'd separately.
    if config.STORE_VIDEO_ALERTS:
        threading.Thread(target=alert_video_worker.run_forever, name="alert_video_worker", daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=config.API_PORT)


if __name__ == "__main__":
    main()
