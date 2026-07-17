# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

"Yard Stats + Vehicle Metadata" extends an existing Frigate NVR setup (Coral TPU detection + LPR)
with a pipeline that logs yard activity and extracts vehicle/person metadata (color, body type,
plate text, clothing description) from Frigate events using local VLMs. It is one project among
several in the user's homelab (alongside n8n, Flowise, WAHA, mcp-proxy, and a `llama_slot_proxy`
multi-model llama.cpp setup), and is deliberately kept decoupled from those via its own Postgres
instance/schema and its own containers.

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
     fire-and-forget Telegram visit summary if TELEGRAM_ALERTS_ENABLED
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
  API if `TELEGRAM_EVENTS_ENABLED=true`).
- **n8n** owns everything AI-shaped: deciding when to claim work and calling the VLM(s), the daily
  report, and the Q&A workflow. Its processors never touch Frigate's API, crop or video anything
  themselves, and never call Telegram — they only ever read `crop_image_base64` that's already
  sitting on the claimed row, and no longer run raw SQL at all — claim/complete/fail all go through
  `ingest-worker`'s `/ai-queue/*` and `/sightings/*` endpoints. `ingest-worker` never calls an LLM,
  by design.
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

`POST /retention/purge` is an ad-hoc counterpart to the scheduled `RETENTION_MONTHS` sweep
(`db.purge_older_than`, same FK-safe child-before-parent delete order as
`db.run_retention_cleanup`) for when you want to purge on a caller-chosen cutoff rather than
waiting on or reconfiguring the scheduled one -- e.g. clearing out old test data. Defaults to a
dry run (`confirm` query param defaults to `false`): it always returns counts of matching rows
per table, and only actually deletes when `confirm=true` is passed explicitly.

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

`TELEGRAM_EVENTS_ENABLED=true` turns on fire-and-forget notifications (`telegram.py`): a photo right after
crop (regardless of `STORE_VIDEO` -- photo-only is a valid steady state), and, once a clip is
stored, a video sent as a reply to that photo (`telegram_photo_message_id`, persisted on the row --
a durable version of the `FrigateRetry.json` workflow's in-memory `pendingReplies` map, so the
reply-threading survives a service restart). Both directions are wrapped so a Telegram failure
(bad token, rate limit, network blip) can never take down the crop or video poll loop.

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

`TELEGRAM_ALERTS_ENABLED=true` turns on a separate notification path for the alerts/visits flow --
one summary message per visit (`telegram.send_visit_summary`), fired once from
`mqtt_ingest._handle_review_message` right after `db.record_visit` succeeds (not from a poll loop).
Uses the visit's representative event's `crop_image_base64` as a photo if the crop stage has
already finished it by the time the review closes; falls back to a text-only `sendMessage`
otherwise, since crop timing isn't guaranteed to have caught up yet. Independent of
`TELEGRAM_EVENTS_ENABLED` above (the existing per-raw_event photo/video messages) -- both, either, or
neither can be on at once, specifically so you can compare which notification granularity is more
useful for your traffic rather than committing to one upfront.

