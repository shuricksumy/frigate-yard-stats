import os


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


MQTT_HOST = _env("MQTT_HOST")
MQTT_PORT = int(_env("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD")
MQTT_TOPIC = _env("MQTT_TOPIC", "frigate/events")

POSTGRES_HOST = _env("POSTGRES_HOST", "postgres-projects")
POSTGRES_PORT = int(_env("POSTGRES_PORT", "5432"))
POSTGRES_DB = _env("POSTGRES_DB", "home_automation")
POSTGRES_USER = _env("POSTGRES_USER", "n8n_projects")
POSTGRES_PASSWORD = _env("POSTGRES_PASSWORD")

FRIGATE_API_BASE = _env("FRIGATE_API_BASE")  # e.g. http://<frigate-host-ip>:5000

# Full-res record-stream resolution -- confirmed via `ffmpeg -i rtsp://127.0.0.1:8554/out`
# on the Frigate host (both outside/outside2 record streams are 3840x2160).
RECORD_WIDTH = int(_env("RECORD_WIDTH", "3840"))
RECORD_HEIGHT = int(_env("RECORD_HEIGHT", "2160"))

PARALLEL_LIMIT = int(_env("PARALLEL_LIMIT", "2"))
STALE_MINUTES = int(_env("STALE_MINUTES", "5"))
MAX_ATTEMPTS = int(_env("MAX_ATTEMPTS", "3"))
MAX_CROP_DIMENSION = int(_env("MAX_CROP_DIMENSION", "1280"))
CROP_PADDING_PCT = float(_env("CROP_PADDING_PCT", "0.2"))
# A second, much smaller copy of the same crop -- for report/preview UIs that would otherwise
# embed the full MAX_CROP_DIMENSION image inline per row (multiplied across every sighting, that's
# what blew up a 2-hour daily report to 42MB and pushed up n8n's memory while building/emailing
# it). VLM calls still use crop_image_base64 at full size; only reporting uses this one.
THUMBNAIL_MAX_DIMENSION = int(_env("THUMBNAIL_MAX_DIMENSION", "240"))
POLL_INTERVAL_SECONDS = float(_env("POLL_INTERVAL_SECONDS", "5"))

# How long to keep data before retention-cleanup deletes it (matches the default that used to
# live only in n8n's retention-cleanup.json -- that workflow is now superseded by this service).
RETENTION_MONTHS = int(_env("RETENTION_MONTHS", "12"))
# Retention is a DELETE sweep across the whole table, not a per-event check -- run it on a much
# slower cadence than the crop poll loop, not every POLL_INTERVAL_SECONDS.
RETENTION_CHECK_INTERVAL_SECONDS = float(_env("RETENTION_CHECK_INTERVAL_SECONDS", "86400"))

# Path to the schema file this service applies on every startup (idempotent -- CREATE ... IF NOT
# EXISTS throughout), so a brand new Postgres instance is ready with no manual `psql -f` step.
SCHEMA_SQL_PATH = _env("SCHEMA_SQL_PATH", "/app/schema.sql")

# Admin/test API (health, status, manual crop/retention trigger) -- Swagger UI at /docs.
API_PORT = int(_env("API_PORT", "8080"))

# Required on the read/query/report endpoints (X-API-Key header) -- NOT on /health, /status,
# /crop/{id}, /retention/run, which stay as the existing unauthenticated internal admin surface.
API_KEY = _env("API_KEY")

# -------------------------------------------------
# Video storage (third queue stage: video_status) -- see video.py / video_worker.py.
# -------------------------------------------------
STORE_VIDEO = _env("STORE_VIDEO", "false").lower() == "true"
# Mount point inside the container -- pair with a bind mount in docker-compose.yml
# (VIDEO_STORAGE_HOST_PATH on the host side). Files are laid out as
# {VIDEO_STORAGE_PATH}/{YYYY}/{MM}/{DD}/{object_type}-{event_id}-{start_ts_epoch}.mp4.
VIDEO_STORAGE_PATH = _env("VIDEO_STORAGE_PATH", "/data/video")
# How many rows may be video_status='processing' at once -- kept separate from (and by default
# lower than) PARALLEL_LIMIT so the video stage doesn't compete with the crop stage for Frigate's
# API/bandwidth.
VIDEO_PARALLEL_LIMIT = int(_env("VIDEO_PARALLEL_LIMIT", "1"))
# Frigate is still finalizing the recording segment when the "end" event fires -- wait this long
# before the *first* download attempt on a freshly claimed row (mirrors the n8n workflow's
# "Wait 10s" node ahead of "Download Clip").
VIDEO_INITIAL_WAIT_SECONDS = float(_env("VIDEO_INITIAL_WAIT_SECONDS", "10"))
# A response body at/below this size is treated as Frigate's "not ready yet" placeholder, not a
# real clip -- same >1000-byte check as the n8n workflow's "Check Clip Size" node.
VIDEO_MIN_VALID_BYTES = int(_env("VIDEO_MIN_VALID_BYTES", "1000"))
VIDEO_MAX_ATTEMPTS = int(_env("VIDEO_MAX_ATTEMPTS", "5"))
VIDEO_RETRY_WAIT_SECONDS = float(_env("VIDEO_RETRY_WAIT_SECONDS", "5"))

# -------------------------------------------------
# Telegram notifications -- see telegram.py. Disabled (no-op) unless explicitly turned on.
# -------------------------------------------------
TELEGRAM_ENABLED = _env("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
