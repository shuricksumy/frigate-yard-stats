# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

"Yard Stats + Vehicle Metadata" extends an existing Frigate NVR setup (Coral TPU detection + LPR)
with a pipeline that logs yard activity and extracts vehicle/person metadata (color, body type,
plate text, clothing description) from Frigate events using local VLMs. It is one project among
several in the user's homelab (alongside n8n, Flowise, WAHA, mcp-proxy, and a
[`llama_slot_proxy`](https://github.com/shuricksumy/llama-slot-proxy) multi-model llama.cpp setup,
itself running on the user's [`llama-service`](https://github.com/shuricksumy/llama-service)
serving setup), and is deliberately kept decoupled from those via its own Postgres instance/schema
and its own containers.

Everything is **MQTT-in, API-in, Postgres-out** — nothing here touches Frigate's own database.

## Repository layout

```
frigate-llm/
  frigate/                   # MAIN project folder -- the pipeline, plus Frigate's own config
    docker-compose.yml        # ONE file, three Compose profiles: pipeline + nvr + mqtt (see below)
    .env.example                # ONE shared template covering both stacks below -- see comments
    sql/queue-debug.sql         # manual check/fix/reset queries for the raw_events queue
    backup-postgres-projects.sh
    ingest-worker/               # the main service -- see below (includes static/, the web UI)
    mosquitto/                    # config/data/log for the optional local MQTT broker profile
    frigate.conf                 # Frigate's own config, read by the "frigate" service/profile
  n8n/                        # additional folder -- importable workflow JSON (AI stage, reports, Q&A)
```

`frigate/docker-compose.yml` holds two independent stacks that still deploy to two different hosts
despite sharing one file and one `.env.example`/`.env` -- `pipeline` (postgres-projects +
ingest-worker) and `nvr` (Frigate itself) -- plus a third, fully optional `mqtt` profile (a local
Mosquitto broker, for a from-scratch dev stack with no external broker dependency). Profiles are
opt-in (`docker compose --profile pipeline up -d` / `docker compose --profile nvr up -d`); a bare
`docker compose up -d` starts nothing, so there's no risk of starting the wrong stack on the wrong
host. Each service only reads the environment variables it references, so the same `.env` can be
copied to both hosts and each one only fills in / relies on its own section (documented via
comments in `.env.example`). Frigate's REST API is reached cross-host at `FRIGATE_API_BASE` (its
LAN-bound IP:port, e.g. `http://192.168.1.10:5000`), not a Docker service name. The `frigate/`
folder name reflects that this pipeline is Frigate-adjacent tooling, not that everything in it
runs on the Frigate host.

## Architecture

```
Frigate (MQTT frigate/events, every object label: car/truck/person/dog/...
         + frigate/reviews, Frigate's own review/alert grouping)
   │
   ▼
ingest-worker/  (Python, one container, no LLM calls)
   - MQTT subscriber -> INSERT raw_events, unfiltered by label
   - Second MQTT subscriber (frigate/reviews) -> INSERT visits, link raw_events.visit_id ->
     fire-and-forget Telegram visit summary if TELEGRAM_ALERTS_MODE includes it
   - Crop-stage poll loop, every POLL_INTERVAL_SECONDS:
       reap stale crop_status='processing' -> count in-progress -> claim batch (FOR UPDATE SKIP LOCKED)
       -> GET Frigate event (region/sub_label/score) -> crop via built-in ffmpeg, size-capped
       -> store crop_image_base64, sub_label, score -> mark crop_status done/retry/failed
       -> fire-and-forget Telegram photo (telegram.py), store its message_id for later reply-threading
   - Video-stage poll loop (own thread, only started if STORE_VIDEO=true), same shape as the crop
     stage but downstream of it (claims crop_status='done' rows):
       reap stale video_status='processing' -> count in-progress -> claim batch
       -> wait VIDEO_INITIAL_WAIT_SECONDS on a fresh claim (Frigate may not have finalized the clip
       yet) -> GET Frigate's clip.mp4 endpoint -> store to VIDEO_STORAGE_PATH, path only in Postgres
       -> mark video_status done/retry/failed -> fire-and-forget Telegram video, replying to the
       stored photo message_id if present
   - Alert-video-stage poll loop (own thread, only started if STORE_VIDEO_ALERTS=true), same shape
     again but against visits instead of raw_events -- one clip per visit's whole span, independent
     of the events flow above
   - Also applies schema.sql on every startup (idempotent) and runs retention cleanup on a slow
     cadence (DB rows *and* their video files, once `video_path` is set)
   - FastAPI surface (Swagger UI at :8080/docs): unauthenticated admin/test endpoints
     (health/status/manual-crop/manual-retention, not part of the normal pipeline) plus an
     X-API-Key-protected API (/events, /sightings, /stats, /reports, /ai-queue, /media/video)
     that n8n and other consumers call instead of querying Postgres directly -- this now includes
     the AI-stage queue mechanics (claim/complete/fail), not just read-only queries -- plus a
     static web report UI at /ui (Alpine.js, no build step) over that same API
   │  (crop_status = 'done', crop_image_base64/sub_label/score already on the row)
   ▼
n8n Metadata Processor (any object type, one shared queue) -- AI stage only, no Frigate/crop/video/
Telegram calls
   - POST /ai-queue/claim -- reap stale, count in-progress, atomically claim a batch, all in one call
   - route by object type (per `profiles.yaml`), call the VLM directly against the claimed row's
     crop_image_base64 -- the prompt alone decides what gets captured, no per-type response schema
   - POST /sightings -- insert + mark ai_status='done' in one call, for ANY object type/label
   - on VLM failure: POST /ai-queue/{id}/fail -- retry-or-fail-with-cap
   │
   ▼
Daily Report / Q&A agent (n8n) -- read-only, calls ingest-worker's query/report API
   (which itself only ever reads sightings rows, i.e. AI-analyzed events)
```

Three independent queue state machines live on `raw_events` -- `crop_status`/`crop_status_changed_at`/
`crop_attempt_count`, `video_status`/`video_status_changed_at`/`video_attempt_count`, and
`ai_status`/`ai_status_changed_at`/`ai_attempt_count` -- and `ingest-worker` *mechanically executes
all three* (the crop- and video-stage poll loops own the first two directly, each in its own
thread; the `/ai-queue/*` endpoints own the third on n8n's behalf). The video stage is a strict
downstream consumer of the crop stage (it only claims `crop_status='done'` rows), the same
relationship the AI stage already has with `crop_status`. n8n still *decides policy* for the AI
stage -- `parallel_limit`/`stale_minutes`/`max_age_hours` are query params on `/ai-queue/claim` and
`max_attempts` is a query param on `/ai-queue/{id}/fail`, all editable directly in those n8n HTTP
Request nodes without touching `ingest-worker` code, the same "tune it here" spirit the old
`Queue Config` node had. `ingest-worker` still never calls an LLM -- n8n still owns the actual VLM
call and prompt; it just no longer runs raw SQL to do so. n8n also never touches Telegram, video
storage, or Frigate directly -- those are entirely `ingest-worker`'s mechanical concern
(`video.py`/`video_worker.py`/`telegram.py`), ported from the `FrigateRetry.json` n8n workflow this
replaced rather than added to n8n.

All three stages use the same shape: `new` (not picked up) → `processing` (claimed, work in flight) →
`retry` (crashed/reaped, or errored below that stage's attempt cap) → `failed` (errored at/above
the cap, terminal) → `done`. `video_status`, `crop_status`, and `ai_status` all additionally have
`skipped`, set at ingest time -- `video_status` when `STORE_VIDEO=false`, `crop_status` **and**
`ai_status` both when the MQTT payload's `has_snapshot` is false. The latter matters because
Frigate can emit a full `new`→`end` MQTT lifecycle for a tracked object it never actually persists
as a real event (confirmed in production: such rows' `det_id` 404s against Frigate's own
`/api/events/<id>`) -- cropping those can never succeed regardless of retries or queue throughput,
so they're marked `skipped` immediately rather than piling up as an eternally-unprocessed `new`.
`ai_status` gets the identical treatment for the identical reason: `claim_ai_batch` hard-requires
`crop_status='done'` (an image is always guaranteed on every claimed row -- see below), so a row
that can never get a crop can also never be claimed for AI analysis; without marking it `skipped`
too, it would sit at `ai_status='new'` forever, indistinguishable on the admin dashboard from a row
genuinely waiting on capacity (confirmed live: this was the large majority -- 1048 of ~1140 -- of a
reported `ai_status='new'` backlog on one production instance). Rows inserted before this existed
are backfilled by a one-time-ish `UPDATE ... WHERE ai_status = 'new' AND crop_status = 'skipped'`
in `schema.sql`, safe to leave in permanently since it's a no-op once caught up.

**Update: `has_snapshot=false` events are no longer inserted into `raw_events` at all.**
`mqtt_ingest._handle_event_message` only ever acts on the `"end"` message (never `"new"`/
`"update"`), so `has_snapshot` is already Frigate's own final, terminal answer for that det_id by
the time it's read -- there's no race where a snapshot could still arrive later for the same
tracked-object lifecycle. Such a row can never be cropped/stored on video/AI-analyzed regardless of
retries (this is exactly why it used to be marked `skipped` immediately -- see above), so inserting
it at all was pure dead weight: confirmed live in production this was the overwhelming majority of
one camera's MQTT traffic (~98% of its `car` detections, roughly 14,000 of the camera's ~16,000
total tracked-object lifecycles) with zero analytical value -- a `skipped` row never gets an image,
video, or AI description, ever, and is never re-queried by any claim function again once marked
terminal. Root-caused to tracker-confidence noise, not a camera hardware/lighting problem alone:
comparing the two cameras' `car` object filters directly, the noisier one's `threshold`/`min_score`/
`min_ratio` were all looser (e.g. `min_score: 0.35` vs `0.45`) and its `motion.threshold` more
sensitive, consistent with more borderline detection candidates being spawned that don't sustain
confidence long enough to become a real confirmed event -- tightening those further was rejected as
a fix on this deployment (one camera is genuinely more light-sensitive; tightening loses real
events without eliminating the noise, confirmed still ~7,000/day skipped even at the camera's own
stricter settings), so the noise is filtered at the application layer instead. This is filtering at
ingest time in `mqtt_ingest.py`, not a change to `db.insert_raw_event` itself (still defensively
marks `crop_status`/`video_status`/`ai_status='skipped'` for a `has_snapshot=false` row if ever
called with one directly, e.g. by a test fixture) -- the skip-status machinery above still exists
and still matters for the rows already in the database from before this change, and for any other
caller that constructs a `raw_events` insert directly.

`FOR UPDATE SKIP LOCKED` is what makes claiming
race-safe against overlapping runs (multiple n8n executions, or this service's own poll loops) --
but only when paired with a CTE, not a plain `WHERE id IN (SELECT ... LIMIT %s FOR UPDATE SKIP
LOCKED)` subquery: confirmed in practice (reproduced directly in psql) that the subquery form,
when it self-references the table being updated, does not reliably cap the claim at `limit` rows
-- 3 eligible rows with `LIMIT 2` claimed all 3. All three claim functions
(`claim_next_batch`/`claim_video_batch`/`claim_ai_batch`) use the CTE form
(`WITH claimable AS (... LIMIT %s FOR UPDATE SKIP LOCKED) UPDATE ... FROM claimable WHERE
raw_events.id = claimable.id`) so `PARALLEL_LIMIT`/`VIDEO_PARALLEL_LIMIT`/n8n's `parallel_limit`
are actually enforced.

## Key pieces

- **`ingest-worker`** does everything that isn't an LLM call: MQTT ingestion, all three queue state
  machines (crop- and video-stage directly, AI-stage via API), Frigate bbox lookup, ffmpeg
  cropping, clip download/storage, Telegram notifications, and a read/query/report/AI-queue/media
  API over the data it collects (`api.py`/`db.py`/`report.py`/`schemas.py`/`auth.py`/`video.py`/
  `video_worker.py`/`telegram.py`), plus the static web report UI (`static/`) served over that same
  API. It's intentionally dumb/mechanical so it can be plain, testable Python instead of n8n
  Code-node gymnastics. Self-contained: builds from its own folder, bakes `schema.sql` and
  `static/` into the image, needs only Postgres + MQTT + Frigate's HTTP API to run (plus Telegram's
  API if `TELEGRAM_EVENTS_MODE` is anything other than `none`).
- **n8n** owns everything AI-shaped: deciding when to claim work and calling the VLM(s), the daily
  report, and the Q&A workflow. Its processors never touch Frigate's API, crop or video anything
  themselves, and never call Telegram — they only ever read `crop_image_base64` that's already
  sitting on the claimed row, and no longer run raw SQL at all — claim/complete/fail all go through
  `ingest-worker`'s `/ai-queue/*` and `/sightings/*` endpoints. `ingest-worker` never calls an LLM,
  by design -- **when this n8n-driven flow is what's active.** `ai_worker.py` (see "Internal AI
  stage" below) is an opt-in, off-by-default alternative that deliberately breaks this one
  invariant, calling `llama_slot_proxy` directly instead of going through n8n; the two are meant to
  be run one at a time, not both.
- **VLM inference** goes through the user's existing `llama_slot_proxy` setup — one more per-agent
  slot/port pointing at its own `.gguf` + `mmproj` pair, one slot per Frigate object label (or
  shared across labels via a YAML anchor in `profiles.yaml`, e.g. `car`/`truck` sharing one
  vehicle slot). There is no structured attribute schema requested of the model at all -- the
  prompt asks for whatever's relevant to that label (color/body type/plate for a car, clothing for
  a person, anything at all for a dog) and the model's plain-text answer is stored as-is. Frigate's
  own LPR read (`raw_events.sub_label`) still exists on every row regardless of what the VLM
  prompt for that label asks about, but there's no dedicated `plate_text_llm` column to
  cross-check it against anymore -- a plate reference the VLM includes just lives inside its free
  text `description`, same as any other detail.
- **Postgres**: `postgres-projects` container, database `home_automation`, schema `yard_stats`
  (schema-per-project convention — future unrelated projects get their own schema).

### Universal sightings -- one table per grouping level, not one per object type

There is exactly one AI-analysis result shape in this project: `yard_stats.sightings`
(`raw_event_id`, `object_label`, `description`, `embedding`, `created_at`) for the events stage,
and `yard_stats.visit_sightings` (same shape, keyed by `visit_id` instead) for the alerts stage.
Neither table has a single structured column beyond `object_label` (the Frigate label the row
came from, e.g. `car`/`truck`/`person`/`dog`) -- `description` is always plain free text, whatever
the VLM said in response to that label's `profiles.yaml` prompt. There is no `vehicle_sightings`/
`person_sightings` split, no `sighting_type` discriminator, and no per-type parsing/JSON-schema
step anywhere in the pipeline: a car, a person, a dog, and any future label all flow through the
exact same `db.complete_sighting`/`complete_visit_sighting` insert and the exact same
`ai_worker.py`/`alert_ai_worker.py` code path. Adding support for a brand-new object type (e.g.
`cat`, `package`) is purely a `profiles.yaml` edit (one more `object_types` entry with its own
`chat_path`/`event_prompt`/`alert_prompt`) -- no schema migration, no new table, no code change of
any kind. This was a deliberate from-scratch redesign, not an incremental migration: the prior
`vehicle_sightings`/`person_sightings`/`visit_vehicle_sightings`/`visit_person_sightings` tables
(with their structured color/body_type/make_guess/model_guess/notable_features/plate_text_llm/
plate_text_frigate/notes columns) were dropped entirely, along with every n8n workflow node and
Python function that assumed a two-category (vehicle/person) world. There is no compatibility
shim and no migration path from the old shape -- a deployment upgrading across this change starts
with an empty `sightings`/`visit_sightings` table, same as a from-scratch install.

### Query/report/AI-queue API

`ingest-worker`'s FastAPI app has two tiers: `/health`, `/status`, `/crop/{id}`, `/retention/run`
are unauthenticated admin/debug endpoints (unchanged since the original split). Everything else --
`/events`, `/sightings`, `/stats/summary`, `/reports/generate`,
`/ai-queue/claim` / `/ai-queue/{id}/fail`, `/search`/`/search/semantic`, and `/retention/purge` --
requires an `X-API-Key` header
(`config.API_KEY`) since they expose queryable sighting data, mutate the
AI-stage queue, or bulk-delete rows over the network. `ingest-worker` never calls an LLM to serve
any of these -- the one exception is `POST /search` (see "Semantic search and the Q&A agent"
below), which calls the embedding backend directly (never a chat/VLM call) since the web UI has no
other way to turn free text into a vector; every other endpoint here just executes the
claim/insert/retry/delete mechanics, with the VLM call and prompt still living entirely in n8n (or
the internal AI stage), which posts the result back.

`POST /retention/purge` is an ad-hoc counterpart to the scheduled `RETENTION_MONTHS` sweep for
when you want to purge on a caller-chosen cutoff rather than waiting on or reconfiguring the
scheduled one. Defaults to a dry run (`confirm` query param defaults to `false`): it always
returns counts of matching rows/files, and only actually acts when `confirm=true` is passed
explicitly. A second mode, `only_media` (defaults to `true`), decides *what* gets purged:

- **`only_media=true`** (default) -- `db.purge_media_older_than`: deletes stored video files off
  disk and clears media columns on rows older than the cutoff, but keeps every row and all its
  text fields (AI analysis description, embeddings) -- old data stays
  fully searchable via `/events`'/`/visits`' `q` filter and `/search/semantic`, just with the media
  payload gone. Never touches `sightings`/`visit_sightings` at all -- neither table carries media
  columns of its own. **Which media gets cleared is itself three independent flags**, not an
  all-or-nothing "clear everything media" switch: `delete_video` (default `true`) clears
  `video_path` on both tables and deletes the file off disk; `delete_snapshots` (default `false`)
  clears `raw_events.crop_image_base64` (the per-event still crop, "Event Snapshots" in the admin
  UI); `delete_alert_images` (default `true`) clears `visits.alert_image_paths` (the alert stage's
  own gathered high-res crops, when `STORE_ALERT_IMAGES` persisted any -- see "Alert AI stage"
  above) and deletes those files too. Video and alert images default on because they're the
  largest stored payloads (alert images can be several JPEGs per visit); still-images default off
  since a still crop is comparatively cheap to keep and often still useful to glance at even once a
  row is old. All three are independent and composable -- the response's `counts` preview always
  reports every metric (`raw_events_video_files`, `raw_events_snapshots`, `visits_video_files`,
  `visits_alert_images`) regardless of which flags are set, so a dry run shows everything that's
  *available* to clear even if the caller only plans to clear a subset. `delete_alert_images`
  follows the exact same visits-only, `object_label`-scoping-never-applies rule `delete_video`
  already has for visits, for the same reason (a visit can span multiple object types). The admin
  dashboard's Retention section exposes these as three checkboxes plus a separate,
  clearly-destructive "Delete ALL" checkbox that switches to `only_media=false` -- checking it
  visually disables the three media checkboxes, since they no longer mean anything once the whole
  row is going away. (An earlier version of this feature also
  had `delete_gif`/`delete_puzzled_preview` flags for `visits.preview_gif_base64`/
  `visits.crop_image_base64` -- removed along with the visit-preview grid/GIF feature itself, see
  "Alert AI stage" below.)