If `STORE_VIDEO_ALERTS` is also on, the visit's video is sent as a reply to that same summary
message once `alert_video_worker` finishes downloading it (`telegram.send_visit_video`, reply-
threaded via `visits.telegram_photo_message_id` -- durable across a restart, same idea as
`raw_events.telegram_photo_message_id`) -- mirroring how the events flow's video reply threads
onto its earlier photo. The two alerts-flow switches are otherwise fully independent (one can be on
without the other; a visit clip download failure/retry never blocks or delays the summary
message, and vice versa) -- this reply-threading is the one place they connect.

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
`has_media`'s default. Like `event_id`, `q` bypasses the time window entirely -- searching your
whole sightings history, not the visible date range.

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
by `object_type`/`camera`/`start`/`end`/`hours` only -- `event_id`/`q`/`ai_status`/`has_media` are
per-raw_event concepts that don't compose cleanly with a grouped view, so this endpoint doesn't
accept them at all rather than half-supporting them. Purely additive and read-only -- doesn't
affect `GET /events`, the AI queue, or Telegram notifications; exists so `visits` data can be
judged visually against real traffic before deciding whether to build the actual dedup behavior
described above.

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
`openLightbox` the Events view uses, rather than a separate lightbox implementation. Filters that
don't apply to the Visits view (Event ID, AI status, Only with media, Search AI analysis) are never
disabled -- an earlier version disabled them via `viewMode === 'visits'`-driven `:disabled`
bindings, but that turned out more confusing than helpful (half the filter bar visually greyed out
with no obvious reason why); `fetchVisits` already silently ignores those fields itself (see its
own comment), so leaving every control always interactive is simpler and no less correct. A
`:title` tooltip on the ones that don't apply notes as much ("Not used in Visits view") without
blocking interaction. The filter bar itself defaults to a simplified view -- Search AI analysis
plus a "Time range" preset dropdown (`filters.hours`, options `[1, 3, 6, 12, 24]` hours, sent as
`GET /events`'/`GET /visits`'s own `hours` param) -- with an "Advanced filters" toggle
(`advancedSearch`) that reveals Event ID/From/To/Type/AI status/Only-with-media on demand; those
fields' wrapping `<div class="advanced-filters">` is `display: contents` in CSS so they flow as
direct flex items of `.filters` when shown, rather than nesting a visible sub-box. The Time range
preset itself is hidden while the advanced panel is open (`x-show="!advancedSearch"`) rather than
shown redundantly alongside From/To -- the advanced panel's own date pickers cover the same need.
Those From/To pickers override the Time range preset when either is set (`fetchEvents`/
`fetchVisits` check `filters.start || filters.end` first, falling back to `hours` only when both
are empty) -- same precedence `q`/`event_id` already had over the time window, just extended to
cover the preset too. `toggleAdvancedSearch` (the button's click handler, not a plain
`advancedSearch = !advancedSearch` toggle) resets every filter back to its default on every
switch, either direction -- values set in one mode otherwise kept applying invisibly once their
field hid again after switching (e.g. a From/To range set in advanced mode silently overriding the
reappeared Time range preset in simple mode, with nothing on screen explaining why), so resetting
on toggle avoids that whole class of confusion rather than patching each case individually.
`GET /events` itself is filterable by
`object_type`/`crop_status`/`ai_status`/`video_status`/`has_media`/`event_id`/`q`, defaults to the
last 1 hour, media-only. `GET /events/{id}/thumbnail` (a small on-the-fly JPEG, same
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
choice programmatically. Switching to fetching Frigate's own snapshot directly (instead of
seeking our own frame from the record-stream clip) was considered and rejected: it's from the
lower-res detect stream (800x448 in testing, vs. this setup's 3840x2160 record stream) with a
bounding-box/label/timestamp overlay baked in that this Frigate version's REST API doesn't expose
a way to suppress -- a real regression for plate/attribute-reading quality, not a fix. `0.5`
stays the default until real usage across your own cameras suggests a specific different value is
consistently better -- there's no universally "more correct" number to guess at upfront.

`CROP_INITIAL_WAIT_SECONDS` (default 5s, same idea as `VIDEO_INITIAL_WAIT_SECONDS`) gives Frigate
a head start to finalize the event/clip before the *first* crop attempt on a freshly claimed row
-- confirmed in production that even an ordinary short event's crop can fail this way if attempted
immediately after the "end" MQTT message, not just long events tripping the clip-duration fallback
above. Only applies once per row (`crop_attempt_count == 0`), not on every retry pass.

### Visit thumbnail re-crop using Frigate's own `thumb_time` (fifth queue stage)

The "Frigate doesn't expose its best-frame timestamp anywhere" finding above turned out to be
*mostly* true, not entirely: it's absent from `/api/events`, but present on **`/api/review`** /
the `frigate/reviews` MQTT payload, as `data.thumb_time` -- confirmed live both ways (REST
`/api/review?limit=N` and by subscribing directly to `frigate/reviews` with `mosquitto`-equivalent
Python MQTT client). It's distinct from `start_time` (a review's `id`/`thumb_path` filename is
just `{start_time}-{suffix}` -- an identifier, not the frame timestamp) and, like the per-event
snapshot timing finding above, clearly content/score-dependent, not a fixed offset -- across 8
real reviews sampled live, `thumb_time - start_time` ranged from ~0.24s to ~38.5s. Frigate's own
`thumb_path` webp (e.g. `/clips/review/thumb-{camera}-{start_time}-{suffix}.webp`, reachable
directly off Frigate's webserver on port 5000, no auth) is unusably low-res for LPR/attribute work
(318x180 in testing, well below even the detect-stream snapshot rejected earlier) -- but it *has*
no bbox/label overlay, and confirming its framing against the full-res record stream at the same
`thumb_time` is what makes this worth using: it's Frigate's own per-review "best frame" judgment,
the same class of decision `CROP_FRAME_OFFSET_PCT` can only approximate with a fixed percentage.

The catch: `thumb_time` is only known once the review closes (`type="end"`), by which point the
representative event's own crop (`crop_status`, off `CROP_FRAME_OFFSET_PCT`) has typically already
run -- the crop stage claims within seconds of the *event's* own "end", while a review can stay
open for tens of seconds to minutes after its first detection. So this can't feed the existing
crop pipeline's first attempt; it's a **separate, opt-in fifth queue stage**
(`VISIT_THUMB_CROP_ENABLED`, `visit_thumb_worker.py`/`crop.crop_visit_thumbnail`), producing a
**separate artifact** (`visits.crop_image_base64`, its own `thumb_crop_status` state machine --
`new`/`processing`/`retry`/`failed`/`done`/`skipped`, same shape as every other queue in this
project) -- not a replacement for the events-flow crop, mirroring how `STORE_VIDEO_ALERTS` sits
alongside `STORE_VIDEO` rather than replacing it. `record_visit` stores `thumb_time` off the
review payload and sets `thumb_crop_status='skipped'` immediately if the flag is off or Frigate
ever omits `thumb_time` (confirmed always present in testing, but the re-crop can never succeed
without it regardless of attempts).

Because `thumb_time` is chosen over the *review's* whole span, it can legitimately fall outside
the representative event's own narrow start/end window (e.g. a review grouping several det_ids
across tens of seconds) -- so `crop_visit_thumbnail` fetches the same visit-scoped
continuous-recording clip `alert_video_worker.py` already downloads (`video.build_clip_url`,
camera + start/end with the same -5s/+5s padding), not the representative event's own
`/api/events/{det_id}/clip.mp4` endpoint `crop_event` uses -- while still using that event's own
`region`/`box` for spatial framing, since Frigate's review payload has no box/region of its own,
only individual tracked-object events do. The seek offset is `thumb_time - (visit.start_ts - 5s)`,
landing on the exact instant `thumb_time` refers to within that fetched clip.

