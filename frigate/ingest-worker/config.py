import os
import sys


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
# Frigate's review/alert stream -- separate from frigate/events, already groups multiple det_ids
# into one segment (Frigate's own tracker re-ID/occlusion handling) via a severity classification
# driven by frigate.conf's review.alerts/detections config. Used to populate the (previously
# unpopulated) visits table / raw_events.visit_id, not to filter or replace anything in the
# crop/video/ai queue pipeline -- see mqtt_ingest.py's review handler / db.record_visit.
MQTT_REVIEWS_TOPIC = _env("MQTT_REVIEWS_TOPIC", "frigate/reviews")

# Optional camera allow-list, comma-separated Frigate camera names (e.g. "outside,outside2") --
# applies to both the events flow (frigate/events) and the alerts flow (frigate/reviews), gating
# at ingest time in mqtt_ingest.py so a camera not on the list never gets a raw_events/visits row
# at all, not just hidden from some later view. Empty/unset (the default) means no filter -- every
# camera Frigate reports is processed, today's exact behavior.
CAMERAS = [c.strip() for c in _env("CAMERAS", "").split(",") if c.strip()]

POSTGRES_HOST = _env("POSTGRES_HOST", "postgres-projects")
POSTGRES_PORT = int(_env("POSTGRES_PORT", "5432"))
POSTGRES_DB = _env("POSTGRES_DB", "home_automation")
POSTGRES_USER = _env("POSTGRES_USER", "n8n_projects")
POSTGRES_PASSWORD = _env("POSTGRES_PASSWORD")

FRIGATE_API_BASE = _env("FRIGATE_API_BASE")  # e.g. http://<frigate-host-ip>:5000

# Full-res record-stream resolution -- confirmed via `ffmpeg -i rtsp://127.0.0.1:8554/out`
# on the Frigate host (both outside/outside2 record streams are 3840x2160). Stays a plain env var
# (unlike the queue-tuning/technical settings below) -- this describes your camera hardware's
# actual resolution, not a tunable behavior knob.
RECORD_WIDTH = int(_env("RECORD_WIDTH", "3840"))
RECORD_HEIGHT = int(_env("RECORD_HEIGHT", "2160"))

