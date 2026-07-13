# Yard Stats + Vehicle Metadata

Extends an existing [Frigate](https://frigate.video) NVR setup with a pipeline that logs yard/
driveway activity and uses local vision-language models to describe vehicles (color, body type,
make, license plate) and people passing through camera zones — no cloud API calls, and nothing
here ever touches Frigate's own database.

## What it does

- Listens to Frigate's `frigate/events` MQTT topic and records every tracked object (car, truck,
  person, dog, ...) to Postgres.
- Crops each event out of the recording (via ffmpeg, using Frigate's own detection region) and
  stores the result — no image analysis yet at this point.
- A single n8n workflow sends the cropped image to a locally-hosted VLM: vehicles get
  color/body-type/make/plate in one call; people get a short description. Frigate's own LPR read
  is kept alongside the VLM's plate read as a cross-check.
- A read-only query/report API on `ingest-worker` (events, sightings, aggregate stats, HTML report
  generation) and a natural-language Q&A workflow sit on top, both scoped to only ever see events
  that have actually been analyzed.
- A configurable retention sweep deletes data past a set age (default 12 months) automatically.

## Architecture

```
Frigate (MQTT frigate/events, every object label: car/truck/person/dog/...)
   │
   ▼
ingest-worker/  (Python, one container, no LLM calls)
   - MQTT subscriber -> Postgres, unfiltered by label
   - Poll loop: claims new events (race-safe, FOR UPDATE SKIP LOCKED), fetches the Frigate event,
     crops via ffmpeg, stores the cropped image back on the row
   - Applies its own Postgres schema on startup and runs retention cleanup on a schedule
   - FastAPI surface (Swagger UI at :8080/docs): unauthenticated admin/debug endpoints
     (/health, /status, /crop/{id}, /retention/run) plus an X-API-Key-protected read/query/report
     API (/events, /sightings, /stats, /reports) that n8n and other consumers call instead of
     querying Postgres directly
   │
   ▼
n8n workflows (AI stage + reporting + Q&A)
   - claims cropped-but-not-yet-analyzed events, calls the VLM(s), writes results back
   - daily report (calls ingest-worker's /reports/generate), natural-language Q&A, retention --
     all read-only against analyzed data
```

Two independent retry-with-backoff queues live on the same `raw_events` table: `ingest-worker`
owns the crop stage, n8n owns the AI stage. Neither can starve or double-process the other.

## Repository layout

```
frigate/                        # main project folder -- the pipeline + Frigate's own config
  docker-compose.yml             # ONE file, two Compose profiles: pipeline + nvr (see below)
  .env.example                    # ONE shared template -- covers both stacks below (see comments)
  sql/queue-debug.sql             # manual check/fix/reset queries
  ingest-worker/                  # the Python service (see below)
  backup-postgres-projects.sh
  frigate.conf                     # Frigate's own config, read by the "frigate" service/profile
n8n/                             # additional folder -- importable workflow JSON
                                  # (AI stage, daily report, Q&A)
```

Despite sharing one `docker-compose.yml`, the `pipeline` profile (postgres-projects + ingest-worker)
and the `nvr` profile (Frigate itself) still deploy to two different hosts — Frigate's stack stays
on the camera/NVR host; the pipeline runs wherever you run n8n (or any Docker host that can reach
both Postgres and Frigate's REST API over the network). Compose profiles are opt-in — a bare
`docker compose up -d` with no `--profile` starts nothing, so there's no risk of accidentally
starting the wrong stack on the wrong host. `frigate/` here means "the Frigate-adjacent pipeline
project," not "everything in it runs on the Frigate host."

## Prerequisites

- Frigate **0.16+**, with `lpr.enabled: true` and a full-resolution record stream (separate from
  the low-res detect stream) — clips and crops come from the record stream.
- A locally-hosted OpenAI-compatible VLM endpoint (e.g. a `llama.cpp` server) reachable over HTTP.
- An n8n instance to import the workflow JSON into.
- Docker + Docker Compose.

## Getting started

1. `cd frigate && cp .env.example .env` and fill in real values. `.env.example` covers both
   stacks in this folder in one file (Postgres password, MQTT broker, Frigate's API base URL,
   `API_KEY` for the pipeline; `FRIGATE_MQTT_PASSWORD`/`FRIGATE_RTSP_PASSWORD`/camera IPs for
   Frigate's own stack) — on each host, fill in only the section that stack's compose file
   actually reads and leave the other section as `changeme` (harmless, unused there).
2. From `frigate/`: `docker compose --profile pipeline up -d`. `ingest-worker` pulls its image
   from `ghcr.io/shuricksumy/frigate-yard-stats/ingest-worker` (built automatically by
   `.github/workflows/ingest-worker-image.yml`); swap the compose file's `image:` line for
   `build: ./ingest-worker` if you want to build locally instead.
3. Check `http://<host>:8080/docs` (Swagger UI) or `http://<host>:8080/status` to confirm it's
   ingesting and cropping events. Use the "Authorize" button with your `API_KEY` to try
   `/sightings/vehicles`, `/stats/summary`, `/reports/generate`, etc.
4. Import each file in `n8n/` via n8n's UI (Import from File). Every file has
   `REPLACE_AFTER_IMPORT` credential placeholders and (where relevant) `REPLACE_WITH_VLM_HOST` /
   `REPLACE_WITH_VLM_PORT` / `REPLACE_WITH_OCR_SLOT_HOST` / `REPLACE_WITH_INGEST_WORKER_HOST` /
   `REPLACE_WITH_INGEST_WORKER_PORT` placeholders to fill in. `daily-report.json` needs an HTTP
   Header Auth credential (`X-API-Key` -> your `API_KEY`) for its call to ingest-worker.
5. Manually trigger each workflow once against a few real rows before enabling its schedule
   trigger — especially the Metadata Processor and the Daily Report.
6. Separately, if you also need Frigate itself: on the NVR host, same `frigate/.env` (or a copy
   of it with just the Frigate section filled in), then `docker compose --profile nvr up -d`.

## Configuration reference

Most of these are optional overrides in `.env` — see `.env.example` for the full list with
defaults (queue parallelism, stale-item timeout, max retry attempts, crop/thumbnail size caps and
padding, retention window). You must set `POSTGRES_PROJECTS_PASSWORD`, the `MQTT_*`/
`FRIGATE_API_BASE` connection details, and `API_KEY` (protects ingest-worker's `/events`,
`/sightings`, `/stats`, `/reports` endpoints — sent as an `X-API-Key` header; the existing
`/health`, `/status`, `/crop/{id}`, `/retention/run` admin endpoints stay unauthenticated).

## Data retention & privacy

Plate text and clips are treated as semi-sensitive — `ingest-worker` runs a retention sweep
(`RETENTION_MONTHS`, default 12) on its own schedule rather than accumulating data indefinitely.
See `frigate/sql/queue-debug.sql` for manual checks/fixes/resets if you need to inspect or
intervene by hand.

## License

MIT — see [LICENSE](LICENSE).
