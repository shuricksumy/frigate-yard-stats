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
n8n Metadata Processor (car/truck/person, one shared queue) -- AI stage only, no Frigate/crop/video/
Telegram calls
   - POST /ai-queue/claim -- reap stale, count in-progress, atomically claim a batch, all in one call
   - route by object type, call the VLM(s) directly against the claimed row's crop_image_base64
   - POST /sightings/vehicles or /sightings/persons -- insert + mark ai_status='done' in one call
   - on VLM failure: POST /ai-queue/{id}/fail -- retry-or-fail-with-cap
   │
   ▼
Daily Report / Q&A agent (n8n) -- read-only, calls ingest-worker's query/report API
   (which itself only ever reads *_sightings rows, i.e. AI-analyzed events)
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
the cap, terminal) → `done`. Both `video_status` and `crop_status` additionally have `skipped`, set
at ingest time -- `video_status` when `STORE_VIDEO=false`, `crop_status` when the MQTT payload's
`has_snapshot` is false. The latter matters because Frigate can emit a full `new`→`end` MQTT
lifecycle for a tracked object it never actually persists as a real event (confirmed in production:
such rows' `det_id` 404s against Frigate's own `/api/events/<id>`) -- cropping those can never
succeed regardless of retries or queue throughput, so they're marked `skipped` immediately rather
than piling up as an eternally-unprocessed `new`. `FOR UPDATE SKIP LOCKED` is what makes claiming
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
  slot/port pointing at its own `.gguf` + `mmproj` pair. Vehicle attributes and plate OCR are a
  single combined call to one model (merged for speed -- see `n8n/metadata-processor.json`'s
  `Call Qwen (Attributes + Plate)` node); `plate_text_frigate` is kept alongside `plate_text_llm`
  as a cross-check regardless.
- **Postgres**: `postgres-projects` container, database `home_automation`, schema `yard_stats`
  (schema-per-project convention — future unrelated projects get their own schema).

### Query/report/AI-queue API

`ingest-worker`'s FastAPI app has two tiers: `/health`, `/status`, `/crop/{id}`, `/retention/run`
are unauthenticated admin/debug endpoints (unchanged since the original split). Everything else --
`/events`, `/sightings/vehicles`, `/sightings/persons`, `/stats/summary`, `/reports/generate`,
`/ai-queue/claim` / `/ai-queue/{id}/fail`, and `/retention/purge` -- requires an `X-API-Key` header
(`config.API_KEY`) since they expose queryable sighting data (including plate text), mutate the
AI-stage queue, or bulk-delete rows over the network. `ingest-worker` never calls an LLM to serve
any of these — the write endpoints just execute the claim/insert/retry/delete mechanics; the VLM
call and prompt still live entirely in n8n, which posts the result back.

`POST /retention/purge` is an ad-hoc counterpart to the scheduled `RETENTION_MONTHS` sweep for
when you want to purge on a caller-chosen cutoff rather than waiting on or reconfiguring the
scheduled one. Defaults to a dry run (`confirm` query param defaults to `false`): it always
returns counts of matching rows/files, and only actually acts when `confirm=true` is passed
explicitly. A second mode, `only_media` (defaults to `true`), decides *what* gets purged:

- **`only_media=true`** (default) -- `db.purge_media_older_than`: deletes stored video files off
  disk and clears the stored image/GIF columns (`crop_image_base64`/`preview_gif_base64` on both
  `raw_events` and `visits`) for rows older than the cutoff, but keeps every row and all its
  text/structured fields (AI analysis, plate reads, descriptions, embeddings) -- old data stays
  fully searchable via `/events`'/`/visits`' `q` filter and `/search/semantic`, just with the media
  payload gone. Never touches `vehicle_sightings`/`person_sightings`/`visit_vehicle_sightings`/
  `visit_person_sightings` at all -- none of those tables carry media columns of their own.
- **`only_media=false`** -- `db.purge_older_than` (today's original behavior): deletes the rows
  entirely -- same FK-safe child-before-parent delete order as `db.run_retention_cleanup`, extended
  to also delete `visit_vehicle_sightings`/`visit_person_sightings` before `visits` (added
  alongside the alert AI stage -- see below) and to decouple `raw_events.visit_id` from a
  to-be-deleted `visit` *before* deleting that `visit`, not after. On a real `confirm=true` run,
  the endpoint also rebuilds both HNSW indexes afterward (`db.reindex_vector_indexes`) -- a full
  purge can remove a large fraction of the rows the index was built over, so this keeps it sized
  and accurate for whatever data survives rather than leaving it bloated for data that's gone.

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
the AI stage analyzes, without touching completion at all -- `POST /sightings/vehicles|persons`
still mark the exact same claimed raw_event's `ai_status='done'` either way, since this is purely
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

`vehicleFields` renders as one combined "Description" line (color + body type + make + model, then
notable_features, then plate) instead of a Color/Body type/Make/Model/Plate table -- reads like the
Person side's single Description line rather than a spreadsheet of individual fields a reader has
to scan across. Same combination logic as `report.py`'s `_vehicle_summary`, kept in sync
deliberately (both exist to answer the same "describe this sighting in one line" need, just for
different surfaces -- the web UI lightbox vs. the alerts report).

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
per distinct object type -- so `n8n/metadata-processor.json` (now the only processing workflow)
just uses plain `source=visits` and never sets `visits_only`. The param still exists for anyone who
wants to go back to strictly alert-scoped analysis (never touch an ungrouped raw_event at all).

(Bug fixed in passing while building `source`: `claim_ai_batch`'s `RETURNING yard_stats.raw_events.*`
never included the computed `has_video`/`has_image` fields `EventDetail` requires -- every call
that actually claimed rows was crashing at FastAPI's response-serialization step with a 500,
*after* the UPDATE had already committed `ai_status='processing'` in the DB. n8n never received
the claimed rows, which then sat until `stale_minutes` reaped them back to `retry` and the cycle
repeated -- confirmed by reproducing the exact 500 locally, then confirming claims complete
cleanly end-to-end once the two computed columns were added to the `RETURNING` clause.)

`POST /sightings/vehicles` and
`POST /sightings/persons` insert the sighting and mark `ai_status='done'` in one DB transaction
(`db.complete_vehicle_sighting`/`complete_person_sighting`, temporarily flipping the module
connection to `autocommit=False`) -- this closes a small gap the old two-Postgres-node version had,
where a crash between Insert and Mark Done left the row `processing` until the next reap.

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

`source=visits`'s HTML also renders differently from `source=events`'s, not just differently
dedup'd: `report.py`'s `_group_by_visit` groups a visit's vehicle and person sightings into one
combined alert row (image, time, camera, a Vehicle summary column, a Person summary column)
instead of two disjoint Vehicles/Persons tables -- a visit's car and person sightings (e.g. someone
getting out of their car) are the same real-world activity, so the alerts report shows them
together rather than as two separately-scrolled, unrelated-looking rows a reader has to manually
reassociate by timestamp. Grouping key is `visit_id` (added to `get_report_data`'s SELECT for
exactly this), falling back to the raw_event's own id for a sighting that was never grouped into a
visit at all (a group of one, same as today). The earliest sighting in a group represents its
time/camera/image (`crop_image_base64` is already consistently the visit's own thumb-crop across
every sighting in the group once `VISIT_THUMB_CROP_ENABLED` and done -- see the `COALESCE` above --
so this only matters for picking which event's own crop to show when it isn't). `_vehicle_summary`/
`_person_summary` flatten each sighting's structured fields (color/body/make/model/notable_features
/plate, or description) into one short line per sighting, joined with `; ` if a visit somehow has
more than one of the same type. `source=events` (the default, `n8n/daily-report.json`) keeps the
original separate-tables rendering -- there's no visit grouping concept to apply there, every
sighting already stands alone.

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
workflows or ever pick plain `source=events` -- `n8n/metadata-processor.json` is now the only
processing workflow, using `source=visits` unconditionally; `metadata-processor-alerts.json` was
removed.

