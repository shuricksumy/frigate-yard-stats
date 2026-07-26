-- Applied on every ingest-worker startup (idempotent -- CREATE ... IF NOT EXISTS throughout), so a
-- brand new Postgres instance is ready with no manual `psql -f` step. This file was consolidated
-- from its incremental ALTER-based migration history into a single clean baseline (the project's
-- one production instance was reset from scratch at the same time) -- any *future* column/table
-- change should still follow the old idiom (ALTER TABLE ... ADD COLUMN IF NOT EXISTS, added below
-- rather than edited into the CREATE TABLE blocks) so this file stays safe to re-apply against a
-- live, already-populated database.

CREATE SCHEMA IF NOT EXISTS yard_stats;

-- Backs sightings.embedding/visit_sightings.embedding (see below) -- requires the
-- pgvector/pgvector:pg16 Postgres image (see docker-compose.yml's postgres-projects service);
-- plain postgres:16 doesn't ship this extension's .so.
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per Frigate review/alert segment (frigate/reviews MQTT topic) -- groups the raw_events
-- det_ids Frigate's own tracker considers the same real-world activity (occlusion handling,
-- re-ID, label flicker e.g. car -> truck mid-track). Populated by db.record_visit. See CLAUDE.md's
-- "Visit grouping via Frigate's review/alert stream" section for the full picture.
CREATE TABLE IF NOT EXISTS yard_stats.visits (
  id SERIAL PRIMARY KEY,
  zone TEXT,
  objects TEXT,
  start_ts TIMESTAMPTZ NOT NULL,
  end_ts TIMESTAMPTZ NOT NULL,
  cameras TEXT,
  camera_count INTEGER,
  -- Fourth queue stage (STORE_VIDEO_ALERTS) -- one clip per visit's whole start_ts->end_ts span,
  -- independent of whether any of its linked raw_events also has its own per-event video. Same
  -- shape as raw_events.video_status below. See alert_video_worker.py.
  video_status TEXT NOT NULL DEFAULT 'new'
    CHECK (video_status IN ('new', 'processing', 'retry', 'failed', 'done', 'skipped')),
  video_status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  video_attempt_count INTEGER NOT NULL DEFAULT 0,
  -- Only the filesystem path is stored here -- the file itself lives on disk only
  -- (VIDEO_STORAGE_PATH_ALERTS), never in Postgres.
  video_path TEXT,
  -- Durable reply-threading target for the visit's video/summary Telegram messages
  -- (TELEGRAM_ALERTS_MODE) -- same idea as raw_events.telegram_photo_message_id below.
  telegram_photo_message_id BIGINT,
  -- Frigate's own review "best frame" timestamp -- stored for reference only, no longer read by
  -- crop.build_visit_preview (see CLAUDE.md's "Visit preview" section for why that seek-based
  -- approach was abandoned in favor of proportional sampling across the clip's own measured
  -- duration).
  thumb_time DOUBLE PRECISION,
  -- Fifth queue stage (VISIT_THUMB_CROP_ENABLED) -- a composite grid image (4 frames sampled
  -- proportionally across the visit's own clip) plus a separate animated GIF for human preview
  -- only. Separate artifacts from any linked raw_event's own crop_image_base64 -- see
  -- crop.build_visit_preview / visit_thumb_worker.py.
  crop_image_base64 TEXT,
  preview_gif_base64 TEXT,
  thumb_crop_status TEXT NOT NULL DEFAULT 'new'
    CHECK (thumb_crop_status IN ('new', 'processing', 'retry', 'failed', 'done', 'skipped')),
  thumb_crop_status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  thumb_crop_attempt_count INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_visits_zone_ts ON yard_stats.visits (zone, start_ts);
CREATE INDEX IF NOT EXISTS idx_visits_video_status ON yard_stats.visits (video_status);
CREATE INDEX IF NOT EXISTS idx_visits_thumb_crop_status ON yard_stats.visits (thumb_crop_status);

-- DEPRECATED -- the alert AI stage (AI_ALERTS_ENABLED/alert_ai_worker.py) that used to populate
-- these columns was removed entirely: a visit's "alert" is now its own video plus its
-- individually-analyzed connected events (see raw_events.image_path below and CLAUDE.md's "Alert
-- AI stage" section for the full history), not a second gathered-image VLM call. These columns
-- stay in schema.sql, unwritten and unread by any code path, following this project's own
-- established precedent for the earlier visit-preview grid/GIF removal (see
-- visits.crop_image_base64/preview_gif_base64/thumb_crop_status below) -- dropping them is a
-- separate, deferred migration, not bundled with the removal itself.
ALTER TABLE yard_stats.visits ADD COLUMN IF NOT EXISTS alert_ai_status TEXT NOT NULL DEFAULT 'new'
  CHECK (alert_ai_status IN ('new', 'processing', 'retry', 'failed', 'done', 'skipped'));
ALTER TABLE yard_stats.visits ADD COLUMN IF NOT EXISTS alert_ai_status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE yard_stats.visits ADD COLUMN IF NOT EXISTS alert_ai_attempt_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_visits_alert_ai_status ON yard_stats.visits (alert_ai_status);

-- DEPRECATED, same reasoning as alert_ai_status above -- backed STORE_ALERT_IMAGES/
-- alert_images.py, both removed. Superseded by raw_events.image_path (see below), which persists
-- the events stage's own full-resolution crop instead.
ALTER TABLE yard_stats.visits ADD COLUMN IF NOT EXISTS alert_image_paths TEXT;

-- One row per Frigate "end" event, any label (car/truck/person/dog/...). Carries three
-- independent queue state machines -- crop_status/video_status owned directly by ingest-worker,
-- ai_status owned by n8n via ingest-worker's /ai-queue/* API -- see CLAUDE.md's "Architecture"
-- section for the full write-up of who owns which and why. All three share the same shape:
-- new -> processing -> retry/failed -> done, plus 'skipped' for a state a row can start in but
-- never needs to leave (crop_status: has_snapshot=false at ingest time; video_status:
-- STORE_VIDEO=false; ai_status: also has_snapshot=false at ingest time -- a row that can never get
-- a crop can never satisfy claim_ai_batch's crop_status='done' requirement either, so it would
-- otherwise sit at ai_status='new' forever, indistinguishable from a row genuinely waiting on
-- capacity).
CREATE TABLE IF NOT EXISTS yard_stats.raw_events (
  id SERIAL PRIMARY KEY,
  camera TEXT NOT NULL,
  zone TEXT,
  objects TEXT,
  start_ts TIMESTAMPTZ NOT NULL,
  end_ts TIMESTAMPTZ NOT NULL,
  det_id TEXT,
  has_clip BOOLEAN,
  has_snapshot BOOLEAN,
  crop_status TEXT NOT NULL DEFAULT 'new'
    CHECK (crop_status IN ('new', 'processing', 'retry', 'failed', 'done', 'skipped')),
  crop_status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  crop_attempt_count INTEGER NOT NULL DEFAULT 0,
  ai_status TEXT NOT NULL DEFAULT 'new'
    CHECK (ai_status IN ('new', 'processing', 'retry', 'failed', 'done', 'skipped')),
  ai_status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ai_attempt_count INTEGER NOT NULL DEFAULT 0,
  video_status TEXT NOT NULL DEFAULT 'new'
    CHECK (video_status IN ('new', 'processing', 'retry', 'failed', 'done', 'skipped')),
  video_status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  video_attempt_count INTEGER NOT NULL DEFAULT 0,
  -- Only the filesystem path is stored here -- the file itself lives on disk only
  -- (VIDEO_STORAGE_PATH), never in Postgres.
  video_path TEXT,
  -- The exact cropped JPEG (base64) ingest-worker produced for this event -- lives here (not the
  -- sightings tables) since it's produced before AI analysis and is label-agnostic. This is
  -- always a downscale (crop.py's scale_image_base64) of the full-resolution crop crop.crop_event
  -- itself built -- see image_path below for the full-resolution original.
  crop_image_base64 TEXT,
  -- Optional filesystem path to the full-resolution version of the same crop (STORE_EVENT_IMAGES/
  -- store_event_images, see event_images.py) -- only the path lives here, the JPEG bytes live
  -- under EVENT_IMAGES_STORAGE_PATH on disk, never in Postgres. NULL until the crop stage has both
  -- run and had this option enabled for the event's own object type.
  image_path TEXT,
  -- Captured from the same Frigate API fetch used to get the crop region -- the settled/final LPR
  -- read and detection score, not the live MQTT "end" payload's values (sub_label in particular
  -- can resolve after the event first fires). Kept here so n8n's AI stage never calls Frigate's
  -- API itself.
  sub_label TEXT,
  score DOUBLE PRECISION,
  -- Durable equivalent of an in-memory pendingReplies map -- lets the later video Telegram send
  -- reply-thread onto the earlier photo send, even across a service restart.
  telegram_photo_message_id BIGINT,
  -- Links this event to the visits row Frigate's own review/alert stream grouped it into -- set by
  -- db.record_visit once the review closes (not at ingest time, since a review can close well
  -- after the event itself).
  reconciled BOOLEAN NOT NULL DEFAULT false,
  visit_id INTEGER REFERENCES yard_stats.visits(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_raw_events_reconciled ON yard_stats.raw_events (reconciled);
CREATE INDEX IF NOT EXISTS idx_raw_events_zone_ts ON yard_stats.raw_events (zone, start_ts);
CREATE INDEX IF NOT EXISTS idx_raw_events_has_snapshot ON yard_stats.raw_events (has_snapshot);
CREATE INDEX IF NOT EXISTS idx_raw_events_crop_status ON yard_stats.raw_events (crop_status);
CREATE INDEX IF NOT EXISTS idx_raw_events_ai_status ON yard_stats.raw_events (ai_status);
CREATE INDEX IF NOT EXISTS idx_raw_events_video_status ON yard_stats.raw_events (video_status);
CREATE INDEX IF NOT EXISTS idx_raw_events_visit_id ON yard_stats.raw_events (visit_id);

-- Widens ai_status's CHECK constraint to allow 'skipped' -- CREATE TABLE IF NOT EXISTS above only
-- takes effect for a brand new table, so an already-deployed database's constraint (added before
-- 'skipped' was a valid ai_status) needs this explicit widen. Safe to re-run every startup: DROP
-- IF EXISTS is a no-op once the constraint is already named as below, and re-adding an identical
-- CHECK is idempotent (Postgres re-validates existing rows against it, cheap at this project's
-- scale). The auto-generated name for an inline CHECK on a freshly-created table would already be
-- this same "<table>_<column>_check" form, so this also matches what CREATE TABLE would have named
-- it on a brand new database -- nothing to drop there, ADD CONSTRAINT just succeeds directly.
ALTER TABLE yard_stats.raw_events DROP CONSTRAINT IF EXISTS raw_events_ai_status_check;
ALTER TABLE yard_stats.raw_events ADD CONSTRAINT raw_events_ai_status_check
  CHECK (ai_status IN ('new', 'processing', 'retry', 'failed', 'done', 'skipped'));

-- One-time-ish backfill for rows inserted before ai_status='skipped' existed -- a row whose crop
-- was never possible (has_snapshot=false at ingest) but was inserted under the old code sits at
-- ai_status='new' forever (claim_ai_batch's crop_status='done' requirement can never be satisfied
-- for it), indistinguishable on the admin dashboard from a row genuinely waiting on capacity.
-- Confirmed live: crop_status='skipped' rows accounted for the large majority of a reported
-- ai_status='new' backlog. Safe to re-run every startup -- it's a no-op once caught up, since new
-- rows are now inserted with the correct initial ai_status directly (see insert_raw_event).
UPDATE yard_stats.raw_events SET ai_status = 'skipped', ai_status_changed_at = now()
  WHERE ai_status = 'new' AND crop_status = 'skipped';

-- One row per AI-analyzed event, ANY object type (car/truck/person/dog/whatever profiles.yaml has
-- a prompt for) -- deliberately universal, no per-type columns/tables. object_label carries the
-- actual Frigate label so many different types can share this one table while staying
-- distinguishable in queries/stats/search; description is the VLM's own free-text answer to
-- whatever event_prompt profiles.yaml configured for that label -- the prompt itself decides
-- what's worth mentioning (color, plate, breed, clothing, whatever), not a fixed set of columns.
-- Replaces the former vehicle_sightings/person_sightings split (see CLAUDE.md's "Universal
-- sightings" section for why) -- deliberately not migrated, this project's data was reset from
-- scratch at the same time, matching this file's own established precedent for a breaking change.
CREATE TABLE IF NOT EXISTS yard_stats.sightings (
  id SERIAL PRIMARY KEY,
  raw_event_id INTEGER REFERENCES yard_stats.raw_events(id),
  object_label TEXT,
  description TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sightings_raw_event ON yard_stats.sightings (raw_event_id);
CREATE INDEX IF NOT EXISTS idx_sightings_object_label ON yard_stats.sightings (object_label);

-- DEPRECATED -- kept, unwritten and unread by any code path, same reasoning as
-- visits.alert_ai_status above: this backed the now-removed alert AI stage (one row per
-- alert-stage-analyzed visit, same universal shape as sightings above but keyed by visit_id).
CREATE TABLE IF NOT EXISTS yard_stats.visit_sightings (
  id SERIAL PRIMARY KEY,
  visit_id INTEGER REFERENCES yard_stats.visits(id),
  object_label TEXT,
  description TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_visit_sightings_visit ON yard_stats.visit_sightings (visit_id);
CREATE INDEX IF NOT EXISTS idx_visit_sightings_object_label ON yard_stats.visit_sightings (object_label);

-- Semantic search over AI-written sighting text, embedded via whatever model is loaded behind
-- LLAMA_PROXY_EMBED_PATH (Qwen3-Embedding-0.6B-GGUF, 1024 dims, in this deployment). Nullable:
-- only newly-analyzed sightings get one; existing rows stay searchable by every other filter, just
-- not semantically, until/unless backfilled. HNSW (not ivfflat) since it needs no existing rows to
-- "train" on, so it's safe to create immediately against a column that starts empty.
--
-- __EMBEDDING_DIMENSIONS__ is a template placeholder, substituted by db.ensure_schema() from
-- config.EMBEDDING_DIMENSIONS (env var, default 1024) before this file is executed -- this file is
-- never run directly against psql with the placeholder still in it. This ADD COLUMN only sizes a
-- brand new column correctly; widening an *existing* column to a new dimension after a model swap
-- is handled separately by db._ensure_embedding_dimension() (conditional on the current dimension
-- actually differing -- unlike this file's other statements, that ALTER can't safely be
-- unconditional/idempotent, since it clears the column's data).
ALTER TABLE yard_stats.sightings ADD COLUMN IF NOT EXISTS embedding vector(__EMBEDDING_DIMENSIONS__);
ALTER TABLE yard_stats.visit_sightings ADD COLUMN IF NOT EXISTS embedding vector(__EMBEDDING_DIMENSIONS__);
CREATE INDEX IF NOT EXISTS idx_sightings_embedding ON yard_stats.sightings
  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_visit_sightings_embedding ON yard_stats.visit_sightings
  USING hnsw (embedding vector_cosine_ops);