`GET /visits/{id}/thumbnail` and `GET /visits/{id}/image` prefer this new crop, falling back to
the representative event's own crop (available almost immediately, long before the re-crop can
be), then a frame pulled from the visit's own stored video -- same belt-and-suspenders chain as
the existing per-event thumbnail/image endpoints. `GET /visits`' `has_image` reflects either
source being available (`has_thumb_crop OR` the representative event's own image), so existing
consumers of that field don't need to change; `has_thumb_crop`/`thumb_crop_status` are additive
fields for observability. The web UI's Visits tab (`visitThumbnailUrl`/`lightboxImageUrl` in
`app.js`) uses these visit-scoped endpoints instead of the representative event's directly, so the
grid/lightbox picks up the better-timed image automatically once it's ready, with no client-side
fallback logic of its own.

(Bug fixed in production right after first deploying this: `ALTER TABLE ... ADD COLUMN
thumb_crop_status ... DEFAULT 'new'` backfills that default onto every *pre-existing* visit row,
but those rows' `thumb_time` is also `NULL` -- `record_visit` is the only thing that ever sets it,
and only at INSERT time, never retroactively. `visit_thumb_worker` claimed one such row and
crashed on `thumb_time - clip_start_epoch` against `None`, repeating on every retry until it hit
`VISIT_THUMB_CROP_MAX_ATTEMPTS` and went `'failed'` -- confirmed live from production logs (a
pre-existing visit crashing this exact way three times in a row right after the upgrade).
`schema.sql` now includes an idempotent `UPDATE ... SET thumb_crop_status = 'skipped' WHERE
thumb_time IS NULL AND thumb_crop_status IN ('new', 'retry', 'failed')`, re-run on every startup --
correct to run unconditionally, since a row with no `thumb_time` can never succeed regardless of
when it was created, not just as a one-time migration repair.)