### Internal AI stage (`ai_worker.py`) -- an alternative to `metadata-processor.json`

`metadata-processor.json`'s own logic -- claim work, call the VLM, parse the response, insert the
sighting -- is genuinely deterministic control flow; the only actual "AI" part is the VLM call
itself, which happens regardless of which language issues it. `ai_worker.py` is that same logic
ported straight into `ingest-worker` as a real, testable Python poll-loop stage, following the
exact same `process_claimed_event`/`run_once`/`run_forever` shape `crop_worker.py`/`video_worker.py`
already use -- own daemon thread, started conditionally in `main.py`
(`if config.AI_EVENTS_STAGE_ENABLED`), off by default like `STORE_VIDEO`/`VISIT_THUMB_CROP_ENABLED`.
This is the **events** stage specifically -- always analyzes a raw_event's own single-frame crop,
never a visit's composite grid, regardless of `VISIT_THUMB_CROP_ENABLED`; see "Alert AI stage"
below for the sibling stage that analyzes the grid. (Renamed from the original single
`AI_STAGE_ENABLED` once a second, independent stage existed to split from -- see that section for
why the split happened and the real gap it fixes.) It calls the exact same three `db.py`
functions n8n's HTTP calls already wrap -- `claim_ai_batch`, `fail_ai_event`,
`complete_vehicle_sighting`/`complete_person_sighting` -- directly rather than over HTTP, so **no
`db.py`/`api.py`/schema change was needed at all** for the queue mechanics. `claim_ai_batch` already
folds reap-stale + count-in-progress + capacity + claim into one call (unlike crop/video's claim
functions), so `ai_worker.run_once` is simpler than `crop_worker.run_once` -- just one call plus a
loop.

**This is an alternative, not a replacement** -- `n8n/metadata-processor.json` is left untouched and
inactive in n8n, not deleted, and every existing API endpoint (`/ai-queue/*`, `/sightings/*`,
`/search/semantic`, etc.) is completely unchanged, so the n8n-driven flow can be re-enabled at any
time. The two must not run against the same queue simultaneously in practice (both would claim
`ai_status='new'`/`'retry'` rows -- safe from a correctness standpoint, `FOR UPDATE SKIP LOCKED`
prevents a double-claim either way, but wasteful/confusing to run both at once) -- pick one.

**Prompts and per-object-type model routing live in `frigate/profiles.yaml`, not env vars** --
`docker-compose.yml` bind-mounts this file (repo root, alongside `docker-compose.yml` itself) over
`/app/profiles.yaml` by default (read-only), the same path `AI_STAGE_PROFILE_PATH` already defaults
to -- so editing it and restarting the container is enough to change prompts/models, no image
rebuild needed. `frigate/ingest-worker/profiles.yaml` is a separate copy still baked into the image
via the Dockerfile's `COPY . .` (same as `schema.sql`) purely as a fallback default for if that
bind mount is ever removed -- the two aren't linked, keep them in sync by hand if you edit one.
Two-level structure: `object_types` maps a Frigate
object label (`raw_events.objects`) to a `sighting_type` ("vehicle" or "person" -- which table/
prompt/parsing shape to use); `vehicle`/`person` sections hold that type's `chat_path` (appended to
`LLAMA_PROXY_BASE_URL` -- `llama_slot_proxy`'s convention is one URL path segment per model slot,
e.g. `/spare/v1/chat/completions`, not a `model` field in the request body), shared by both stages,
plus **two** separate prompts rather than one: `event_prompt` (this stage -- framed for the single
static frame it actually receives) and `alert_prompt` (the alert stage below -- framed for the
composite grid it receives instead). Originally a single `prompt` field, ported verbatim from
`metadata-processor.json`'s `Call Qwen (Attributes + Plate)`/`Call VLM (Person)` nodes and already
written as if it were receiving a 2x2 grid -- split in two once it became clear this stage never
actually sent one (see "Alert AI stage" below). Keep `event_prompt` in sync with
`metadata-processor.json`'s own node if you're switching between the two flows, so analysis quality
doesn't change with the switch -- that n8n workflow only ever sends a single-frame crop today, same
as this stage's `event_prompt`, never the grid. **A Frigate object label with no `object_types` entry (e.g. `dog`, which
has no matching sightings table) is simply never claimed by this stage at all** -- its
`object_types` keys become exactly the `object_types` list passed to `claim_ai_batch`, so an
unmapped type's rows just stay `ai_status='new'` indefinitely, the same as if the stage were off for
that one type. Not a bug to fix -- there's nowhere to store a "dog sighting" today.

Vehicle response parsing (`ai_worker.parse_vehicle_response`) is a straight Python port of
`Parse VLM Responses (Vehicle)`'s JS -- regex-extract the `{...}` JSON blob, `json.loads`, the same
plate-text sanitizer (`_sanitize_plate`: a clean short token passes through as-is, a narrative
answer gets the most plate-like token pulled out of it instead). Embed text reuses
`report._vehicle_summary`/`report._person_summary` directly (`ai_worker.py` imports `report`)
instead of a third copy of that combination logic -- n8n's `Build Embedding Text (Vehicle)`/
`(Person)` nodes are the second copy, `report.py`'s own use in the alerts report is the first; all
three now describe a sighting in the exact same one-line format. An embedding-call failure falls
back to `embedding=None` rather than losing the whole sighting (same decision as the n8n version's
`continueErrorOutput` nodes) -- only a chat-call/parsing failure routes to
`db.fail_ai_event(event_id, config.AI_STAGE_MAX_ATTEMPTS)`, mirroring `crop_worker.py`'s
except-block pattern exactly.

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

### Alert AI stage (`alert_ai_worker.py`) -- analyzes a visit's own composite grid

#### The bug this fixes