- **`only_media=false`** -- `db.purge_older_than` (today's original behavior): deletes the rows
  entirely -- same FK-safe child-before-parent delete order as `db.run_retention_cleanup`, extended
  to also delete `visit_sightings` before `visits` (added
  alongside the alert AI stage -- see below) and to decouple `raw_events.visit_id` from a
  to-be-deleted `visit` *before* deleting that `visit`, not after. On a real `confirm=true` run,
  the endpoint also rebuilds both HNSW indexes afterward (`db.reindex_vector_indexes`) -- a full
  purge can remove a large fraction of the rows the index was built over, so this keeps it sized
  and accurate for whatever data survives rather than leaving it bloated for data that's gone.

A third, optional `object_label` param restricts either mode to a single Frigate object type
(e.g. `car`) -- for cleaning up one noisy/low-value type without touching everything else's
retention. Deliberately scoped to `raw_events`/`sightings` only: `visits`/`visit_sightings` are
**never** touched at all when `object_label` is set (their counts in the response come back `0`),
since a visit can span multiple distinct object types (`visits.objects` is comma-joined -- see
"Visit grouping" below) and there's no single-type-safe way to decide a multi-type visit row
belongs to just one type's purge. Omitting `object_label` (the
default) still covers `visits`/`visit_sightings` exactly as it did before this param existed --
only a type-scoped purge narrows to events/sightings alone.

A fourth, optional `camera` param restricts either mode to a single Frigate camera -- composable
with `object_label` (both can be set at once, narrowing to their intersection). Unlike
`object_label`, this **does** apply to `visits`/`visit_sightings` too: visit grouping is
per-camera only (see "Visit grouping" below), so `visits.cameras` is always a single, unambiguous
value with none of `object_label`'s multi-type-per-visit ambiguity -- there's no reason to exempt
visits from a camera-scoped purge the way a type-scoped one has to. `purge_older_than`/
`purge_media_older_than` both thread `camera` through every one of their raw_events- and
visits-scoped queries (counts, video-path lookups, and the DELETE/UPDATE statements themselves)
via the same pattern `object_label` already established, just without the "skip visits entirely"
branch.

**Bug found and fixed while adding this**: both `purge_older_than` and `run_retention_cleanup`
deleted `visits` *before* `raw_events`, but `raw_events.visit_id` references `visits(id)` -- the
opposite direction from that delete order. Reproduced live (a raw_event still linked to an
about-to-be-deleted visit): `psycopg2.errors.ForeignKeyViolation` on the `visits` DELETE. This
predates the alert AI stage entirely (the FK direction has always been `raw_events -> visits`) but
had never been exercised in practice -- nothing in this codebase had integration test coverage for
either purge function until this change. Fixed by nulling `raw_events.visit_id` for every row
pointing at a to-be-deleted visit immediately before the `visits` DELETE in both functions, rather
than relying on every visit's linked raw_events always being at least as old as the visit itself
(a long-lived visit -- e.g. a car parked for 20+ minutes -- can have a later-linked event that
individually isn't old enough to be purged in the same pass, so ordering alone wasn't sufficient).

`POST /ai-queue/claim` folds reap-stale + count-in-progress + claim-next-batch into one call
(`db.claim_ai_batch`), returning `{events: [...]}` -- n8n Split Out's that array into items before
looping (an HTTP node's raw JSON-array response doesn't reliably auto-split into n8n items across
versions, so this is explicit rather than relied-upon). It's one shared queue across every
requested `object_types` (never claimed separately per type) ordered newest-`created_at`-first --
when eligible rows outnumber available capacity, older ones simply keep waiting rather than
being processed strictly in arrival order, and only get swept up once the backlog of newer rows
drops below capacity. The optional `max_age_hours` param goes further: rows older than that cutoff
are never claimed at all (they stay `ai_status='new'` indefinitely), a throughput safety valve for
when incoming events outpace analysis capacity and stale backlog isn't worth spending capacity on.
`claim_next_batch` (crop) and `claim_video_batch` (video) claim newest-first too, for the same
reason -- crop is the very first stage, so an oldest-first crop queue meant fresh events waited
behind however deep a backlog had piled up before they were even croppable at all, which cascades
to everything downstream since video/AI can't start until `crop_status='done'`. Confirmed
necessary in production: the crop backlog reached five digits and kept growing faster than
`PARALLEL_LIMIT` could clear it oldest-first.
An image is always guaranteed on every claimed row (`crop_status='done'` is a hard requirement,
not configurable) -- the optional `require_video` param narrows further, only claiming rows that
also already have a stored video (`video_status='done'`) ready, for a future workflow that wants
both artifacts before processing. The VLM call itself still only ever uses the image regardless --
no model in this setup analyzes video directly; `require_video` only changes which rows are
eligible to claim, not what gets sent to the VLM.

The optional `source` param (`events`, the default, or `visits`) lets n8n A/B which grouping level
the AI stage analyzes, without touching completion at all -- `POST /sightings`
still marks the exact same claimed raw_event's `ai_status='done'` either way, since this is purely
a claim-time filter (`db.claim_ai_batch`'s `only_visit_representative` param), not a schema or
queue-state change (no `ai_status` column was added to `visits`). `source=visits` skips analyzing
every duplicate det_id a visit (see "Visit grouping" below) already grouped together -- one
representative raw_event *per distinct object type* the visit grouped is eligible, computed via a
correlated subquery partitioned by `(visit_id, objects)`, not `visit_id` alone (`id = (SELECT ...
WHERE re2.visit_id = raw_events.visit_id AND re2.objects = raw_events.objects ORDER BY start_ts,
id LIMIT 1)`) -- plus every raw_event that was never grouped into a visit at all (`visit_id IS
NULL`), so events Frigate's review never bundled still get analyzed one-to-one exactly as
`source=events` would.

Partitioning by object type too (not just `visit_id`) is a fix, not the original behavior: a
visit's det_ids can be several re-tracks of the *same* real object (tracker re-ID, label flicker --
the case this dedup was originally built for) or genuinely distinct simultaneous objects (a car
and a person in one visit). Partitioning by `visit_id` alone collapsed both cases down to a single
analyzed event, silently dropping a whole object type whenever a visit happened to group more than
one -- confirmed live: a visit with a car det_id and a person det_id only ever got the earlier of
the two analyzed, never both, with nothing surfacing the gap (the other det_id just stayed
`ai_status='new'` forever, since only the representative row is ever eligible under
`source=visits`). Partitioning by `(visit_id, objects)` keeps the original same-type dedup (still
just one analyzed event per repeated re-track) while giving each distinct object type in a visit
its own representative -- `get_report_data`'s matching correlated subquery (`source=visits` on
`/reports/generate`) got the identical fix, for the same reason: it would otherwise keep silently
showing only one of a visit's already-analyzed sightings.

`GET /visits/{visit_id}/sightings` is the visit-scoped combined read this enables -- every
sighting linked to the visit (one per distinct object type, via `db.get_sightings_for_visit`), not
just the single representative event `GET /events/{id}` returns. The web UI's visit lightbox
(`static/app.js`'s `openLightbox`) calls this instead of `GET /events/{id}` whenever opened from
the Visits view (`lightboxEvent.visitId` set), rendering one info block per returned sighting
(`lightboxGroups`) instead of assuming at most one -- a visit with both a car and a person sighting
now shows both, labeled, in the same lightbox. Unlike the plain per-event case, this fetch isn't
gated on the visit's own `ai_status` (that field only reflects the visit's single earliest-linked
event -- a different, display-only "representative" used by `list_visits`, unrelated to which
events actually got analyzed) -- the visit branch always fetches, since one object type's sighting
can be ready while another's is still pending.

The web UI's lightbox renders a sighting's `description` directly, as one plain-text line -- there
is no per-field table (Color/Body type/Make/Model/Plate or similar) to render at all, since the
universal `sightings`/`visit_sightings` schema has no structured fields beyond `object_label`.
This is the same free-text line every other surface (the alerts report, Telegram, semantic search)
reads and embeds -- there's exactly one representation of "what did the VLM say about this
sighting," not a structured form the UI reflows and a separate summary line the report/embedding
code builds from it.

The optional `visits_only` param (default `false`, only meaningful alongside `source=visits`)
drops that ungrouped-event fallback entirely -- with it set, a raw_event Frigate's review never
grouped into a visit is never claimed by this call at all, however long it waits. This used to be
`n8n/metadata-processor-alerts.json`'s default config, back when there were two separate
processing workflows (see below) -- confirmed necessary at the time because plain `source=visits`
was still marking ordinary, non-alert raw_events `ai_status='done'` (visible as unexpected "done"
rows under the web UI's Events tab, not the Visits tab) in a way that workflow didn't want, since
its whole purpose was staying alerts-scoped while a separate events-only workflow handled the
plain case. Now that `claim_ai_batch`'s dedup is object-type-aware (see below) rather than
collapsing a whole visit to one event regardless of type, plain `source=visits` (i.e.
`visits_only=false`) is a strict superset of the old events-only mode -- every ungrouped raw_event
still gets analyzed one-to-one via the fallback, and every visit-grouped one gets analyzed once
per distinct object type -- this was the shape `n8n/metadata-processor.json` used back when it was
n8n's only processing workflow (plain `source=visits`, never `visits_only`), before that file was
deleted as stale (see "Internal AI stage" below); `ai_worker.py`, the only AI-stage implementation
running today, doesn't set `source` at all (plain `source=events`). The param still exists for
whichever caller wants strictly alert-scoped analysis (never touch an ungrouped raw_event at all).

(Bug fixed in passing while building `source`: `claim_ai_batch`'s `RETURNING yard_stats.raw_events.*`
never included the computed `has_video`/`has_image` fields `EventDetail` requires -- every call
that actually claimed rows was crashing at FastAPI's response-serialization step with a 500,
*after* the UPDATE had already committed `ai_status='processing'` in the DB. n8n never received
the claimed rows, which then sat until `stale_minutes` reaped them back to `retry` and the cycle
repeated -- confirmed by reproducing the exact 500 locally, then confirming claims complete
cleanly end-to-end once the two computed columns were added to the `RETURNING` clause.)

`POST /sightings` inserts the sighting (any `object_label`) and marks `ai_status='done'` in one DB
transaction (`db.complete_sighting`, temporarily flipping the module connection to
`autocommit=False`) -- this closes a small gap the old two-Postgres-node version had, where a
crash between Insert and Mark Done left the row `processing` until the next reap. One endpoint
handles every object type -- there's no `/sightings/vehicles` vs `/sightings/persons` split to
route between, since the row shape is identical regardless of label.

`/reports/generate` replaced what used to be two Postgres query nodes plus a Code-node HTML
builder inside `n8n/daily-report.json` (`report.py` now owns that logic) — this also fixed a real
bug: the old n8n version embedded the full `MAX_CROP_DIMENSION`-sized crop *twice* per row (once
for the visible thumbnail, once again in the click-to-enlarge lightbox — identical bytes both
times), which blew a 2-hour report window up to 42MB. `report.py` generates a real small
on-the-fly thumbnail per row (`THUMBNAIL_MAX_DIMENSION`, default 240px, via
`crop.scale_image_base64`) for the inline preview, and only embeds the full-size image once, in
the lightbox.

`/reports/generate` also takes the same `source=events|visits` param `/ai-queue/claim` does
(`report.generate_report`/`db.get_report_data`) -- `source=visits` applies the identical dedup
`only_visit_representative` does (see above: one sighting per distinct object type a visit
grouped, partitioned by `(visit_id, objects)`, plus every sighting whose raw_event was never
grouped into a visit at all), so one real-world visit spanning several det_ids of the *same*
object (re-track, label flicker) shows up once per object type in the report instead of once per
det_id. Unlike the `source` param on the AI-queue claim (which changes which rows are *eligible to
claim*, i.e. a live queue-state decision), this is a pure read-time filter over already-`done`
sightings -- it never touches `ai_status`, so `n8n/daily-report.json` (events, `source=events`,
the default) and `n8n/alerts-report.json` (visits, `source=visits`) can both run on their own
schedules without any conflict.

An optional `object_label` param (e.g. `car`) restricts the report to one Frigate object type --
for a "cars only" report alongside the default report covering every type. Applied as one more
`WHERE s.object_label = %s` clause in `db.get_report_data`'s query, so it composes with `source`
exactly like every other filter there: under `source=visits`, a visit spanning several object
types (e.g. a car and a person) still groups by `visit_id` as normal, just with only the
matching-type sighting(s) present in that group. The HTML title/caption gain a
`(<object_label> only)` suffix (`report.generate_report`) so a filtered report doesn't read
identically to the unfiltered one; the table itself needs no separate rendering path, since a
filtered query just returns fewer rows.

`source=visits`'s HTML also renders differently from `source=events`'s, not just differently
dedup'd: `report.py`'s `_group_by_visit` groups every sighting a visit produced (any mix of object
labels -- a car and a person, two cars, whatever Frigate actually grouped) into one combined alert
row (image, time, camera, one "Sightings" column) instead of one row per sighting -- a visit's
several sightings (e.g. someone getting out of their car) are the same real-world activity, so the
alerts report shows them together rather than as separately-scrolled, unrelated-looking rows a
reader has to manually reassociate by timestamp. Grouping key is `visit_id` (added to
`get_report_data`'s SELECT for exactly this), falling back to the raw_event's own id for a sighting
that was never grouped into a visit at all (a group of one, same as today). The earliest sighting
in a group represents its time/camera/image -- `crop_image_base64` always comes from that
representative event's own crop (there's no visit-level stored image to prefer instead, see
"Alert AI stage" above). `_build_alert_rows` joins each group's sightings into one labeled line per sighting
(`"{object_label}: {description}"`, e.g. `"car: orange suv, roof rails, plate 10MO407"` /
`"person: dark jacket"`), joined with `; ` -- there's no separate summary-flattening step, since
`description` already is the one-line summary for every object type.

An optional `include_alert_images` param (default `false`, `source=visits` only -- a no-op under
`source=events`, which has no visit-level image series concept) embeds an additional thumbnail
strip under each alert row's main image, for whichever visits actually have `STORE_ALERT_IMAGES`
data (`visits.alert_image_paths`). `db.get_report_data` only joins `visits` at all when this flag
is set (`LEFT JOIN yard_stats.visits v ON v.id = re.visit_id`, gated behind the param so a caller
that doesn't want this pays no extra join cost), and `report._alert_images_cell` reads each path
fresh off disk at report-generation time, base64-encoding it the same way `_img_cell` does for the
main crop -- a path that's gone (e.g. already cleared by a retention purge since the images were
gathered) is silently skipped, not fatal to the rest of the report. Off by default since it's a
real payload-size increase (several extra base64 images per alert row, on top of the same
double-embed concern this report already solved once for the old n8n version -- see below).

`source=events` (the default,
`n8n/daily-report.json`) renders one row per sighting with its own Type/Description columns --
there's no visit grouping concept to apply there, every sighting already stands alone.

### One AI-stage n8n processing workflow, not two

`n8n/metadata-processor.json` used to have a sibling, `n8n/metadata-processor-alerts.json`,
identical except for `source=events` vs `source=visits`+`visits_only=true` on their `Claim Next
Batch (API)` node -- kept as two workflows specifically because, at the time, `source=visits`
alone couldn't safely replace `source=events`: the dedup partitioned by `visit_id` alone, so a
visit grouping genuinely distinct object types (a car and a person) collapsed down to analyzing
only one of them, silently dropping the other. Since `only_visit_representative` now partitions by
`(visit_id, objects)` instead (see above), plain `source=visits` (no `visits_only`) is a strict
superset of the old `source=events` mode -- every ungrouped raw_event still gets analyzed
one-to-one via the fallback, every visit-grouped one gets analyzed once per distinct object type,
and same-type re-tracked duplicates still collapse to one. There's no longer a reason to run two
workflows or ever pick plain `source=events` -- `n8n/metadata-processor.json` became the only
processing workflow, using `source=visits` unconditionally; `metadata-processor-alerts.json` was
removed. `metadata-processor.json` itself has since been deleted too, once it was clear it needed a
real rework (see "Internal AI stage" below and the note near the bottom of this section) rather
than a quick fix to keep pace with the universal `/sightings` schema -- `ai_worker.py` is now the
only AI-stage implementation in this project, in n8n or otherwise.

### Internal AI stage (`ai_worker.py`) -- now the only AI-stage implementation

`metadata-processor.json`'s own logic -- claim work, call the VLM, parse the response, insert the
sighting -- was genuinely deterministic control flow; the only actual "AI" part is the VLM call
itself, which happens regardless of which language issues it. `ai_worker.py` is that same logic
ported straight into `ingest-worker` as a real, testable Python poll-loop stage, following the
exact same `process_claimed_event`/`run_once`/`run_forever` shape `crop_worker.py`/`video_worker.py`
already use -- own daemon thread, started conditionally in `main.py`
(`if config.AI_EVENTS_STAGE_ENABLED`), off by default like `STORE_VIDEO`/`AI_ALERTS_ENABLED`.
This is the **events** stage specifically -- always analyzes a raw_event's own single low-res
crop/snapshot, never the multiple high-res per-event crops the alert stage gathers; see "Alert AI
stage" below for the sibling stage that analyzes those. (Renamed from the original single
`AI_STAGE_ENABLED` once a second, independent stage existed to split from -- see that section for
why the split happened and the real gap it fixes.) It calls the exact same three `db.py`
functions n8n's HTTP calls already wrap -- `claim_ai_batch`, `fail_ai_event`,
`complete_sighting` -- directly rather than over HTTP, so **no
`db.py`/`api.py`/schema change was needed at all** for the queue mechanics. `claim_ai_batch` already
folds reap-stale + count-in-progress + capacity + claim into one call (unlike crop/video's claim
functions), so `ai_worker.run_once` is simpler than `crop_worker.run_once` -- just one call plus a
loop.