#### Wired into the AI queue, the alerts report, and Telegram

All three remaining consumers now use the thumb-crop, each with a different cost/latency
trade-off appropriate to how that consumer works:

- **`POST /ai-queue/claim`** (`db.claim_ai_batch`): whenever a `source=visits` claim's row is a
  visit's representative event, the response's `crop_image_base64` opportunistically prefers the
  visit's own thumb-crop over the representative event's own crop *whenever it's already done by
  claim time* -- zero latency cost, since this never changes which rows are eligible or when, only
  which image comes back. The optional `require_thumb_crop` param goes further: it makes the claim
  itself wait (`AND visit_id IS NOT NULL AND EXISTS (... v.thumb_crop_status = 'done')`) so the
  crop is *guaranteed* to be the well-timed one, never the representative's -- a real trade-off
  (alerts-flow analysis is delayed until the review closes and the re-crop finishes) that's opt-in,
  not the default, since the right answer depends on your traffic. Deliberately scoped to
  `source=visits` only -- under plain `source=events` (no dedup), several distinct raw_events can
  share one visit, and overriding all of them with the identical thumb-crop image would mean
  redundant VLM calls analyzing the same picture, not an improvement.
- **`/reports/generate?source=visits`** (`db.get_report_data`): always prefers the visit's own
  thumb-crop via `COALESCE(v.crop_image_base64, re.crop_image_base64)` (`LEFT JOIN
  yard_stats.visits v ON v.id = re.visit_id AND v.thumb_crop_status = 'done'`) -- unconditional,
  not opt-in, since a report runs well after the fact on a schedule, so unlike the AI queue there's
  no real latency cost to just always taking the better image when it exists. `source=events`
  reports never apply this (matches the AI queue's own scoping decision).
- **`TELEGRAM_ALERTS_ENABLED`'s visit-summary message**: deferred, not edited after the fact.
  `mqtt_ingest._handle_review_message` only sends the summary immediately when
  `db.visit_thumb_crop_will_be_attempted(review)` is false (i.e. the flag is off, or Frigate ever
  omits `thumb_time`) -- otherwise it skips the immediate send entirely, and
  `visit_thumb_worker.process_claimed_visit` sends it once `thumb_crop_status` reaches a terminal
  state: `done` -> the fresh high-res crop; `failed` (attempts exhausted) -> falls back to the
  representative event's own crop (or text-only), so a visit is never left without its
  notification just because the re-crop never panned out. `mark_visit_thumb_crop_retry_or_failed`
  returns the resulting status specifically so the worker can tell "still retrying, don't send
  yet" apart from "just went terminal, send now" without re-deriving that from the attempt-count
  arithmetic itself. This is a genuine behavior change from before -- the notification now arrives
  however long the review takes to close plus however long the re-crop takes, not near-instantly --
  a deliberate trade (quality over speed) rather than the alternative of editing an already-sent
  photo in place (`editMessageMedia`), which was considered and rejected as more moving parts for
  the same result.

#### Bug: a truncated continuous-recording clip could silently produce a wrong-moment crop

`crop_visit_thumbnail`'s offset math (`thumb_time - (visit.start_ts - 5s)`) assumes
`video.build_clip_url`'s requested window (`start_ts-5s` to `end_ts+5s`) comes back in full. It
doesn't always: confirmed live in production that Frigate's continuous-recording clip endpoint can
silently return far less footage than requested -- a genuine case had a 13-second request
(`start_ts-5` to `end_ts+5`) come back only ~4.06 seconds long (`ffprobe`-confirmed), almost
certainly a motion-based recording gap rather than a "not ready yet" condition
`VIDEO_MIN_VALID_BYTES` would catch (the response wasn't too *small*, just shorter in *duration*
than the window asked for). The computed `thumb_time` offset (~6.1s) was past that real 4.06s of
footage -- ffmpeg doesn't error when `-ss` seeks past the actual end of an HTTP-streamed input, it
silently clamps to whatever frame is near the tail instead, so the resulting crop looked plausible
but was quietly from the wrong moment, with nothing in logs or `thumb_crop_status='done'` to
signal it. This was diagnosed by cross-referencing three things against the same visit: (1) the
crop's own burned-in camera-OSD-clock timestamp reading ~5s earlier than `thumb_time`/`start_ts`
predicted, (2) `ffprobe`'s reported clip duration (4.06s) vs. the requested window (13s), and (3) a
sweep of frames grabbed at 0.1s/1.0s/2.0s/3.0s/3.9s offsets showing perfectly linear 1:1 playback
within the clip -- ruling out a decode/seek-precision issue and pointing squarely at "the clip is
shorter than we assumed" as the root cause (a *separate*, purely cosmetic ~5s drift between the
camera's own onboard OSD clock and Frigate/NTP time is real too, but isn't what was causing the
wrong-frame problem; separately, this camera's `frigate.conf` had `record.continuous.days: 0`,
meaning nothing outside actual detected motion was ever retained -- the real, permanent fix for
the recording-gap side of this is raising that in `frigate.conf`, not anything in `ingest-worker`).
Fixed by probing the actual clip duration (`crop._probe_duration_seconds`, `ffprobe -show_format`)
before seeking and raising if the offset would land within `crop._DURATION_SAFETY_MARGIN_SECONDS`
(a fixed `0.5`, intentionally **not** an env-configurable setting -- see below) of it, so this now
fails cleanly into the existing retry-then-fallback path
(`visit_thumb_worker.mark_visit_thumb_crop_retry_or_failed` -> eventually the representative
event's own crop) instead of silently returning a mistimed image.

This margin is a buffer against landing right at the very tail of whatever duration Frigate *did*
return (encoder/keyframe rounding: e.g. offset=6.05s vs. an actual duration of 6.1s is technically
before the end, but close enough that ffmpeg can still grab a garbled/black tail frame) -- it does
nothing for `thumb_time` landing far later than what actually got recorded (the recording-gap case
above had a ~1.8s deficit; no margin value papers over a gap that size). Originally shipped as
`VISIT_THUMB_CROP_DURATION_SAFETY_MARGIN_SECONDS`, an env var -- removed after review because it
doesn't address the problem operators actually hit (a real time-shift, not an edge-of-clip
rounding case) and just added a confusing knob that looked like it should help but couldn't.
Demoted to a fixed internal constant, same treatment as `crop._FALLBACK_FRAME_OFFSET_SECONDS`.

