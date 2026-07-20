# Configuring `ingest-worker`, explained for this project

Every setting below lives in `frigate/.env` (copied from `frigate/.env.example`) and is read by
`ingest-worker` on container start — see [`docker.md`](docker.md) if you haven't set that up yet.
This page groups them by *feature* and explains what each one actually does in plain language;
`.env.example` itself has the exact variable names and defaults.

## Suggested rollout order

Everything except the handful of settings below is **off by default**. Don't turn everything on at
once — bring it up in stages so if something looks wrong, you know which piece caused it:

1. **Just the core pipeline first.** Fill in the required settings below, leave everything else at
   its default (off), start `ingest-worker`, and confirm real events show up cropped at
   `http://<host>:8080/ui` or via `/events` in Swagger.
2. **Turn on video storage** (`STORE_VIDEO`) once step 1 looks right, if you want stored clips
   alongside the crops.
3. **Turn on the alerts/visits flow** (`STORE_VIDEO_ALERTS`, `VISIT_THUMB_CROP_ENABLED`) once
   you're comfortable with the events flow — these group multiple detections into one real-world
   "visit" and are a separate, independently-toggleable layer on top (see
   [`frigate.md`](frigate.md) for why the visit-preview feature specifically depends on your
   Frigate recording retention settings).
4. **Turn on Telegram** whenever you want notifications — independent of everything else.
5. **Semantic search and the internal AI stages are both separate, later opt-ins** — neither is
   needed to get the core pipeline or n8n's `metadata-processor.json` working. Turn on pgvector
   embeddings once you're already running `metadata-processor.json` successfully; only consider
   the internal AI stages (`AI_EVENTS_STAGE_ENABLED`, `AI_ALERTS_ENABLED`) once you're comfortable
   letting them replace or supplement that n8n workflow.

## Required settings

You must set these — `ingest-worker` won't start without them:

- `POSTGRES_PROJECTS_PASSWORD` — password for the Postgres database this project creates for
  itself (a fresh database, own schema — never shares data with anything else you run).
- `MQTT_HOST` (+ `MQTT_USERNAME`/`MQTT_PASSWORD` if your broker needs auth) — the same broker
  Frigate itself publishes `frigate/events`/`frigate/reviews` to.
- `FRIGATE_API_BASE` — Frigate's own REST API, reachable from wherever `ingest-worker` runs (its
  real LAN IP:port, e.g. `http://192.168.1.10:5000` — not a Docker service name, since these two
  services usually run on different physical hosts).
- `API_KEY` — a secret you make up yourself (any random string) that protects `ingest-worker`'s
  read/query/report/AI-queue API. n8n needs this same value in its HTTP Header Auth credential
  (see [`n8n.md`](n8n.md)).

## Crop tuning

Controls how `ingest-worker` turns a Frigate event into the still image that gets displayed and
analyzed:

- `RECORD_WIDTH` / `RECORD_HEIGHT` — your cameras' actual full-resolution record-stream size (see
  [`frigate.md`](frigate.md)'s "detect vs record" section) — needed to correctly scale Frigate's
  normalized bounding-box coordinates.
- `MAX_CROP_DIMENSION` (default `1280`) — the cropped JPEG's long side is capped here. VLMs
  downsample beyond this internally anyway, so a bigger value only adds load, not analysis quality.
- `CROP_PADDING_PCT` (default `0.2`) — extra margin added around Frigate's own detected region, so
  the crop isn't razor-tight around the object.
- `CROP_FRAME_OFFSET_PCT` (default `0.5`) — *where* in the event's timespan to grab the frame
  (`0.0` = right at the start, `0.5` = midpoint, `1.0` = right at the end). There's no universally
  "correct" value — Frigate picks its own best-scoring frame per event using logic it doesn't
  expose, so this is a starting point to tune against your own footage if `0.5` consistently looks
  off.
- `CROP_DISABLED` (default `false`) — skips cropping entirely; the full original camera frame
  (still scaled to `MAX_CROP_DIMENSION`) is used instead of a region around the object. This is a
  real trade-off, not a strict improvement: a full wide frame gives more context but makes small
  detail (plates, notable features) harder for the VLM to read. The same image is what's displayed
  in the web UI *and* sent to the VLM — there's no separate "wide for humans, cropped for the
  model" mode. Only applies for events when `FRIGATE_SNAPSHOT_ENABLED` below is `false`.