**This started as an alternative, not a replacement, but is now the only implementation** --
`n8n/metadata-processor.json` was originally left untouched and inactive in n8n specifically so the
n8n-driven flow could be re-enabled at any time; every relevant API endpoint (`/ai-queue/*`,
`/sightings/*`, `/search/semantic`, etc.) still fully supports that mode today, unchanged. But the
n8n file itself was never reworked for the universal `/sightings` schema (see the note near the
bottom of this section) and had drifted into duplicate-import clutter on the live n8n instance with
no upside over the maintained Python version, so it was deleted from this repo -- `ai_worker.py` is
now the only AI-stage implementation, in n8n or otherwise. Reviving an n8n-driven AI stage from
scratch is still possible (the API contract hasn't changed), it just isn't a matter of reactivating
an existing file anymore.

**Prompts and per-object-type model routing live in `frigate/profiles.yaml`, not env vars** --
`docker-compose.yml` bind-mounts this file (repo root, alongside `docker-compose.yml` itself) over
`/app/profiles.yaml` by default (read-only), the same path `AI_STAGE_PROFILE_PATH` already defaults
to -- so editing it and restarting the container is enough to change prompts/models, no image
rebuild needed. `frigate/ingest-worker/profiles.yaml` is a separate copy still baked into the image
via the Dockerfile's `COPY . .` (same as `schema.sql`) purely as a fallback default for if that
bind mount is ever removed -- the two aren't linked, keep them in sync by hand if you edit one.
**Flat structure, one level, universal across every object type**: `object_types` maps a Frigate
object label (`raw_events.objects`, e.g. `car`, `truck`, `person`, or any future label like `dog`)
directly to that label's own `{chat_path, timeout_seconds, event_prompt, alert_prompt}` -- there is
no intermediate `sighting_type`/"vehicle-or-person" grouping level, and no separate
`vehicle:`/`person:` sections holding shared config. `chat_path` is appended to
`LLAMA_PROXY_BASE_URL` (`llama_slot_proxy`'s convention is one URL path segment per model slot,
e.g. `/spare/v1/chat/completions`, not a `model` field in the request body); two labels that should
share one model/prompt (e.g. `car` and `truck`) just point at the same YAML anchor
(`&vehicle_profile`/`*vehicle_profile`) rather than needing a grouping concept in the schema.
`event_prompt` (this stage -- framed for the single static frame it actually receives) and
`alert_prompt` (the alert stage below -- framed for the series of separate high-res crops it
receives instead) are plain free-text instructions, e.g. "describe the vehicle's color, body type,
and the license plate text if visible" or "describe their clothing colors and what they appear to
be doing" -- the model's
answer is stored verbatim as `description`, with **no JSON schema requested and no response
parsing at all**. This is deliberate, not a simplification left for later: a fixed JSON
schema/per-field parser is exactly the kind of per-type structure this redesign removed -- adding a
new object type is a `profiles.yaml` edit alone, never a new parser function. **A Frigate object
label with no `object_types` entry (e.g. a label you haven't written a prompt for yet) is simply
never claimed by this stage at all** -- its `object_types` keys become exactly the `object_types`
list passed to `claim_ai_batch`, so an unmapped type's rows just stay `ai_status='new'`
indefinitely, the same "nothing to do" treatment described below for the alert stage.

`ai_worker.parse_sighting_response` is a two-line function: pull `response["choices"][0]
["message"]["content"]` as-is for `description`, and `row["objects"]` as-is for `object_label` --
there is no regex/JSON-extraction/plate-sanitizing step of any kind, since there's no structured
shape to extract. Embed text is just that same `description` string, passed straight to
`_embed_text` -- `report.py` no longer has a `_vehicle_summary`/`_person_summary` combination step
to reuse, because `description` already **is** the one-line summary for every object type; the
report, Telegram, and the embedding call all read the identical field. An embedding-call failure
falls back to `embedding=None` rather than losing the whole sighting (same decision the n8n version
used) -- only a chat-call failure routes to `db.fail_ai_event(event_id,
config.AI_STAGE_MAX_ATTEMPTS)`, mirroring `crop_worker.py`'s except-block pattern exactly.

`LLAMA_PROXY_BASE_URL`/`LLAMA_PROXY_TOKEN`/`LLAMA_PROXY_EMBED_PATH` point this stage at
`llama_slot_proxy` directly, the same host n8n's VLM nodes already call -- `LLAMA_PROXY_TOKEN` is
optional (blank means no `Authorization` header at all, since `llama_slot_proxy` is unauthenticated
on the LAN today, same as every VLM call n8n makes directly); it exists for whenever that changes,
not because it's required now.

Each `profiles.yaml` type entry has its own `timeout_seconds` for that type's chat-completion call
(falls back to `AI_STAGE_DEFAULT_TIMEOUT_SECONDS`, default 180, if omitted) -- a local model's
response time genuinely depends on which model/prompt is selected (a longer combined-attributes-
plus-plate prompt vs. a short one-sentence description prompt), so this is a per-type profile value,
not a single global one. The embedding call gets its own separate, shorter default
(`AI_STAGE_EMBED_TIMEOUT_SECONDS`, default 60) -- a single forward pass, not autoregressive
generation, so normally much faster regardless of which chat model/prompt was used for the same
row. A timeout still counts as a failure for retry-with-a-cap purposes -- it routes to
`db.fail_ai_event` exactly like any other chat-call exception (see above), it isn't a special case.

Each poll tick's claimed batch is processed sequentially within the thread (one `_chat_request` at
a time, same limitation `video_worker.py` already has regardless of its own `*_PARALLEL_LIMIT` --
see "Video storage" below) -- a slow call only delays this stage's own next claimed row, never the
crop/video/visit-thumb-crop stages, MQTT ingestion, or the FastAPI app, since each runs in its own
daemon thread and Python releases the GIL during the blocking HTTP wait. The one shared resource is
the single global Postgres connection (`db.get_conn()`) every thread already uses -- `ai_worker.py`
only touches it briefly, for the claim and the final insert, never while waiting on the VLM/
embedding response.

### Alert AI stage (`alert_ai_worker.py`) -- analyzes a series of ephemeral high-res per-event crops

#### History: single-frame bug, then a composite-grid fix, now superseded

`profiles.yaml`'s `alert_prompt` was originally written as if it were analyzing a 2x2 grid, but
`ai_worker.py` never actually requested one -- every alert-stage VLM call was silently analyzing a
plain single low-res frame while being told it was looking at 4 frames of motion. The fix at the
time added a genuinely separate stage/queue (`AI_ALERTS_ENABLED`, `visits.alert_ai_status`, this
same `alert_ai_worker.py`) that built and stored a real composite 2x2 grid image plus a matching
animated GIF (`visits.crop_image_base64`/`preview_gif_base64`, via `crop.build_visit_preview`,
sampling 4 frames proportionally across the visit's own clip) -- the grid was what got analyzed,
the GIF was a human-preview-only artifact for the web UI/Telegram/report. **This grid+GIF design
has since been removed entirely** (schema columns kept but unwritten -- see the schema-migration
note near the end of this section -- retention purge, web UI, Telegram, and the report all no
longer reference either artifact) in favor of the design described below: the fixed 4-frame,
proportionally-sampled composite was low-resolution and didn't reliably land on the moment worth
seeing, while a genuine per-event high-res crop (already built by the crop stage for every
`raw_event`, see "Cropping" above) was sitting right there unused by this stage. `visits.thumb_time`
and the `VISIT_THUMB_CROP_ENABLED`/`VISIT_PREVIEW_FRAME_PERCENTAGES` knobs described in earlier
history of this feature no longer exist.

#### Current design: gather several real high-res crops, ephemerally, per visit

`alert_ai_worker.process_claimed_visit` no longer depends on any stored visit-level image at all.
Per claimed visit, it:

1. Calls `db.get_raw_events_for_visit(visit_id)` -- a plain read (no claim/lock) of every
   `raw_events` row Frigate's review actually linked to this visit (`id`, `det_id`, `objects`,
   `start_ts`, `end_ts`), ordered `(objects, start_ts)`.
2. Runs `_select_events_for_alert(events, config.ALERT_AI_MAX_IMAGES)` to pick which of those
   linked events to actually send: one representative per distinct object type first (sorted by
   `start_ts`), then -- if still under the cap -- fills remaining slots with a temporally-spread
   re-track from each same-type bucket (taking the middle element of what's left in that bucket
   each round), so a visit with many re-tracked det_ids of the same object still gets a handful of
   images spread across the visit's span rather than just its very first and last moments.
3. Calls `_gather_alert_images`, which runs `crop.crop_event_high_res` (see "Cropping" above --
   the same durable, event-id-scoped Frigate clip endpoint/seek-and-crop logic the events stage's
   own non-snapshot crop path already uses, factored out into its own function) once per selected
   event, returning `(event, image)` pairs rather than a bare image list (a per-event failure means
   the two lists can end up different lengths, and the optional disk-persistence step below needs
   each image's own source event, not just the flat list the VLM call itself uses). **Ephemeral in
   memory by design**: each image is fetched/cropped, added to the in-memory list sent to the VLM,
   and then discarded by default -- nothing is written back to `raw_events`/`visits` unless
   `store_alert_images` opts in (see below). A single event's crop failing (e.g. its clip has
   already rolled off Frigate's retention) is caught and logged, not fatal to the whole visit --
   only an empty resulting image list raises (routing to `fail_alert_ai_event`, the same
   retry-with-a-cap `alert_ai_status` already has).
4. If `store_alert_images` (see below) resolves true for this visit's representative object type,
   persists the gathered images to disk (`alert_images.store_alert_images`) and records the
   resulting paths on `visits.alert_image_paths` (`db.set_visit_alert_image_paths`) -- see
   "Optional filesystem persistence" below. This runs *before* the chat call, so the images survive
   on disk even if that call subsequently fails; best-effort and non-fatal (a disk-write failure is
   logged and swallowed, never blocking the analysis that already has its images in hand).
5. Calls `ai_worker._chat_request` with the full `images` list and `alert_prompt` -- see "Cloud VLM
   providers" below for how a multi-image call actually reaches the model.

#### Optional filesystem persistence (`STORE_ALERT_IMAGES`/`store_alert_images`)

Off by default -- the gathered images stay purely ephemeral (built, sent to the VLM, discarded),
matching the "smaller dataset" goal this whole redesign was built around. Turning it on for a
type persists that type's gathered images to disk, mirroring how video storage already works:
only the file paths live in Postgres (`visits.alert_image_paths`, comma-joined, same convention as
`visits.objects`), the actual JPEG bytes live only under `ALERT_IMAGES_STORAGE_PATH` on disk --
its own bind mount/host path (`ALERT_IMAGES_STORAGE_HOST_PATH`), not a subfolder of either video
storage location, so this flow's disk usage can be measured/managed independently too. A plain
per-row resolver (`profile_config.store_alert_images`, same `_resolve`-style tier as
`crop_disabled`), **not** a claim-filter/thread-gating setting like `store_video`/
`store_video_alerts` -- persisting is a synchronous side effect inside the existing alert stage
thread, not a separate poll loop/queue stage, so there's no `any_store_alert_images_enabled`/claim
filter machinery needed.

`alert_images.store_alert_images(visit, events, images)` writes one file per gathered image under
`{ALERT_IMAGES_STORAGE_PATH}/{camera}/{YYYY}/{MM}/{DD}/visit-{object_type}-{visit_id}-{index}-
{event_id}.jpg` -- the same camera-first layout `video.store_visit_clip` already established (so
`admin.dir_size_bytes`/`dir_size_by_object_type`/`dir_size_by_camera` apply unchanged, no new
filesystem-walk logic needed). Each file is named after *that specific image's own source event's*
object type, not the visit's overall representative type -- gives an accurate per-type disk-usage
breakdown even for a visit spanning distinct types (e.g. a car and a person). Filenames are
deterministic (visit id + index + event id, not a timestamp), so a retried attempt overwrites the
same files rather than accumulating duplicates on disk.

`GET /visits/{id}/sightings` exposes `alert_image_count` (how many were stored, 0 if the option
was off or nothing was stored) alongside the existing `alert_sighting` field, and a new
`GET /media/alert-image/{visit_id}/{index}` endpoint serves one stored image by its 0-based index
-- the web UI's Visit lightbox uses these to render a small thumbnail-strip gallery of exactly
what the alert stage analyzed, each thumbnail opening the full-size image in a new tab (not a
nested lightbox-within-lightbox). `/reports/generate`'s optional `include_alert_images` param
(source=visits only) embeds this same series as an additional thumbnail strip under each alert
row's main image, reading the files fresh off disk at report-generation time (a missing/
already-purged file is silently skipped, not fatal to the report). `/retention/purge`'s
`delete_alert_images` flag (default `true`, `only_media=true` mode) clears both the files and the
column independently of `delete_video`/`delete_snapshots` -- same visits-only scoping `delete_video`
already has under an `object_label`-scoped purge (never touched, since a visit can span multiple
object types). `run_retention_cleanup`/`purge_older_than` (the full-row-delete paths) also collect
and delete any `alert_image_paths` files before their visit rows are removed, same as they already
do for `video_path`.

`ALERT_AI_MAX_IMAGES` (default `4`, matching the old grid's panel count as a starting point) and
`ALERT_AI_INITIAL_WAIT_SECONDS` (default `5`, applied once per visit on its very first attempt,
not once per image -- same idea as `CROP_INITIAL_WAIT_SECONDS`/`VIDEO_INITIAL_WAIT_SECONDS` giving
Frigate a moment to settle) are both global technical-tuning knobs, resolved via
`config.apply_profile_defaults`'s `defaults:`-section mechanism like every other knob in that
family (see "Per-object-type overrides" below).

- **`AI_EVENTS_STAGE_ENABLED`** (renamed from `AI_STAGE_ENABLED`) -- `ai_worker.py`, unchanged
  behavior, explicitly framed as the events-only stage. Uses `profiles.yaml`'s `event_prompt`.
- **`AI_ALERTS_ENABLED`** -- `alert_ai_worker.py`, this stage, claiming from **`visits`**, not
  `raw_events`, via its own queue-state-machine column, `visits.alert_ai_status` (same
  `new -> processing -> retry/failed -> done` shape, plus `alert_ai_status_changed_at`/
  `alert_ai_attempt_count`, `idx_visits_alert_ai_status`). Uses `profiles.yaml`'s `alert_prompt`.
  A visit is claimable as soon as it exists (subject to the usual reap-stale/capacity/object-type
  filters) -- there's no readiness gate on a stored artifact to wait on anymore, since every image
  this stage sends is gathered fresh at processing time.

Both stages can run at once, on or off independently -- an event's own `ai_status` and its visit's
`alert_ai_status` are two entirely separate state machines on two separate tables, so the same
underlying activity can be analyzed once per event (events stage) and once per visit (alerts
stage) without either blocking or overwriting the other. Both are started conditionally in
`main.py`, one `threading.Thread` each, same shape as every other opt-in poll-loop stage.

#### `db.claim_alert_ai_batch` -- matching a visit to a single object type despite `visits.objects` being multi-valued

`visits.objects` (populated by `record_visit` from Frigate's own `data.objects`, comma-joined --
`mqtt_ingest.py`'s `",".join(data.get("objects") or [])`) can legitimately span more than one
distinct type per visit (e.g. `"car,person"` -- see "Visit grouping" above). `object_types`
filtering for this claim matches against the visit's own **representative** event's `objects`
(`db.get_representative_event_for_visit`'s definition -- earliest-linked raw_event, `ORDER BY
start_ts ASC, id ASC LIMIT 1`), joined in via `LATERAL` inside the claim's CTE, not `visits.objects`
-- a visit spanning both a car and a person still gets exactly one alert analysis, of whichever
type the representative event actually is (`_select_events_for_alert`, above, still gathers images
from every distinct type the visit linked, regardless of which one is "representative" for
claim-filtering purposes -- those are two separate concerns). (This is a different, narrower
matching concern from `claim_ai_batch`'s own `(visit_id, objects)` partitioning for
`only_visit_representative` -- that dedups *raw_events* per type per visit for the *events* stage;
this alerts-stage claim never touches `raw_events.ai_status` or that partitioning at all.) Same
reap-stale + count-in-progress + CTE-`FOR UPDATE SKIP LOCKED` shape every other claim function in
this project uses, newest-`start_ts`-first, with the same optional `max_age_hours` throughput
safety valve `claim_ai_batch`/`claim_video_batch` already have.

#### Storage: `visit_sightings`, the visit-level twin of `sightings`

`visit_sightings` -- same universal shape as `sightings` (`object_label`, `description`,
`embedding`, its own nullable HNSW index sized off `EMBEDDING_DIMENSIONS`), but keyed by
`visit_id` instead of `raw_event_id`. Chosen over reusing `sightings` (adding a nullable `visit_id`
+ making `raw_event_id` nullable + a source discriminator) specifically because every other
alerts-vs-events split in this project already keeps the two flows' storage fully separate rather
than overloading one table/column set for both (`STORE_VIDEO_ALERTS`'s own `video_path`/storage
directory vs. `raw_events.video_path`). `db.complete_visit_sighting` mirrors `complete_sighting`'s
insert-plus-mark-done-in-one-transaction shape exactly, just against `visits.alert_ai_status`
instead of `raw_events.ai_status`. `alert_ai_worker.parse_alert_sighting_response` mirrors
`ai_worker.parse_sighting_response` -- same two-line "take the response content and the
representative event's `objects` as-is" shape, no parsing of any kind either. `alert_prompt` can
(and does, in the shipped vehicle/person/dog prompts) ask the model to describe what changed
across the gathered series (e.g. "pulled into the driveway and parked") -- that's just part of the
free-text `description` now, not a separate structured `notes` field the way the old per-type
schema had one.

#### Web UI: `GET /visits/{id}/sightings` gains `alert_sighting`, preferred over the per-event fallback

`db.get_visit_alert_sighting` returns the visit's own `visit_sightings`
row if one exists, `null` otherwise -- wired into the existing
`GET /visits/{id}/sightings` response as one more field (`alert_sighting`) alongside the unchanged
`sightings` list, rather than a second endpoint, so the web UI's visit lightbox only
needs the one fetch it already made. `static/app.js`'s `openLightbox` now prefers
`data.alert_sighting` when present (labeled "{object_label} (alert analysis)"
in the lightbox) and only falls back to the per-event `sightings` list when it's `null`
-- the same "richer artifact when available, graceful fallback otherwise" precedent this project
already uses elsewhere. This is deliberately a fallback, not an exclusive switch: a visit whose
alert stage is off, or hasn't finished yet, still shows whatever per-event analysis already exists
instead of an empty lightbox. On the Events tab (plain events, never visits), `GET /events/{id}`'s
`sighting` -- the events stage's own result -- is unaffected and unchanged; the alert stage/
`alert_sighting` field only ever applies to the Visits tab. A visit's own thumbnail/lightbox image
(`GET /visits/{id}/thumbnail`, `GET /visits/{id}/image`) now always falls back to the
representative event's own crop (then a frame pulled from the visit's stored video, same
belt-and-suspenders chain the events endpoint uses) -- there is no visit-level stored image of its
own left to prefer, now that the grid is gone.

#### Schema migration note

`visits.crop_image_base64`/`preview_gif_base64`/`thumb_crop_status`/
`thumb_crop_status_changed_at`/`thumb_crop_attempt_count` and `idx_visits_thumb_crop_status`
remain in `schema.sql` for now, unwritten and unread by any code path -- dropping them is a
separate, later migration (explicit backup via `backup-postgres-projects.sh` plus explicit
confirmation first, since it destroys real historical grid/GIF data permanently), not bundled with
this change.

### Cloud VLM providers (OpenAI / Claude) as an alternative to `llama_slot_proxy`

Both internal AI stages (`ai_worker.py`/`alert_ai_worker.py`) originally spoke exactly one wire
shape for their chat call -- `llama_slot_proxy`'s OpenAI-compatible chat-completions API, model
selection entirely via `chat_path` (one URL path segment per slot), no `model` field in the body
at all. That single-provider assumption was lifted into a **per-object-type provider dispatch**:
`ai_worker._chat_request(type_config, prompt, images, timeout)` (`images: list[str]` -- one or
several base64 JPEGs, widened from a single image specifically so the alert stage's gathered
series, see above, can be sent in one call) now reads `type_config["provider"]` (`profiles.yaml`,
same tier the always-per-type `chat_path`/`event_prompt`/`alert_prompt`/`timeout_seconds` already
live at -- **not** `profile_config.py`'s two-tier `defaults:`-then-hardcoded-fallback resolver,
since there's no sensible profile-wide default for "which cloud account" the way there is for e.g.
`crop_padding_pct`) and dispatches to one of three private request builders:
`_llama_proxy_chat_request` (today's original behavior, unchanged for the single-image case, and
still the default when `provider` is omitted entirely -- an existing deployment's `profiles.yaml`
needs no edit), `_openai_chat_request`, or `_anthropic_chat_request`. OpenAI/Anthropic build one
image content block per entry in `images`, so a multi-image call is native to those two providers;
`_llama_proxy_chat_request` only ever sends `images[0]`, logging a warning once
(`_warned_llama_proxy_multi_image`) if handed more than one -- multi-image support on a self-hosted
backend is unverified, so this is a graceful degradation rather than an error (see `alert_provider`
below for how a type actually avoids hitting this path for its alert calls).
`alert_ai_worker.process_claimed_visit` calls the exact same `ai_worker._chat_request` (it already
imported `ai_worker` and called its `_chat_request` directly, pre-dating this change) with its own
resolved `type_config`/`alert_prompt`/gathered `images` list, so both stages get every provider for
free from one dispatch point -- no alert-stage-specific provider code exists anywhere.

**`alert_provider`/`alert_model`/`alert_chat_path`** -- optional per-object-type overrides, same
tier as `provider`/`model`/`chat_path` above, letting one type's *alert* analysis route to a
different provider than its own *event* analysis (the natural use case this refactor introduced:
route event-stage single-frame calls to the existing local `llama_slot_proxy` slot as before, but
route the alert stage's multi-image series to a hosted provider that actually supports it).
`alert_ai_worker._resolve_alert_type_config(type_config)` builds an effective config dict with
`provider`/`model`/`chat_path` overridden by the `alert_*` keys whenever present, falling back to
the plain keys unchanged when they're absent -- so a `profiles.yaml` that only sets `provider` (no
`alert_*` overrides) behaves identically for both stages, the same "omitting the key preserves old
behavior" convention `provider` itself established.

OpenAI's Chat Completions API is close enough to `llama_slot_proxy`'s own (deliberately
OpenAI-compatible) shape that `_openai_chat_request` reuses the identical message/content-block
structure (`image_url` with a `data:image/jpeg;base64,...` URI) -- the only real differences are
the base URL/auth (`OPENAI_BASE_URL`/`OPENAI_API_KEY`, `Authorization: Bearer` header) and that
OpenAI selects the model via a `"model"` body field (`type_config["model"]`) rather than the URL
path, since OpenAI has no per-model URL convention the way a self-hosted multi-slot proxy does.
Claude's Messages API is a genuinely different shape, not just a different host:
`_anthropic_chat_request` posts to `{ANTHROPIC_BASE_URL}/v1/messages` with `x-api-key`/
`anthropic-version` headers (not `Authorization: Bearer`), an image `source` block instead of a
data-URI `image_url` (`{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
"data": ...}}`), and a required top-level `max_tokens` -- unlike the other two providers, Claude's
API has no server-side default and 400s without it. `max_tokens` follows the identical two-tier
shape `timeout_seconds` already established (`type_config.get("max_tokens",
config.AI_STAGE_DEFAULT_MAX_TOKENS)`, default `1024`), for the same reason: a type-appropriate
value (a one-sentence person description needs far fewer output tokens than a detailed vehicle
plate/make/model/features answer), not a single global constant with no per-type escape hatch.

Response parsing has the identical branch point, in reverse: `ai_worker._extract_response_text
(response, type_config)` reads `content[0]["text"]` for `provider: anthropic`, else falls through
to the original `choices[0]["message"]["content"]` shape both `llama_proxy` and `openai` share.
`type_config` is optional (defaults treat a missing/`None` config as the original shape) so every
pre-existing caller/test that only ever dealt with the OpenAI-compatible response continues to
work with no signature change forced on it -- `parse_sighting_response`/
`parse_alert_sighting_response` both grew an optional third `type_config` parameter for exactly
this reason, threaded through from `process_claimed_event`/`process_claimed_visit`, which already
had the row's `type_config` in scope. No JSON parsing was added on either branch -- this is
strictly "which response envelope holds the text," not a return to the structured-response world
this project deliberately left behind (see "Universal sightings" above); the extracted string
still becomes `sightings.description`/`visit_sightings.description` verbatim regardless of which
provider produced it.

**Embeddings are a separate axis, deliberately not folded into the same per-type `provider`
key.** Claude has no embeddings endpoint at all, so a type routed to `provider: anthropic` for its
chat/description call still needs semantic search's embedding step pointed somewhere else --
`config.EMBEDDING_PROVIDER` (`llama_proxy` default, or `openai`) is a **global** `.env` setting,
not a `profiles.yaml` per-type one, read by a new shared `ai_worker._embed_request(text, timeout)`
helper that both `_embed_text` (the AI-stage's own post-chat embed step) and `embed_query_text`
(the web UI Search tab's query-embed path) now call instead of building the `requests.post` call
inline themselves. `OPENAI_EMBED_MODEL` (default `text-embedding-3-small`, 1536 dimensions) only
matters when `EMBEDDING_PROVIDER=openai`; switching providers still means updating
`EMBEDDING_DIMENSIONS` and re-running `POST /embeddings/backfill?confirm=true` for the same reason
switching local embedding models already required one (an incomparable vector space regardless of
dimension) -- this migration cost is unchanged by this feature, not new.

This is opt-in and additive in every direction: a deployment that never sets `provider` in
`profiles.yaml`, never sets `EMBEDDING_PROVIDER`, and never sets `OPENAI_API_KEY`/
`ANTHROPIC_API_KEY` behaves byte-for-byte identically to before this existed (confirmed by the
full existing test suite passing unmodified). See `frigate/profiles.yaml.example`'s `car` entry
and `frigate/.env.example` for the exact keys, and `docs/configuration.md`'s "Hosted VLM providers"
section for the operational/cost/privacy tradeoffs and which provider tends to suit which kind of
description task.

### Per-object-type overrides (`profile_config.py`)

A number of settings live entirely in `profiles.yaml`, not `.env` -- deliberately: these are all
settings you'd realistically want different per Frigate object type, so this file (not a split
between `.env` and here) is the one place to configure them. These settings were originally plain
env vars, then grew a per-type-override capability on top while keeping the env var as the global
default; that middle stage is gone now -- the env vars themselves were removed from `config.py`,
`docker-compose.yml`, and `.env.example` entirely, since keeping the same setting configurable in
two different files was confusing without adding real flexibility (a `profiles.yaml`-only
`defaults:` section already covers "set it globally" just as well as an env var did). Two tiers,
checked in order: a type's own `object_types.<label>` entry (highest), then a profile-wide
`defaults` section (a common value applied to every type that doesn't set its own -- for "change
this everywhere except one or two exceptions"). If neither tier sets a given key, resolution falls
through to a plain Python constant in `config.py` -- a hardcoded last-resort default matching this
project's original behavior, **not** a third configurable tier: nothing backs it with an env var
any more, so changing it means editing `config.py` and building a new image, the same as changing
any other hardcoded literal in this codebase would. Every resolver lives in `profile_config.py` --
a small, pure (no I/O, no caching) module built around one shared `_resolve(profile, object_label,
key, hardcoded_default)` helper that walks the two tiers.

Two families of overridable settings:

- **Plain per-row settings**, resolved fresh for whatever row is currently being processed:
  `telegram_events_mode`, `telegram_alerts_mode`, `ai_events_stage_enabled`, `ai_alerts_enabled`
  (the original four), plus `crop_disabled`, `crop_frame_offset_pct`, `crop_padding_pct`,
  `frigate_snapshot_enabled` (the crop-family settings `crop.py`'s `crop_event`/`crop_and_scale`/
  `crop_event_high_res` now accept as optional overrides instead of only ever reading
  `config.CROP_DISABLED`/etc. directly -- `None` still means "use the global config value", so
  every other caller is unaffected). None of these have any claim-time/thread implications --
  `crop_worker.py` already processes every object type regardless, so resolving per-row is enough.
- **`store_video` / `store_video_alerts`** -- these gate a whole poll thread (`main.py`, via
  `profile_config.any_store_video_enabled`/`any_store_video_alerts_enabled`, same "per-type
  override can start it even when the global default is off" precedent the two AI-stage flags
  already established) *and* narrow which rows their claim function is even allowed to look at
  (`claim_video_batch`/`claim_visit_video_batch`, each taking optional
  `object_types`/`exclude_object_types` params). Unlike the AI-stage flags (which only ever apply
  to types with a `profiles.yaml` prompt entry in the first place), these two apply to *any*
  Frigate label by default -- so their resolvers (`profile_config.store_video_claim_filter`/etc.)
  deliberately return an **include-or-exclude split**, never a plain include-list checked against
  every "known" label: if the effective base (the `defaults` section, else `config.py`'s hardcoded
  fallback) is enabled, only the explicit per-type opt-outs need excluding (or `(None, None)`, i.e.
  no filter at all, exactly the unfiltered query this project ran before per-type overrides
  existed); if the base is disabled, only the explicit per-type opt-ins are eligible. This avoids a
  real regression an include-list approach would have introduced: `OBJECT_TYPES` (the env var
  powering the web UI's Type dropdown) has always been cosmetic-only, never a pipeline allow-list,
  so filtering against it as a completeness enumeration would have silently stopped storing video
  for any real Frigate label that was never added to it. `claim_visit_video_batch` applies this
  filter via a `LATERAL`-joined representative event (same convention `claim_alert_ai_batch`
  already uses), not `visits.objects`, for the same multi-type-per-visit reason described
  elsewhere in this doc.

`main.py` loads `profiles.yaml` once at startup and threads that same dict down to every worker
that needs it (`crop_worker`/`video_worker`/`mqtt_ingest`/`alert_video_worker`/`ai_worker`/
`alert_ai_worker`) rather than each thread re-reading the file independently.
`ai_worker.run_once`/`alert_ai_worker.run_once` filter which types actually get claimed via
`profile_config.ai_events_stage_enabled`/`ai_alerts_enabled` per label, so a type that doesn't want
this stage never gets claimed even while the thread itself is running for other types;
`video_worker.run_once`/`alert_video_worker.run_once` do the analogous thing via their own
`*_claim_filter` functions, additionally skipping the claim call entirely (rather than calling it
with an always-empty filter) whenever nothing at all is enabled.

Telegram's four send functions (`telegram.send_photo`/`send_video`/`send_visit_summary`/
`send_visit_video`) each gained an optional `mode: str | None = None` parameter -- when a caller
passes an already-resolved mode (`profile_config.telegram_events_mode`/`telegram_alerts_mode`),
that wins; omitted (`None`), the function falls back to the matching global `config.
TELEGRAM_EVENTS_MODE`/`TELEGRAM_ALERTS_MODE` exactly as before this existed. `crop_worker.py`/
`video_worker.py` resolve against the claimed row's own `objects` label directly. The alerts-flow
callers (`mqtt_ingest.py`'s immediate summary, sent right after `record_visit` succeeds, and
`alert_video_worker.py`'s video reply) all resolve against the visit's own **representative**
event's `objects` (`db.get_representative_event_for_visit`), not `visits.objects` -- the same
single-type-per-visit convention `claim_alert_ai_batch` already uses, since a visit can span
multiple distinct object types (`visits.objects` is comma-joined) but there's still exactly one
representative event whose type the notification is resolved against.
`mqtt_ingest.py` stores the loaded profile in a module-level `_profile` (set once by
`start(profile)`) rather than threading it through every MQTT callback argument, since paho-mqtt's
`on_message` signature is fixed and leaves no room for an extra parameter.

**A second, unrelated category also lives in `defaults:` now**: plain technical tuning knobs with
no per-object-type meaning at all -- `parallel_limit`/`stale_minutes`/`max_attempts`/
`crop_initial_wait_seconds`/`max_crop_dimension`/`thumbnail_max_dimension`/`poll_interval_seconds`,
`retention_months`/`retention_check_interval_seconds`, the `video_*`/`ai_stage_*`/`alert_ai_*`
queue-tuning equivalents, and `ai_stage_default_timeout_seconds`/
`ai_stage_embed_timeout_seconds` (see `config.py`'s own comment for the exact list and each one's
hardcoded fallback). These were plain env vars with no override capability at all until now; there
was never a reason to make them *per-object-type* resolvable (there's no "`PARALLEL_LIMIT` for cars
only"), but the user still wanted `.env` reserved for genuinely external facts (connection info,
paths, tokens, URLs) and everything else centralized in `profiles.yaml`. Rather than inventing a
second top-level YAML section for "global-only technical settings" alongside `defaults:` (which
already means "the global tier" from the per-object-type reader's perspective), these reuse the
exact same `defaults:` section -- just resolved differently under the hood:
`config.apply_profile_defaults(profile)` (a new `config.py` function, driven by a
`_PROFILE_DEFAULTS_MAP` of `{CONSTANT_NAME: "profiles.yaml key"}` pairs) overwrites the
corresponding module-level constants **once**, called from `main.py` right after `profile` is
loaded and before any worker thread starts -- not a per-call resolution like
`profile_config.py`'s functions, since these settings can't vary by row/type anyway. This works
because every reader of these constants elsewhere in the codebase (`crop_worker.py`, `db.py`,
`retention.py`, `video.py`, `ai_worker.py`, etc.) does a plain `config.SOME_SETTING` module-attribute
access rather than `from config import SOME_SETTING` (confirmed via grep -- the latter would freeze
a stale copy at import time and silently ignore the override), so overwriting the attribute once at
startup is sufficient for the new value to reach every caller with no signature threading needed
anywhere. `EMBEDDING_DIMENSIONS` and `RECORD_WIDTH`/`RECORD_HEIGHT` deliberately stay plain env
vars and were not swept into this migration: the former because `db.ensure_schema()` reads it
*before* `profiles.yaml` is even loaded in `main.py` and changing it has real DB-migration
implications (a backfill), the latter because they describe camera hardware, not a tunable
behavior.

**Migrating from the env-var era**: an existing deployment with, say, `STORE_VIDEO=true` in `.env`
needs that moved into `profiles.yaml`'s `defaults:` section (`defaults: {store_video: true, ...}`)
to keep behaving identically after upgrading -- the env var is silently ignored once this ships
(`docker-compose.yml` no longer even passes it through), not an error, so double-check
`profiles.yaml` actually has the equivalent `defaults:` entries before/immediately after upgrading
rather than assuming the old `.env` values still apply. Same applies to any of the technical tuning
knobs above that were previously set to a non-default value in `.env`.

**Bug found and fixed while migrating `store_video`/`store_video_alerts` off their env vars**:
`db.insert_raw_event`/`db.record_visit` (which decide a freshly-ingested row's *initial*
`video_status` -- `'new'` vs `'skipped'`, see the queue-state-machine section above) were still
reading the bare `config.STORE_VIDEO`/`config.STORE_VIDEO_ALERTS` constants directly, never
resolving through `profile_config.py`. This gap existed from the very first per-object-type-
overrides round (these settings only ever affected which *worker thread* started and which rows a
*claim* function could see, never the ingest-time initial value) but stayed invisible as long as a
matching env var kept the global constant in sync with what a deployment actually wanted. Once the
env var was removed entirely (this round), the constant became a permanently-hardcoded `False`
with no way to override it at ingest time at all -- confirmed live in production: `profiles.yaml`'s
`defaults: {store_video_alerts: true}` correctly started the matching worker thread and correctly
scoped its claim query, but every new visit still got `video_status='skipped'` at insert time
regardless, so the worker never had anything to claim. Fixed by adding plain per-label resolvers
(`profile_config.store_video_enabled`/`store_video_alerts_enabled`) and threading `profile` into
`insert_raw_event`/`record_visit` (both optional params, defaulting to `None` for backward
compatibility). `record_visit` resolves the visit's representative object type via a new
`_get_representative_object_label_for_det_ids` helper -- the usual `get_representative_event_for_
visit` can't be used yet at this point, since the visit row (and its `raw_events.visit_id` link)
doesn't exist until later in the same function; this queries by `det_id` instead, same
`ORDER BY start_ts ASC, id ASC LIMIT 1` convention. (This bug predates, and is unrelated to, the
now-removed visit-preview grid/thumb-crop stage -- see "Alert AI stage" above -- which used to have
its own analogous per-type resolver and a Telegram-summary-deferral mechanism, both deleted along
with that feature; `mqtt_ingest.py`'s visit summary is sent immediately now, with no deferral of
any kind.)

### Video storage, Telegram notifications, and the web report UI

`STORE_VIDEO=true` turns on the third queue stage (`video_status`) and its poll loop thread
(`video_worker.py`/`video.py`). Frigate is often still finalizing the recording segment when the
`end` event fires, so a freshly claimed row waits `VIDEO_INITIAL_WAIT_SECONDS` before the first
download attempt; the clip is fetched from Frigate's own
`/api/{camera}/start/{start_ts-5s}/end/{end_ts+5s}/clip.mp4` endpoint (not the event-id endpoint
`crop.py` uses), and a response at/below `VIDEO_MIN_VALID_BYTES` is treated as Frigate's
not-ready-yet placeholder rather than a real clip, retried up to `VIDEO_MAX_ATTEMPTS` times. Only
the resulting filesystem path (`VIDEO_STORAGE_PATH/{camera}/{YYYY}/{MM}/{DD}/{object_type}-
{event_id}-{start_ts_epoch}-{start_ts_iso}.mp4` -- epoch for a stable/sortable key, an ISO-ish UTC
timestamp alongside it since the epoch alone isn't recognizable at a glance in a directory
listing) is stored in Postgres (`video_path`) -- the file itself lives on disk only. The
camera-first directory (`video.store_clip`/`store_visit_clip`, `row.get("camera")`/
`visit.get("cameras")`, falling back to `"unknown"` if somehow absent) lets disk usage/backup/
retention be reasoned about per-camera directly on the filesystem, the same thing that motivated
the admin dashboard's "By camera" disk-usage breakdown (`admin.dir_size_by_camera`, see "Admin
dashboard" below) -- it only works because the camera is now the top-level directory, not
something to parse out of a filename. This is the layout for newly-stored files only: a clip
stored before this existed sits directly under a `{YYYY}/{MM}/{DD}` folder with no camera
directory above it, and nothing migrates existing files into the new layout automatically (the
value stored in `raw_events.video_path`/`visits.video_path` is always whatever `store_clip`/
`store_visit_clip` actually returned, so an old row's path stays correct regardless). The
`{YYYY}/{MM}/{DD}` folder itself is keyed on the *event's* `start_ts`, not on when the file was actually written -- under a
backlog, a folder for a day that's already passed can still gain new files today if that backlog
hasn't been swept up yet (`claim_video_batch` claims newest-first, see above, so this is now the
exception once fresh events are caught up, not the default state it was before that change). The
worker is also single-threaded, one clip at a time regardless of `VIDEO_PARALLEL_LIMIT` -- that
only lets it claim/burn through a bigger batch per poll tick without the inter-item poll-sleep,
not true concurrent downloads. `VIDEO_MAX_AGE_HOURS`, if set, goes further than newest-first
ordering alone: same throughput safety valve as the AI queue's `max_age_hours` (see above) --
past the cutoff a row just stays `video_status='new'`/`'retry'` rather than spending an attempt
on a clip that's very likely already rolled off Frigate's continuous-recording buffer (confirmed
in production: a clip was already gone `"No recordings found for the specified time range"` only
~36 minutes after the event -- a much shorter retention window than the event-scoped clip
`crop.py` reads from, which persisted for over an hour in the same test). This whole stage ports
the behavioral spec proved out by the `FrigateRetry.json` n8n workflow it replaces, straight into
Python rather than adding new n8n nodes.

`TELEGRAM_EVENTS_MODE` turns on fire-and-forget notifications (`telegram.py`) -- a mode, not a
bool: `none` (off, the default), `image` (photo only, right after crop -- regardless of
`STORE_VIDEO`, photo-only is a valid steady state), `video` (the clip only, once stored, sent
standalone rather than threaded onto a photo that was never sent), or `all` (both -- the video
sent as a reply to the earlier photo, `telegram_photo_message_id` persisted on the row so the
reply-threading survives a service restart, a durable version of the `FrigateRetry.json`
workflow's in-memory `pendingReplies` map). `image` and `video` are independent halves, not a
ladder -- `video` does not imply `image` is also sent, only `all` sends both. Both directions are
wrapped so a Telegram failure (bad token, rate limit, network blip) can never take down the crop
or video poll loop.

`STORE_VIDEO_ALERTS=true` turns on a fourth, independent video queue -- same `new` -> `processing`
-> `retry`/`failed` -> `done`/`skipped` shape, but on `visits` instead of `raw_events`
(`alert_video_worker.py`, its own poll thread, only started when the flag is on). One clip per
visit's whole `start_ts`->`end_ts` span (not per det_id) is fetched from the same Frigate
continuous-recording endpoint `video.py` already uses for the events flow, via a small adapter
dict (`{start_ts, end_ts, camera: visit["cameras"], det_id: "visit-{id}"}`) so `download_clip`/
`build_clip_url` need no changes -- and stored under `VIDEO_STORAGE_PATH_ALERTS` (its own mount
point/bind mount, `VIDEO_STORAGE_ALERTS_HOST_PATH` on the host side -- a genuinely separate
storage location from `VIDEO_STORAGE_PATH`/`VIDEO_STORAGE_HOST_PATH`, not a subfolder of it, so
the two flows' disk usage can be measured/managed independently) with a `visit-` filename prefix
(`video.store_visit_clip`) so it's never confused with a per-event clip that happens to share the
same numeric id (visit ids and raw_event ids are independent sequences). Shares
`VIDEO_PARALLEL_LIMIT`/`VIDEO_INITIAL_WAIT_SECONDS`/`VIDEO_MIN_VALID_BYTES`/`VIDEO_MAX_ATTEMPTS`/
`VIDEO_RETRY_WAIT_SECONDS`/`VIDEO_MAX_AGE_HOURS` with the events flow (mechanically identical
download/validation logic) -- only the on/off switch, storage location, and poll thread are
separate, so the two flows can be A/B'd without doubling every tuning knob. Retention cleanup
(`run_retention_cleanup`/`purge_older_than`) collects and deletes `visits.video_path` files the
same way it already did for `raw_events.video_path`, so a visit-level clip doesn't outlive its
retention window as an orphaned file once its DB row is swept.

`TELEGRAM_ALERTS_MODE` turns on a separate notification path for the alerts/visits flow -- same
`none`/`image`/`video`/`all` shape as `TELEGRAM_EVENTS_MODE` above, just against `visits` instead
of `raw_events`. `image` sends one summary message per visit (`telegram.send_visit_summary`),
fired once from `mqtt_ingest._handle_review_message` right after `db.record_visit` succeeds (not
from a poll loop) -- uses the visit's representative event's `crop_image_base64` as a photo if the
crop stage has already finished it by the time the review closes, falls back to a text-only
`sendMessage` otherwise, since crop timing isn't guaranteed to have caught up yet. `video` sends
the visit's own stored clip (see `STORE_VIDEO_ALERTS` below) as a reply to that summary once
downloaded; `all` sends both, `none` neither. Independent of `TELEGRAM_EVENTS_MODE` above (the
existing per-raw_event photo/video messages) -- any combination of the two can be set at once,
specifically so you can compare which notification granularity is more useful for your traffic
rather than committing to one upfront.

If `STORE_VIDEO_ALERTS` is also on and `TELEGRAM_ALERTS_MODE` includes `video`, the visit's video
is sent as a reply to that same summary message once `alert_video_worker` finishes downloading it
(`telegram.send_visit_video`, reply-threaded via `visits.telegram_photo_message_id` -- durable
across a restart, same idea as `raw_events.telegram_photo_message_id`) -- mirroring how the events
flow's video reply threads onto its earlier photo. `STORE_VIDEO_ALERTS` and `TELEGRAM_ALERTS_MODE`
are otherwise fully independent (one can be on
without the other; a visit clip download failure/retry never blocks or delays the summary
message, and vice versa) -- this reply-threading is the one place they connect.

#### `TELEGRAM_API_BASE_URL` -- optional self-hosted Local Bot API server

Every Telegram request in `telegram.py` (`send_photo`/`send_visit_summary`/`_post_video`) builds
its URL as `f"{config.TELEGRAM_API_BASE_URL}/bot{config.TELEGRAM_BOT_TOKEN}/<method>"` rather than
a hardcoded `https://api.telegram.org` -- `TELEGRAM_API_BASE_URL` defaults to that same cloud API,
so this is purely additive, but can instead point at a self-hosted Local Bot API server
(`telegram-bot-api`, an optional Compose profile alongside `mqtt`, image
`aiogram/telegram-bot-api:latest` -- a prebuilt wrapper around the official
`github.com/tdlib/telegram-bot-api`) reachable over the Docker network at
`http://telegram-bot-api:8081`. Same request/response shape either way (still one POST per
`<method>`), so this is the only change `telegram.py` needed.

Two independent reasons to turn it on, both about `STORE_VIDEO`/`STORE_VIDEO_ALERTS` clips
specifically, since those are by far the largest payloads this project ever sends to Telegram
(a cropped JPEG or composite-grid/GIF is comparatively tiny): lower latency (the request never
leaves the Docker network/LAN, unlike a round trip to `api.telegram.org` over the public
internet), and a much higher upload cap -- Telegram's cloud Bot API caps a bot's own file uploads
at 50MB, while the Local Bot API server raises that to 2000MB. This project's clips come from a
3840x2160 record stream, so a `STORE_VIDEO_ALERTS` clip spanning a longer visit can realistically
exceed 50MB and simply fail to send (`_post_video`'s `except Exception` swallows it as a logged
warning, same as any other Telegram failure -- there's no separate signal distinguishing
"too large" from "network blip" today). The Local Bot API server needs its own `api_id`/`api_hash`
from `https://my.telegram.org` (a Telegram *account* credential used to authenticate the server
itself against Telegram's MTProto backend -- unrelated to, and not a replacement for, the bot
token `TELEGRAM_BOT_TOKEN` already used in every request's URL) -- set as `TELEGRAM_API_ID`/
`TELEGRAM_API_HASH` in `.env`. Bring it up with `docker compose --profile pipeline --profile
telegram-bot-api up -d`, same fully-opt-in pattern the `mosquitto` profile already uses -- it
never collides with plain `api.telegram.org` usage unless `TELEGRAM_API_BASE_URL` is deliberately
pointed at it.

`GET /events` also defaults `has_media=true` -- rows with neither `crop_image_base64` nor
`video_path` (not yet `crop_status='done'`, including `'skipped'` rows) are hidden by default
since there's nothing to show for them; pass `has_media=false` to see every row regardless.
**Update: the web UI's own "Only with media" checkbox was removed** -- it always sent
`has_media=true` (its checked-by-default, never-unchecked-in-practice state), so the toggle itself
added a control for a choice nobody was making; the frontend now simply omits the `has_media`
param and lets the backend's own default apply, same effective behavior with one less UI element.
`has_media=false` is still fully supported for any other caller that wants it. In practice
`video_path` is never set without `crop_image_base64` already being set too (`claim_video_batch`
only ever claims `crop_status='done'` rows), so this default is currently equivalent to
crop-image-only -- but the check covers both so it stays correct if that invariant ever changes.
`GET /events?event_id=<id>` exact-matches a single event and bypasses every other filter
(time window and `has_media` included) -- searching for one specific known event should find it
regardless, not get filtered out by the defaults built for browsing a range.

`GET /events?visit_id=<id>` is the same bypass, scoped to every raw_event a visit grouped together
instead of one specific event -- same reasoning: a connected event's own age or crop/media state
shouldn't hide it from "show me everything this visit grouped." Lets the web UI's visit lightbox
show a "Connected events" strip (`static/app.js`'s `openLightbox`, fetched alongside `GET
/visits/{id}/sightings` in parallel) -- every det_id the visit grouped, not just the deduped
AI-analyzed representative(s) that endpoint returns, each clickable to open that specific event's
own lightbox. `db._build_events_query`'s `has_media` clause is skipped whenever either `event_id`
or `visit_id` is given (not just `event_id` alone); the time-window bypass itself lives one level
up, in `GET /events`'s own handler (skips resolving `start`/`end` from `hours` at all when either
is given) -- `db.list_events`/`db.count_events` still apply whatever window they're explicitly
passed, they just aren't passed one for this call.

`GET /events?q=<text>` free-text searches (case-insensitive substring) across the AI analysis
result -- `sightings.description`, the one free-text field every object type's sighting has -- via
a `LEFT JOIN` to that single table (`SELECT DISTINCT` guards against a fan-out if `sightings` ever
had more than one row per `raw_event_id`, which nothing enforces at the schema level). Only ever
matches rows that already have a sighting, i.e. `ai_status='done'`, so it composes harmlessly with
`has_media`'s default. Unlike `event_id`, `q` does *not* bypass the time window -- it combines
with `start`/`end`/`hours` (and every other filter) rather than overriding them, so a search only
looks within whatever range is currently selected. (An earlier version bypassed the window
entirely, the same way `event_id` still does -- reverted once it became clear a search result
from outside the visibly selected range, with no indication why, read as broken rather than a
deliberate whole-history search.)

`GET /events/{id}/thumbnail` and `GET /events/{id}/image` fall back to extracting a frame from the
stored video (`video.extract_frame_jpeg`, ffmpeg, 0.1s in to dodge a black first frame on some
encoders) when there's a video but no crop image -- belt and suspenders for the same reason
`has_media` checks both; not reachable in practice today either.

`GET /object-types` returns `config.OBJECT_TYPES` (from the `OBJECT_TYPES` env var,
comma-separated, e.g. `car,truck,person,dog`) -- Frigate's object labels aren't fixed (depends on
your model/config), so the web UI's Type filter dropdown is populated from this at load time
instead of being hardcoded in the HTML; add a label to the env var and it shows up in the
dropdown on next restart.

`GET /cameras` (`db.get_distinct_cameras`, `SELECT DISTINCT camera FROM raw_events`) backs the web
UI's Camera filter dropdown -- deliberately queried live rather than sourced from a config value
the way `OBJECT_TYPES` is, since `config.CAMERAS` is an optional ingest-time allow-list that's
usually unset (meaning "no filter," not "here is the list of cameras") and would otherwise give an
empty/stale dropdown, or silently miss a newly added camera until someone remembered to update a
env var. `camera` is now a plain equality filter alongside `object_type` on `GET /events`
(`re.camera = %s`) and `GET /visits` (`v.cameras = %s` -- exact match is safe since visit grouping
is per-camera only, see "Visit grouping" above) -- both already accepted this param before the web
UI exposed it, so only the frontend and `POST /search`/`db.semantic_search_combined` (which didn't
have a camera filter at all) needed changes to add it.

`GET /events/{id}` also returns `sighting` (via
`db.get_sighting_for_event`, one targeted indexed lookup against the single universal table) --
`null` until `ai_status='done'`. Kept off the `GET /events` list response deliberately (same
reasoning as `crop_image_base64` already being list-response-only) -- the web UI's lightbox fetches
full detail only when actually opened, not for every row in a page.

`GET /visits` is a read-only comparison view alongside `GET /events` -- one row per Frigate
review/alert segment (`visits`, see above) instead of one per raw_event, so duplicate det_ids from
tracker re-ID/label flicker collapse into a single row. `representative_event_id` is the visit's
earliest-linked raw_event (`row_number()` over each visit's linked `raw_events`, ordered by
`start_ts` then `id` -- the simplest deterministic pick for a first comparison pass, not a
"best crop" heuristic); `event_count` is how many det_ids were grouped into it, both computed in
one pass via window functions over a `visit_id`-linked join, not two separate queries. Filterable
by `object_type`/`camera`/`start`/`end`/`hours`/`q` -- `event_id`/`ai_status`/`has_media` are still
per-raw_event concepts that don't compose cleanly with a grouped view, so this endpoint doesn't
accept them at all rather than half-supporting them. Purely additive and read-only -- doesn't
affect `GET /events`, the AI queue, or Telegram notifications; exists so `visits` data can be
judged visually against real traffic before deciding whether to build the actual dedup behavior
described above.

`q` (added after the fact, once `only_visit_representative`'s dedup became object-type-aware --
see above) matches a visit if **any** of its linked raw_events has a sighting
whose AI analysis text matches -- same fields/ILIKE substring match `GET /events`'
own `q` uses (`db.list_visits`'s `EXISTS` subquery against a fresh `raw_events`/sighting join, not
a condition on the row `list_visits`'s own CTE already joined in for `representative_event_id`/
`event_count` -- a visit's match can come from a *different* linked event than whichever one
`row_number()` picks as representative, e.g. searching a person's description on a visit whose
representative happens to be the car, so this has to check across every linked event
independently rather than filtering the CTE's per-row join, which would also wrongly skew
`event_count`). Same as `GET /events`' `q` -- combines with `start`/`end`/`hours` rather than
bypassing them, so a search only looks within the currently selected range.

`has_video`/`video_status` on `GET /visits` describe the *visit's own* video
(`STORE_VIDEO_ALERTS`/`alert_video_worker.py`), not the representative raw_event's -- those are
two entirely separate video flows/storage locations (`VIDEO_STORAGE_PATH_ALERTS` vs.
`VIDEO_STORAGE_PATH`). Bug fixed in production: `list_visits`' original `WITH linked AS (...)` CTE
selected `re.video_status`/`(re.video_path IS NOT NULL)` from the representative raw_event instead
of `v.video_status`/`v.video_path` from the visit itself -- confirmed live (7 visits with genuine,
correctly-downloaded clips on disk, `video_status='done'`, but every one reported `has_video:
false` via the API, since `STORE_VIDEO` was off so the representative event's own video_path was
always NULL). `GET /media/video/{event_id}` also only ever served a raw_event's video_path, with
no route at all for a visit's -- so even a correctly-reported `has_video` couldn't have been
played. Fixed with a parallel `GET /media/video/visit/{visit_id}` (`db.get_visit`, same
range-request `FileResponse` pattern) and the web UI's `openVisitLightbox` now carries the
visit's own id (`visitId`) alongside `representative_event_id` so `lightboxVideoUrl()` can pick
the right endpoint -- the image/AI-analysis side of the lightbox still always comes from the
representative event (that's the only place crop images and sightings exist), only video
playback branches on which id space it's in.

The web report UI (`/ui`, static files baked into the image, Alpine.js vendored locally -- no CDN
requests) reads the same API everything else does. An Events/Visits toggle switches the whole page
between `GET /events` and `GET /visits` (`viewMode`, drives `fetchEvents`/`fetchVisits` via a
shared `refresh()` dispatcher so `applyFilters`/`prevPage`/`nextPage` stay view-agnostic); a visit
card's click handler (`openVisitLightbox`) builds a minimal event-shaped object from the visit's
`representative_event_id`/`has_image`/`has_video`/`ai_status` and hands it to the same
`openLightbox` the Events view uses, rather than a separate lightbox implementation.

The filter bar shows only whatever's actually relevant to the active view, rather than every field
regardless of `viewMode`. Event ID, AI status, and Only-with-media are per-raw_event concepts
`fetchVisits` has no use for (see its own comment) -- their `<label>`s carry
`x-show="viewMode === 'events'"` and disappear entirely on the Visits tab, rather than sitting
there doing nothing. This replaced two earlier, less direct attempts: first disabling them via
`:disabled` bindings (half the filter bar visually greyed out with no obvious reason why), then
leaving them enabled with just a `:title` tooltip (Search/Event ID/AI status doing nothing on the
Visits tab read as a real bug in practice, not just an unclear-but-inert control) plus an
auto-switch-to-Events-on-search fallback. Search AI analysis (`q`) no longer needs any of that --
`GET /visits` gained its own `q` support (see above), so it's a real filter in both views now, not
an Events-only one; it's shown unconditionally. `applyFilters` still auto-switches `viewMode` to
`'events'` if Event ID or AI status is somehow set while on the Visits tab (a stale value rather
than the normal path, since both fields are hidden there), as a safety net rather than the primary
mechanism now.

Switching tabs (`switchView`) or toggling advanced/simple mode (`toggleAdvancedSearch`) both reset
every filter back to its default (`_defaultFilters()`, one shared helper the two plus
`resetFilters` all call) -- a value set in one view/mode otherwise kept silently applying once its
field disappeared after switching (e.g. an Events-only AI status filter carrying over after
switching to Visits and back, or an advanced-mode From/To range overriding the reappeared Time
range preset in simple mode) -- resetting on every context switch avoids that whole class of
confusion rather than patching each case individually. The filter bar itself defaults to
a simplified view -- Search AI analysis
plus a "Time range" preset dropdown (`filters.hours`, options `[1, 3, 6, 12, 24]` hours, sent as
`GET /events`'/`GET /visits`'s own `hours` param) -- with an "Advanced filters" toggle
(`advancedSearch`) that reveals From/To/Type (both views) plus Event ID/AI status/Only-with-media
(Events view only, per the `x-show` above) on demand; those fields' wrapping
`<div class="advanced-filters">` is `display: contents` in CSS so they flow as direct flex items
of `.filters` when shown, rather than nesting a visible sub-box. The Time range preset itself is
hidden while the advanced panel is open (`x-show="!advancedSearch"`) rather than shown redundantly
alongside From/To -- the advanced panel's own date pickers cover the same need. Those From/To
pickers override the Time range preset when either is set (`fetchEvents`/`fetchVisits` check
`filters.start || filters.end` first, falling back to `hours` only when both are empty) -- same
precedence `q`/`event_id` already had over the time window, just extended to cover the preset too.

Every filter except the two free-text inputs (Search AI analysis, Event ID) applies immediately on
`@change` (Time range, From, To, Type, AI status, Only-with-media) rather than needing the Search
button/Enter -- changing a dropdown or picking a date with no visible effect until a separate
submit click read as those controls being broken, not just requiring an extra step. The two
text inputs stay submit-only deliberately -- firing a request per keystroke would be wasteful and
janky for something typed character-by-character, unlike a discrete dropdown/date selection.

**Update: Camera promoted to the simple filter bar; Event ID removed from the web UI entirely.**
`filters.camera` (backed by the new `GET /cameras`, see above) now sits in the simple/default
filter row alongside Search/Time range, shown unconditionally (no `x-show` gating at all, same
always-visible treatment Search AI analysis already had) rather than being one more field buried
behind "Advanced filters" -- camera is common enough to filter by that hiding it behind a toggle
added friction for no real benefit. Event ID's own `<label>`/`<input>` was removed from
`index.html` outright (not just moved), along with `filters.eventId` and every reference to it in
`static/app.js` (`_defaultFilters`, `applyFilters`' events-only auto-switch check, `fetchEvents`'
former `if (eventId) {...} else {...}` branch, now just the unconditional `else` contents) --
dead frontend state/branches otherwise, once the only thing that ever set `filters.eventId` no
longer exists. `GET /events`'s own `event_id` query param is untouched (see below) -- this was a
web-UI-only removal, not an API contract change; a caller that already knows a specific event id
can still request it directly (`GET /events?event_id=...`), just not from this control.
`GET /events` itself is filterable by
`object_type`/`crop_status`/`ai_status`/`video_status`/`has_media`/`event_id`/`q`, defaults to the
last 1 hour, media-only. Both `GET /events` and `GET /visits` set an `X-Total-Count` response
header -- total rows matching the current filters with `limit`/`offset` ignored (`db.count_events`/
`db.count_visits`, sharing the exact same filter-building as `db.list_events`/`db.list_visits` via
`_build_events_query`/`_build_visits_query` so the two can never drift apart) -- so the web UI's
pager can show "page X of Y" (`totalPages()` in `static/app.js`) instead of just a bare "Prev/Next"
with no sense of how much data there is. `GET /events/{id}/thumbnail` (a small on-the-fly JPEG, same
`crop.scale_image_base64` helper `report.py` uses) feeds the grid in both views, and
`GET /media/video/{id}` (range-request `FileResponse`, so the browser's scrubber works) or
`GET /events/{id}/image` feed the lightbox depending on `has_video`/`has_image` -- when an event
has both, toggle buttons switch between them (video shown by default) instead of only ever picking
one; the lightbox also shows the AI analysis result (via `GET /events/{id}`) once
`ai_status='done'`. Those three endpoints alone also accept the API key as an `?api_key=` query
param (in addition to the usual `X-API-Key` header) since `<img>`/`<video>` tags can't attach
custom headers -- the UI itself just stores the key in a long-lived cookie after validating it
against the API once. A download button (`lightboxDownloadUrl`/`lightboxDownloadFilename`) sits
next to the close button, pointing at whichever of video/image is currently on screen (same
`has_video`/`lightboxMode` check the toggle buttons use) -- a plain `<a download>` works here since
every one of these media endpoints already accepts the API key via `?api_key=`, no extra plumbing
needed. The suggested filename is `event-{id}` or `visit-{id}` (whichever id space the open
lightbox is in) with a `.mp4`/`.jpg` extension matching what's actually being downloaded.


An optional `mosquitto` Compose profile (`--profile mqtt`) provides a local/dev MQTT broker for
bringing up the whole pipeline from scratch without an existing broker -- fully opt-in, never
collides with a production broker unless you deliberately point `MQTT_HOST=mosquitto` at it.

### Cropping — `region`, not `box`, and why it's capped

Frigate's event `data.box` is the tight detected-object box — often just a few percent of the
frame — and produces an unusably narrow crop. `data.region` is Frigate's own padded,
hysteresis-smoothed context area around the object (often 3-10x larger than `box`), and is what
the Explore UI's own crops are framed around; `ingest-worker/crop.py` crops from `region`.

Both `box` and `region` are normalized `[x, y, width, height]` (top-left + size), not
`[x1, y1, x2, y2]` — and both are in the record-stream's coordinate space already (confirmed via
Frigate's own API response), so no detect→record scaling is needed once you're reading them from
`GET /api/events/<id>` (this differs from the raw MQTT `frigate/events` payload's `box`, which IS
pixel-space `[x1, y1, x2, y2]` — that raw payload is only used for the initial ingest, never for
cropping).

Because `region` can be large, the cropped JPEG is downscaled to `MAX_CROP_DIMENSION` (default
1280px, long side) before being base64-encoded — VLMs downsample beyond that internally anyway,
so there's no analysis benefit to sending a bigger image, only more load on the vision encoder.

**`CROP_DISABLED`** (default `false`) skips the crop filter entirely -- `crop_image_base64` becomes
the full original camera frame (still scaled to `MAX_CROP_DIMENSION`) instead of a region around
the object. This is the one field the web UI, Telegram, the report, and the VLM call all share, so
the single flag changes what's displayed *and* what gets analyzed at once -- there's no separate
"wide view for humans, cropped for the model" split, since both consumers read the same stored
value. `crop.crop_and_scale` branches on it before building the ffmpeg `-vf` filter: with it on,
`box` is entirely unused (no crop-region math, no box-validity check either, since an invalid box
never affects a result that doesn't depend on it) and only the scale filter runs. Off by default
because the crop exists specifically so the VLM can read small detail (plates, notable features)
that's illegible in a full wide frame at any reasonable resolution -- this is a real trade-off
(context vs. legibility), not a strict improvement, so it's opt-in.

`crop.py` grabs its frame from a configurable offset into the event's own start/end span
(`CROP_FRAME_OFFSET_PCT`, `crop.compute_frame_offset_seconds`, default `0.5` = midpoint, this
project's original fixed behavior) -- but for a long-lived tracked object (a car sitting in a
zone for 20+ minutes, say), Frigate's saved event clip can be much shorter than that logical span
(confirmed in production: a ~20-minute event's clip was only ~7 minutes long). Seeking `-ss
<offset>` past the real end of that shorter file doesn't error -- ffmpeg exits 0 having written
nothing, so it isn't caught via the subprocess's exit code, only surfaces later when the next
ffmpeg call tries to read the (missing) frame file. `crop_and_scale` checks for that and retries
once at a small fixed offset near the start of the clip, which is always within whatever got
saved regardless of how much the tail was truncated.

Why this is a tunable rather than a fixed formula: Frigate's own alert thumbnail is taken at
whatever frame scored highest during the event, which is content-dependent, not a fixed offset --
confirmed live against production by comparing two real events' Frigate-side snapshot timestamps
(read off the snapshot's own burned-in clock) against their start/end: one event's snapshot
landed almost exactly at event *start*, another landed *past* the midpoint. Frigate doesn't expose
this "best frame" timestamp anywhere in its API (checked both the events list and detail
endpoints, including `data.path_data`), so there's no way to compute or sync to Frigate's exact
choice programmatically. `0.5` stays `CROP_FRAME_OFFSET_PCT`'s default until real usage across your
own cameras suggests a specific different value is consistently better -- there's no universally
"more correct" number to guess at upfront, for this project's *own* seek-based approach.

`CROP_INITIAL_WAIT_SECONDS` (default 5s, same idea as `VIDEO_INITIAL_WAIT_SECONDS`) gives Frigate
a head start to finalize the event/clip before the *first* crop attempt on a freshly claimed row
-- confirmed in production that even an ordinary short event's crop can fail this way if attempted
immediately after the "end" MQTT message, not just long events tripping the clip-duration fallback
above. Only applies once per row (`crop_attempt_count == 0`), not on every retry pass. Still
applies as a generic "give Frigate a moment" wait regardless of `FRIGATE_SNAPSHOT_ENABLED` below --
its own timing concern (has the event settled at all) is orthogonal to which image source is used.

#### `FRIGATE_SNAPSHOT_ENABLED` -- revisiting the earlier "use Frigate's own snapshot" rejection, for events only

Fetching Frigate's own snapshot directly (`GET /api/events/<det_id>/snapshot.jpg`) instead of
seeking our own frame from the record-stream clip was considered and rejected earlier in this
project's history for exactly the reasons above: it's from the lower-res detect stream (800x448 in
testing, vs. this setup's 3840x2160 record stream) with a bounding-box/label/timestamp overlay
baked in that this Frigate version's REST API doesn't expose a way to suppress -- confirmed
directly (not just assumed) by re-testing with `bbox=0&timestamp=0&h=720` query params appended to
the snapshot URL: byte-identical response to the same request with no params at all, overlay still
present, resolution still 800x448.

That trade-off was true then and is still true now -- what changed is the *decision*, not the
facts: this Frigate snapshot is Frigate's own best-detection-score frame judgment (the same
content-dependent choice CROP_FRAME_OFFSET_PCT's own comment above says can't be replicated by any
fixed offset), and in practice that beats a fixed-offset seek often enough that **this is now the
default** (`FRIGATE_SNAPSHOT_ENABLED` default `true`, flipped from the original opt-in `false`
once the trade-off was judged worth it broadly, not just for some deployments) -- not merely an
available option anymore. `crop.crop_event` calls `crop.fetch_frigate_snapshot_base64` instead of
`crop_and_scale` by default -- no ffmpeg involved at all for this path, just the raw JPEG bytes
Frigate already rendered, base64-encoded directly. `sub_label`/`score` still come from the same
`fetch_frigate_event` call either way, since those aren't image-related. `CROP_DISABLED`/
`CROP_FRAME_OFFSET_PCT`/`CROP_PADDING_PCT` only take effect once `FRIGATE_SNAPSHOT_ENABLED` is set
back to `false` -- with the new default, there's no frame-seeking or region-cropping happening on
our side to tune unless you opt back into it.

**Confirmed again, live, against a real production event**: Frigate exposes no timestamp for its
own best-frame/snapshot choice anywhere -- not in `GET /api/events/<id>`'s JSON (`data.score`/
`data.top_score` are bare numbers with no associated time; `data.path_data` is a movement trail,
`[[x, y], timestamp]` pairs for drawing a path overlay, not a per-frame score), not in the snapshot
image's own HTTP response headers (no `Last-Modified`, no custom header), and not in its EXIF (none
present at all). The *only* place a moment's real wall-clock time is visible at all is the camera's
own burned-in on-screen timestamp overlay baked into the image's pixels -- readable by eye, not
programmatically extractable without OCR (fragile: overlay position/format varies per camera, and
the low-res snapshot makes it small). This means any offset-based seek -- `CROP_FRAME_OFFSET_PCT`
for events, or the alert stage's own per-event seek below -- is fundamentally a guess, not a sync to
Frigate's actual choice, and a single fixed percentage can't universally be "more correct": one real
comparison for this section showed the snapshot's actual best moment landing right at the very
start of a 14-second event (not the midpoint), consistent with the "content-dependent, no universal
value" conclusion already reached above from separate live comparisons.

**Bug found and fixed**: `alert_ai_worker.py`'s `_gather_alert_images`/`process_claimed_visit` never
resolved or threaded `crop_frame_offset_pct` through to `crop.crop_event_high_res` at all -- every
alert-stage high-res crop silently fell back to `crop_event_high_res`'s own default parameter
handling (`config.CROP_FRAME_OFFSET_PCT`, the hardcoded module constant), regardless of whatever
`profiles.yaml` actually set for `crop_frame_offset_pct`. Confirmed live: changing production's
`defaults: crop_frame_offset_pct` had zero observable effect on the alert stage, since the value
was never read from there in the first place. Fixed by resolving
`profile_config.alert_crop_frame_offset_pct(profile, object_label)` in `process_claimed_visit` and
threading it through `_gather_alert_images` into `crop.crop_event_high_res`, the same way
`crop_disabled`/`crop_padding_pct` already were (those two *were* correctly threaded through the
whole time -- only the offset was missing). `alert_crop_frame_offset_pct` is a new optional
alert-stage-specific override (`profile_config.py`, same two-tier-then-fallback shape as every
other per-row resolver): a type's own entry, then `defaults:`, then falls back to the plain
`crop_frame_offset_pct` resolution unchanged -- so a `profiles.yaml` that never sets this new key
keeps behaving exactly as before this fix (still ultimately `config.CROP_FRAME_OFFSET_PCT`'s
hardcoded default, unless the plain `crop_frame_offset_pct` is itself now correctly reaching this
path). Lets one type's alert-stage timing be tuned independently of its own event-stage value,
same rationale `alert_provider`/`alert_model`/`alert_chat_path` already established.

**Events only, deliberately** -- the events stage is entirely unaffected by anything below; it
never used the visit-level preview feature this used to gate on.

### Visit preview (removed) -- a composite grid + animated GIF, superseded by the alert stage

This project used to have a fifth queue stage, a visit-level "preview" artifact built specifically
because the alert AI stage (see above) needed *some* image richer than a single low-res frame to
analyze. Its history, briefly, since several real production bugs shaped it: Frigate's own
per-review "best frame" timestamp (`data.thumb_time`) turned out to have no reliable way to
reproduce against the full-res record stream (its continuous-recording clip endpoint pads an
*unpredictable*, direction-varying amount of extra footage onto either edge of a requested
window, confirmed across three separate real visits, each breaking a different anchor-correction
attempt) -- so the design pivoted to sampling 4 frames proportionally across the clip's own
measured duration (`VISIT_PREVIEW_FRAME_PERCENTAGES`) instead of chasing one precise "best moment."
Those 4 frames were combined into a composite 2x2 grid image (`visits.crop_image_base64`, the
artifact actually analyzed) plus a separate animated GIF (`visits.preview_gif_base64`,
human-preview-only, served via a since-removed `GET /visits/{id}/preview.gif`, never sent to any
VLM). Later fixes hardened this against Frigate returning a not-yet-finalized/placeholder clip
(a probed-duration-vs-nominal-window ratio check) and against its own continuous-recording
retention window being shorter than the visit's span (replacing one whole-span clip request with
four independent per-moment requests, each with its own small padding window, so a gap at one
moment didn't fail the whole grid).

**This whole feature has been removed** -- schema/DB/GET-endpoint/web-UI/Telegram/report
references are gone (the columns/index remain unwritten in `schema.sql` for now, see the
schema-migration note under "Alert AI stage" above) -- in favor of the alert stage gathering a
series of genuinely high-resolution *per-event* crops directly, ephemerally, at analysis time (see
"Alert AI stage" above for the current design). The fixed 4-percentage sampling never reliably
landed on the actual moment worth seeing, and the composite grid's resolution was capped well
below what a real per-event crop already provides for free -- once the alert stage could gather
real per-event crops instead, the whole preview-building apparatus (and its several production
bugs) had no remaining purpose. A visit's own thumbnail/lightbox image now always falls back to
the representative event's own crop, same as before this feature existed.

#### Bug (still applies): a media-only retention purge made its own rows permanently unopenable

`purge_media_older_than` (see "Query/report/AI-queue API" above) deliberately clears media columns
but keeps every row and its AI analysis text, specifically so old data stays "fully searchable...
just with the media payload gone." But every card's clickability in the web UI (Events, Visits,
Search) was gated purely on `has_image || has_video` -- once a purge cleared those, the card
stopped being clickable at all, taking the description text down with it even though `ai_status`
(and the underlying `sightings`/`visit_sightings` row) was untouched. There was no way to reach
that surviving text from the grid view once its media was gone -- confirmed by reading the actual
gating logic, not just inferred from the purge's own behavior.

Fixed with a shared `isOpenable(row)` helper in `static/app.js`
(`row.has_image || row.has_video || row.ai_status === "done"`), used by all three views' card
`clickable` class and click handler instead of the old `has_image || has_video` check directly --
`ai_status === 'done'` survives a media purge by design (see above), so it's the correct fallback
signal for "there's still something worth opening this for." Each card's own `.no-image`
placeholder is similarly now two-state, not a flat "no media (crop_status)" -- once `ai_status`
is `'done'`, it reads "media cleared — click for description" instead, so the empty thumbnail
itself hints that clicking still does something.

The lightbox's own media template had a matching gap once purged-but-analyzed rows became
openable: `<img :src="lightboxImageUrl()">` rendered unconditionally whenever no video applied,
with no check that `has_image` was actually true -- would have shown a broken image for exactly
the rows this fix was meant to make openable. Fixed by adding `lightboxEvent.has_image &&` to that
template's condition, plus a sibling `.lightbox-no-media` placeholder (shown only when neither
`has_image`/`has_video` are true) explaining the media was likely cleared by a retention purge and
that the AI analysis below is still preserved. The `.lightbox-info` panel itself needed no change
-- it was already gated on `visitId || ai_status === 'done'`, independent of media presence.

### Camera allow-list

`CAMERAS` (optional, comma-separated Frigate camera names, e.g. `outside,outside2`) gates both
`mqtt_ingest.py` handlers at ingest time -- `_handle_event_message` and `_handle_review_message`
each check `event["camera"]`/`review["camera"]` against the list right after confirming
`type == "end"`, before calling `db.insert_raw_event`/`db.record_visit` at all. A camera not on
the list never gets a `raw_events` or `visits` row -- not filtered out later, not hidden from some
view, simply never ingested. One shared list across both flows (not separate events/alerts
filters) -- unset/blank (the default) means no filter, every camera Frigate reports is processed,
today's behavior unchanged.

### Visit grouping via Frigate's review/alert stream

`frigate/reviews` (MQTT, same `{type, before, after}` envelope as `frigate/events`) is Frigate's
own review/alert system -- it already groups multiple tracked-object det_ids into one segment
representing a single real-world activity, using Frigate's own tracker (occlusion handling,
re-ID, label flicker -- confirmed live against production: one review spanned 4 det_ids over
~19 seconds with `data.objects` showing both `car` and `truck`, clearly the same vehicle mid-track
rather than two separate ones). `mqtt_ingest.py` subscribes to this as a second topic alongside
`frigate/events` (`config.MQTT_REVIEWS_TOPIC`, default `frigate/reviews`) and, on each `end`
message, calls `db.record_visit` to INSERT into `visits` and link every `raw_events` row whose
`det_id` appears in that review's `data.detections` (`visit_id` + `reconciled`, both columns that
already existed on `raw_events` but were previously never populated by any code). This is purely
additive -- it doesn't touch `crop_status`/`video_status`/`ai_status` or any of the three queue
poll loops/claim functions at all; a raw_event still moves through crop/video/AI exactly as before
regardless of whether or when it later gets linked to a visit.

Grouping is per-camera only -- confirmed live that a review's `camera` field is a single value,
never a list, so `visits.cameras`/`camera_count` are currently always one camera / `1`. Frigate
does *not* merge the same real-world vehicle seen by both `outside` and `outside2` into one
review, even though both cameras share zone names specifically so a cross-camera merge could work
(see Prerequisites below) -- this is deliberate, not a gap to fill: two overlapping cameras can be
framing genuinely different angles/areas of the same yard, so a raw_event appearing once per
camera is correct, wanted behavior, not duplication to collapse.

Using `visit_id` to actually reduce work is now available but opt-in, not the default: `POST
/ai-queue/claim`'s `source=visits` skips analyzing duplicate det_ids a visit already grouped (see
Query/report/AI-queue API above), and `STORE_VIDEO_ALERTS`/`TELEGRAM_ALERTS_MODE` add
independent per-visit video/notification flows alongside (not instead of) the existing per-event
`STORE_VIDEO`/`TELEGRAM_EVENTS_MODE` ones (see Video storage above). All three are deliberately
independent switches from their events-flow counterparts -- the point is to A/B per-event vs.
per-visit behavior against real traffic, not to pick one and commit. `GET /visits` remains the
read-only comparison view for judging `visits` data itself, separate from these behavior switches.

`review.alerts`/`review.detections` in `frigate.conf` currently share identical `required_zones`
per camera, so `severity` (`alert` vs `detection`) isn't a useful noise filter today -- nearly
everything in-zone comes back `alert`. Tightening `detections.required_zones` to be narrower than
`alerts.required_zones` would change that, but that's a Frigate config decision, not something
`ingest-worker` can affect.

### Semantic search and the Q&A agent

Answering free-form questions ("any new cars in the last 2 weeks?", "what interesting happened
today?") combines two different kinds of lookup: **structured filtering** (time range, camera,
object type -- resolved from natural language into concrete `start`/`end` by the agent itself, then
passed as real query params to the existing read API) and **semantic/fuzzy matching** over the
AI-written sighting text for asks that don't map to a column ("anything unusual", "a red truck with
a ladder rack"). Embeddings are generated by **n8n**, not `ingest-worker` -- preserves the existing
"`ingest-worker` never calls an LLM" boundary (see above) -- and stored as a `vector` column
directly on `sightings`/`visit_sightings` via **pgvector**, not a separate vector DB. This
keeps the project's "own Postgres instance/schema, no new moving parts" philosophy, and means
embeddings are swept for free by the existing retention-cleanup delete (`run_retention_cleanup`/
`purge_older_than`) with no separate sync-on-delete logic needed -- a row's embedding lives and
dies with the row itself. Regeneration is always possible for any row that still exists, since the
source text is stored durably alongside it.

`postgres-projects` runs `pgvector/pgvector:pg16` (a drop-in build on top of plain `postgres:16` --
same data directory/volume, existing data untouched, just adds `CREATE EXTENSION vector`
capability) instead of plain `postgres:16`; the CI workflow's Postgres service container was
switched the same way, for the same reason the ffmpeg CI gap got fixed -- a capability the code now
depends on has to actually be present in the CI service container, not just assumed. `schema.sql`
adds `CREATE EXTENSION IF NOT EXISTS vector;` near the top (idempotent, applied by `ensure_schema()`
on every startup like everything else in that file) plus a nullable `embedding vector(N)` column
on both `sightings` and `visit_sightings` (N = `config.EMBEDDING_DIMENSIONS`, deployment-configurable
since the exact embedding model in use varies -- 1024 for `Qwen3-Embedding-0.6B-GGUF`, 768 for
`nomic-embed-text-v1.5`, etc. -- one more slot in the user's existing `llama_slot_proxy` multi-model
setup, no `mmproj` needed since it's text-only) with an HNSW cosine-distance index on each (`vector_cosine_ops` --
HNSW rather than ivfflat since it needs no existing rows to "train" on, safe to create immediately
against a column that starts empty).

`db.py` formats a Python list as a pgvector input literal (`"[0.1,0.2,...]"`) passed through
psycopg2 as a plain string param and cast with `::vector` in SQL (`_vector_literal`), rather than
depending on the separate `pgvector` package's connection-level type adapter -- avoids that
package's own registration-ordering hazard (it needs the extension already created in the database
before it can register) for a column this code only ever writes or ranks by distance, never reads
back as a Python list. `complete_sighting`/`complete_visit_sighting` both take an
optional `embedding` parameter, stored in the same existing transaction -- no new queue stage, since
n8n (or the internal AI stage) computes the vector *before* calling `POST /sightings`, the same
request/response shape as today plus one more optional field. Omitted or null just means that
sighting isn't semantically searchable, not an error -- this is how every pre-existing sighting row
(from before this feature existed) behaves until/unless backfilled.

**`POST /search/semantic`** (`X-API-Key` protected, `db.semantic_search_sightings`): cosine-distance
(`<=>`) ordered search against `sightings`, filtered by the caller-resolved
`object_types`/`start`/`end` window -- `object_types` filters `sightings.object_label = ANY(...)`
directly now (any Frigate label, not a "vehicle"/"person" pseudo-category), since there's only one
table to search regardless of which types are requested. A POST, not GET, since a
multi-hundred-float array doesn't belong in a query string. `embedding IS NOT NULL` naturally
excludes sightings that predate this feature or came from a run that didn't attach one; that's a
narrower result set, not an error. Rows without their own embedding just aren't candidates, same as
`GET /events`' `q` only ever matching rows that already have a sighting.

**`POST /embeddings/backfill`** (`X-API-Key` protected, `ai_worker.run_embedding_backfill`) fills
in `embedding` for sightings that existed before this feature did, or came from any run that didn't
attach one -- same dry-run-by-default shape `/retention/purge` already uses (`confirm` defaults to
`false`, previews `db.count_sightings_missing_embedding()`'s counts with no embedding calls made;
`confirm=true` actually processes up to `limit` rows per table, call it repeatedly until
both counts reach zero). Deliberately independent of `AI_EVENTS_STAGE_ENABLED`/`process_claimed_event` --
it only ever re-embeds a sighting's own already-stored `description` (`db.get_sightings_missing_
embedding`/`get_visit_sightings_missing_embedding`), never re-runs the VLM, so it works regardless
of whether `ai_worker.py` (the only AI-stage implementation now) is currently running. Embeds
`description` directly (no combination step needed -- it's already the one-line
summary for every object type) via the same `ai_worker._embed_text` helper
`process_claimed_event`'s own embed step already uses, so a backfilled row's embedding means the
same thing as a freshly-computed one. Requires
`LLAMA_PROXY_BASE_URL` to be set regardless of `AI_EVENTS_STAGE_ENABLED` (400 if it isn't, checked before
any row is touched) -- this is the one place a plain n8n-only deployment still needs that env var,
specifically to backfill.

**`GET /status`** additionally returns `retention_months` (`config.RETENTION_MONTHS`) and
`oldest_available_start_ts` (`db.get_retention_info`, `MIN(raw_events.start_ts)`) -- lets the Q&A
agent tell "nothing happened in that range" apart from "that range was already purged" instead of
reporting a quiet day that was actually just missing data. The true oldest surviving row can be
somewhat newer than the nominal `RETENTION_MONTHS` cutoff, since the scheduled sweep runs on its own
slow cadence -- this reflects what's actually still in the database right now, not the configured
policy alone.

**Update:** `n8n/metadata-processor.json` was deleted from this repo -- it never received the
follow-up pass it would have needed (new prompts per `profiles.yaml`'s `event_prompt`s, one shared
POST node instead of two) and remained on the old vehicle/person shape (separate `Call Qwen
(Attributes + Plate)`/`Call VLM (Person)` branches, JSON-schema prompts, `POST
/sightings/vehicles|persons`), all of which the universal `/sightings` schema had already removed
-- reactivating it as-is would have 500'd on its very first insert. Keeping a known-broken workflow
file around had no upside and had contributed to n8n import clutter in practice (multiple stale
re-imported copies of it were found and cleaned up from a live n8n instance). `ai_worker.py` remains
the reference implementation for the universal shape, and is now the only one.

**`n8n/yard-stats-qa.json`** was upgraded in place (same `Ask Webhook`/`Respond` shape any existing
caller already uses) from a naive "dump the last 200 rows, ask once" workflow -- which had no time
filtering at all and silently truncated past 200 rows -- into a real tool-calling **AI Agent**
(`@n8n/n8n-nodes-langchain.agent`, the first use of LangChain-style nodes in this project's `n8n/`
folder). Its system prompt injects the current date/time (`{{ $now.toISO() }}`) so it resolves
"last week"/"today" itself before calling any tool, plus the retention-boundary fact from
`GET /status` above (fetched once up front by a `Get Status (API)` node). Tools, each following the
existing `httpHeaderAuth`/`REPLACE_AFTER_IMPORT` pattern already used for every `ingest-worker` call
in this project:
- **`get_summary_stats`** -> `GET /stats/summary` (aggregate counts)
- **`search_events`** -> `GET /events` (structured filters: time range, camera, object type, exact
  substring match)
- **`semantic_search`** -> a separate sub-workflow, **`n8n/yard-stats-semantic-search-tool.json`**
  (`@n8n/n8n-nodes-langchain.toolWorkflow`, called via `workflowId`, filled in after both workflows
  are imported), rather than a single HTTP Request Tool node -- a tool node can only make one HTTP
  call, but this needs two (embed the query text, then `POST /search/semantic`), and packaging it as
  its own callable sub-workflow means the 1024-float embedding vector is computed and consumed
  entirely server-side, never round-tripping through the Agent's own context/tokens the way passing
  it between two separate tool calls would require.
- **`get_event_detail`** / **`get_visit_sightings`** -> `GET /events/{id}` /
  `GET /visits/{id}/sightings`, for drilling into whichever specific rows the agent decides are
  worth a closer look, instead of dumping every row into context up front.

The Chat Model (`@n8n/n8n-nodes-langchain.lmChatOpenAi`) points at the same VLM host via
`llama_slot_proxy` the old `Ask Qwen` node called directly -- unlike the plain HTTP nodes used
everywhere else in this project, this LangChain sub-node type requires a credential object to hold
its base URL, it can't call a bare unauthenticated URL the way `Call Qwen (Attributes + Plate)` etc.
do; the API Key field can be any placeholder value since `llama_slot_proxy` doesn't check it.

### Web UI Search tab -- `POST /search`, `db.semantic_search_combined`

A third tab alongside Events/Visits (`static/index.html`'s `view-toggle`, `viewMode === "search"`)
gives the same semantic search the n8n Q&A agent's `semantic_search` tool already has, but reached
directly from the browser with no agent/LLM synthesis step in between -- a ranked grid of results,
not a written answer (a narrower, deliberately simpler feature than the Q&A agent: no natural-
language date resolution, no follow-up questions, just "rank what's already been analyzed by how
well it matches this text").

`POST /search` (`schemas.TextSearchRequest`/`TextSearchResponse`) is a new, separate endpoint from
n8n's existing `POST /search/semantic` contract, which is left completely untouched -- n8n's own
Q&A sub-workflow already embeds its query text itself (via a dedicated tool-workflow call) before
calling that endpoint with a vector, and changing that contract wasn't needed or wanted for this.
`POST /search` instead takes plain free text (`ai_worker.embed_query_text`, a new function --
raises on any failure, unlike the existing `_embed_text` used when completing a sighting, which
swallows a failure and just stores `embedding=None`; a search request has nothing useful to return
with no vector, so `api.py` turns that raise into a 502) and does the embed-then-search round trip
in one call, since a browser can't reach the embedding backend directly the way n8n's own workflow
does.

`db.semantic_search_combined` (the function backing this, also new and separate from
`semantic_search_sightings`, which keeps serving `/search/semantic` unchanged) is a `UNION ALL`
across `sightings` and `visit_sightings` together, tagging each row `kind` ("event"/"visit") plus
that row's own `id` (a raw_event id or visit id -- independent sequences that can collide, so
`kind` is mandatory, never inferred) so the web UI knows which lightbox to open. An optional
`source` param (`"events"`/`"visits"`) narrows to one table -- the web UI itself doesn't expose
this, always searching both, per an explicit product decision that "anything relevant" beats
picking one flow upfront.

Each result also carries `has_image`/`has_video`/`ai_status` -- the same fields
`EventSummary`/`VisitSummary` already expose -- computed directly in this query rather than
requiring a follow-up fetch per clicked result. This was a deliberate design choice over the
alternative (add a new `GET /visits/{visit_id}` single-item endpoint, since none currently exists,
and have the frontend fetch full detail on click): computing these fields once, in the same query
that already joins to the row, is strictly cheaper than a second network round-trip per click, and
avoids growing the API surface for a need this query can already satisfy. For the visit branch,
`has_image` mirrors `_build_visits_query`'s own definition exactly -- a correlated subquery for the
representative (earliest-linked, same `get_representative_event_for_visit` definition) raw_event's
own crop -- same fallback chain `GET /visits/{id}/thumbnail` already applies server-side, so a
search result never disagrees with what every other view of the same visit already shows.

The web UI's Search tab (`static/app.js`'s `fetchSearchResults`/`openSearchResult`) reuses the
existing filter bar rather than inventing a new one -- the same "Search AI analysis" text field
(relabeled "Ask about your yard" while this tab is active) is the query text, and Time range/
From-To/Type all carry over unchanged; Event ID/AI status/"Only with media" stay hidden (already
gated to the Events tab only) since they're per-raw_event concepts with no equivalent here. There
is no pagination -- this is a fixed-size ranked top-N grid (`limit`, no `offset`), not a browsable
list, so the pager is hidden for this tab. Clicking a result routes into the exact same shared
lightbox (`openLightbox`) Events/Visits already use, building the same `{id, visitId, has_video,
has_image, ai_status}` shape `openVisitLightbox` already constructs for a plain
`VisitSummary` -- no new lightbox code was needed, only a new way to construct its input.

### Search relevance -- default time window, a `max_distance` cutoff, and a whole-word keyword fallback

Three related bugs, found by directly comparing what the Search tab showed against a raw
production `POST /search` call over SSH, not by inspection alone.

**Bug 1 -- Search tab silently inherited the Events/Visits tabs' 1-hour default.** `static/app.js`'s
`filters.hours` defaulted to `1` everywhere, including the Search tab, while `POST /search`'s own
schema default is `hours: 24`. A query like "dog" with genuinely relevant sightings older than an
hour would have them excluded by the UI's own default before the request was even sent, and the
API (correctly) returned whatever was next-closest *within* that narrow window instead -- which
read as "semantic search returns nonsense" rather than "an invisible 1-hour filter is active."
Fixed by making `_defaultFilters(mode)` take the view mode: Search now defaults to `hours: 24`
(matching the backend), Events/Visits keep `hours: 1` unchanged. Applied everywhere the filters get
reset (`switchView`/`resetFilters`/`toggleAdvancedSearch`), not just initial load.

**Bug 2 -- no relevance cutoff at all, so a weak query always pads out to `limit`.** Confirmed live:
searching "dog" over 24h returned exactly `limit` (24) results, but only 8 of them actually
mentioned a dog -- the embedding model (`Qwen3-Embedding-0.6B`, 1024-dim) doesn't separate "dog"
from generic "person walking near parked cars" strongly: true matches landed at cosine distance
~0.39-0.52, false positives at ~0.50-0.52, a heavily overlapping range for this small/general model.
This isn't a ranking bug (results were correctly sorted by distance) -- it's that `POST /search`
had no way to say "stop once matches get weak," so it always filled the response out to `limit`
with whatever was next-closest, however irrelevant. Fixed with an optional `max_distance` param on
`TextSearchRequest`/`db.semantic_search_combined` (rows past the cutoff are excluded, see
`schemas.py` for the exact param doc) and a **Precision** dropdown on the Search tab's simple view
(`High precision` = 0.45 default / `Balanced` = 0.55 / `Show everything` = no cutoff), plus a
`Precision (exact)` `step="0.01"` numeric override in Advanced mode (defaults to `0.5`, only takes
effect while the advanced panel is open -- gated on `advancedSearch` itself, not just whether the
field has a value, so its own default can't silently override the simple dropdown when the panel
isn't even shown). The simple Precision dropdown hides while Advanced mode is open, so there's
exactly one active precision control at a time, not two disagreeing ones. Each result card shows a
`matchPercent(distance)` badge (`round((1-distance)*100) + "% match"`, clamped to [0,100] -- a
rough human-friendly stand-in, not a calibrated probability; the raw distance is still in the
badge's tooltip).

**Bug 3 -- the cutoff's own literal-keyword fallback used a plain substring match, not a whole
word.** A cutoff can still exclude a sighting that literally contains the query word, just because
the rest of the sentence is about something else (confirmed: "...an adult in a grey t-shirt...
with a small dog nearby" scored 0.457, just past a 0.45 cutoff, despite "dog" being right there) --
so `max_distance` filtering always ORs in `description ILIKE '%{query}%'` as a fallback,
guaranteeing a literal keyword is never hidden by the cutoff regardless of embedding geometry. The
first implementation of that fallback used a plain substring match, which was itself a bug:
confirmed live that searching "cat" (no `cat` object type exists in `profiles.yaml` at all, so
there's nothing genuinely cat-related in this dataset) returned 24 completely unrelated
car/person/truck results, every one already past its own distance cutoff on merit -- all 24 matched
via the substring fallback catching "cat" inside **indi-CAT-ion** and **lo-CAT-ion**. Fixed by
switching the fallback to Postgres's `~*` case-insensitive regex with `\y` word-boundary anchors
(`\ydog\y` matches "a dog on a leash" but not "underdog"/"doggo"), with the caller's query text
passed through `re.escape()` before being embedded in the pattern -- still a bound parameter, never
concatenated into the SQL string, so this is about correct regex semantics, not injection safety.
Re-tested "cat" after the fix: 0 results, correctly.

### Prompt-echo in `person`/`dog` sightings -- a missing anti-narration instruction

Found the same way -- directly querying production `sightings`/`visit_sightings` for suspicious
text, not from code inspection. ~3% of all sightings (25 of 862 at the time) had `description`
starting with the literal prompt text itself ("The image shows a single moment of one person.
Describe their clothing colors...") instead of an actual answer, and a further ~11 more recent rows
had an otherwise-fine description with `profiles.yaml`'s own trailing guardrail clause
("...No speculation about identity, exact age, or other personal characteristics beyond apparent
child vs. adult.") echoed back verbatim by the model. Breakdown by object type: **100% `person`**
(25/25 full-echo sightings, 4/5 of the milder trailing-echo ones) plus one anomalous `car`-labeled
visit sighting (traced to a real mixed `car,person` visit -- the alert stage correctly used the
representative, earliest car det_id's prompt/label, but the grid's later sampled frames genuinely
showed the person who arrived ~26s in, which is why the model *mentioned* a person at all).
Zero from `truck`/`dog` at the time this was found. Root cause: `car`/`truck`'s prompts both end
with an explicit `"Do not describe your process."` instruction; `person`'s (and `dog`'s) prompts
had no equivalent anti-narration instruction at all, ending only on the content instruction itself
-- which the model apparently sometimes treats as something to recite back rather than silently
follow. Fixed by appending `"Do not describe your process or repeat these instructions --
respond with only the one-sentence description."` to `person`'s and `dog`'s `event_prompt`/
`alert_prompt` in `profiles.yaml`/`profiles.yaml.example`, matching the instruction `car`/`truck`
already had.

### Admin dashboard (`/ui/admin`)

A second static page alongside the report UI (`/ui`), for operational health/maintenance rather
than browsing sightings -- born directly out of a real production incident (an embedding
dimension mismatch that silently failed 34 events' AI analysis) that had to be diagnosed and fixed
by hand over SSH/psql, following `sql/queue-debug.sql`'s manual queries. Every action this page
exposes was previously only reachable that way; this turns them into real, authenticated buttons.
Same auth as `/ui` -- shares the same `api_key` cookie (`static/admin.js` reuses the identical
cookie name/mechanism as `static/app.js`, logging in on one page logs you into both), and every
`/admin/*` endpoint requires `X-API-Key` like any other write/read endpoint beyond `/health`/
`/status`. `GET /ui/admin` itself is a plain unauthenticated static page (same as `/ui/index.html`)
-- the login modal and every actual data fetch is what's protected, not the HTML shell.

Registered as an explicit `@app.get("/ui/admin")` route (`api.py`, just above the `/ui`
`StaticFiles(html=True)` mount) rather than relying on the mount alone -- `StaticFiles(html=True)`
only auto-resolves `index.html` for a directory path, not an arbitrary `/admin` -> `admin.html`
mapping, so without this route the page would only be reachable at the uglier `/ui/admin.html`.
Registered before the mount so it isn't shadowed; `static/admin.js`/shared `static/style.css` are
still served fine through the mount itself (`/ui/admin.js`, `/ui/style.css`).

**`GET /admin/overview`** is the dashboard's one fast-loading call -- row counts (`raw_events`/
`visits`/`sightings`/`visit_sightings`), per-stage queue status breakdown (`db.
get_stage_counts()`: crop/video/ai on `raw_events`, video/alert_ai on `visits`),
embedding coverage (reuses `count_sightings_missing_embedding`), DB size (`db.get_db_size_info()` --
`pg_database_size` total plus `pg_total_relation_size` per `yard_stats` table, so it matches what
actually shows up on the Postgres data volume, not just row bytes), vector index health (`db.
get_vector_index_status()` -- pgvector extension version, `EMBEDDING_DIMENSIONS`, and each HNSW
index's `indisvalid`/`indisready`), `get_retention_info()` (already existed, reused as-is), and a
feature-flags summary (`AI_EVENTS_STAGE_ENABLED`/`AI_ALERTS_ENABLED`/`STORE_VIDEO`/
`STORE_VIDEO_ALERTS`/`STORE_ALERT_IMAGES`/`CROP_DISABLED`/`TELEGRAM_EVENTS_MODE`/
`TELEGRAM_ALERTS_MODE`) so "what's currently turned on" is visible at a glance instead of having to
check `profiles.yaml` by hand. Everything in this call is cheap SQL -- deliberately excludes
anything that's a real filesystem walk or network call, so the dashboard's main section always
loads fast regardless of video backlog size or whether the VLM host is reachable. Note: this
feature-flags summary only ever reflects `config.py`'s hardcoded fallback defaults -- it doesn't
parse `profiles.yaml`, so a `defaults:` section or per-type override (see "Per-object-type
overrides" above) won't show up here, and since these settings no longer have a backing env var at
all, the hardcoded fallback shown may not reflect what's actually configured for any real type. The
"By object
type" section's row counts (below) do reflect whatever actually happened, which is shaped by any
per-type override already in effect.

`row_counts` additionally includes `row_counts_by_object_type` (`db.get_row_counts_by_object_type`)
-- a per-Frigate-label breakdown of `raw_events`/`sightings`/`visit_sightings` row counts (three
separate lists, one `GROUP BY` each, rather than one joined table -- a type can have `raw_events`
with no sighting yet, so summing across tables would either double- or under-count depending on
how it's done). `db_size` similarly gains `db_size_by_object_type`
(`db.get_db_size_by_object_type`) -- an *approximate* per-type Postgres footprint via
`sum(pg_column_size(t.*))` grouped by that table's own label column. This is a real byte count of
each row's stored data, but still an approximation of the type's true on-disk footprint: it
excludes per-row tuple overhead, TOAST storage for the crop column's actual out-of-line
chunks, and index space entirely -- `get_db_size_info()`'s `pg_total_relation_size` figures remain
the authoritative whole-table sizes; this is for relative "which type is using the most space"
comparison, not a precise accounting. The dashboard's "By object type" section combines this with
disk usage below into one row per type.

`row_counts` also includes `row_counts_by_camera` (`db.get_row_counts_by_camera`) -- same shape as
`row_counts_by_object_type`, grouped by camera instead. `raw_events` has its own `camera` column
directly; `sightings`/`visit_sightings` don't (only `object_label`), so those two lists join back
to `raw_events`/`visits` to attribute a camera. No per-camera DB-size figure exists (unlike
object type) -- that would need the same join-back cost for every table just to duplicate totals
already shown elsewhere in this endpoint, for a "nice to have" the video-storage-by-camera figure
below already covers the actual "what's using space" question per camera.

**`frigate_health` on `GET /admin/overview`** -- Frigate's own system-health heartbeat over MQTT
(`frigate/stats`, a periodic JSON blob every `frigate.conf`'s `mqtt.stats_interval` seconds, and
`frigate/available`, an online/offline flag), surfaced on the admin dashboard's "Frigate health"
card -- found by directly subscribing to `frigate/#` on a live broker to catalog what Frigate
actually publishes beyond the `frigate/events`/`frigate/reviews` topics already consumed, since
`frigate/stats` turned out to carry genuinely useful, currently-uncaptured signal (per-camera
`camera_fps`/`detection_fps`, detector `inference_speed`, CPU/GPU usage) that would otherwise only
surface indirectly, days later, as degraded/missed detections. `mqtt_ingest.py` subscribes to both
topics alongside its existing two, keeping only the latest snapshot of each in memory via plain
module-level globals (`_latest_stats`/`_frigate_available`, same pattern `_profile` already uses --
paho-mqtt callbacks all run on one thread, so there's no concurrent-write race to guard against) --
this is live current-state, not history worth persisting to Postgres the way `raw_events`/`visits`
are. `mqtt_ingest.summarize_stats` trims the raw payload down before storing it: the raw blob also
includes a `cpu_usages` entry per OS process inside Frigate's own container (`s6-supervise`,
`nginx`, `go2rtc`, ...), irrelevant noise for this purpose, kept down to per-camera fps/detection
state, detector inference speed, Frigate's own overall process CPU/mem
(`cpu_usages["frigate.full_system"]`), and `gpu_usages` passed through generically (the vendor key
varies by hardware -- `amd-vaapi`, `nvidia`, etc. -- so nothing here hardcodes one). `available` is
three-state on the dashboard (`true`/`false`/`null`), not a plain boolean flag like the feature
flags above it -- `null` means no `frigate/available` message has been received at all yet (e.g.
right after an `ingest-worker` restart, before Frigate's next heartbeat), which reads differently
from a confirmed `false` (Frigate genuinely reported itself offline).

**`GET /admin/disk-usage`** is split out specifically because it *is* a real filesystem walk
(`admin.dir_size_bytes`, `os.walk` summing real file sizes under `VIDEO_STORAGE_PATH`/
`VIDEO_STORAGE_PATH_ALERTS`/`ALERT_IMAGES_STORAGE_PATH`) -- kept separate so a large video backlog's
scan time never blocks the rest of the dashboard from rendering. A path that doesn't exist (e.g.
`VIDEO_STORAGE_PATH_ALERTS` when `STORE_VIDEO_ALERTS` has never been turned on, or
`ALERT_IMAGES_STORAGE_PATH` when `STORE_ALERT_IMAGES` hasn't) reports as zero bytes rather than an
error -- an unused optional storage location isn't a fault. Also returns
`video_storage[_alerts]_by_object_type`/`alert_images_storage_by_object_type`
(`admin.dir_size_by_object_type`) -- the same walk, but bucketed by object type parsed from each
file's own name (`video.py`'s `store_clip`/`store_visit_clip` and `alert_images.py`'s
`store_alert_images` all name a file `{object_type}-{id}-...` or `visit-{object_type}-{id}-...`,
so the type is always either the first hyphen-token or the token right after a leading `visit-`).
A name that doesn't match this pattern at all buckets under `"unknown"` rather than raising or
being silently dropped from the total.

Also returns `video_storage[_alerts]_by_camera`/`alert_images_storage_by_camera`
(`admin.dir_size_by_camera`) -- unlike the object-type breakdown, this needs no filename parsing at
all: `video.py`'s `store_clip`/`store_visit_clip` and `alert_images.py`'s `store_alert_images` all
write under a camera-named top-level directory
(`VIDEO_STORAGE_PATH/{camera}/{YYYY}/{MM}/{DD}/...` -- see "Video storage" below and "Alert AI
stage" above), so the camera is just that top-level directory's own name. `admin.dir_size_by_camera`
walks one level with `os.scandir` to enumerate the top-level directories (the cameras), then
`os.walk`s each one to sum its bytes -- a file sitting directly at the root (not under any camera
directory) isn't itself a camera and is correctly excluded from every bucket, unlike
`dir_size_by_object_type`'s flat walk which has no such root/non-root distinction to make. A clip
stored before this layout existed sits directly under a year directory instead of a camera one, so
it buckets under that year (e.g. `"2026"`) rather than a real camera name -- an expected one-time
artifact of files predating this change, not a bug: nothing migrates existing files into the new
layout automatically (see "Video storage" below). `alert_images.py`'s layout was camera-first from
the start, so this only applies to video.

**`GET /admin/embedding-backend/check`** is a live, on-demand smoke test against
`LLAMA_PROXY_BASE_URL`/`LLAMA_PROXY_EMBED_PATH` (`admin.check_embedding_backend`) -- sends a tiny
real embedding request and checks both that something answers at all and that the dimension
matches `config.EMBEDDING_DIMENSIONS`, the exact same check `ai_worker._embed_text` already applies
on every real call. Button-triggered rather than part of `/admin/overview` since it's a genuine
network round-trip, not a cheap query -- this is precisely the check that would have caught the
`llama-slot-proxy` embedding-slot outage (a `501 not_supported_error` from a `--embeddings`-less
model) discovered live in production while building this feature, without needing to manually curl
the endpoint from a shell.

**`POST /admin/vector/reindex`** (`db.reindex_vector_indexes`) runs `REINDEX INDEX` on both HNSW
embedding indexes -- fixes an `indisvalid=false` index (e.g. left behind by an interrupted
concurrent build) and is a reasonable "tidy up" action after a large `/embeddings/backfill` run.
Non-destructive to the underlying embedding data either way, so no confirmation step is needed
(unlike retention purge below).

**`POST /admin/queue/requeue-failed?table=<raw_events|visits>&stage=<...>`** (`db.requeue_failed`)
is the exact fix `sql/queue-debug.sql`'s "retry every crop-failed / ai-failed item" query already
documented for manual use, now a real button: resets every row at `{stage}_status='failed'` back
to `'retry'` with `{stage}_attempt_count` reset to `0`, so the next poll tick/claim picks it back
up. `table`/`stage` are validated against a fixed whitelist (`db._REQUEUE_TARGETS`) before ever
touching SQL -- a `raw_events` row can be requeued for `crop`/`video`/`ai`, a `visits` row for
`video`/`alert_ai`; an unknown combination is a 400, not a SQL injection surface. The dashboard
shows a "Requeue N failed" button next to any stage currently at 1+ failed, matching how this
session's production incident (34 events failed on an embedding dimension mismatch) was actually
resolved by hand over SSH before this button existed.

**Embeddings backfill and retention purge are exposed as buttons too, reusing the existing
endpoints** (`POST /embeddings/backfill?confirm=true&limit=200`, `POST /retention/purge`) -- both
already had the right dry-run-by-default shape. The retention purge control mirrors the API's two
independent purge flags directly (`purgeDeleteVideo`/`purgeDeleteSnapshots`/`purgeDeleteAll` -- see
"Query/report/AI-queue API" above for what each one clears): the two media checkboxes are disabled
(greyed, not just ignored) while "Delete ALL" is checked, since they no longer mean anything once
the whole row is going away. Checking "Delete ALL" switches the effective `only_media` param to
`false` and shows the full row-count preview plus a starker PERMANENTLY-delete confirmation that
also mentions the vector-index rebuild that follows; leaving it unchecked keeps `only_media=true`
and shows a media-focused preview (video file/snapshot counts) with a lighter confirmation listing
only the categories actually checked ("rows and all AI analysis text are kept"). Confirming with
every media checkbox unchecked and "Delete ALL" also unchecked is a no-op the UI catches before the
confirm dialog even opens ("Select at least one category to clear (or check \"Delete ALL\")."),
rather than silently doing nothing after a scary-sounding dialog. Either way, a native JS
`confirm()` dialog spells out
exact counts (from a mandatory preview call first) before the real `confirm=true` call fires --
the same two-step preview-then-confirm flow the API itself already enforces, just made impossible
to skip from the UI as well, since both modes are irreversible once confirmed. An "Object type"
dropdown and a "Camera" dropdown (backed by `GET /object-types`/`GET /cameras`, the latter fetched
once on first login the same way `objectTypes` already was) map to `object_label`/`camera` and
compose with each other and with every checkbox above -- see "Query/report/AI-queue API" above for
why `camera` (unlike `object_label`) also scopes visits.

### Schema (`yard_stats`)

- `raw_events` — one row per Frigate `end` event, any label. Carries all three queue state machines
  plus `crop_image_base64`, `sub_label` (Frigate's own LPR read), `score` — all captured by
  `ingest-worker` from one Frigate API fetch, so n8n never needs to call Frigate itself — and,
  when video storage is on, `video_path` (filesystem path only, never the file itself) and
  `telegram_photo_message_id` (for threading the later video reply). `visit_id`/`reconciled` link
  a row to the `visits` segment Frigate's own review/alert stream grouped it into (see above).
- `visits` — one row per Frigate review/alert segment (`frigate/reviews`), grouping the
  `raw_events` det_ids Frigate's own tracker considers the same real-world activity. Populated by
  `db.record_visit`; cross-camera merging is not yet implemented (see above). Carries `thumb_time`
  (Frigate's own review "best frame" timestamp, informational only -- not read by any code path).
  `crop_image_base64`/`preview_gif_base64`/`thumb_crop_status`/`thumb_crop_status_changed_at`/
  `thumb_crop_attempt_count` remain in the schema but are unwritten/unread by any code path -- these
  backed a now-removed visit-level composite-grid/GIF preview stage, superseded by the alert stage
  gathering high-res per-event crops directly instead (see "Alert AI stage" above); dropping these
  columns is a deferred, separate migration. `alert_ai_status`/`alert_ai_status_changed_at`/
  `alert_ai_attempt_count` (see "Alert AI stage" above) are this visit's own queue stage,
  entirely independent of any linked raw_event's `ai_status`. `alert_image_paths` (nullable,
  comma-joined) is the opt-in `STORE_ALERT_IMAGES` persistence of the alert stage's own gathered
  high-res crops -- only the filesystem paths live here, the JPEG bytes live under
  `ALERT_IMAGES_STORAGE_PATH` on disk (see "Alert AI stage" above).
- `sightings` — one row per AI-analyzed event, **any** object label (`object_label`, straight from
  `raw_events.objects` -- car, truck, person, dog, whatever `profiles.yaml` maps). `description` is
  the VLM's plain free-text answer to that label's `event_prompt` -- there is no structured
  per-type column of any kind (no `color`/`body_type`/`plate_text_llm`/`notes`, etc.); a plate
  reference, if the model mentions one, just lives inside `description` like any other detail.
  Frigate's own LPR read (`raw_events.sub_label`) still exists on the event row itself regardless
  of what a given label's prompt asks about, but there's no dedicated cross-check column against
  it anymore. Also carries a nullable `embedding vector(N)` (pgvector, N = `EMBEDDING_DIMENSIONS`)
  for `POST /search/semantic` -- see "Semantic search and the Q&A agent" above.
- `visit_sightings` — one row per alert-AI-analyzed visit (see "Alert AI stage" above), same
  universal shape as `sightings` (`object_label`, `description`, its own nullable `embedding
  vector(N)` + HNSW index) but keyed by `visit_id` instead of `raw_event_id`. `description` can
  include a note about what changed across the visit's 4 sampled frames if `alert_prompt` asks for
  one -- that's just part of the same free-text field, not a separate structured column.

### Prerequisites this plan assumes

- Frigate **0.16+** (required for LPR and face recognition), `lpr.enabled: true`.
- Record stream at full camera resolution (separate from the low-res detect stream the Coral
  uses) — clips and crops come from the record stream (confirmed 3840x2160 on this setup).
- Same zone name configured across overlapping cameras so cross-camera dedup can match on zone.

## Working conventions

- Keep new pieces as **separate containers**, not baked into a monolith (matches WAHA, mcp-proxy).
- Version/store prompts in one place (a `Set` node / small config table), not inlined across
  multiple n8n workflows.
- Unattended workflows retry-with-a-cap rather than failing immediately or retrying forever:
  all three queue stages increment an attempt counter and only go terminal (`failed`) at/above
  that stage's max-attempts setting (`MAX_ATTEMPTS`/`VIDEO_MAX_ATTEMPTS`, both default small) —
  below that, a failure goes back to `retry` and is picked up on a later run, not looped within
  the same execution.
- Treat plate text and clips as semi-sensitive data — `ingest-worker` applies a retention sweep
  (`RETENTION_MONTHS`, default 12) on its own schedule (`RETENTION_CHECK_INTERVAL_SECONDS`),
  deleting stored video files off disk (best-effort) alongside the DB rows; an equivalent n8n
  workflow existed early on but has since been removed from `n8n/` as superseded.
- The Coral's base detection model is the accuracy ceiling for anything reaching this pipeline
  (missed detections never generate an event at all) — a Frigate/Frigate+ concern, not something
  to compensate for at the LLM layer.

## Commands

- Run the pipeline stack: `docker compose --profile pipeline up -d` (from `frigate/`; requires
  `.env` filled in from `.env.example`). `ingest-worker` pulls its image from GHCR by default
  (built by `.github/workflows/ingest-worker-image.yml`); use `docker compose --profile pipeline
  build ingest-worker` first only if overriding the compose file's `image:` with `build:
  ./ingest-worker` for local development.
- Add `--profile mqtt` to also bring up a local Mosquitto broker (`MQTT_HOST=mosquitto`) for a
  from-scratch local/dev stack with no external broker dependency.
- Manual DB checks/fixes: `frigate/sql/queue-debug.sql` (status breakdowns, force-retry, resets).
- Manual API testing: `http://<host>:8080/docs` (Swagger UI) once `ingest-worker` is running; the
  web report UI is at `http://<host>:8080/ui`.
- n8n workflows are plain JSON exports under `n8n/` — import via n8n's UI, fill in credentials
  after import (`REPLACE_AFTER_IMPORT` placeholders), then manually trigger once against a few
  real rows before enabling a workflow's schedule trigger.
- Frigate's own stack: same `frigate/.env` (fill in its section), then deploy on the actual NVR
  host via `docker compose --profile nvr up -d`.