**`VISIT_THUMB_CROP_OFFSET_ADJUST_SECONDS`** (default `0`) is the actual tunable for "my crops
consistently land a bit off from `thumb_time`" -- a plain manual shift of the seek target applied
before the duration check (positive = later/forward, negative = earlier/backward). Unlike the
margin above, this directly addresses a real, observed symptom (a camera's crops consistently
looking ~1s off from the expected moment, most likely keyframe spacing during the seek) --
tune it by comparing a handful of real crops against what `thumb_time` should show.

#### Bug: the opposite case -- a clip with extra, unrequested lead-in also produced a wrong-moment crop

The bug above was Frigate's clip coming back *shorter* than requested. Confirmed separately in
production that the same endpoint can also come back *longer* than requested, with the extra
footage prepended *before* the requested start rather than appended after it -- silently breaking
the (until then reasonable-looking) assumption that byte offset 0 of the returned clip lines up
with `start_ts - 5s`. A real visit asked for the usual `start_ts-5s`/`end_ts+5s` window (~15.2s)
and Frigate returned a 21.3s clip, ~6.1s longer. The old start-anchored offset
(`thumb_time - (start_ts - 5s)`, ~5.2s into the file) landed on an empty stretch of parking lot;
cross-referencing Frigate's own `thumb-*.webp` review thumbnail (which *does* show the moment --
a person standing near a van) against a sweep of frames from the actual downloaded clip found that
person ~11-13s in, not ~5s. The old duration-safety check never caught this because it only guards
against the offset landing too *close to* or *past* the duration -- an offset of ~5.2s in a 21.3s
clip looks completely unremarkable to that check; the crop still completed, `thumb_crop_status`
still went `done`, and nothing signaled the frame was wrong.