# -------------------------------------------------
# Everything from here down to EMBEDDING_DIMENSIONS (excluding paths/tokens/URLs/ports, which stay
# plain env vars) is a technical tuning knob -- queue parallel limits, retry counts, timeouts,
# retention schedule, image-size caps -- with no per-object-type meaning (there's no "PARALLEL_LIMIT
# for cars only"). These are deliberately NOT env vars: configure them via profiles.yaml's
# `defaults:` section instead (apply_profile_defaults() below applies it once at startup, before
# any worker thread starts -- see main.py). The literals assigned here are only the last-resort
# fallback for a deployment that never sets a `defaults:` value at all -- matches this project's
# original behavior, so an empty/missing `defaults:` section is a perfectly valid, fully-working
# configuration.
# -------------------------------------------------
PARALLEL_LIMIT = 2
STALE_MINUTES = 5
MAX_ATTEMPTS = 3
# Frigate is still finalizing the event/clip when the "end" MQTT message fires -- wait this long
# before the *first* crop attempt on a freshly claimed row (mirrors VIDEO_INITIAL_WAIT_SECONDS;
# confirmed in production that a short event's crop can genuinely fail this way, not just long
# events tripping the clip-duration fallback in crop.py).
CROP_INITIAL_WAIT_SECONDS = 5.0
MAX_CROP_DIMENSION = 1280
# -------------------------------------------------
# CROP_PADDING_PCT/CROP_DISABLED/CROP_FRAME_OFFSET_PCT/FRIGATE_SNAPSHOT_ENABLED below are instead
# per-object-type settings (a car needs different crop framing than a person) -- see
# profile_config.py's resolvers (crop_disabled/crop_frame_offset_pct/crop_padding_pct/
# frigate_snapshot_enabled), which check a type's own object_types.<label> entry before falling
# back to `defaults:`. The literals below are only the last-resort fallback once neither of those
# sets a value either.
# -------------------------------------------------
CROP_PADDING_PCT = 0.2
# Off by default -- the crop exists specifically so the VLM can read small detail (plates, notable
# features) that's illegible in a full wide frame; only turn this on if you've decided that
# trade-off is worth it. Affects both what the web UI displays and what gets analyzed, since both
# read the same stored crop_image_base64.
CROP_DISABLED = False
# Where in the event's start_ts->end_ts span to seek for the crop frame (0.0=start, 0.5=midpoint,
# 1.0=end) -- only applies when FRIGATE_SNAPSHOT_ENABLED (per type) is false. Frigate picks its own
# alert thumbnail at whatever frame scored highest during the event, which isn't a fixed offset and
# isn't exposed via its API (confirmed live: one event's own snapshot landed almost exactly at
# start, another well past the midpoint) -- there's no universal value that matches Frigate's
# per-event choice, so this stays a tunable rather than a guessed new default.
CROP_FRAME_OFFSET_PCT = 0.5
# True: uses Frigate's own already-rendered event snapshot (GET /api/events/{det_id}/snapshot.jpg)
# instead of seeking+cropping a frame from the record-stream clip ourselves -- Frigate picks this
# frame by its own best-detection-score judgment (content-dependent, not a fixed offset guess like
# CROP_FRAME_OFFSET_PCT above), which in practice beats our own fixed-offset seek often enough that
# this is the default. Known trade-off, accepted as the default anyway: it's from the lower-res
# detect stream (800x448 in testing, vs. this setup's 3840x2160 record stream) with a
# bounding-box/label/timestamp overlay burned in that this Frigate version's API gives no way to
# suppress (confirmed empirically -- bbox=0/timestamp=0/h=<n> query params on the snapshot endpoint
# have no effect at all, byte-identical response). Set false (per type, in profiles.yaml) to fall
# back to this project's original seek-based approach if that trade-off doesn't work for your
# footage -- CROP_DISABLED/CROP_FRAME_OFFSET_PCT/CROP_PADDING_PCT above only apply once this is
# false for that type. Events only -- a visit's own composite grid (VISIT_THUMB_CROP_ENABLED) is
# unaffected either way, since a single Frigate snapshot has no "4 frames of change" equivalent to
# offer it.
FRIGATE_SNAPSHOT_ENABLED = True
# A second, much smaller copy of the same crop -- for report/preview UIs that would otherwise
# embed the full MAX_CROP_DIMENSION image inline per row (multiplied across every sighting, that's
# what blew up a 2-hour daily report to 42MB and pushed up n8n's memory while building/emailing
# it). VLM calls still use crop_image_base64 at full size; only reporting uses this one.
THUMBNAIL_MAX_DIMENSION = 240
POLL_INTERVAL_SECONDS = 5.0

# How long to keep data before retention-cleanup deletes it (matches the default that used to
# live only in n8n's retention-cleanup.json -- that workflow is now superseded by this service).
RETENTION_MONTHS = 12
# Retention is a DELETE sweep across the whole table, not a per-event check -- run it on a much
# slower cadence than the crop poll loop, not every POLL_INTERVAL_SECONDS.
RETENTION_CHECK_INTERVAL_SECONDS = 86400.0

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
# STORE_VIDEO/STORE_VIDEO_ALERTS/VISIT_THUMB_CROP_ENABLED/VISIT_PREVIEW_FRAME_PERCENTAGES below are
# deliberately NOT env vars, same reasoning as the crop settings above -- configure them via
# profiles.yaml (a `defaults:` section for a global change, object_types.<label> for one type) --
# see profile_config.py. The literals below are only the last-resort fallback when profiles.yaml
# doesn't set a `defaults:` value for that key either -- matches this project's original defaults.
# -------------------------------------------------
STORE_VIDEO = False
# Mount point inside the container -- pair with a bind mount in docker-compose.yml
# (VIDEO_STORAGE_HOST_PATH on the host side). Files are laid out as
# {VIDEO_STORAGE_PATH}/{YYYY}/{MM}/{DD}/{object_type}-{event_id}-{start_ts_epoch}.mp4.
VIDEO_STORAGE_PATH = _env("VIDEO_STORAGE_PATH", "/data/video")
# How many rows may be video_status='processing' at once -- kept separate from (and by default
# lower than) PARALLEL_LIMIT so the video stage doesn't compete with the crop stage for Frigate's
# API/bandwidth. A technical tuning knob (see the block above PARALLEL_LIMIT) -- configure via
# profiles.yaml's `defaults:`, not an env var.
VIDEO_PARALLEL_LIMIT = 1
# Frigate is still finalizing the recording segment when the "end" event fires -- wait this long
# before the *first* download attempt on a freshly claimed row (mirrors the n8n workflow's
# "Wait 10s" node ahead of "Download Clip").
VIDEO_INITIAL_WAIT_SECONDS = 10.0
# A response body at/below this size is treated as Frigate's "not ready yet" placeholder, not a
# real clip -- same >1000-byte check as the n8n workflow's "Check Clip Size" node.
VIDEO_MIN_VALID_BYTES = 1000
VIDEO_MAX_ATTEMPTS = 5
VIDEO_RETRY_WAIT_SECONDS = 5.0
# If set, never claim rows older than this many hours -- same throughput safety valve as
# /ai-queue/claim's max_age_hours, applied here since the video stage's clip source (Frigate's
# continuous-recording buffer, a much shorter retention window than the event-scoped clip crop.py
# reads from) can roll a clip off before a backlogged worker ever gets to it -- confirmed in
# production a clip was already gone ~36 minutes after the event. Past this cutoff a row just
# stays video_status='new'/'retry' indefinitely rather than burning attempts on a clip that's
# very likely already gone. None (the fallback default) means no age limit.
VIDEO_MAX_AGE_HOURS = None