`profiles.yaml`'s original single `prompt` field was already written as if it were analyzing a
2x2 grid ("This image is a 2x2 grid of 4 frames of the SAME vehicle...") -- but `ai_worker.py`'s
`run_once` calls `db.claim_ai_batch` with none of `source="visits"`/`only_visit_representative`/
`require_thumb_crop` set, so it behaves exactly like plain `source=events`: it claims individual
`raw_events` and always analyzes that event's own single-frame crop (`crop.crop_and_scale`'s
output, at `CROP_FRAME_OFFSET_PCT`), never `visits.crop_image_base64` (the composite grid). Those
query params only ever existed for n8n's `POST /ai-queue/claim` -- `ai_worker.py` never wired them
up. Confirmed live in production: every VLM call this stage made was analyzing a plain single
frame while being told it was looking at 4 frames of motion, silently producing worse/inconsistent
results (a "notable_features"/plate read based on one frame passed off as cross-referenced across
4, a "what changed across the sequence" question a single static frame can't actually answer). Not
a config toggle that was missed (`require_thumb_crop` defaulting to `false` is itself fine, an
intentional latency/quality trade-off for n8n callers) -- `ai_worker.py` simply never requested the
grid at all, under any configuration.

#### The fix: two genuinely separate, independently-toggleable stages

Rather than have one stage try to opportunistically use the grid when available (the n8n-facing
`require_thumb_crop` approach, which still only produces one sighting per raw_event either way),
this splits into two real stages with two real prompts, matching this project's existing precedent
for every other events-vs-alerts split (`STORE_VIDEO`/`STORE_VIDEO_ALERTS`,
`TELEGRAM_EVENTS_MODE`/`TELEGRAM_ALERTS_MODE`): independent enable flag, independent queue,
independent poll thread, shared tuning knobs.

- **`AI_EVENTS_STAGE_ENABLED`** (renamed from `AI_STAGE_ENABLED`) -- `ai_worker.py`, unchanged
  behavior, now explicitly framed as the events-only stage. Uses `profiles.yaml`'s `event_prompt`.
- **`AI_ALERTS_ENABLED`** -- `alert_ai_worker.py`, a new stage claiming from **`visits`**, not
  `raw_events`, via a new sixth queue-state-machine column, `visits.alert_ai_status` (same
  `new -> processing -> retry/failed -> done` shape, plus `alert_ai_status_changed_at`/
  `alert_ai_attempt_count`, `idx_visits_alert_ai_status`). Uses `profiles.yaml`'s `alert_prompt`
  against `visits.crop_image_base64` (the actual composite grid) -- the image this stage sends is
  *always* the grid, never opportunistic, since that's the entire point of this stage existing.
  Requires `VISIT_THUMB_CROP_ENABLED=true` to ever have anything to claim (`db.claim_alert_ai_batch`
  hard-requires `thumb_crop_status='done'`) -- with it off, this stage just stays idle, the same
  graceful "nothing to do" treatment an unmapped object type already gets elsewhere in this project,
  not an error.

Both stages can run at once, on or off independently -- an event's own `ai_status` and its visit's
`alert_ai_status` are two entirely separate state machines on two separate tables, so the same
underlying activity can be analyzed once per event (events stage) and once per visit (alerts
stage) without either blocking or overwriting the other. Both are started conditionally in
`main.py`, one `threading.Thread` each, same shape as every other opt-in poll-loop stage.

#### `db.claim_alert_ai_batch` -- matching a visit to a single object type despite `visits.objects` being multi-valued

`visits.objects` (populated by `record_visit` from Frigate's own `data.objects`, comma-joined --
`mqtt_ingest.py`'s `",".join(data.get("objects") or [])`) can legitimately span more than one
distinct type per visit (e.g. `"car,person"` -- see "Visit grouping" above). But the composite grid
itself is inherently single-object-framed: `crop.build_visit_preview` crops all 4 sampled frames to
one specific event's own region/box (the representative event's), not "the whole visit." So
`object_types` filtering for this claim matches against the visit's own **representative** event's
`objects` (`db.get_representative_event_for_visit`'s definition -- earliest-linked raw_event,
`ORDER BY start_ts ASC, id ASC LIMIT 1`), joined in via `LATERAL` inside the claim's CTE, not
`visits.objects` -- a visit spanning both a car and a person still gets exactly one alert analysis,
of whichever type the grid was actually framed around. (This is a different, narrower matching
concern from `claim_ai_batch`'s own `(visit_id, objects)` partitioning for `only_visit_representative`
-- that dedups *raw_events* per type per visit for the *events* stage; this alerts-stage claim
never touches `raw_events.ai_status` or that partitioning at all.) Same reap-stale +
count-in-progress + CTE-`FOR UPDATE SKIP LOCKED` shape every other claim function in this project
uses, newest-`start_ts`-first, with the same optional `max_age_hours` throughput safety valve
`claim_ai_batch`/`claim_video_batch` already have.

#### Storage: new tables, not new columns on existing ones

`visit_vehicle_sightings`/`visit_person_sightings` -- new tables, same shape as
`vehicle_sightings`/`person_sightings` (including their own nullable `embedding vector(N)` +
HNSW index, sized off the same `EMBEDDING_DIMENSIONS`/`_ensure_embedding_dimension()` machinery),
but keyed by `visit_id` instead of `raw_event_id`. Chosen over reusing the existing tables (adding
a nullable `visit_id` + making `raw_event_id` nullable + a source discriminator) specifically
because every other alerts-vs-events split in this project already keeps the two flows' storage
fully separate rather than overloading one table/column set for both (`STORE_VIDEO_ALERTS`'s own
`video_path`/storage directory, `visits.crop_image_base64`/`preview_gif_base64` vs.
`raw_events.crop_image_base64`) -- this is purely additive, so zero risk to any existing query
against `vehicle_sightings`/`person_sightings` (`report.py`, `/search/semantic`, the web UI's
per-event lightbox, `/embeddings/backfill`), none of which needed to change at all.
`db.complete_visit_vehicle_sighting`/`complete_visit_person_sighting` mirror
`complete_vehicle_sighting`/`complete_person_sighting`'s insert-plus-mark-done-in-one-transaction
shape exactly, just against `visits.alert_ai_status` instead of `raw_events.ai_status`; no
`plate_text_frigate` equivalent exists on the visit-level table -- Frigate's own LPR read is
per-event, not per-visit. `alert_ai_worker.parse_alert_vehicle_response`/
`parse_alert_person_response` mirror `ai_worker.parse_vehicle_response`/`parse_person_response`
(and directly reuse `ai_worker._chat_request`/`_sanitize_plate`/`_embed_text`/`_JSON_BLOB_RE` via
`import ai_worker`, rather than a second copy of that request/parse/embed plumbing) -- the one
real difference is `alert_prompt` asks for and gets a `notes` field the vehicle parser now
actually captures (a short description of what changed across the 4 frames, e.g. "pulled into the
driveway and parked"), where `event_prompt`/`parse_vehicle_response` never asked for one at all
(single frame, nothing to describe "across").

Currently out of scope, deliberately: `/embeddings/backfill` and `/search/semantic` don't cover
`visit_vehicle_sightings`/`visit_person_sightings` yet -- freshly-analyzed alert sightings still
get an embedding computed inline (same `_embed_text` call every events-stage sighting already
gets), so there's no backlog needing a backfill on day one; wiring the read/backfill side to also
cover these two tables is a reasonable follow-up once there's real data to search, not something
this change needed to include to be complete.

#### Web UI: `GET /visits/{id}/sightings` gains `alert_sighting`, preferred over the per-event fallback

`db.get_visit_alert_sighting` (new) returns the visit's own `visit_vehicle_sightings`/
`visit_person_sightings` row if one exists, `null` otherwise -- wired into the existing
`GET /visits/{id}/sightings` response as one more field (`alert_sighting`) alongside the unchanged
`vehicles`/`persons` lists, rather than a second endpoint, so the web UI's visit lightbox only
needs the one fetch it already made. `static/app.js`'s `openLightbox` now prefers
`data.alert_sighting` when present (labeled "Vehicle (alert analysis)"/"Person (alert analysis)"
in the lightbox) and only falls back to the per-event `vehicles`/`persons` groups when it's `null`
-- the same "richer artifact when available, graceful fallback otherwise" precedent this project
already uses for the preview GIF/composite-grid/event-crop chain. This is deliberately a fallback,
not an exclusive switch: a visit whose alert stage is off, or hasn't finished yet, still shows
whatever per-event analysis already exists instead of an empty lightbox. On the Events tab (plain
events, never visits), `GET /events/{id}`'s `vehicle_sighting`/`person_sighting` -- the events
stage's own result -- is unaffected and unchanged; the alert stage/`alert_sighting` field only
ever applies to the Visits tab.

### Video storage, Telegram notifications, and the web report UI

`STORE_VIDEO=true` turns on the third queue stage (`video_status`) and its poll loop thread
(`video_worker.py`/`video.py`). Frigate is often still finalizing the recording segment when the
`end` event fires, so a freshly claimed row waits `VIDEO_INITIAL_WAIT_SECONDS` before the first
download attempt; the clip is fetched from Frigate's own
`/api/{camera}/start/{start_ts-5s}/end/{end_ts+5s}/clip.mp4` endpoint (not the event-id endpoint
`crop.py` uses), and a response at/below `VIDEO_MIN_VALID_BYTES` is treated as Frigate's
not-ready-yet placeholder rather than a real clip, retried up to `VIDEO_MAX_ATTEMPTS` times. Only
the resulting filesystem path (`VIDEO_STORAGE_PATH/{YYYY}/{MM}/{DD}/{object_type}-{event_id}-
{start_ts_epoch}-{start_ts_iso}.mp4` -- epoch for a stable/sortable key, an ISO-ish UTC timestamp
alongside it since the epoch alone isn't recognizable at a glance in a directory listing) is
stored in Postgres (`video_path`) -- the file itself lives on disk only. The `{YYYY}/{MM}/{DD}`
folder is keyed on the *event's* `start_ts`, not on when the file was actually written -- under a
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
since there's nothing to show for them; pass `has_media=false` to see every row regardless. The
web UI's "Only with media" checkbox (checked by default) is this same param, not a client-side
filter. In practice `video_path` is never set without `crop_image_base64` already being set too
(`claim_video_batch` only ever claims `crop_status='done'` rows), so this is currently equivalent
to crop-image-only -- but the check covers both so it stays correct if that invariant ever
changes. `GET /events?event_id=<id>` exact-matches a single event and bypasses every other filter
(time window and `has_media` included) -- searching for one specific known event should find it
regardless, not get filtered out by the defaults built for browsing a range.

`GET /events?q=<text>` free-text searches (case-insensitive substring) across the AI analysis
result -- `vehicle_sightings`' color/body_type/make_guess/model_guess/notable_features/
plate_text_llm/plate_text_frigate/notes, or `person_sightings`' description/notes -- via a `LEFT
JOIN` to both tables (`SELECT DISTINCT` guards against a fan-out if either sighting table ever had
more than one row per `raw_event_id`, which nothing enforces at the schema level). Only ever
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

`GET /events/{id}` also returns `vehicle_sighting`/`person_sighting` (via
`db.get_vehicle_sighting_for_event`/`get_person_sighting_for_event`, one targeted indexed lookup
each, tried unconditionally rather than branching on `objects` since at most one ever matches) --
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
see above) matches a visit if **any** of its linked raw_events has a vehicle_sighting/
person_sighting whose AI analysis text matches -- same fields/ILIKE substring match `GET /events`'
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
fixed offset), so for some deployments better framing/timing outweighs the resolution/overlay cost.
`FRIGATE_SNAPSHOT_ENABLED` (default `false`, preserves all prior behavior) makes `crop.crop_event`
call the new `crop.fetch_frigate_snapshot_base64` instead of `crop_and_scale` -- no ffmpeg
involved at all for this path, just the raw JPEG bytes Frigate already rendered, base64-encoded
directly. `sub_label`/`score` still come from the same `fetch_frigate_event` call either way, since
those aren't image-related. `CROP_DISABLED`/`CROP_FRAME_OFFSET_PCT`/`CROP_PADDING_PCT` all stop
applying to events once this is on -- there's no frame-seeking or region-cropping happening on our
side anymore to tune.

**Events only, deliberately** -- a visit's own composite grid (`VISIT_THUMB_CROP_ENABLED`,
`crop.build_visit_preview`) is completely unaffected either way, kept on this project's own
proportional-sampling-across-the-clip approach. A single Frigate snapshot has no "4 frames of
change" equivalent to offer a visit's grid -- Frigate's snapshot is one frame, and the whole point
of the visit-level grid is showing motion/change across a span, which this endpoint can't provide
regardless of how good that one frame's framing is.

### Visit preview -- a composite grid + animated GIF sampled across the visit's own clip (fifth queue stage)

Frigate exposes a per-review "best frame" judgment as `data.thumb_time` on `/api/review` /
`frigate/reviews` (confirmed live both ways) -- distinct from `start_time` (a review's `thumb_path`
filename is just `{start_time}-{suffix}`, an identifier, not the frame timestamp), and clearly
content/score-dependent rather than a fixed offset: across 8 real reviews sampled live, `thumb_time
- start_time` ranged from ~0.24s to ~38.5s. Frigate's own `thumb_path` webp (e.g.
`/clips/review/thumb-{camera}-{start_time}-{suffix}.webp`, reachable directly off Frigate's
webserver, no auth) is unusably low-res for LPR/attribute work (318x180 in testing) but has no
bbox/label overlay -- reproducing its framing against the full-res record stream at the same
`thumb_time` was the original goal of this whole feature.

**This was tried and abandoned.** `crop_visit_thumbnail` (the original implementation) seeked to a
single offset computed from `thumb_time` within a freshly-downloaded visit-scoped continuous-
recording clip (`video.build_clip_url`, -5s/+5s padding around the visit's own span) -- but
confirmed live in production, Frigate's continuous-recording clip endpoint pads an *unpredictable*
amount of extra footage onto **either** edge of the requested window, inconsistently request to
request:
- One visit's clip came back *shorter* than requested (a 13s request returned only ~4.06s,
  confirmed by `ffprobe` -- a genuine motion-based recording gap, likely caused by that camera's
  `frigate.conf` having `record.continuous.days: 0` at the time). Fixed by probing actual duration
  and raising if the offset landed within a small safety margin of it.
- A *different* visit's clip came back *longer* than requested, with the extra ~6.1s **prepended
  before the start** rather than appended after -- confirmed by pulling Frigate's own review
  thumbnail (which did show the right moment) and sweeping frames across the actual downloaded
  clip: the real moment was ~11-13s in, not ~5s. Fixed by anchoring the seek offset from the clip's
  measured *end* instead of its assumed start.
- A **third** visit then broke the *end*-anchor fix the same way: its clip had ~4.1s of extra
  footage appended **after** the requested end instead of before it -- the end-anchored formula
  computed 10.96s into the clip, but the real moment (confirmed the same way: pulling Frigate's own
  thumb, sweeping frames) was at 5.26s, almost exactly what the original *start*-anchored formula
  would have given.

Conclusion: **neither a start- nor an end-anchor is reliable** -- Frigate pads whichever edge it
feels like, request to request, and no fixed manual correction (`VISIT_THUMB_CROP_OFFSET_ADJUST_
SECONDS`, since removed) can compensate, since the error's size *and direction* varies per visit.

**Current approach**: stop chasing one precise "best moment" against a moving target. `crop.
build_visit_preview` instead samples **`VISIT_PREVIEW_FRAME_PERCENTAGES`** (default `0,25,50,100`,
deployment-tunable -- e.g. `5,35,65,90` to stay a bit clear of both edges instead of landing
exactly on them; must be exactly 4 comma-separated values, since the grid assembly below is a
fixed 2x2 layout) proportionally across the clip's own **measured** duration
(`_probe_duration_seconds`) -- covering the visit's whole span regardless of where the real
footage boundaries land, which sidesteps the edge-padding problem entirely rather than needing yet
another anchor/correction scheme. The four sampled frames (each cropped to the representative
event's own `region`/box, or left uncropped under `CROP_DISABLED` -- same `_build_vf_filter`
helper `crop_and_scale` uses) are combined into:
- **`visits.crop_image_base64`** -- one composite 2x2 grid image (`ffmpeg` `hstack`/`vstack`), the
  artifact actually used for AI analysis, the thumbnail, Telegram, and the report. Deliberately a
  single flat image, not several separate images sent in one VLM prompt -- a chat-completion vision
  API decodes each `image_url` as an independent input, and whether a given self-hosted backend
  (this project's `llama_slot_proxy` + Qwen + mmproj included) actually reasons sensibly across
  multiple images in one prompt depends on that backend's specific chat-template/mmproj support,
  not just the API shape -- a single composite image sidesteps that uncertainty completely, since
  every VLM handles exactly one image per input by definition.
- **`visits.preview_gif_base64`** -- a separate animated GIF (`ffmpeg` image2 sequence input +
  palette generation) of the same four moments playing as a slideshow, for the web UI lightbox and
  Telegram only. Never sent to the VLM -- for the same reason multiple separate images weren't
  used for AI analysis, an animated GIF's `image_url` would just be decoded as its first frame by a
  standard vision pipeline, conveying no temporal information to a model at all. Served via
  `GET /visits/{id}/preview.gif`; the web UI's lightbox gets a third toggle button ("Preview",
  alongside Video/Image) shown whenever `has_preview_gif` is true.

  The GIF's own frames are scaled to the full `MAX_CROP_DIMENSION` (the same cap a normal
  single-event `crop_image_base64` gets), not the grid's half-size panels -- the two artifacts are
  built from the same 4 raw frames but sized independently: the grid halves each panel
  specifically so the assembled 2x2 combination lands near `MAX_CROP_DIMENSION` overall rather than
  4x it, but the GIF only ever shows one frame at a time, so that constraint doesn't apply to it at
  all. An earlier version additionally downscaled the GIF to a hardcoded 480px width on top of
  that (to keep file size down) -- dropped once it was clear that made the human-facing preview
  (Telegram, the web UI) noticeably blurrier than the actual crop quality for no real benefit.

`thumb_time` is still stored on the visit row (`record_visit`, informational -- Frigate's own
opinion of the best moment) but no longer read by `build_visit_preview` at all, and no longer gates
whether a preview will be attempted (`db.visit_thumb_crop_will_be_attempted` used to also require
`thumb_time is not None`; now it's purely `VISIT_THUMB_CROP_ENABLED`, since the new approach only
needs `start_ts`/`end_ts`/`cameras`). A schema-migration cleanup that used to force-skip pre-
existing rows with `thumb_time IS NULL` (correct under the old thumb_time-dependent approach) was
removed from `schema.sql` accordingly -- such a row can now succeed like any other.

`GET /visits/{id}/thumbnail` prefers the animated GIF over the composite grid -- unlike
`GET /events/{id}/thumbnail`, which always returns a still JPEG, a visit's grid card in the web
UI's Visits tab plays the sampled-frames sequence directly (the GIF is already built at a modest
size, served as-is, no further scaling). Falls back to a scaled-down JPEG of the composite grid,
then the representative event's own crop (available almost immediately, long before the preview
can be), then a frame pulled from the visit's own stored video -- same belt-and-suspenders chain as
the existing per-event thumbnail endpoint. `GET /visits/{id}/image` (the lightbox's dedicated
"Image" mode) stays grid-only, deliberately -- the lightbox already has a separate "Preview" mode
for the GIF, so `/image` isn't the place to also prefer it. `GET /visits`' `has_image` reflects
either source being available (`has_thumb_crop OR` the representative event's own image);
`has_thumb_crop`/`has_preview_gif`/`thumb_crop_status` are additive fields for observability. The
web UI's Visits tab uses these visit-scoped endpoints instead of the representative event's
directly, so the grid/lightbox picks up the better artifact automatically once ready, with no
client-side fallback logic of its own.

The lightbox's toggle order is Preview/Video/Image (`static/index.html`), and `openLightbox`
defaults to `'preview'` when `has_preview_gif` is true (falling back to `'video'`, then `'image'`)
-- the GIF is richer than a still frame and already framed to the sampled moments of interest, so
it's the best default when available, ahead of even the full video.

#### Bug: no defense against Frigate's clip endpoint returning a not-yet-finalized/placeholder clip

The pivot to proportional sampling (above) sidestepped the *edge-padding* problem, but confirmed
live in production it introduced a new, more basic gap: `build_visit_preview` has no check at all
that the clip it got back is actually complete. A real visit's nominal window (`end_ts+5 -
(start_ts-5)`) was ~44.6s, but the clip Frigate returned was only ~3.9s -- Frigate's continuous-
recording endpoint hadn't finished writing that segment yet, the same "not ready" condition
`video.download_clip`'s `VIDEO_MIN_VALID_BYTES` check exists to catch on the byte-size axis.
Sampling `VISIT_PREVIEW_FRAME_PERCENTAGES` of that *tiny* duration instead of the visit's real span
produced 4 frames all crammed within the same ~3.9s window (confirmed by pulling the actual stored
grid: all 4 panels' burned-in OSD timestamps landed within a 3-second span, nowhere near the
visit's real ~34.6s of activity) -- silently wrong, `thumb_crop_status` still went `done`, nothing
signaled it. Diagnosed by comparing the visit's DB-recorded duration (event_count > 1, i.e. a
multi-event visit expected to span tens of seconds) against the grid's actual sampled timestamps.

Fixed by comparing the probed duration against the nominal requested window before sampling
anything: if `duration < requested_span * _MIN_DURATION_RATIO` (a fixed `0.5`, not deployment-
tunable, same treatment as `_DURATION_SAFETY_MARGIN_SECONDS` used to be), raise instead of silently
proceeding -- routes into the existing retry-then-fallback path
(`visit_thumb_worker.mark_visit_thumb_crop_retry_or_failed`) so a later retry (once Frigate has
actually finished writing the segment) can succeed instead. `0.5` is deliberately generous -- this
guards against an ~11x shortfall like the one found, not the few-seconds segment-boundary jitter
proportional sampling is already designed to tolerate.

#### Reusing an already-downloaded visit video instead of re-racing Frigate's clip endpoint

The `_MIN_DURATION_RATIO` guard above was framed as catching Frigate "not having finished writing
the segment yet" -- confirmed live that this is often not a timing race that a later retry
outlasts, but a race against Frigate's own cleanup of continuous-recording segments (both cameras
run `record.continuous.days: 0` in `frigate.conf`, so continuous footage only survives in a very
short-lived rolling buffer before Frigate purges it). Confirmed directly: the identical clip URL,
requested only 5 seconds apart by two different queue stages, returned a full-length clip the
first time and a near-empty one the second -- and re-requesting the same URL minutes later
continued to return the same near-empty result, ruling out "just needs more time." Worse,
`STORE_VIDEO_ALERTS`'s `alert_video_worker` requests this exact same URL (via
`video.build_clip_url`) independently, and empirically tends to win that race more often (it only
needs one successful attempt, shortly after the review closes) -- meaning a full-length clip was
frequently already sitting on disk (`visits.video_path`) at the exact moments `build_visit_preview`
was re-losing the same race against Frigate.

Fixed by having `build_visit_preview` prefer that already-downloaded file
(`visit.get("video_path")`, when the file exists on disk) over re-fetching from Frigate at all --
confirmed against the two real visits that prompted this fix that in both cases, by the time of the
grid-building attempt that would have used it, the video worker had already stored a full-length
clip. This is a pure opportunistic win, not a hard dependency between the two independent
`STORE_VIDEO_ALERTS`/`VISIT_THUMB_CROP_ENABLED` switches -- each queue stage still re-fetches its
own fresh copy of the visit row on every attempt (a separate `claim_visit_thumb_crop_batch` call
per retry pass), so a video that finishes downloading in between two thumb-crop attempts is picked
up automatically on the next one; `video_path` unset (video storage off, or not yet done) still
falls back to requesting Frigate directly, exactly as before.

#### When there's no downloaded video: sampling each moment independently instead of one whole-span request

The "requesting Frigate directly" fallback above used to mean one request for the visit's whole
`start_ts`->`end_ts` span (the same shape `alert_video_worker` uses), then sampling 4 frames out of
whatever came back -- which meant the `_MIN_DURATION_RATIO` guard's fate was all-or-nothing: if
Frigate hadn't retained the *entire* window, the whole grid attempt failed, even if some individual
moments within it were perfectly retrievable. That single-request design was also never guaranteed
to succeed just because `alert_video_worker` happened to; both independently ask for the same
range, and per Frigate's own recording model, retention is decided **per recording segment** (each
~10s), not per review/visit as a whole -- a segment only gets long-lived retention
(`alerts`/`detections`/`motion`, 5/10/30 days respectively) if something actually triggered one of
those categories *within that specific segment*; a segment with no fresh trigger only qualifies as
`continuous`, which is `days: 0` on both cameras here and gets purged almost immediately by
Frigate's own cleanup. A visit with a mostly-stationary object (little re-triggering) can easily
have long stretches that were never anything but `continuous`-tagged, gone within seconds, even
though the review/visit itself is retained as metadata indefinitely.

Fixed by replacing the single whole-span request with `_panels_from_independent_timestamps`: each
of the 4 `VISIT_PREVIEW_FRAME_PERCENTAGES` points is converted to its own absolute epoch timestamp
(`visit.start_ts + pct/100 * (end_ts - start_ts)`) and requested as its own tiny window via
`_grab_frame_near_timestamp` (reusing `build_clip_url`'s own -5s/+5s padding by passing the same
instant as both `start_ts` and `end_ts`, so the target moment lands ~5s into its own small clip).
A gap at any one moment (nothing retained right there) no longer takes the other three down with
it -- it reuses the nearest earlier successful frame instead (a leading gap borrows the first
success found); only raises if *none* of the 4 moments produced anything at all. This also drops
the need for `_MIN_DURATION_RATIO`/`_VISIT_PREVIEW_EDGE_MARGIN_SECONDS` entirely on this path --
there's no single probed duration to sanity-check a nominal window against anymore, since every
request is already scoped to exactly the moment it wants.

`build_visit_preview`'s already-downloaded-video path (`_panels_from_clip`) can itself now fall
through to this same independent-timestamp method rather than being a second all-or-nothing dead
end: `alert_video_worker` only validates a byte-size floor (`VIDEO_MIN_VALID_BYTES`, `1000` bytes
in this deployment -- confirmed live to be far below what even a genuinely short few-second clip
weighs), so a bad, too-short download can still pass that check and get stored as `video_path`, at
which point it never changes again. Without a fallback, `_panels_from_clip`'s own duration-ratio
check would then raise on *every* retry, forever, even though the more robust independent-timestamp
method sitting right next to it could very likely still succeed. `build_visit_preview` now catches
that specific `ValueError` and falls through instead of propagating it. Net effect: the
already-downloaded video is purely a cheap optimization (one file read instead of 4 HTTP round
trips) layered on top of the independent-timestamp method, which is the actual source of
correctness -- it can only make a visit's preview faster, never worse, and `VISIT_THUMB_CROP_ENABLED`
needs no dependency on `STORE_VIDEO_ALERTS` at all (a visit with no stored video, or none ever
attempted, still builds a preview entirely from direct-to-Frigate per-moment requests). Verified
against real production data via `docker exec` (not just mocked unit tests): both the
already-downloaded-video path and the independent-timestamp path produce correct, real grids from
the same live visit -- the only difference being the independent path's timestamp spread across
panels is naturally tighter for a very short visit (it samples proportionally across the visit's
own real span, not the wider padded window a downloaded clip provides for free).

#### Wired into the AI queue, the alerts report, and Telegram

All three remaining consumers use `crop_image_base64` (the composite grid, once built) exactly as
they did the old single-frame thumb-crop -- none of them needed to change for the pivot, since
they only ever cared about "is there a better image than the representative event's own crop
available yet," not what that image's internal structure is:

- **`POST /ai-queue/claim`** (`db.claim_ai_batch`): whenever a `source=visits` claim's row is a
  visit's representative event, the response's `crop_image_base64` opportunistically prefers the
  visit's own composite grid over the representative event's own crop *whenever it's already done
  by claim time* -- zero latency cost, since this never changes which rows are eligible or when,
  only which image comes back. The optional `require_thumb_crop` param goes further: it makes the
  claim itself wait (`AND visit_id IS NOT NULL AND EXISTS (... v.thumb_crop_status = 'done')`) so
  the grid is *guaranteed* to be the one analyzed, never the representative's own single crop -- a
  real trade-off (alerts-flow analysis is delayed until the review closes and the preview build
  finishes) that's opt-in, not the default, since the right answer depends on your traffic.
  Deliberately scoped to `source=visits` only -- under plain `source=events` (no dedup), several
  distinct raw_events can share one visit, and overriding all of them with the identical grid image
  would mean redundant VLM calls analyzing the same picture, not an improvement.
- **`/reports/generate?source=visits`** (`db.get_report_data`): always prefers the visit's own
  composite grid via `COALESCE(v.crop_image_base64, re.crop_image_base64)` (`LEFT JOIN
  yard_stats.visits v ON v.id = re.visit_id AND v.thumb_crop_status = 'done'`) -- unconditional,
  not opt-in, since a report runs well after the fact on a schedule, so unlike the AI queue there's
  no real latency cost to just always taking the better image when it exists. `source=events`
  reports never apply this (matches the AI queue's own scoping decision). The HTML report itself
  (`report.py`'s `_img_cell`) goes one step further for the *display* choice: it prefers the
  visit's own `preview_gif_base64` (also selected in `get_report_data`'s `gif_image_expr`, same
  `source=visits`-only scoping) over that static grid whenever it's ready, same "richer artifact
  when available" preference Telegram's visit summary already applies -- the static grid alone
  used to read as a flat "puzzled" 2x2 image in the report even once the nicer animated preview
  existed. Embedded once, directly, with no separate click-to-enlarge lightbox the way the JPEG
  grid gets -- there's no cheap way to re-encode a second, smaller GIF the way `crop.
  scale_image_base64` does for a JPEG, and duplicating the same GIF bytes in a lightbox `<img>`
  would reintroduce the exact double-embed bloat this report already fixed once (the old n8n
  version's 42MB report bug, see below). The grid is still what the AI queue actually analyzes
  either way (see above) -- only this HTML report's own inline preview changed.

  `/reports/generate`'s optional `include_preview` param is a mode, not a bool (same shape as
  `TELEGRAM_EVENTS_MODE`): `"gif"` (the default) is today's original behavior described above;
  `"image"` drops only the GIF -- `get_report_data` forces `gif_image_expr` to `NULL` regardless
  of `thumb_crop_status`, at the SQL level, not just hidden by `report.py`'s rendering, since it's
  typically the single largest field in this query (a multi-frame animated GIF vs. one flat JPEG
  grid) -- falling back to the visit's own static composite grid exactly as `_img_cell` already
  does for a visit whose preview genuinely isn't ready yet; `"none"` goes further and forces
  `crop_image_expr` to `NULL` too, dropping the row image entirely (for either `source=events` or
  `source=visits`) -- `_img_cell` already renders `"(no image)"` whenever `crop_image_base64`
  comes back NULL, so neither narrower mode needs a separate rendering path, only the SQL-level
  field selection changes. Each mode past the default is a real payload-size reduction, not a
  cosmetic one -- the dropped field(s) are never fetched from Postgres at all. Exists for a
  caller like an n8n workflow that emails/messages the report and wants a smaller result --
  `n8n/alerts-report.json` itself is unchanged (still omits the param, i.e. `"gif"`), since
  that's just one example caller, not the only one.
- **`TELEGRAM_ALERTS_MODE`'s visit-summary message** (the `image`/`all` half): deferred, not edited after the fact.
  `mqtt_ingest._handle_review_message` only sends the summary immediately when
  `db.visit_thumb_crop_will_be_attempted(review)` is false (i.e. `VISIT_THUMB_CROP_ENABLED` is off)
  -- otherwise it skips the immediate send entirely, and `visit_thumb_worker.process_claimed_visit`
  sends it once `thumb_crop_status` reaches a terminal state: `done` -> the animated preview GIF
  (`telegram.send_visit_summary`'s `gif_base64` param, sent via Telegram's `sendAnimation` so it
  actually plays -- `sendPhoto`/`sendDocument` would show it as a static first frame or a bare file
  attachment instead); `failed` (attempts exhausted) -> falls back to the representative event's
  own crop as a plain photo (`image_base64` param, `sendPhoto`) or text-only, so a visit is never
  left without its notification just because the preview build never panned out. Deliberately the
  GIF, not the composite grid, for this one consumer -- Telegram is the human-facing notification,
  where the animation is more informative than a single still; the AI queue always analyzes the
  grid (see above), since that's what's actually sent for analysis -- the HTML report's own inline
  preview also now prefers the GIF the same way Telegram does (see above), independent of this. The
  immediate-send path
  in `mqtt_ingest.py` (used when a deferred send won't happen at all) always passes `image_base64`
  only -- no GIF ever exists yet at that point, since `build_visit_preview` hasn't run.
  `mark_visit_thumb_crop_retry_or_failed` returns the resulting status specifically so the worker
  can tell "still retrying, don't send yet" apart from "just went terminal, send now" without
  re-deriving that from the attempt-count arithmetic itself. This is a genuine behavior change from
  the original per-event notifications -- the notification now arrives however long the review
  takes to close plus however long the preview build takes, not near-instantly -- a deliberate
  trade (quality over speed) rather than the alternative of editing an already-sent photo in place
  (`editMessageMedia`), which was considered and rejected as more moving parts for the same result.

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
directly on `vehicle_sightings`/`person_sightings` via **pgvector**, not a separate vector DB. This
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
on every startup like everything else in that file) plus a nullable `embedding vector(1024)` column
on both sighting tables (1024 = `Qwen3-Embedding-0.6B-GGUF`'s output size, the embedding model
chosen for this -- one more slot in the user's existing `llama_slot_proxy` multi-model setup, no
`mmproj` needed since it's text-only) with an HNSW cosine-distance index on each (`vector_cosine_ops` --
HNSW rather than ivfflat since it needs no existing rows to "train" on, safe to create immediately
against a column that starts empty).

`db.py` formats a Python list as a pgvector input literal (`"[0.1,0.2,...]"`) passed through
psycopg2 as a plain string param and cast with `::vector` in SQL (`_vector_literal`), rather than
depending on the separate `pgvector` package's connection-level type adapter -- avoids that
package's own registration-ordering hazard (it needs the extension already created in the database
before it can register) for a column this code only ever writes or ranks by distance, never reads
back as a Python list. `complete_vehicle_sighting`/`complete_person_sighting` both gained an
optional `embedding` parameter, stored in the same existing transaction -- no new queue stage, since
n8n computes the vector *before* calling `POST /sightings/vehicles|persons`, the same request/
response shape as today plus one more optional field. Omitted or null just means that sighting
isn't semantically searchable, not an error -- this is how every pre-existing sighting row (from
before this feature existed) behaves until/unless backfilled.

**`POST /search/semantic`** (`X-API-Key` protected, `db.semantic_search_sightings`): cosine-distance
(`<=>`) ordered search across whichever of `vehicle_sightings`/`person_sightings` `object_types`
selects (default both), filtered by the caller-resolved `start`/`end` window -- a POST, not GET,
since a 1024-float array doesn't belong in a query string. `embedding IS NOT NULL` naturally excludes
sightings that predate this feature or came from an n8n run that didn't attach one; that's a
narrower result set, not an error. Rows without their own embedding just aren't candidates, same as
`GET /events`' `q` only ever matching rows that already have a sighting.

**`POST /embeddings/backfill`** (`X-API-Key` protected, `ai_worker.run_embedding_backfill`) fills
in `embedding` for sightings that existed before this feature did, or came from any run that didn't
attach one -- same dry-run-by-default shape `/retention/purge` already uses (`confirm` defaults to
`false`, previews `db.count_sightings_missing_embedding()`'s counts with no embedding calls made;
`confirm=true` actually processes up to `limit` rows per sighting type, call it repeatedly until
both counts reach zero). Deliberately independent of `AI_EVENTS_STAGE_ENABLED`/`process_claimed_event` --
it only ever re-embeds a sighting's own already-stored fields (`db.get_vehicle_sightings_missing_
embedding`/`get_person_sightings_missing_embedding`), never re-runs the VLM, so it works whether
`metadata-processor.json` or the internal AI stage is your primary AI flow, or neither is currently
running. Reuses the exact same `report._vehicle_summary`/`_person_summary` combination logic and
`ai_worker._embed_text` helper `process_claimed_event`'s own embed step already uses, so a
backfilled row's embedding means the same thing as a freshly-computed one. Requires
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

`n8n/metadata-processor.json` gained one step per branch, right before each existing final POST:
**Build Embedding Text (Person/Vehicle)** (code node) combines the sighting's fields into one line
-- the vehicle side reuses the exact same color/body/make/model + notable_features + plate
combination `report.py`'s `_vehicle_summary` already uses for the alerts report's one-line
description, so the text that gets embedded matches what a human would read about the same sighting,
not a separately-invented format -- then **Call Embedding Model (Person/Vehicle)** (HTTP node) POSTs
it to the new embedding slot, following the existing VLM-call nodes' convention (hardcoded
`REPLACE_WITH_...` placeholders, no credentials block, since it's unauthenticated on the LAN like
every other `llama_slot_proxy` call in this project). Both nodes use `onError: continueErrorOutput`
wired straight into the existing `Insert Person/Vehicle Sighting (API)` node either way (success or
error output) -- an embedding failure shouldn't lose an already-computed sighting, so the Insert
node's `embedding` field falls back to `null` via optional chaining
(`$('Call Embedding Model...').item.json.data?.[0]?.embedding ?? null`) rather than routing through
`Handle Failure (API)` the way a real VLM-call failure does.

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
`visits`/`vehicle_sightings`/`person_sightings`, plus `visit_vehicle_sightings`/
`visit_person_sightings` once the alert AI stage existed), per-stage queue status breakdown (`db.
get_stage_counts()`: crop/video/ai on `raw_events`, video/thumb_crop/alert_ai on `visits`),
embedding coverage (reuses `count_sightings_missing_embedding`), DB size (`db.get_db_size_info()` --
`pg_database_size` total plus `pg_total_relation_size` per `yard_stats` table, so it matches what
actually shows up on the Postgres data volume, not just row bytes), vector index health (`db.
get_vector_index_status()` -- pgvector extension version, `EMBEDDING_DIMENSIONS`, and each HNSW
index's `indisvalid`/`indisready`), `get_retention_info()` (already existed, reused as-is), and a
feature-flags summary (`AI_EVENTS_STAGE_ENABLED`/`AI_ALERTS_ENABLED`/`STORE_VIDEO`/
`STORE_VIDEO_ALERTS`/`VISIT_THUMB_CROP_ENABLED`/`CROP_DISABLED`/`TELEGRAM_EVENTS_MODE`/
`TELEGRAM_ALERTS_MODE`) so "what's currently turned on" is visible at a glance instead of having to
check `.env` by hand. Everything in this call is cheap SQL -- deliberately excludes anything
that's a real filesystem walk or network call, so the dashboard's main section always loads fast
regardless of video backlog size or whether the VLM host is reachable.

**`GET /admin/disk-usage`** is split out specifically because it *is* a real filesystem walk
(`admin.dir_size_bytes`, `os.walk` summing real file sizes under `VIDEO_STORAGE_PATH`/
`VIDEO_STORAGE_PATH_ALERTS`) -- kept separate so a large video backlog's scan time never blocks the
rest of the dashboard from rendering. A path that doesn't exist (e.g. `VIDEO_STORAGE_PATH_ALERTS`
when `STORE_VIDEO_ALERTS` has never been turned on) reports as zero bytes rather than an error --
an unused optional storage location isn't a fault.

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
`video`/`thumb_crop`/`alert_ai`; an unknown combination is a 400, not a SQL injection surface. The dashboard
shows a "Requeue N failed" button next to any stage currently at 1+ failed, matching how this
session's production incident (34 events failed on an embedding dimension mismatch) was actually
resolved by hand over SSH before this button existed.

**Embeddings backfill and retention purge are exposed as buttons too, reusing the existing
endpoints** (`POST /embeddings/backfill?confirm=true&limit=200`, `POST /retention/purge`) -- both
already had the right dry-run-by-default shape. The retention purge control adds a "Media only"
checkbox (checked by default) mapping directly to the API's `only_media` param -- checked shows a
media-focused preview (video/image/GIF counts) and its own lighter confirmation text ("rows and
all AI analysis text are kept"); unchecked shows the full row-count preview and a starker
PERMANENTLY-delete confirmation that also mentions the vector-index rebuild that follows. Either
way, a native JS `confirm()` dialog spells out exact counts (from a mandatory preview call first)
before the real `confirm=true` call fires -- the same two-step preview-then-confirm flow the API
itself already enforces, just made impossible to skip from the UI as well, since both modes are
irreversible once confirmed.

### Schema (`yard_stats`)

- `raw_events` — one row per Frigate `end` event, any label. Carries all three queue state machines
  plus `crop_image_base64`, `sub_label` (Frigate's own LPR read), `score` — all captured by
  `ingest-worker` from one Frigate API fetch, so n8n never needs to call Frigate itself — and,
  when video storage is on, `video_path` (filesystem path only, never the file itself) and
  `telegram_photo_message_id` (for threading the later video reply). `visit_id`/`reconciled` link
  a row to the `visits` segment Frigate's own review/alert stream grouped it into (see above).
- `visits` — one row per Frigate review/alert segment (`frigate/reviews`), grouping the
  `raw_events` det_ids Frigate's own tracker considers the same real-world activity. Populated by
  `db.record_visit`; cross-camera merging is not yet implemented (see above). Also carries
  `thumb_time` (Frigate's own review "best frame" timestamp, stored but no longer used for
  cropping -- see below) and, when `VISIT_THUMB_CROP_ENABLED`, its own `crop_image_base64`
  (composite grid image) plus `preview_gif_base64` (animated GIF, human preview only) and
  `thumb_crop_status` state machine -- separate artifacts from any linked raw_event's own crop
  (see "Visit preview" above). `alert_ai_status`/`alert_ai_status_changed_at`/
  `alert_ai_attempt_count` (see "Alert AI stage" above) are this visit's own sixth queue stage,
  entirely independent of any linked raw_event's `ai_status`.
- `vehicle_sightings` / `person_sightings` — one row per AI-analyzed event. `vehicle_sightings`
  keeps `plate_text_frigate` (from `raw_events.sub_label`) next to `plate_text_llm` (the OCR
  model's read) as a cross-check. Both also carry a nullable `embedding vector(1024)` (pgvector,
  `Qwen3-Embedding-0.6B-GGUF`) for `POST /search/semantic` -- see "Semantic search and the Q&A
  agent" above.
- `visit_vehicle_sightings` / `visit_person_sightings` — one row per alert-AI-analyzed visit (see
  "Alert AI stage" above), keyed by `visit_id` instead of `raw_event_id` -- same shape as
  `vehicle_sightings`/`person_sightings` (including their own nullable `embedding vector(1024)` +
  HNSW index) minus `plate_text_frigate` (no per-visit LPR read exists), plus a `notes` field
  `alert_prompt` actually populates with a description of what changed across the visit's 4
  sampled frames.

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