- `FRIGATE_SNAPSHOT_ENABLED` (default **`true`**) — for **events only**, uses Frigate's own
  already-rendered event snapshot instead of seeking+cropping a frame from the record-stream clip
  yourself. Frigate picks this frame by its own best-detection-score judgment, so in practice it
  beats the fixed-offset guess `CROP_FRAME_OFFSET_PCT` makes often enough to be the default —
  accepted trade-off: Frigate's snapshot is from the lower-res detect stream (typically much
  smaller than your record stream) with a burned-in bounding-box/label/timestamp overlay this
  Frigate version's API gives no way to turn off (confirmed directly — `bbox=0`/`timestamp=0`/`h=`
  query params on the snapshot endpoint have no effect at all). Set to `false` to fall back to
  this project's original seek-based approach if that trade-off doesn't work for your footage —
  `CROP_DISABLED`/`CROP_FRAME_OFFSET_PCT`/`CROP_PADDING_PCT` only take effect once you do. A
  visit's own composite grid (`VISIT_THUMB_CROP_ENABLED`) is unaffected either way — a single
  Frigate snapshot has no multi-frame equivalent to offer it.

## Camera allow-list

`CAMERAS` (optional, comma-separated, e.g. `outside,outside2`) — if set, only these cameras'
events/reviews are ever recorded at all; anything else Frigate reports is silently ignored at
ingest time. Leave unset (default) to process every camera Frigate has.

## Queue tuning

How aggressively `ingest-worker`'s own crop stage works through events — defaults are reasonable
starting points, not something you need to touch immediately:

- `PARALLEL_LIMIT` (default `2`) — how many events can be mid-crop at once.
- `STALE_MINUTES` (default `5`) — how long a stuck claim (e.g. the service crashed mid-crop) sits
  before it's automatically retried.
- `MAX_ATTEMPTS` (default `3`) — how many failures before an event is given up on (marked
  `failed`, not retried further).
- `POLL_INTERVAL_SECONDS` (default `5`) — how often the crop poll loop checks for new work.

## Video storage

Two **independent** switches — either, both, or neither can be on:

- `STORE_VIDEO` (default `false`) — downloads and keeps the clip for every individual event,
  alongside its crop. Stored under `VIDEO_STORAGE_HOST_PATH` (default `./video-storage` on the
  host).
- `STORE_VIDEO_ALERTS` (default `false`) — same idea, but one clip per *visit* (a whole grouped
  real-world activity) instead of per raw event. Stored completely separately, under
  `VIDEO_STORAGE_ALERTS_HOST_PATH` (default `./video-storage-alerts`), so you can measure/manage
  the two flows' disk usage independently.

Both share the same download-retry tuning (`VIDEO_INITIAL_WAIT_SECONDS`, `VIDEO_MIN_VALID_BYTES`,
`VIDEO_MAX_ATTEMPTS`, `VIDEO_RETRY_WAIT_SECONDS`, `VIDEO_MAX_AGE_HOURS`) — the defaults account for
Frigate needing a few seconds to finish writing a clip before it's downloadable, and skip a clip
that's very likely already rolled off Frigate's recording buffer rather than retrying forever.

## Visit previews (composite grid + GIF)

`VISIT_THUMB_CROP_ENABLED` (default `false`) turns on a fifth artifact: once a visit (a Frigate
review/alert closes), `ingest-worker` samples 4 frames proportionally across that visit's own span
and combines them into one composite grid image (what actually gets analyzed and shown) plus a
separate animated GIF (human preview only, in the web UI). `VISIT_PREVIEW_FRAME_PERCENTAGES`
(default `0,25,50,100`) controls exactly which 4 points get sampled — e.g. `5,35,65,90` to stay a
little clear of both edges. See [`frigate.md`](frigate.md) for why this feature's reliability
depends on your `record.continuous.days` setting.

## Telegram notifications

Two more **independent** settings, each a *mode* (`none` / `image` / `video` / `all`), not a bool
— `none` by default:

- `TELEGRAM_EVENTS_MODE` — per-event notifications. `image` sends a photo right after cropping;
  `video` sends the clip once it's stored (`STORE_VIDEO`), standalone rather than threaded onto a
  photo that was never sent; `all` sends both (the video as a reply to the earlier photo).
- `TELEGRAM_ALERTS_MODE` — per-*visit* notifications instead. `image` sends one summary message
  per visit (photo/GIF once the preview is ready, or text-only immediately if
  `VISIT_THUMB_CROP_ENABLED` is off); `video` sends the visit's own clip (`STORE_VIDEO_ALERTS`) as
  a reply to that summary; `all` sends both.