# Independent video-storage switch for the alerts/visits flow (frigate/reviews) -- separate from
# STORE_VIDEO above, which only ever gates the events flow (frigate/events, per-raw_event clips).
# Both flows share the same VIDEO_PARALLEL_LIMIT/VIDEO_INITIAL_WAIT_SECONDS/VIDEO_MIN_VALID_BYTES/
# VIDEO_MAX_ATTEMPTS/VIDEO_RETRY_WAIT_SECONDS/VIDEO_MAX_AGE_HOURS tuning above (mechanically
# identical download/validation logic, just against visits instead of raw_events) -- only the
# on/off switch is separate, so you can toggle each flow independently without doubling every
# tuning knob. See alert_video_worker.py.
STORE_VIDEO_ALERTS = False
# A genuinely separate storage location from VIDEO_STORAGE_PATH (own mount point, own bind mount
# in docker-compose.yml via VIDEO_STORAGE_ALERTS_HOST_PATH) rather than a subfolder of it -- lets
# the two flows' disk usage/retention be measured and managed independently, e.g. pointing alerts
# clips at different storage entirely. Files are laid out as
# {VIDEO_STORAGE_PATH_ALERTS}/{YYYY}/{MM}/{DD}/visit-{object_type}-{visit_id}-{start_ts_epoch}-
# {start_ts_iso}.mp4 (see video.store_visit_clip).
VIDEO_STORAGE_PATH_ALERTS = _env("VIDEO_STORAGE_PATH_ALERTS", "/data/video-alerts")

# -------------------------------------------------
# Visit thumbnail/preview re-crop (fifth queue stage: visits.thumb_crop_status) -- see
# visit_thumb_worker.py / crop.build_visit_preview. Produces a composite grid image (frames
# sampled proportionally across the visit's own clip, not a single "best moment" seek to Frigate's
# thumb_time -- that turned out unreliable, see build_visit_preview's docstring) plus a separate
# animated GIF for human preview. Only known once the review closes, well after the representative
# event's own crop already ran, so this is a separate poll-loop stage producing separate artifacts
# (visits.crop_image_base64/preview_gif_base64), not a replacement for the events-flow crop.
# -------------------------------------------------
VISIT_THUMB_CROP_ENABLED = False
# Technical tuning knobs (see the block above PARALLEL_LIMIT) -- configure via profiles.yaml's
# `defaults:`, not env vars.
VISIT_THUMB_CROP_PARALLEL_LIMIT = 1
# Same head-start reasoning as CROP_INITIAL_WAIT_SECONDS/VIDEO_INITIAL_WAIT_SECONDS -- Frigate may
# still be finalizing the continuous-recording segment right after the review closes.
VISIT_THUMB_CROP_INITIAL_WAIT_SECONDS = 5.0
VISIT_THUMB_CROP_MAX_ATTEMPTS = 3
VISIT_THUMB_CROP_RETRY_WAIT_SECONDS = 5.0
# Which 4 points of the visit's own clip duration to sample for the preview grid/GIF, as
# percentages (0=clip start, 100=clip end) -- e.g. [5, 35, 65, 90] to stay a bit clear of both
# edges instead of landing exactly on them. Exactly 4 values required -- the grid assembly is a
# fixed 2x2 layout (crop.build_visit_preview), not a variable-count one. Configure via profiles.yaml
# (a real YAML list there, not a comma-separated string) -- this literal is only the fallback.
VISIT_PREVIEW_FRAME_PERCENTAGES = [0.0, 25.0, 50.0, 100.0]

