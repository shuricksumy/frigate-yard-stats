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
    docker-compose.yml        # ONE file, two Compose profiles: pipeline + nvr (see below)
    .env.example                # ONE shared template covering both stacks below -- see comments
    sql/queue-debug.sql         # manual check/fix/reset queries for the raw_events queue
    backup-postgres-projects.sh
    ingest-worker/               # the main service -- see below
    frigate.conf                 # Frigate's own config, read by the "frigate" service/profile
  n8n/                        # additional folder -- importable workflow JSON (AI stage, reports, Q&A)
```

`frigate/docker-compose.yml` holds two independent stacks as Compose profiles -- `pipeline`
(postgres-projects + ingest-worker) and `nvr` (Frigate itself) -- that still deploy to two
different hosts despite sharing one file and one `.env.example`/`.env`. Profiles are opt-in
(`docker compose --profile pipeline up -d` / `docker compose --profile nvr up -d`); a bare
`docker compose up -d` starts nothing, so there's no risk of starting the wrong stack on the wrong
host. Each service only reads the environment variables it references, so the same `.env` can be
copied to both hosts and each one only fills in / relies on its own section (documented via
comments in `.env.example`). Frigate's REST API is reached cross-host at `FRIGATE_API_BASE` (its
LAN-bound IP:port, e.g. `http://192.168.1.10:5000`), not a Docker service name. The `frigate/`
folder name reflects that this pipeline is Frigate-adjacent tooling, not that everything in it
runs on the Frigate host.

## Architecture

```
Frigate (MQTT frigate/events, every object label: car/truck/person/dog/...)
   │
   ▼
ingest-worker/  (Python, one container, no LLM calls)
   - MQTT subscriber -> INSERT raw_events, unfiltered by label
   - Poll loop, every POLL_INTERVAL_SECONDS:
       reap stale crop_status='processing' -> count in-progress -> claim batch (FOR UPDATE SKIP LOCKED)
       -> GET Frigate event (region/sub_label/score) -> crop via built-in ffmpeg, size-capped
       -> store crop_image_base64, sub_label, score -> mark crop_status done/retry/failed
   - Also applies schema.sql on every startup (idempotent) and runs retention cleanup on a slow cadence
   - FastAPI surface (Swagger UI at :8080/docs): unauthenticated admin/test endpoints
     (health/status/manual-crop/manual-retention, not part of the normal pipeline) plus an
     X-API-Key-protected API (/events, /sightings, /stats, /reports, /ai-queue) that n8n and other
     consumers call instead of querying Postgres directly -- this now includes the AI-stage queue
     mechanics (claim/complete/fail), not just read-only queries
   │  (crop_status = 'done', crop_image_base64/sub_label/score already on the row)
   ▼
n8n Metadata Processor (car/truck/person, one shared queue) -- AI stage only, no Frigate/crop calls
   - POST /ai-queue/claim -- reap stale, count in-progress, atomically claim a batch, all in one call
   - route by object type, call the VLM(s) directly against the claimed row's crop_image_base64
   - POST /sightings/vehicles or /sightings/persons -- insert + mark ai_status='done' in one call
   - on VLM failure: POST /ai-queue/{id}/fail -- retry-or-fail-with-cap
   │
   ▼
Daily Report / Q&A agent (n8n) -- read-only, calls ingest-worker's query/report API
   (which itself only ever reads *_sightings rows, i.e. AI-analyzed events)
```

