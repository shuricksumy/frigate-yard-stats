CREATE SCHEMA IF NOT EXISTS yard_stats;

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
  -- Two independent queue state machines, owned by two different systems:
  --   crop_status -- owned by ingest-worker (Python). Ingests every Frigate label (car, truck,
  --                  person, dog, ...), crops via ffmpeg, stores crop_image_base64. Never calls an LLM.
  --   ai_status   -- owned by the n8n Vehicle/Person Metadata Processor workflows. Only ever
  --                  looks at rows where crop_status = 'done'; calls the VLM(s), writes results
  --                  to vehicle_sightings/person_sightings.
  -- Both use the same shape:
  --   new        -> not yet picked up
  --   processing -> claimed by a run, work in flight
  --   retry      -> was 'processing' but the run never finished (crash/timeout, reaped), or
  --                 errored below that stage's max-attempts cap
  --   failed     -- errored at/above that stage's max-attempts cap (terminal)
  --   done       -> crop_status: crop_image_base64 populated. ai_status: a row exists in
  --                 vehicle_sightings / person_sightings.
  crop_status TEXT NOT NULL DEFAULT 'new'
    CHECK (crop_status IN ('new', 'processing', 'retry', 'failed', 'done')),
  crop_status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  crop_attempt_count INTEGER NOT NULL DEFAULT 0,
  ai_status TEXT NOT NULL DEFAULT 'new'
    CHECK (ai_status IN ('new', 'processing', 'retry', 'failed', 'done')),
  ai_status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ai_attempt_count INTEGER NOT NULL DEFAULT 0,
  -- The exact cropped JPEG (base64) ingest-worker produced for this event -- lives on raw_events
  -- (not the sightings tables) since it's produced before AI analysis and is label-agnostic.
  crop_image_base64 TEXT,
  -- Captured by ingest-worker from the same Frigate API fetch used to get the crop region --
  -- the settled/final LPR read and detection score, not the live MQTT "end" payload's values
  -- (sub_label in particular can resolve after the event first fires). Kept here so the n8n
  -- AI-processing stage never needs to call Frigate's API itself.
  sub_label TEXT,
  score DOUBLE PRECISION,
  reconciled BOOLEAN NOT NULL DEFAULT false,
  visit_id INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_raw_events_reconciled ON yard_stats.raw_events (reconciled);
CREATE INDEX IF NOT EXISTS idx_raw_events_zone_ts ON yard_stats.raw_events (zone, start_ts);
CREATE INDEX IF NOT EXISTS idx_raw_events_has_snapshot ON yard_stats.raw_events (has_snapshot);
CREATE INDEX IF NOT EXISTS idx_raw_events_crop_status ON yard_stats.raw_events (crop_status);
CREATE INDEX IF NOT EXISTS idx_raw_events_ai_status ON yard_stats.raw_events (ai_status);

CREATE TABLE IF NOT EXISTS yard_stats.visits (
  id SERIAL PRIMARY KEY,
  zone TEXT,
  objects TEXT,
  start_ts TIMESTAMPTZ NOT NULL,
  end_ts TIMESTAMPTZ NOT NULL,
  cameras TEXT,
  camera_count INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_visits_zone_ts ON yard_stats.visits (zone, start_ts);

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
-- ADD COLUMN IF NOT EXISTS instead of relying on CREATE TABLE IF NOT EXISTS above, since this
-- schema.sql reapplies on every ingest-worker startup against already-existing tables.
ALTER TABLE yard_stats.vehicle_sightings ADD COLUMN IF NOT EXISTS model_guess TEXT;
ALTER TABLE yard_stats.vehicle_sightings ADD COLUMN IF NOT EXISTS model_confidence TEXT;
ALTER TABLE yard_stats.vehicle_sightings ADD COLUMN IF NOT EXISTS notable_features TEXT;

CREATE TABLE IF NOT EXISTS yard_stats.person_sightings (
  id SERIAL PRIMARY KEY,
  raw_event_id INTEGER REFERENCES yard_stats.raw_events(id),
  description TEXT,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_person_sightings_raw_event ON yard_stats.person_sightings (raw_event_id);