# -------------------------------------------------
# Telegram notifications -- see telegram.py. Each is a mode, not a bool: "none" (off, the
# default), "image" (photo/GIF only, no video clip), "video" (video clip only, no photo/GIF), or
# "all" (both). Splitting photo from video lets you skip uploading large video clips to Telegram
# while still getting the lightweight photo/GIF notification, or the other way around, instead of
# an all-or-nothing switch.
#
# TELEGRAM_EVENTS_MODE and TELEGRAM_ALERTS_MODE below are deliberately NOT env vars -- same
# reasoning as the crop settings above, configure via profiles.yaml (`defaults:` for a global
# change, object_types.<label> to silence/enable just one noisy type) -- see profile_config.py.
# The literal below is only the last-resort fallback ("none", off, matching this project's
# original default) for a deployment that never sets this in profiles.yaml at all.
# -------------------------------------------------
TELEGRAM_EVENTS_MODE = "none"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# Defaults to Telegram's own cloud API -- can be pointed at a self-hosted Local Bot API server
# (github.com/tdlib/telegram-bot-api, the optional telegram-bot-api Compose service/profile in
# docker-compose.yml) instead, for lower latency (LAN/Docker-network hop instead of the public
# internet) and a much higher upload cap (2000MB vs. the cloud API's 50MB, which this project's
# 4K-record-stream video clips -- STORE_VIDEO/STORE_VIDEO_ALERTS -- can realistically exceed).
# Same request shape either way (still POSTs to /bot<token>/<method>), so telegram.py needs no
# other change. Trailing slash stripped so callers can always do f"{base}/bot...".
TELEGRAM_API_BASE_URL = _env("TELEGRAM_API_BASE_URL", "https://api.telegram.org").rstrip("/")
# Independent mode for the alerts/visits flow -- "image" sends the single per-visit summary
# (preview GIF/photo + caption), "video" sends the visit's own stored clip as a reply to that
# summary, fired once when a Frigate review closes. Separate from TELEGRAM_EVENTS_MODE above,
# which gates the existing per-raw_event photo/video notifications -- lets you A/B whether
# per-event or per-visit notifications (or both, or neither) are more useful for your traffic.
TELEGRAM_ALERTS_MODE = "none"

# -------------------------------------------------
# Web report UI -- see static/index.html. Frigate object labels aren't fixed (depends on your
# model/config, e.g. car/truck/person/dog), so the UI's "Type" filter dropdown is populated from
# this list (via GET /object-types) rather than being hardcoded in the HTML -- add a label here
# and it shows up in the dropdown on next restart, no frontend change needed.
# -------------------------------------------------
OBJECT_TYPES = [t.strip() for t in _env("OBJECT_TYPES", "car,truck,person,dog").split(",") if t.strip()]

