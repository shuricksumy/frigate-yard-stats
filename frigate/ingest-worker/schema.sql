-- Applied on every ingest-worker startup (idempotent -- CREATE ... IF NOT EXISTS throughout), so a
-- brand new Postgres instance is ready with no manual `psql -f` step. This file was consolidated
-- from its incremental ALTER-based migration history into a single clean baseline (the project's
-- one production instance was reset from scratch at the same time) -- any *future* column/table
-- change should still follow the old idiom (ALTER TABLE ... ADD COLUMN IF NOT EXISTS, added below
-- rather than edited into the CREATE TABLE blocks) so this file stays safe to re-apply against a
-- live, already-populated database.

CREATE SCHEMA IF NOT EXISTS yard_stats;

-- Backs vehicle_sightings.embedding/person_sightings.embedding (see below) -- requires the
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

-- Sixth queue stage (AI_ALERTS_ENABLED) -- analyzes the visit's own composite grid
-- (crop_image_base64 above, built once thumb_crop_status='done') with alert_ai_worker.py, using
-- profiles.yaml's alert_prompt (framed for 4-frames-of-change, unlike AI_EVENTS_STAGE_ENABLED's
-- event_prompt which is framed for one static frame). Independent of raw_events.ai_status
-- entirely -- a visit's alert-level analysis and its linked raw_events' own event-level analysis
-- are two separate results, stored in two separate places (see visit_vehicle_sightings/
-- visit_person_sightings below vs. vehicle_sightings/person_sightings), so both can run at once
-- without one overwriting or blocking the other.
ALTER TABLE yard_stats.visits ADD COLUMN IF NOT EXISTS alert_ai_status TEXT NOT NULL DEFAULT 'new'
  CHECK (alert_ai_status IN ('new', 'processing', 'retry', 'failed', 'done', 'skipped'));
ALTER TABLE yard_stats.visits ADD COLUMN IF NOT EXISTS alert_ai_status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE yard_stats.visits ADD COLUMN IF NOT EXISTS alert_ai_attempt_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_visits_alert_ai_status ON yard_stats.visits (alert_ai_status);

-- One row per Frigate "end" event, any label (car/truck/person/dog/...). Carries three
-- independent queue state machines -- crop_status/video_status owned directly by ingest-worker,
-- ai_status owned by n8n via ingest-worker's /ai-queue/* API -- see CLAUDE.md's "Architecture"
-- section for the full write-up of who owns which and why. All three share the same shape:
-- new -> processing -> retry/failed -> done, plus 'skipped' for a state a row can start in but
-- never needs to leave (crop_status: has_snapshot=false at ingest time; video_status:
-- STORE_VIDEO=false).
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
    CHECK (ai_status IN ('new', 'processing', 'retry', 'failed', 'done')),
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
  -- sightings tables) since it's produced before AI analysis and is label-agnostic.
  crop_image_base64 TEXT,
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

-- One row per AI-analyzed vehicle event (car/truck). plate_text_frigate (from raw_events.sub_label)
-- is kept alongside plate_text_llm (the OCR model's own read) as a cross-check.
CREATE TABLE IF NOT EXISTS yard_stats.vehicle_sightings (
  id SERIAL PRIMARY KEY,
  raw_event_id INTEGER REFERENCES yard_stats.raw_events(id),
  color TEXT,
  body_type TEXT,
  make_guess TEXT,
  make_confidence TEXT,
  model_guess TEXT,
  model_confidence TEXT,
  notable_features TEXT,
  plate_text_llm TEXT,
  plate_text_frigate TEXT,
  plate_confidence TEXT,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_vehicle_sightings_raw_event ON yard_stats.vehicle_sightings (raw_event_id);

-- One row per AI-analyzed person event.
CREATE TABLE IF NOT EXISTS yard_stats.person_sightings (
  id SERIAL PRIMARY KEY,
  raw_event_id INTEGER REFERENCES yard_stats.raw_events(id),
  description TEXT,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_person_sightings_raw_event ON yard_stats.person_sightings (raw_event_id);

-- One row per alert-stage-analyzed visit (AI_ALERTS_ENABLED) -- same shape as vehicle_sightings
-- above, but keyed by visit_id instead of raw_event_id, since this analyzes the visit's own
-- composite grid (visits.crop_image_base64) rather than any single raw_event's crop. A visit
-- whose representative event is a vehicle gets at most one row here; there is no plate_text_frigate
-- equivalent (Frigate's own LPR read is per-event, not per-visit).
CREATE TABLE IF NOT EXISTS yard_stats.visit_vehicle_sightings (
  id SERIAL PRIMARY KEY,
  visit_id INTEGER REFERENCES yard_stats.visits(id),
  color TEXT,
  body_type TEXT,
  make_guess TEXT,
  make_confidence TEXT,
  model_guess TEXT,
  model_confidence TEXT,
  notable_features TEXT,
  plate_text_llm TEXT,
  plate_confidence TEXT,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_visit_vehicle_sightings_visit ON yard_stats.visit_vehicle_sightings (visit_id);

-- One row per alert-stage-analyzed visit whose representative event is a person.
CREATE TABLE IF NOT EXISTS yard_stats.visit_person_sightings (
  id SERIAL PRIMARY KEY,
  visit_id INTEGER REFERENCES yard_stats.visits(id),
  description TEXT,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_visit_person_sightings_visit ON yard_stats.visit_person_sightings (visit_id);

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
ALTER TABLE yard_stats.vehicle_sightings ADD COLUMN IF NOT EXISTS embedding vector(__EMBEDDING_DIMENSIONS__);
ALTER TABLE yard_stats.person_sightings ADD COLUMN IF NOT EXISTS embedding vector(__EMBEDDING_DIMENSIONS__);
ALTER TABLE yard_stats.visit_vehicle_sightings ADD COLUMN IF NOT EXISTS embedding vector(__EMBEDDING_DIMENSIONS__);
ALTER TABLE yard_stats.visit_person_sightings ADD COLUMN IF NOT EXISTS embedding vector(__EMBEDDING_DIMENSIONS__);
CREATE INDEX IF NOT EXISTS idx_vehicle_sightings_embedding ON yard_stats.vehicle_sightings
  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_person_sightings_embedding ON yard_stats.person_sightings
  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_visit_vehicle_sightings_embedding ON yard_stats.visit_vehicle_sightings
  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_visit_person_sightings_embedding ON yard_stats.visit_person_sightings
  USING hnsw (embedding vector_cosine_ops);