`image` and `video` are independent halves within each mode, not a ladder — setting `video` alone
does *not* also send the photo/summary; only `all` sends both.

To use either, you need a Telegram bot and your own chat ID:

1. Message [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`, follow the prompts — it
   gives you a bot token. That's `TELEGRAM_BOT_TOKEN`.
2. Message your new bot anything once (so it can see your chat), then visit
   `https://api.telegram.org/bot<your-token>/getUpdates` in a browser — your numeric chat ID is in
   the JSON response under `message.chat.id`. That's `TELEGRAM_CHAT_ID`.

`TELEGRAM_EVENTS_MODE` and `TELEGRAM_ALERTS_MODE` can be set to any combination independently —
this is deliberately a place to A/B which granularity (and which of photo vs. video) is actually
useful for your traffic rather than a choice you're expected to get right upfront.

Both are global defaults only — `profiles.yaml` can override either **per object type**
(`telegram_events_mode`/`telegram_alerts_mode` keys under that type's `object_types` entry), e.g.
to silence a noisy low-priority type's notifications without changing the mode for everything
else. Omit the override and that type just inherits the global default above. See
`profile_config.py` and `profiles.yaml`'s own comments for the full list of per-type overrides.

## Retention

- `RETENTION_MONTHS` (default `12`) — how long data (DB rows, and any stored video files) is kept
  before an automatic sweep deletes it.
- `RETENTION_CHECK_INTERVAL_SECONDS` (default `86400`, once a day) — how often that sweep runs.

`POST /retention/purge` (Swagger UI, or the "Media only" checkbox on `/ui/admin`) is a separate,
ad-hoc counterpart if you want to purge on a cutoff of your own choosing right now rather than
waiting for or reconfiguring the scheduled sweep — defaults to a dry run (just shows you counts)
until you pass `confirm=true`. `only_media` (default `true`) keeps every row and its AI analysis
text/plate reads searchable forever, only clearing stored video/images/GIFs; set it to `false` for
the original full-row delete (rebuilds the semantic search index afterward).

An optional `object_label` param (also a dropdown on `/ui/admin`) restricts either mode to a
single Frigate object type, e.g. clean up just `dog` events without touching everything else's
retention. Only ever affects events/sightings of that type — visits (which can span multiple
distinct object types in one row) are never touched by a type-scoped purge; omit `object_label`
(the default) to keep covering visits too, same as before this param existed.

## Web UI

`OBJECT_TYPES` (default `car,truck,person,dog`) — the labels your own Frigate config actually
tracks, so the web UI's Type filter dropdown matches reality. Add a label here (matching what you
added to `frigate.conf`'s `objects.track`) and it appears in the dropdown on next restart, no code
change needed. See [`web-ui.md`](web-ui.md) for a tour of the UI itself.

## Semantic search (pgvector)

Requires `postgres-projects` to run the `pgvector/pgvector:pg16` image (already the default in
`docker-compose.yml`) rather than plain `postgres:16` — `schema.sql`'s `CREATE EXTENSION IF NOT
EXISTS vector` needs that extension actually present in the image. No `ingest-worker` env var
turns this on/off by itself — the universal `sightings`/`visit_sightings` tables gain a nullable
`embedding` column either way; it just stays empty until something (n8n's `metadata-processor.json`,
or the internal AI stage below) actually sends one via `POST /sightings`. `POST
/search/semantic` is the read side — cosine-similarity search over whatever sightings do have an
embedding, filtered by a time range and (optionally) which object labels to include. See CLAUDE.md's
"Semantic search and the Q&A agent" section for the full design, and
`n8n/yard-stats-semantic-search-tool.json` / `n8n/yard-stats-qa.json` for the Q&A agent that uses it.

**Backfilling old sightings**: anything analyzed before you turned this on has `embedding = NULL`
and won't show up in semantic search results. `POST /embeddings/backfill` fills those in — call it
once with no `confirm` to see how many rows are missing an embedding, then repeatedly with
`confirm=true` (each call processes up to `limit`, default 50, per table) until both counts
hit zero. Needs `LLAMA_PROXY_BASE_URL` set (see "Internal AI stage" below) even if you're not using
that stage for anything else — it's the only thing this endpoint needs from that section.

## Internal AI stages (alternative to n8n's metadata-processor.json)

Two independent stages, both off by default — n8n's `metadata-processor.json` is the AI stage
until you deliberately opt into one or both instead:

- **`AI_EVENTS_STAGE_ENABLED=false`** — analyzes each event's own single-frame crop with
  `profiles.yaml`'s `event_prompt`. Don't run alongside n8n's `metadata-processor.json` against
  the same queue at once (see CLAUDE.md's "Internal AI stage" section for why that's wasteful,
  though not unsafe).
- **`AI_ALERTS_ENABLED=false`** — analyzes a visit's own composite grid (4 frames sampled across
  its span) with `profiles.yaml`'s `alert_prompt`, storing results separately in
  `visit_sightings`. Requires `VISIT_THUMB_CROP_ENABLED=true` —
  without it, no visit ever has a grid ready to analyze, so this stage just stays idle. Can run
  alongside or instead of `AI_EVENTS_STAGE_ENABLED` — the two are fully independent queues.

Both are global defaults only — `profiles.yaml` can override either **per object type**
(`ai_events_stage_enabled`/`ai_alerts_enabled` keys), e.g. to run the events stage for `car`/
`person` only while `dog` sits out, or to enable a stage for just one type even while the global
flag stays `false`. Setting either override `true` for at least one type is enough to start that
stage's poll thread even if its global env var is `false` — the thread then only claims the
type(s) that resolve to enabled (their own override, or the global default when they don't set
one), never every mapped type unconditionally.

- Object types + prompts + per-type model slot/timeout live in **`frigate/profiles.yaml`** (repo
  root, alongside `docker-compose.yml`), not env vars — that's genuinely a lot of config to cram
  into `.env` readably. `docker-compose.yml` already bind-mounts this file into the container, so
  just edit it and restart `ingest-worker` — no rebuild needed. (`AI_STAGE_PROFILE_PATH`, default
  `/app/profiles.yaml`, is the path the bind mount lands on; you'd only touch this env var if you
  wanted to point at a differently-named file instead.) This is a flat map — every Frigate object
  label (`car`, `truck`, `person`, or any label you add, e.g. `dog`) gets its own entry with two
  prompts: `event_prompt` (single static frame) and `alert_prompt` (the 2x2 grid, framed to also
  describe what changed across the 4 frames, not just static attributes). Both prompts are answered
  as plain free text — there is no JSON schema or per-field response format, so adding a brand-new
  object type is purely a `profiles.yaml` edit, never a code change. Labels that should share one
  model/prompt (e.g. `car` and `truck`) can point at the same YAML anchor instead of duplicating the
  block. A Frigate object label with no entry in this file is simply never analyzed by either stage.
- `AI_STAGE_PARALLEL_LIMIT`/`AI_STAGE_STALE_MINUTES`/`AI_STAGE_MAX_ATTEMPTS`/
  `AI_STAGE_MAX_AGE_HOURS`/`AI_STAGE_POLL_INTERVAL_SECONDS` — same queue-tuning shape as the crop
  stage above, shared between both stages (each claims from its own separate queue, so this
  doesn't mean they compete for capacity).
- `LLAMA_PROXY_BASE_URL` (required once either stage is enabled) — your
  [`llama_slot_proxy`](https://github.com/shuricksumy/llama-slot-proxy)'s own base URL, called
  directly instead of going through n8n. `LLAMA_PROXY_TOKEN` is optional (blank = no
  `Authorization` header — `llama_slot_proxy` is unauthenticated on the LAN in most setups today).
  `LLAMA_PROXY_EMBED_PATH` is the embedding model's own URL path segment (same one-path-per-slot
  convention `profiles.yaml`'s `chat_path` uses).
- `EMBEDDING_DIMENSIONS` (default `1024`) — must match the output size of whatever model is loaded
  behind `LLAMA_PROXY_EMBED_PATH` (e.g. `1024` for Qwen3-Embedding-0.6B-GGUF, `768` for
  nomic-embed-text-v1.5). Sizes the pgvector `embedding` columns on `sightings`/
  `visit_sightings`. Changing this after sightings already have embeddings stored clears them (a
  different model's vectors are an incomparable vector space regardless of dimension) — re-run
  `POST /embeddings/backfill?confirm=true` afterwards.
- `AI_STAGE_DEFAULT_TIMEOUT_SECONDS` (default `180`)/`AI_STAGE_EMBED_TIMEOUT_SECONDS` (default
  `60`) — fallback timeouts; the real per-type chat timeout belongs in `profiles.yaml` itself
  (`timeout_seconds`), since a local model's response time genuinely depends on which model/prompt
  you've picked for that type.
