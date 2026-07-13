import logging
import threading

import uvicorn

import config
import crop_worker
import db
import mqtt_ingest
from api import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main():
    db.ensure_schema()
    mqtt_ingest.start()
    # The pipeline itself (MQTT ingest + crop poll loop) runs regardless of the API -- it's a
    # background thread so uvicorn can own the main thread for the admin/test API below.
    threading.Thread(target=crop_worker.run_forever, name="crop_worker", daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=config.API_PORT)


if __name__ == "__main__":
    main()