Likely explanation: Frigate stores continuous recording in fixed-length segments and builds an
arbitrary clip by concatenating whichever whole segments cover the requested range -- rounding the
*start* backward to the nearest segment boundary (adding a variable amount of lead-in, bounded by
one segment length) while the *end* lines up with what was actually asked for (confirmed: the
measured duration in that case was within ~0.1s of the requested `end_ts+5`).

Fixed by anchoring the offset from the clip's measured *end* instead of its assumed start --
`crop_visit_thumbnail` now computes `duration - ((end_ts+5) - thumb_time)` (`duration` from the
same `_probe_duration_seconds` call the safety-margin check already needed), so a variable amount
of extra lead-in at the front no longer shifts the target frame at all. The safety-margin check
now also rejects a negative offset (thumb_time computed as landing *before* the clip's actual
start), not just one too close to its end, so a genuine recording gap -- which can still push the
computed offset outside the clip's real bounds in either direction -- still fails cleanly into the
same retry-then-fallback path rather than silently returning a wrong-moment crop either way.

Practical implication: since the previous drift this setup was compensating for is now handled by
the anchor change itself, an existing `VISIT_THUMB_CROP_OFFSET_ADJUST_SECONDS` override tuned
against the old (start-anchored) behavior should be reset to `0` and re-tuned from scratch against
real crops if still needed -- it no longer means the same thing as before.

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
Query/report/AI-queue API above), and `STORE_VIDEO_ALERTS`/`TELEGRAM_ALERTS_ENABLED` add
independent per-visit video/notification flows alongside (not instead of) the existing per-event
`STORE_VIDEO`/`TELEGRAM_EVENTS_ENABLED` ones (see Video storage above). All three are deliberately
independent switches from their events-flow counterparts -- the point is to A/B per-event vs.
per-visit behavior against real traffic, not to pick one and commit. `GET /visits` remains the
read-only comparison view for judging `visits` data itself, separate from these behavior switches.

`review.alerts`/`review.detections` in `frigate.conf` currently share identical `required_zones`
per camera, so `severity` (`alert` vs `detection`) isn't a useful noise filter today -- nearly
everything in-zone comes back `alert`. Tightening `detections.required_zones` to be narrower than
`alerts.required_zones` would change that, but that's a Frigate config decision, not something
`ingest-worker` can affect.

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
  `thumb_time` (Frigate's own review "best frame" timestamp) and, when `VISIT_THUMB_CROP_ENABLED`,
  its own re-cropped `crop_image_base64` plus `thumb_crop_status` state machine -- a separate
  artifact from any linked raw_event's own crop (see "Visit thumbnail re-crop" above).
- `vehicle_sightings` / `person_sightings` — one row per AI-analyzed event. `vehicle_sightings`
  keeps `plate_text_frigate` (from `raw_events.sub_label`) next to `plate_text_llm` (the OCR
  model's read) as a cross-check.

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