# -------------------------------------------------
# Internal AI stages (ai_worker.py / alert_ai_worker.py) -- an alternative to
# n8n/metadata-processor.json, not a replacement for it: that workflow is left untouched in the
# repo and can be re-enabled in n8n at any time. Off by default, same convention as
# STORE_VIDEO/VISIT_THUMB_CROP_ENABLED above.
#
# Two independent stages, each with its own enable flag, queue, and prompt (profiles.yaml's
# event_prompt vs alert_prompt) -- same "independent switch, shared tuning knobs" split this
# project already uses for STORE_VIDEO vs STORE_VIDEO_ALERTS:
#   AI_EVENTS_STAGE_ENABLED -- ai_worker.py, analyzes each raw_event's own single-frame crop with
#     event_prompt. When on, this thread claims the exact same ai_status='new'/'retry' rows
#     metadata-processor.json's "Claim Next Batch (API)" node does (via db.claim_ai_batch directly,
#     not over HTTP), so the two must not run against the same queue at the same time. This is the
#     renamed former AI_STAGE_ENABLED -- was previously the only stage; splitting it out clarifies
#     that it only ever analyzed one event's own crop, never a visit's composite grid, regardless
#     of VISIT_THUMB_CROP_ENABLED (a real gap: the grid was being built but never actually claimed
#     or analyzed by this stage).
#   AI_ALERTS_ENABLED -- alert_ai_worker.py, analyzes a visit's own composite grid
#     (visits.crop_image_base64, built once thumb_crop_status='done') with alert_prompt, storing
#     the result in visit_sightings -- a separate table from (and independent of) the events
#     stage's own sightings table. Requires VISIT_THUMB_CROP_ENABLED to actually have anything to
#     claim; if that's off, this stage just has nothing ready and stays idle, the same graceful
#     no-op every other stage/object-type mismatch in this project already gets.
#
# Both flags are deliberately NOT env vars -- configure via profiles.yaml (`defaults:` for a global
# change, object_types.<label> for a type that needs to behave differently from the rest, e.g. opt
# out of the events stage while everything else stays on it, or opt in despite everything else
# being off) -- see profile_config.py. The literals below are only the last-resort fallback (off,
# matching this project's original default) for a deployment that never sets either in
# profiles.yaml at all.
# -------------------------------------------------
AI_EVENTS_STAGE_ENABLED = False
AI_ALERTS_ENABLED = False
# Same idea as SCHEMA_SQL_PATH -- baked into the image by default, bind-mount a different file and
# point this at it to customize prompts/models without a rebuild. Shared by both stages -- each
# reads its own prompt key (event_prompt/alert_prompt) out of the same per-type profile section.
AI_STAGE_PROFILE_PATH = _env("AI_STAGE_PROFILE_PATH", "/app/profiles.yaml")
# Queue-tuning knobs below are shared between both stages (each stage claims from its own separate
# queue -- raw_events.ai_status vs visits.alert_ai_status -- so sharing these doesn't mean they
# compete for the same capacity). Technical tuning knobs (see the block above PARALLEL_LIMIT) --
# configure via profiles.yaml's `defaults:`, not env vars.
AI_STAGE_PARALLEL_LIMIT = 2
AI_STAGE_STALE_MINUTES = 5
AI_STAGE_MAX_ATTEMPTS = 3
# Optional throughput safety valve, same purpose as VIDEO_MAX_AGE_HOURS -- None (the fallback
# default) means no cutoff, every eligible row is still claimable regardless of age.
AI_STAGE_MAX_AGE_HOURS = None
AI_STAGE_POLL_INTERVAL_SECONDS = 5.0
# llama_slot_proxy's own base URL, called directly instead of going through n8n -- e.g.
# http://llama-proxy-host:port. Only required when AI_EVENTS_STAGE_ENABLED or AI_ALERTS_ENABLED is
# true.
LLAMA_PROXY_BASE_URL = _env("LLAMA_PROXY_BASE_URL", "").rstrip("/")
# Optional -- llama_slot_proxy is unauthenticated on the LAN today (same as every VLM call n8n
# makes directly), so this is future-proofing rather than a hard requirement. Blank means no
# Authorization header is sent at all.
LLAMA_PROXY_TOKEN = _env("LLAMA_PROXY_TOKEN", "")
LLAMA_PROXY_EMBED_PATH = _env("LLAMA_PROXY_EMBED_PATH", "/REPLACE_WITH_EMBED_SLOT/v1/embeddings")
# Fallback chat-completion timeout (seconds) for a profiles.yaml entry that doesn't set its own
# timeout_seconds -- a local model's response time genuinely varies by prompt/model, so the real
# per-call value lives in the profile, not here (see profiles.yaml's own comment). A technical
# tuning knob (see the block above PARALLEL_LIMIT) -- configure via profiles.yaml's `defaults:`.
AI_STAGE_DEFAULT_TIMEOUT_SECONDS = 180.0
# Separate, shorter default -- a single small forward pass, not autoregressive generation like a
# chat completion, so it's normally much faster regardless of which chat model/prompt was used.
AI_STAGE_EMBED_TIMEOUT_SECONDS = 60.0
# Must match the output size of whatever model is loaded behind LLAMA_PROXY_EMBED_PATH (e.g. 1024
# for Qwen3-Embedding-0.6B-GGUF, 768 for nomic-embed-text-v1.5) -- db.ensure_schema() sizes the
# pgvector embedding columns off this value, and db._vector_literal validates against it before
# every insert. Switching embedding models means changing this AND re-running
# POST /embeddings/backfill?confirm=true for every sighting -- a different model's vectors live in
# an incomparable vector space, so old embeddings can't just be kept around at the new dimension.
# Stays a plain env var (unlike the technical tuning knobs above) -- db.ensure_schema() reads this
# before profiles.yaml is even loaded (main.py applies schema before loading the profile), and
# changing it has real DB-migration implications (a backfill), unlike a queue timeout/retry count.
EMBEDDING_DIMENSIONS = int(_env("EMBEDDING_DIMENSIONS", "1024"))

