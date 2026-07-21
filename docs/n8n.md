# n8n setup, explained for this project

[n8n](https://n8n.io) is a visual workflow-automation tool — you build a flow out of connected
"nodes" (an HTTP call, an if/else branch, a bit of code, a scheduled trigger...) instead of writing
a script from scratch. This project ships three ready-made workflows, plus one callable
sub-workflow, as plain JSON files under `n8n/` — you import them into your own n8n instance rather
than writing anything from scratch. This page assumes you have a working n8n instance already
(self-hosted or n8n Cloud) but have never imported a workflow before.

**None of these are required for the pipeline to actually do anything useful.** The AI analysis
stage itself — claiming cropped/grouped events, sending the image(s) to your local VLM, writing the
result back — is handled internally by `ingest-worker`'s own `ai_worker.py` poll loop, not by an
n8n workflow; see [configuration.md](configuration.md#internal-ai-stages) to turn it on
(`ai_events_stage_enabled` in `profiles.yaml`). Everything on this page is optional, report-and-Q&A
tooling on top of that.

## The workflows, in one sentence each

| File | What it does | Trigger |
|---|---|---|
| `daily-report.json` | Emails/Telegrams an HTML report of the day's analyzed events | Scheduled (7am default) |
| `alerts-report.json` | Same idea, but grouped by visit (one row per real-world activity) instead of one row per raw event | Scheduled (7am default) |
| `yard-stats-qa.json` | Answer a plain-English question ("any red cars today?") against recent sightings | Webhook (called on demand, not scheduled) |
| `yard-stats-semantic-search-tool.json` | Sub-workflow `yard-stats-qa.json` calls internally for fuzzy/meaning-based search — not meant to run on its own | Called by `yard-stats-qa.json` |

They're independent of each other — import and enable whichever you actually want, whenever you're
ready.

## Step 1: Import

In n8n: **Workflows → Import from File**, pick one of the four `.json` files, repeat for each one
you want. Every workflow gets imported disabled (not running) by default — that's intentional, see
Step 4.

## Step 2: Fill in the placeholders

Every workflow has a few `REPLACE_WITH_...` values baked into node parameters (URLs, mostly) —
n8n's search (Ctrl+F / Cmd+F inside the workflow editor) for `REPLACE_WITH_` is the fastest way to
find every one that needs attention in a given workflow. What each one means:

- **`REPLACE_WITH_INGEST_WORKER_HOST` / `REPLACE_WITH_INGEST_WORKER_PORT`** (all of these) —
  wherever `ingest-worker` is reachable from n8n. If n8n and `ingest-worker` are on the same Docker
  host and the same network, this can be the Docker service name (`ingest-worker`) and its internal
  port; otherwise use the host's real LAN IP and `INGEST_WORKER_API_PORT` from `.env` (default
  `8080`).
- **`REPLACE_WITH_VLM_HOST` / `REPLACE_WITH_VLM_PORT`** (`yard-stats-qa.json`'s Chat Model
  credential, `yard-stats-semantic-search-tool.json`) — your own locally-hosted OpenAI-compatible
  VLM endpoint (e.g. a `llama.cpp` server,
  [`llama_slot_proxy`](https://github.com/shuricksumy/llama-slot-proxy), or similar). Not something
  this repo provides — bring your own.
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
3. Only once a manual run looks right, toggle the workflow **Active** (top-right in the workflow
   editor) to let its trigger (schedule, or a webhook for `yard-stats-qa.json`) take over.

## About `yard-stats-qa.json` specifically

This one is triggered by a **webhook**, not a schedule — n8n gives it its own unique URL once
activated (shown on the Webhook node). You call that URL yourself (`curl`, a browser bookmark, a
shortcut on your phone, a small chat frontend — whatever's convenient) with your question as the
request body/query, and it responds with an answer built from recent sightings. It's designed to
be driven by whatever's convenient for you to trigger from, rather than assuming any particular
chat client.

## Tuning the AI queue, if you build your own n8n-based AI stage

There's no shipped n8n workflow for the AI stage anymore — `ai_worker.py` is the maintained
implementation, tuned via `profiles.yaml`/`.env` (see
[configuration.md](configuration.md#internal-ai-stages)). But `ingest-worker`'s
`/ai-queue/claim` endpoint is unchanged and fully supports a custom n8n workflow calling it instead,
the same way the old `metadata-processor.json` did: a few query parameters you can freely edit
without redeploying anything — `parallel_limit` (how many events to claim per run),
`stale_minutes` (how long before a stuck claim is reclaimed), `max_age_hours` (skip events older
than this rather than spending capacity on backlog). See `CLAUDE.md`'s "Query/report/AI-queue API"
section for the full reasoning behind each one if you go this route.
