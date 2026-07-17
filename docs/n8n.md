# n8n setup, explained for this project

[n8n](https://n8n.io) is a visual workflow-automation tool — you build a flow out of connected
"nodes" (an HTTP call, an if/else branch, a bit of code, a scheduled trigger...) instead of writing
a script from scratch. This project ships four ready-made workflows as plain JSON files under
`n8n/` — you import them into your own n8n instance rather than writing anything from scratch.
This page assumes you have a working n8n instance already (self-hosted or n8n Cloud) but have never
imported a workflow before.

## The four workflows, in one sentence each

| File | What it does | Trigger |
|---|---|---|
| `metadata-processor.json` | The AI stage: claims cropped/grouped events, sends the image(s) to your local VLM, writes the result back | Every minute |
| `daily-report.json` | Emails/Telegrams an HTML report of the day's analyzed events | Scheduled (7am default) |
| `alerts-report.json` | Same idea, but grouped by visit (one row per real-world activity) instead of one row per raw event | Scheduled (7am default) |
| `yard-stats-qa.json` | Answer a plain-English question ("any red cars today?") against recent sightings | Webhook (called on demand, not scheduled) |

Only `metadata-processor.json` is required for the pipeline to actually do anything useful (without
it, events sit forever with `ai_status='new'`, never analyzed). The report workflows and Q&A are
genuinely optional — turn them on whenever you're ready.

## Step 1: Import

In n8n: **Workflows → Import from File**, pick one of the four `.json` files, repeat for each one
you want. Every workflow gets imported disabled (not running) by default — that's intentional, see
Step 4.

## Step 2: Fill in the placeholders

Every workflow has a few `REPLACE_WITH_...` values baked into node parameters (URLs, mostly) —
n8n's search (Ctrl+F / Cmd+F inside the workflow editor) for `REPLACE_WITH_` is the fastest way to
find every one that needs attention in a given workflow. What each one means:

- **`REPLACE_WITH_INGEST_WORKER_HOST` / `REPLACE_WITH_INGEST_WORKER_PORT`** (all four workflows) —
  wherever `ingest-worker` is reachable from n8n. If n8n and `ingest-worker` are on the same Docker
  host and the same network, this can be the Docker service name (`ingest-worker`) and its internal
  port; otherwise use the host's real LAN IP and `INGEST_WORKER_API_PORT` from `.env` (default
  `8080`).
- **`REPLACE_WITH_VLM_HOST` / `REPLACE_WITH_VLM_PORT`** (`metadata-processor.json`,
  `yard-stats-qa.json`) — your own locally-hosted OpenAI-compatible VLM endpoint (e.g. a
  `llama.cpp` server, `llama_slot_proxy`, or similar). Not something this repo provides — bring
  your own.
- **`REPLACE_WITH_CHAT_ID`** (report workflows) — your Telegram chat ID, if you want the report
  sent there too (see [Telegram setup](configuration.md#telegram-notifications)).
- **`REPLACE_WITH_FROM_ADDRESS` / `REPLACE_WITH_TO_ADDRESS`** (report workflows) — if you also want
  the report emailed.

## Step 3: Wire up credentials

A few nodes need an actual n8n **credential** (not just a plain text value) — these show up with a
`REPLACE_AFTER_IMPORT` placeholder name until you point them at a real one:

- **HTTP Header Auth** (every workflow's calls into `ingest-worker`) — create one credential with
  header name `X-API-Key` and value equal to your `.env`'s `API_KEY`. All the ingest-worker HTTP
  Request nodes across all four workflows can share this same one credential.
- **Telegram** (report workflows, if you want Telegram delivery) — n8n's built-in Telegram
  credential type, needs a bot token (see [Telegram setup](configuration.md#telegram-notifications)
  for how to create one — the same bot token works here and in `ingest-worker`'s own
  `TELEGRAM_BOT_TOKEN`, or use a separate bot, your choice).
- **SMTP** (report workflows, if you want email delivery) — n8n's built-in SMTP credential, your
  own mail provider's details.

If you don't want email or Telegram delivery for the reports, just delete (or disable) the
corresponding `Send Email` / `Send Telegram` node after import instead of filling in credentials
you don't need.

## Step 4: Test manually before trusting the schedule

Every workflow imports **disabled**. Before flipping it on:

1. Open the workflow, click **Execute Workflow** (or the equivalent "test this node" on the
   trigger) to run it once by hand against real data.
2. Check the output of each node as it runs (n8n highlights failures in red) — especially the
   first HTTP Request node into `ingest-worker`, since a wrong host/port or missing API key shows
   up immediately here.
3. For `metadata-processor.json` specifically: run it once, then check
   `http://<host>:8080/ui` (or `/sightings/vehicles` / `/sightings/persons` via Swagger) to confirm
   a real sighting actually got written with sensible-looking values, not just "the HTTP calls
   didn't error."
4. Only once a manual run looks right, toggle the workflow **Active** (top-right in the workflow
   editor) to let its trigger (schedule or, for the metadata processor, "every minute") take over.

## About `yard-stats-qa.json` specifically

This one is triggered by a **webhook**, not a schedule — n8n gives it its own unique URL once
activated (shown on the Webhook node). You call that URL yourself (`curl`, a browser bookmark, a
shortcut on your phone, a small chat frontend — whatever's convenient) with your question as the
request body/query, and it responds with an answer built from recent sightings. It's designed to
be driven by whatever's convenient for you to trigger from, rather than assuming any particular
chat client.

## Tuning the AI queue from n8n, without touching `ingest-worker`

`metadata-processor.json`'s **Claim Next Batch (API)** node has a few query parameters you can
freely edit without redeploying anything — `parallel_limit` (how many events to claim per run),
`stale_minutes` (how long before a stuck claim is reclaimed), `max_age_hours` (skip events older
than this rather than spending capacity on backlog). See `CLAUDE.md`'s "Query/report/AI-queue API"
section if you want the full reasoning behind each one — the short version is: these are meant to
be tuned live in n8n as your traffic changes, not treated as fixed.