# Maps this module's technical-tuning constants (see the block above PARALLEL_LIMIT) to their
# profiles.yaml `defaults:` key -- used by apply_profile_defaults() below. Deliberately excludes
# every per-object-type-resolvable setting (crop_disabled, store_video, telegram_events_mode,
# etc.) -- those are resolved fresh per-call by profile_config.py instead, since they can vary by
# Frigate object type; the settings below apply once, globally, with no such per-type meaning.
_PROFILE_DEFAULTS_MAP = {
    "PARALLEL_LIMIT": "parallel_limit",
    "STALE_MINUTES": "stale_minutes",
    "MAX_ATTEMPTS": "max_attempts",
    "CROP_INITIAL_WAIT_SECONDS": "crop_initial_wait_seconds",
    "MAX_CROP_DIMENSION": "max_crop_dimension",
    "THUMBNAIL_MAX_DIMENSION": "thumbnail_max_dimension",
    "POLL_INTERVAL_SECONDS": "poll_interval_seconds",
    "RETENTION_MONTHS": "retention_months",
    "RETENTION_CHECK_INTERVAL_SECONDS": "retention_check_interval_seconds",
    "VIDEO_PARALLEL_LIMIT": "video_parallel_limit",
    "VIDEO_INITIAL_WAIT_SECONDS": "video_initial_wait_seconds",
    "VIDEO_MIN_VALID_BYTES": "video_min_valid_bytes",
    "VIDEO_MAX_ATTEMPTS": "video_max_attempts",
    "VIDEO_RETRY_WAIT_SECONDS": "video_retry_wait_seconds",
    "VIDEO_MAX_AGE_HOURS": "video_max_age_hours",
    "VISIT_THUMB_CROP_PARALLEL_LIMIT": "visit_thumb_crop_parallel_limit",
    "VISIT_THUMB_CROP_INITIAL_WAIT_SECONDS": "visit_thumb_crop_initial_wait_seconds",
    "VISIT_THUMB_CROP_MAX_ATTEMPTS": "visit_thumb_crop_max_attempts",
    "VISIT_THUMB_CROP_RETRY_WAIT_SECONDS": "visit_thumb_crop_retry_wait_seconds",
    "AI_STAGE_PARALLEL_LIMIT": "ai_stage_parallel_limit",
    "AI_STAGE_STALE_MINUTES": "ai_stage_stale_minutes",
    "AI_STAGE_MAX_ATTEMPTS": "ai_stage_max_attempts",
    "AI_STAGE_MAX_AGE_HOURS": "ai_stage_max_age_hours",
    "AI_STAGE_POLL_INTERVAL_SECONDS": "ai_stage_poll_interval_seconds",
    "AI_STAGE_DEFAULT_TIMEOUT_SECONDS": "ai_stage_default_timeout_seconds",
    "AI_STAGE_EMBED_TIMEOUT_SECONDS": "ai_stage_embed_timeout_seconds",
}


def apply_profile_defaults(profile: dict | None) -> None:
    """Overrides this module's technical-tuning constants from profiles.yaml's `defaults:` section,
    if present. Called once at startup (main.py), before any worker thread starts -- these settings
    have no per-object-type meaning, so unlike profile_config.py's resolvers this is a single global
    resolution done once, not a per-call lookup. Every other read of these constants elsewhere in
    the codebase is a plain `config.SOME_SETTING` module-attribute access (never `from config import
    SOME_SETTING`, which would freeze a stale copy at import time), so overwriting the attribute
    here is enough for the new value to take effect everywhere without threading `profile` through
    every function that uses one of these settings.
    """
    defaults = (profile or {}).get("defaults") or {}
    module = sys.modules[__name__]
    for attr, key in _PROFILE_DEFAULTS_MAP.items():
        if key in defaults:
            setattr(module, attr, defaults[key])