Two independent queue state machines live on `raw_events` -- `crop_status`/`crop_status_changed_at`/
`crop_attempt_count` and `ai_status`/`ai_status_changed_at`/`ai_attempt_count` -- and
`ingest-worker` now *mechanically executes both* (the crop-stage poll loop owns the first
directly; the `/ai-queue/*` endpoints own the second on n8n's behalf). n8n still *decides policy*
for the AI stage -- `parallel_limit`/`stale_minutes`/`max_age_hours` are query params on
`/ai-queue/claim` and `max_attempts` is a query param on `/ai-queue/{id}/fail`, all editable
directly in those n8n HTTP Request nodes without touching `ingest-worker` code, the same "tune it
here" spirit the old `Queue Config` node had. `ingest-worker` still never calls an LLM -- n8n
still owns the actual VLM call and prompt; it just no longer runs raw SQL to do so.

Both stages use the same shape: `new` (not picked up) → `processing` (claimed, work in flight) →
`retry` (crashed/reaped, or errored below that stage's attempt cap) → `failed` (errored at/above
the cap, terminal) → `done`. `FOR UPDATE SKIP LOCKED` is what makes claiming race-safe against
overlapping runs (multiple n8n executions, or this service's own poll loop).

## Key pieces

- **`ingest-worker`** does everything that isn't an LLM call: MQTT ingestion, both queue state
  machines (crop-stage directly, AI-stage via API), Frigate bbox lookup, ffmpeg cropping, and a
  read/query/report/AI-queue API over the data it collects
  (`api.py`/`db.py`/`report.py`/`schemas.py`/`auth.py`). It's intentionally dumb/mechanical so it
  can be plain, testable Python instead of n8n Code-node gymnastics. Self-contained: builds from
  its own folder, bakes `schema.sql` into the image, needs only Postgres + MQTT + Frigate's HTTP
  API to run.
- **n8n** owns everything AI-shaped: deciding when to claim work and calling the VLM(s), the daily
  report, and the Q&A workflow. Its processors never touch Frigate's API or crop anything
  themselves — they only ever read `crop_image_base64` that's already sitting on the claimed row,
  and no longer run raw SQL at all — claim/complete/fail all go through `ingest-worker`'s
  `/ai-queue/*` and `/sightings/*` endpoints. `ingest-worker` never calls an LLM, by design.
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

### Schema (`yard_stats`)

- `raw_events` — one row per Frigate `end` event, any label. Carries both queue state machines
  plus `crop_image_base64`, `sub_label` (Frigate's own LPR read), `score` — all captured by
  `ingest-worker` from one Frigate API fetch, so n8n never needs to call Frigate itself.
- `visits` — deduplicated, cross-camera-merged events (not yet wired into the current pipeline).
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
  both queue stages increment an attempt counter and only go terminal (`failed`) at/above
  `MAX_ATTEMPTS` (default 3) — below that, a failure goes back to `retry` and is picked up on a
  later run, not looped within the same execution.
- Treat plate text and clips as semi-sensitive data — `ingest-worker` applies a retention sweep
  (`RETENTION_MONTHS`, default 12) on its own schedule (`RETENTION_CHECK_INTERVAL_SECONDS`); an
  equivalent n8n workflow existed early on but has since been removed from `n8n/` as superseded.
- The Coral's base detection model is the accuracy ceiling for anything reaching this pipeline
  (missed detections never generate an event at all) — a Frigate/Frigate+ concern, not something
  to compensate for at the LLM layer.

## Commands

- Run the pipeline stack: `docker compose --profile pipeline up -d` (from `frigate/`; requires
  `.env` filled in from `.env.example`). `ingest-worker` pulls its image from GHCR by default
  (built by `.github/workflows/ingest-worker-image.yml`); use `docker compose --profile pipeline
  build ingest-worker` first only if overriding the compose file's `image:` with `build:
  ./ingest-worker` for local development.
- Manual DB checks/fixes: `frigate/sql/queue-debug.sql` (status breakdowns, force-retry, resets).
- Manual API testing: `http://<host>:8080/docs` (Swagger UI) once `ingest-worker` is running.
- n8n workflows are plain JSON exports under `n8n/` — import via n8n's UI, fill in credentials
  after import (`REPLACE_AFTER_IMPORT` placeholders), then manually trigger once against a few
  real rows before enabling a workflow's schedule trigger.
- Frigate's own stack: same `frigate/.env` (fill in its section), then deploy on the actual NVR
  host via `docker compose --profile nvr up -d`.
