-- Debug/maintenance toolkit for both queue tables: raw_events (crop_status owned directly by
-- ingest-worker; ai_status mechanically executed via its /ai-queue/* API -- n8n's Metadata
-- Processor just calls that API, it no longer touches Postgres directly; video_status owned by
-- video_worker.py) and visits (video_status owned by alert_video_worker.py; thumb_crop_status
-- owned by visit_thumb_worker.py -- see the VISITS section below). Run these against
-- postgres-projects / home_automation as needed -- nothing here runs automatically.

-- ============================================================================
-- RAW_EVENTS (events flow -- frigate/events, one row per Frigate object detection)
-- ============================================================================

-- CHECK: current status breakdown by object type
SELECT objects, crop_status, ai_status, count(*)
FROM yard_stats.raw_events
GROUP BY objects, crop_status, ai_status
ORDER BY objects, crop_status, ai_status;

-- CHECK: what's in flight right now (crop stage -- ingest-worker), and for how long
SELECT id, camera, objects, det_id, crop_status, crop_status_changed_at, now() - crop_status_changed_at AS age
FROM yard_stats.raw_events
WHERE crop_status = 'processing'
ORDER BY crop_status_changed_at;

-- CHECK: what's in flight right now (AI stage -- n8n), and for how long
SELECT id, camera, objects, det_id, ai_status, ai_status_changed_at, now() - ai_status_changed_at AS age
FROM yard_stats.raw_events
WHERE ai_status = 'processing'
ORDER BY ai_status_changed_at;

-- CHECK: recently failed items, either stage
SELECT id, camera, objects, det_id, crop_status, crop_attempt_count, ai_status, ai_attempt_count,
       greatest(crop_status_changed_at, ai_status_changed_at) AS last_changed
FROM yard_stats.raw_events
WHERE crop_status = 'failed' OR ai_status = 'failed'
ORDER BY last_changed DESC
LIMIT 50;

-- CHECK: events waiting to be cropped by ingest-worker
SELECT id, camera, objects, det_id, created_at
FROM yard_stats.raw_events
WHERE crop_status IN ('new', 'retry')
ORDER BY created_at
LIMIT 50;

-- CHECK: events cropped and waiting for n8n's AI stage
SELECT id, camera, objects, det_id, crop_status_changed_at
FROM yard_stats.raw_events
WHERE crop_status = 'done' AND ai_status IN ('new', 'retry')
ORDER BY crop_status_changed_at
LIMIT 50;

-- ============================================================================
-- FIX: force everything stuck in a given stage back to 'retry' right now
-- (normally happens on its own once past that stage's staleMinutes)
-- ============================================================================
UPDATE yard_stats.raw_events SET crop_status = 'retry', crop_status_changed_at = now()
WHERE crop_status = 'processing';

UPDATE yard_stats.raw_events SET ai_status = 'retry', ai_status_changed_at = now()
WHERE ai_status = 'processing';

-- FIX: retry every crop-failed / ai-failed item (fresh attempt count -- these already used up
-- maxAttempts, so without resetting attempt_count they'd just fail again on the next error)
UPDATE yard_stats.raw_events
SET crop_status = 'retry', crop_attempt_count = 0, crop_status_changed_at = now()
WHERE crop_status = 'failed';

UPDATE yard_stats.raw_events
SET ai_status = 'retry', ai_attempt_count = 0, ai_status_changed_at = now()
WHERE ai_status = 'failed';

-- FIX: retry one specific item's AI stage by id (fresh attempt count, see above)
UPDATE yard_stats.raw_events
SET ai_status = 'retry', ai_attempt_count = 0, ai_status_changed_at = now()
WHERE id = 1234; -- <-- replace

-- FIX: fully reprocess one item's AI stage that already has a sighting row (e.g. after a
-- prompt/bugfix change you want re-run) -- delete its old result first, then reset ai_status, or
-- the processor would insert a duplicate sighting alongside the old one. crop_status/
-- crop_image_base64 are untouched -- no need to re-crop just to redo the AI stage.
DELETE FROM yard_stats.vehicle_sightings WHERE raw_event_id = 1234; -- <-- replace, or person_sightings
UPDATE yard_stats.raw_events SET ai_status = 'new', ai_attempt_count = 0, ai_status_changed_at = now() WHERE id = 1234; -- <-- replace

-- FIX: fully reprocess one item from scratch, including re-cropping (e.g. after a crop/padding
-- change in ingest-worker) -- clears the stored crop too so ingest-worker regenerates it.
DELETE FROM yard_stats.vehicle_sightings WHERE raw_event_id = 1234; -- <-- replace, or person_sightings
UPDATE yard_stats.raw_events
SET crop_status = 'new', crop_attempt_count = 0, crop_status_changed_at = now(), crop_image_base64 = NULL,
    ai_status = 'new', ai_attempt_count = 0, ai_status_changed_at = now()
WHERE id = 1234; -- <-- replace

-- FIX: reprocess the AI stage for the last N events (e.g. after a VLM prompt change you want to
-- retest against recent real data without wiping everything). Same duplicate-row caveat as above.
-- A CTE (WITH ... AS (...)) only scopes to the single statement it's attached to, not the
-- following ones -- a TEMP TABLE is what actually persists across the 3 statements below within
-- the same psql session.
CREATE TEMP TABLE targets AS
SELECT id FROM yard_stats.raw_events ORDER BY created_at DESC LIMIT 20; -- <-- replace N

DELETE FROM yard_stats.vehicle_sightings WHERE raw_event_id IN (SELECT id FROM targets);
DELETE FROM yard_stats.person_sightings WHERE raw_event_id IN (SELECT id FROM targets);
UPDATE yard_stats.raw_events SET ai_status = 'new', ai_attempt_count = 0, ai_status_changed_at = now()
WHERE id IN (SELECT id FROM targets);

DROP TABLE targets;

-- ============================================================================
-- VISITS (alerts flow -- frigate/reviews) -- separate table, separate queue stages
-- (video_status/thumb_crop_status) from raw_events' crop_status/ai_status/video_status above.
-- See CLAUDE.md's "Visit grouping"/"Visit preview" sections.
-- ============================================================================

-- CHECK: current status breakdown
SELECT objects, video_status, thumb_crop_status, count(*)
FROM yard_stats.visits
GROUP BY objects, video_status, thumb_crop_status
ORDER BY objects, video_status, thumb_crop_status;

-- CHECK: what's in flight right now (visit video stage -- alert_video_worker), and for how long
SELECT id, cameras, objects, video_status, video_status_changed_at, now() - video_status_changed_at AS age
FROM yard_stats.visits
WHERE video_status = 'processing'
ORDER BY video_status_changed_at;

-- CHECK: what's in flight right now (visit preview/grid stage -- visit_thumb_worker), and for how long
SELECT id, cameras, objects, thumb_crop_status, thumb_crop_status_changed_at, now() - thumb_crop_status_changed_at AS age
FROM yard_stats.visits
WHERE thumb_crop_status = 'processing'
ORDER BY thumb_crop_status_changed_at;

-- CHECK: recently failed visits, either stage
SELECT id, cameras, objects, video_status, video_attempt_count, thumb_crop_status, thumb_crop_attempt_count,
       greatest(video_status_changed_at, thumb_crop_status_changed_at) AS last_changed
FROM yard_stats.visits
WHERE video_status = 'failed' OR thumb_crop_status = 'failed'
ORDER BY last_changed DESC
LIMIT 50;

-- CHECK: how many raw_events got linked to each visit (event_count -- same computation
-- GET /visits uses) -- a quick way to spot whether Frigate's review grouping is behaving as
-- expected on your cameras (e.g. one real vehicle tracked across several det_ids vs. one row per
-- visit meaning nothing is being grouped at all)
SELECT v.id AS visit_id, v.cameras, v.objects, count(re.id) AS event_count
FROM yard_stats.visits v
LEFT JOIN yard_stats.raw_events re ON re.visit_id = v.id
GROUP BY v.id, v.cameras, v.objects
ORDER BY v.id DESC
LIMIT 50;

-- FIX: force everything stuck in a given visit stage back to 'retry' right now
UPDATE yard_stats.visits SET video_status = 'retry', video_status_changed_at = now()
WHERE video_status = 'processing';

UPDATE yard_stats.visits SET thumb_crop_status = 'retry', thumb_crop_status_changed_at = now()
WHERE thumb_crop_status = 'processing';

-- FIX: retry every video-failed / thumb_crop-failed visit (fresh attempt count -- these already
-- used up their max-attempts cap, so without resetting attempt_count they'd just fail again)
UPDATE yard_stats.visits
SET video_status = 'retry', video_attempt_count = 0, video_status_changed_at = now()
WHERE video_status = 'failed';

UPDATE yard_stats.visits
SET thumb_crop_status = 'retry', thumb_crop_attempt_count = 0, thumb_crop_status_changed_at = now()
WHERE thumb_crop_status = 'failed';

-- FIX: fully regenerate one visit's preview grid/GIF from scratch (e.g. after a
-- crop.build_visit_preview change you want to re-run against real data) -- clears the stored
-- artifacts too so visit_thumb_worker regenerates them.
UPDATE yard_stats.visits
SET thumb_crop_status = 'new', thumb_crop_attempt_count = 0, thumb_crop_status_changed_at = now(),
    crop_image_base64 = NULL, preview_gif_base64 = NULL
WHERE id = 1234; -- <-- replace

-- ============================================================================
-- RESET FROM SCRATCH (destructive -- only for test/dev data, not for production history)
-- ============================================================================

-- Option A: wipe AI-stage state only (keep crops, re-run just the AI pass on everything)
-- UPDATE yard_stats.raw_events SET ai_status = 'new', ai_attempt_count = 0, ai_status_changed_at = now();
-- TRUNCATE yard_stats.vehicle_sightings, yard_stats.person_sightings;

-- Option B: wipe everything queue-related, keep the raw_events rows themselves (re-crop and
-- re-analyze from scratch)
-- TRUNCATE yard_stats.vehicle_sightings, yard_stats.person_sightings;
-- UPDATE yard_stats.raw_events SET
--   crop_status = 'new', crop_attempt_count = 0, crop_status_changed_at = now(), crop_image_base64 = NULL,
--   ai_status = 'new', ai_attempt_count = 0, ai_status_changed_at = now();

-- Option B2: same idea, for visits' own video/preview stages (STORE_VIDEO_ALERTS/
-- VISIT_THUMB_CROP_ENABLED) -- independent of Option B above, since these are separate artifacts.
-- UPDATE yard_stats.visits SET
--   video_status = 'new', video_attempt_count = 0, video_status_changed_at = now(), video_path = NULL,
--   thumb_crop_status = 'new', thumb_crop_attempt_count = 0, thumb_crop_status_changed_at = now(),
--   crop_image_base64 = NULL, preview_gif_base64 = NULL;

-- Option C: nuke everything in the schema and let ingest-worker rebuild it
-- DROP SCHEMA IF EXISTS yard_stats CASCADE;
-- -- then restart ingest-worker (it applies ingest-worker/schema.sql on every startup), or
-- -- run it by hand: psql -f ../ingest-worker/schema.sql (relative to this file's frigate/sql/ dir)
