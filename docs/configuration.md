# Configuring `ingest-worker`, explained for this project

Most settings below live in `frigate/.env` (copied from `frigate/.env.example`) and are read by
`ingest-worker` on container start — see [`docker.md`](docker.md) if you haven't set that up yet.
A specific subset — anything you'd realistically want different per Frigate object type (crop
framing, video storage, visit previews, Telegram modes, the internal AI stages) — instead lives
entirely in `frigate/profiles.yaml`; see "Per-object-type overrides" below for the full list and
why. This page groups everything by *feature* and explains what each setting actually does in
plain language; `.env.example`/`profiles.yaml` themselves have the exact names and defaults.

## Suggested rollout order

Everything except the handful of settings below is **off by default**. Don't turn everything on at
once — bring it up in stages so if something looks wrong, you know which piece caused it:

1. **Just the core pipeline first.** Fill in the required settings below, leave everything else at
   its default (off), start `ingest-worker`, and confirm real events show up cropped at
   `http://<host>:8080/ui` or via `/events` in Swagger.
2. **Turn on video storage** (`store_video` in `profiles.yaml`) once step 1 looks right, if you
   want stored clips alongside the crops.
3. **Turn on the alerts/visits flow** (`store_video_alerts`, `ai_alerts_enabled`, both in
   `profiles.yaml`) once you're comfortable with the events flow — these group multiple detections
   into one real-world "visit" and are a separate, independently-toggleable layer on top.
4. **Turn on Telegram** whenever you want notifications — independent of everything else.
5. **Semantic search and the internal AI stages are both separate, later opt-ins** — neither is
   needed to get the core pipeline running. The AI stage itself (`ai_events_stage_enabled`,
   `ai_alerts_enabled`, both in `profiles.yaml`) is what actually analyzes events with a VLM and
   writes `sightings` rows — turn it on once you're comfortable with the events/visits flow above.
   Only turn on pgvector embeddings once the AI stage is already writing real sightings, since
   there's nothing to embed until then.

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
analyzed. `RECORD_WIDTH`/`RECORD_HEIGHT` stay plain `.env` settings (they describe your camera
hardware, not a tunable behavior); everything else here is configured entirely in `profiles.yaml`
instead — see "Per-object-type overrides" below for the full mechanism.

- `RECORD_WIDTH` / `RECORD_HEIGHT` — your cameras' actual full-resolution record-stream size (see
  [`frigate.md`](frigate.md)'s "detect vs record" section) — needed to correctly scale Frigate's
  normalized bounding-box coordinates.
- `max_crop_dimension` (default `1280`, a plain technical knob in `profiles.yaml`'s `defaults:`,
  not a per-type setting or an env var) — the cropped JPEG's long side is capped here. VLMs
  downsample beyond this internally anyway, so a bigger value only adds load, not analysis quality.
- `crop_padding_pct` (default `0.2`, in `profiles.yaml`) — extra margin added around Frigate's own
  detected region, so the crop isn't razor-tight around the object.
- `crop_frame_offset_pct` (default `0.5`, in `profiles.yaml`) — *where* in the event's timespan to
  grab the frame (`0.0` = right at the start, `0.5` = midpoint, `1.0` = right at the end). There's
  no universally "correct" value — Frigate never exposes a timestamp for its own best-frame choice
  anywhere in its API (checked directly: not in the event JSON, not in the snapshot's response
  headers, not in EXIF — the only place it's visible at all is the camera's own burned-in
  on-screen clock, readable by eye but not extractable programmatically), so this is a starting
  point to tune against your own footage if `0.5` consistently looks off, not a value that can be
  computed or synced to Frigate's exact choice. This also drives the alert stage's own per-event
  high-res crops (see "Internal AI stages" below) — set `alert_crop_frame_offset_pct` instead if
  you want that stage's timing to differ from the events stage's own value for the same type.
- `crop_disabled` (default `false`, in `profiles.yaml`) — skips cropping entirely; the full
  original camera frame (still scaled to `max_crop_dimension`) is used instead of a region around
  the object. This is a real trade-off, not a strict improvement: a full wide frame gives more
  context but makes small detail (plates, notable features) harder for the VLM to read. The same
  image is what's displayed in the web UI *and* sent to the VLM — there's no separate "wide for
  humans, cropped for the model" mode. Only applies for events when `frigate_snapshot_enabled`
  below is `false`.
- `frigate_snapshot_enabled` (default **`true`**, in `profiles.yaml`) — for **events only**, uses
  Frigate's own already-rendered event snapshot instead of seeking+cropping a frame from the
  record-stream clip yourself. Frigate picks this frame by its own best-detection-score judgment,
  so in practice it beats the fixed-offset guess `crop_frame_offset_pct` makes often enough to be
  the default — accepted trade-off: Frigate's snapshot is from the lower-res detect stream
  (typically much smaller than your record stream) with a burned-in bounding-box/label/timestamp
  overlay this Frigate version's API gives no way to turn off (confirmed directly —
  `bbox=0`/`timestamp=0`/`h=` query params on the snapshot endpoint have no effect at all). Set to
  `false` to fall back to this project's original seek-based approach if that trade-off doesn't
  work for your footage — `crop_disabled`/`crop_frame_offset_pct`/`crop_padding_pct` only take
  effect once you do. The alert stage's own high-res per-event crops (see "Internal AI stages"
  below) always use the seek-based approach regardless of this setting, since a durable, high-res
  frame is what that stage needs — `FRIGATE_SNAPSHOT_ENABLED` only affects the events stage's own
  single-frame crop.

All four can be set globally via `profiles.yaml`'s `defaults:` section, or per object type, e.g. to
have `car` use a seek-based crop with extra padding for plate legibility while everything else
keeps using Frigate's own snapshot. See "Per-object-type overrides" below for how the tiers work.

## Camera allow-list

`CAMERAS` (optional, comma-separated, e.g. `outside,outside2`) — if set, only these cameras'
events/reviews are ever recorded at all; anything else Frigate reports is silently ignored at
ingest time. Leave unset (default) to process every camera Frigate has.

## Queue tuning

How aggressively `ingest-worker`'s own crop stage works through events — defaults are reasonable
starting points, not something you need to touch immediately. These are plain technical tuning
knobs with no per-object-type meaning (see "Per-object-type overrides" below) — set them in
`profiles.yaml`'s `defaults:` section, not `.env`:

- `parallel_limit` (default `2`) — how many events can be mid-crop at once.
- `stale_minutes` (default `5`) — how long a stuck claim (e.g. the service crashed mid-crop) sits
  before it's automatically retried.
- `max_attempts` (default `3`) — how many failures before an event is given up on (marked
  `failed`, not retried further).
- `poll_interval_seconds` (default `5`) — how often the crop poll loop checks for new work.

## Video storage

Two **independent** switches, both configured in `profiles.yaml` (not `.env` — see "Per-object-type
overrides" below), each defaulting to `false` (off) unless set in `profiles.yaml`'s `defaults:`
section or per type:

- `store_video` — downloads and keeps the clip for every individual event, alongside its crop.
  Stored under `VIDEO_STORAGE_HOST_PATH` (default `./video-storage` on the host).
- `store_video_alerts` — same idea, but one clip per *visit* (a whole grouped real-world activity)
  instead of per raw event. Stored completely separately, under `VIDEO_STORAGE_ALERTS_HOST_PATH`
  (default `./video-storage-alerts`), so you can measure/manage the two flows' disk usage
  independently.

Both share the same download-retry tuning (technical knobs in `profiles.yaml`'s `defaults:`, no
per-type meaning — see "Per-object-type overrides" below): `video_initial_wait_seconds`,
`video_min_valid_bytes`, `video_max_attempts`, `video_retry_wait_seconds`, `video_max_age_hours` —
the defaults account for Frigate needing a few seconds to finish writing a clip before it's
downloadable, and skip a clip that's very likely already rolled off Frigate's recording buffer
rather than retrying forever.

`store_video`/`store_video_alerts` can each be set globally via `defaults:`, or per object type —
e.g. skip storing clips for `person` while `car` still gets them. Setting either `true` for at
least one type is enough to start that stage's poll thread even if nothing else enables it (same
precedent the AI stages below use).

### Video storage layout

Clips are written under `{VIDEO_STORAGE_PATH}/{camera}/{YYYY}/{MM}/{DD}/{object_type}-{event_id}-
{start_ts_epoch}-{start_ts_iso}.mp4` (visit clips: `{VIDEO_STORAGE_PATH_ALERTS}/{camera}/{YYYY}/
{MM}/{DD}/visit-{object_type}-{visit_id}-...`) — the camera name is the top-level directory, so
you can `du -sh` one camera's own usage directly, or point a backup/retention tool at just one
camera's tree, without parsing filenames or querying Postgres. This is only the layout for files
written going forward: a clip stored before this existed sits directly under a `{YYYY}/{MM}/{DD}`
folder with no camera directory above it (today's original layout) — nothing migrates existing
files into the new structure automatically, matching this project's usual "no compatibility shim"
approach to layout changes. Only the on-disk path changed; the value stored in Postgres
(`raw_events.video_path`/`visits.video_path`) is always whatever `store_clip`/`store_visit_clip`
actually returned, so existing rows keep pointing at their real (pre-migration) location and
nothing needs to be re-pathed in the database.

The admin dashboard's "By camera" section (see [`web-ui.md`](web-ui.md#admin-dashboard)) walks
each camera's own top-level directory to report real on-disk video bytes — this only works
because of the layout above, so a pre-migration file (still under a bare year directory) won't be
attributed to a real camera name there; it'll show up bucketed under that year instead.

## Alert-analysis image storage

`store_alert_images` (default `false`, in `profiles.yaml` — same per-object-type override
mechanism as `store_video`/`store_video_alerts` above, see "Per-object-type overrides" below)
persists the alert stage's own gathered high-res crops (see "Internal AI stages" below) to disk
under `ALERT_IMAGES_STORAGE_HOST_PATH` (default `./alert-images` on the host) — same "only the
path lives in Postgres" shape video storage already uses. Off by default: these images stay purely
ephemeral (built, sent to the VLM, discarded) unless you opt in. Unlike `store_video`/
`store_video_alerts`, turning this on doesn't start a separate poll thread — it's a synchronous
step inside the existing alert-stage thread, so there's no extra queue/capacity tuning to configure.

Files land under `{ALERT_IMAGES_STORAGE_PATH}/{camera}/{YYYY}/{MM}/{DD}/visit-{object_type}-
{visit_id}-{index}-{event_id}.jpg` — the same camera-first layout video clips use, so the admin
dashboard's disk-usage/by-camera/by-object-type breakdowns (see
[`web-ui.md`](web-ui.md#admin-dashboard)) apply here with no extra code. A retried analysis
attempt overwrites the same files (deterministic names), so a backlog of retries doesn't pile up
duplicate images on disk.

Once stored, these images are reachable three ways: the web UI's Visit lightbox shows a small
thumbnail-strip gallery of exactly what the alert stage analyzed; `/reports/generate`'s optional
`include_alert_images` param embeds the same series into the alerts report; and
`/retention/purge`'s `delete_alert_images` flag (default on) clears them independently of video/
snapshots. See [`web-ui.md`](web-ui.md) and "Retention" below for each.

## Telegram notifications

Two more **independent** settings, each a *mode* (`none` / `image` / `video` / `all`), not a bool
— `none` by default, both configured in `profiles.yaml` (not `.env` — see "Per-object-type
overrides" below):

- `telegram_events_mode` — per-event notifications. `image` sends a photo right after cropping;
  `video` sends the clip once it's stored (`store_video`), standalone rather than threaded onto a
  photo that was never sent; `all` sends both (the video as a reply to the earlier photo).
- `telegram_alerts_mode` — per-*visit* notifications instead. `image` sends one summary message
  per visit immediately (the representative event's own crop as a photo, or text-only if that
  crop isn't ready yet); `video` sends the visit's own clip (`store_video_alerts`) as a reply to
  that summary; `all` sends both.

`image` and `video` are independent halves within each mode, not a ladder — setting `video` alone
does *not* also send the photo/summary; only `all` sends both.

To use either, you need a Telegram bot and your own chat ID (these two stay plain `.env` settings
— a bot token isn't something you'd ever want different per object type):

1. Message [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`, follow the prompts — it
   gives you a bot token. That's `TELEGRAM_BOT_TOKEN`.
2. Message your new bot anything once (so it can see your chat), then visit
   `https://api.telegram.org/bot<your-token>/getUpdates` in a browser — your numeric chat ID is in
   the JSON response under `message.chat.id`. That's `TELEGRAM_CHAT_ID`.

`telegram_events_mode` and `telegram_alerts_mode` can be set to any combination independently, and
both globally via `defaults:` or per object type (e.g. to silence a noisy low-priority type's
notifications without changing the mode for everything else) — this is deliberately a place to A/B
which granularity (and which of photo vs. video) is actually useful for your traffic rather than a
choice you're expected to get right upfront. See "Per-object-type overrides" below.

## Retention

Technical tuning knobs, no per-object-type meaning — set in `profiles.yaml`'s `defaults:` section,
not `.env` (see "Per-object-type overrides" below):

- `retention_months` (default `12`) — how long data (DB rows, and any stored video files) is kept
  before an automatic sweep deletes it.
- `retention_check_interval_seconds` (default `86400`, once a day) — how often that sweep runs.

`POST /retention/purge` (Swagger UI, or the checkboxes on `/ui/admin` — see
[`web-ui.md`](web-ui.md#admin-dashboard)) is a separate, ad-hoc counterpart if you want to purge on
a cutoff of your own choosing right now rather than waiting for or reconfiguring the scheduled
sweep — defaults to a dry run (just shows you counts) until you pass `confirm=true`. `only_media`
(default `true`) keeps every row and its AI analysis text/plate reads searchable forever; what it
actually clears is three independent flags, not one all-or-nothing "media" switch: `delete_video`
(default `true`), `delete_snapshots` (default `false`), `delete_alert_images` (default `true`,
`STORE_ALERT_IMAGES`'s own gathered high-res crops) — see `web-ui.md` for what each one maps to.
Set `only_media=false` for the original full-row delete instead (rebuilds the semantic search
index afterward); the three `delete_*` params are ignored entirely in that mode, since a full row
delete already covers all of their columns.

Two optional, composable scoping params (both also dropdowns on `/ui/admin`) restrict any of the
above to a subset of your data:

- `object_label` restricts to a single Frigate object type, e.g. clean up just `dog` events
  without touching everything else's retention. Only ever affects events/sightings of that type —
  visits (which can span multiple distinct object types in one row) are never touched by a
  type-scoped purge; omit `object_label` (the default) to keep covering visits too, same as before
  this param existed.
- `camera` restricts to a single Frigate camera. Unlike `object_label`, this **does** apply to
  visits/visit_sightings too — visit grouping is per-camera only, so a visit's own camera is
  always a single, unambiguous value, with none of `object_label`'s multi-type-per-visit ambiguity.

Both can be set at once (e.g. `object_label=car&camera=outside2`) to narrow to their intersection.

## Per-object-type overrides

A number of settings live entirely in `frigate/profiles.yaml`, not `.env` at all. Two categories:

**Per-object-type settings** — things you'd realistically want different per Frigate object type
(`car`, `truck`, `person`, `dog`, or any label you've added), resolved fresh for whatever row is
currently being processed:

- `telegram_events_mode` / `telegram_alerts_mode`
- `ai_events_stage_enabled` / `ai_alerts_enabled`
- `crop_disabled` / `crop_frame_offset_pct` / `crop_padding_pct` / `frigate_snapshot_enabled`
- `store_video` / `store_video_alerts` / `store_alert_images`
- `provider` / `model` / `chat_path` (event-stage VLM routing) and `alert_provider` /
  `alert_model` / `alert_chat_path` (alert-stage override — see "Hosted VLM providers" below)
- `alert_crop_frame_offset_pct` — alert-stage-only override of `crop_frame_offset_pct` (see
  "Crop tuning" below), falling back to the plain `crop_frame_offset_pct` value when unset

Two tiers, checked in this order:

1. That type's own `object_types.<label>` entry in `profiles.yaml` — highest priority.
2. A profile-wide `defaults` section (optional, sits alongside `object_types` in the same file) —
   applied to every type that doesn't set its own value for that key. Useful for "change this
   everywhere except one or two exceptions" instead of repeating the same override on every type.

**Plain technical tuning knobs** — queue parallel limits, retry counts, timeouts, poll intervals,
retention schedule, image-size caps. These have no per-object-type meaning at all (there's no
"`parallel_limit` for cars only"), so they can *only* be set in `defaults:`, resolved once at
startup rather than per-call:

- `parallel_limit` / `stale_minutes` / `max_attempts` / `crop_initial_wait_seconds` /
  `max_crop_dimension` / `thumbnail_max_dimension` / `poll_interval_seconds` (crop-stage queue tuning)
- `retention_months` / `retention_check_interval_seconds`
- `video_parallel_limit` / `video_initial_wait_seconds` / `video_min_valid_bytes` /
  `video_max_attempts` / `video_retry_wait_seconds` / `video_max_age_hours`
- `ai_stage_parallel_limit` / `ai_stage_stale_minutes` / `ai_stage_max_attempts` /
  `ai_stage_max_age_hours` / `ai_stage_poll_interval_seconds`
- `ai_stage_default_timeout_seconds` / `ai_stage_embed_timeout_seconds`
- `alert_ai_initial_wait_seconds` (default `5`) / `alert_ai_max_images` (default `4`) — alert-stage
  only, see "Internal AI stages" below.

For *either* category, if a key is set nowhere, `ingest-worker` falls back to a plain hardcoded
default in `config.py` (matching this project's original behavior) — there's no third `.env`-backed
tier here, unlike most other settings in this doc. An empty/missing `profiles.yaml` (or one with no
`defaults:` section and no per-type overrides) is a perfectly valid, fully-working configuration,
not a half-finished one.

```yaml
defaults:
  store_video: false        # off for everything...
  parallel_limit: 4         # a plain technical knob, defaults: is the only place it can go
object_types:
  car:
    store_video: true        # ...except cars
    crop_padding_pct: 0.3
  person:
    telegram_events_mode: none
```

`frigate/profiles.yaml.example`'s own comments have the full list with examples (including each
key's hardcoded fallback value); `profile_config.py` (per-object-type settings) and
`config.apply_profile_defaults` (the technical tuning knobs) are the actual resolver code if you
want the exact tie-break logic.

**Upgrading from an older version**: these settings used to be plain `.env` vars (`STORE_VIDEO`,
`TELEGRAM_EVENTS_MODE`, `AI_EVENTS_STAGE_ENABLED`, `PARALLEL_LIMIT`, `RETENTION_MONTHS`,
`AI_STAGE_MAX_ATTEMPTS`, etc.) — some grew a per-type-override capability in `profiles.yaml` on top
first, all of them ended up here eventually. That env-var tier is gone now — if your `.env`
currently sets any of these, copy the equivalent value into `profiles.yaml`'s `defaults:` section
*before* upgrading, or the setting silently reverts to its hardcoded default (`docker-compose.yml`
no longer even passes the old env var through, so it's not an error, just ignored).

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
`embedding` column either way; it just stays empty until something (the internal AI stage below, or
a custom n8n workflow) actually sends one via `POST /sightings`. `POST
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

**`POST /search`** is the web UI's own entry point (the "Search" tab — see
[`web-ui.md`](web-ui.md)) — unlike `POST /search/semantic` above, it takes plain query text
instead of a pre-computed embedding, since a browser can't call the embedding backend directly.
`ingest-worker` embeds the text server-side (same backend/model as everything else here) and ranks
across **both** `sightings` and `visit_sightings` together (or just one, via an optional `source`
param the UI itself doesn't expose). Same `LLAMA_PROXY_BASE_URL`/`LLAMA_PROXY_EMBED_PATH`
requirement as the backfill endpoint above — a 502 response means that backend is unreachable or
misconfigured, surfaced in the UI as an error banner rather than a silent empty result.

## Internal AI stages

Two independent stages, both configured in `profiles.yaml` (not `.env` — see "Per-object-type
overrides" below) and off by default unless enabled there — nothing analyzes events with a VLM at
all until you turn at least one of these on (there's no n8n workflow shipped for this anymore, see
[`n8n.md`](n8n.md)):

- **`ai_events_stage_enabled`** — analyzes each event's own single-frame crop with
  `profiles.yaml`'s `event_prompt`. If you ever build your own n8n workflow against the same
  `/ai-queue/claim` endpoint, don't run it alongside this at once against the same queue (safe
  either way — `FOR UPDATE SKIP LOCKED` prevents a double-claim — just wasteful/confusing).
- **`ai_alerts_enabled`** — analyzes a visit with `profiles.yaml`'s `alert_prompt`, storing results
  separately in `visit_sightings`. Rather than one low-res frame, this stage gathers up to
  `alert_ai_max_images` (default `4`) genuinely high-resolution crops — one per distinct object
  type the visit's linked events cover, then temporally-spread re-tracks to fill any remaining
  slots — built fresh from the record stream at analysis time and discarded right after by
  default, never stored (unless `store_alert_images` opts in — see "Alert-analysis image storage"
  above). No readiness gate to wait on: a visit is claimable as soon as it exists. Can run
  alongside or instead of `ai_events_stage_enabled` — the two are fully independent queues.

Both can be set globally via `profiles.yaml`'s `defaults:` section, or per object type — e.g. to
run the events stage for `car`/`person` only while `dog` sits out, or to enable a stage for just
one type even while everything else stays off. Setting either `true` for at least one type is
enough to start that stage's poll thread — the thread then only claims the type(s) that resolve to
enabled, never every mapped type unconditionally. See "Per-object-type overrides" below.

- Object types + prompts + per-type model slot/timeout live in **`frigate/profiles.yaml`** (repo
  root, alongside `docker-compose.yml`), not env vars — that's genuinely a lot of config to cram
  into `.env` readably. `docker-compose.yml` already bind-mounts this file into the container, so
  just edit it and restart `ingest-worker` — no rebuild needed. (`AI_STAGE_PROFILE_PATH`, default
  `/app/profiles.yaml`, is the path the bind mount lands on; you'd only touch this env var if you
  wanted to point at a differently-named file instead.) This is a flat map — every Frigate object
  label (`car`, `truck`, `person`, or any label you add, e.g. `dog`) gets its own entry with two
  prompts: `event_prompt` (single static frame) and `alert_prompt` (a series of separate high-res
  photos, framed to also describe what changed across them, not just static attributes). Both
  prompts are answered as plain free text — there is no JSON schema or per-field response format,
  so adding a brand-new object type is purely a `profiles.yaml` edit, never a code change. Labels
  that should share one model/prompt (e.g. `car` and `truck`) can point at the same YAML anchor
  instead of duplicating the block. A Frigate object label with no entry in this file is simply
  never analyzed by either stage.
- `ai_stage_parallel_limit`/`ai_stage_stale_minutes`/`ai_stage_max_attempts`/
  `ai_stage_max_age_hours`/`ai_stage_poll_interval_seconds` — same queue-tuning shape as the crop
  stage above, shared between both stages (each claims from its own separate queue, so this
  doesn't mean they compete for capacity). Plain technical knobs, `profiles.yaml`'s `defaults:`
  only (see "Per-object-type overrides" above), not env vars.
- `LLAMA_PROXY_BASE_URL` (required once either stage is enabled) — your
  [`llama_slot_proxy`](https://github.com/shuricksumy/llama-slot-proxy)'s own base URL, called
  directly instead of going through n8n. `LLAMA_PROXY_TOKEN` is optional (blank = no
  `Authorization` header — `llama_slot_proxy` is unauthenticated on the LAN in most setups today).
  `LLAMA_PROXY_EMBED_PATH` is the embedding model's own URL path segment (same one-path-per-slot
  convention `profiles.yaml`'s `chat_path` uses). All three stay plain `.env` settings (connection
  info, not tunable behavior).
- `EMBEDDING_DIMENSIONS` (default `1024`) — must match the output size of whatever model is loaded
  behind `LLAMA_PROXY_EMBED_PATH` (e.g. `1024` for Qwen3-Embedding-0.6B-GGUF, `768` for
  nomic-embed-text-v1.5). Sizes the pgvector `embedding` columns on `sightings`/
  `visit_sightings`. Changing this after sightings already have embeddings stored clears them (a
  different model's vectors are an incomparable vector space regardless of dimension) — re-run
  `POST /embeddings/backfill?confirm=true` afterwards. Stays a plain `.env` setting even though it's
  arguably "technical" — `db.ensure_schema()` reads it before `profiles.yaml` is even loaded, and
  changing it has real DB-migration implications, unlike a queue timeout.
- `ai_stage_default_timeout_seconds`/`ai_stage_embed_timeout_seconds` (defaults `180`/`60`) —
  fallback timeouts; the real per-type chat timeout belongs in `profiles.yaml` itself
  (`timeout_seconds`), since a local model's response time genuinely depends on which model/prompt
  you've picked for that type. Plain technical knobs, `profiles.yaml`'s `defaults:` only.

## Hosted VLM providers (OpenAI / Claude)

Both internal AI stages default to calling a **locally-hosted** model through `LLAMA_PROXY_BASE_URL`
(e.g. `llama_slot_proxy`) — the "no cloud calls" behavior this project started with. You can
instead route individual object types to a hosted provider (OpenAI or Anthropic/Claude) by adding
two keys to that type's `profiles.yaml` entry, alongside (or instead of) `chat_path`:

```yaml
object_types:
  car:
    provider: llama_proxy    # event stage stays local -- multi-image not needed for one frame
    chat_path: /video/v1/chat/completions
    alert_provider: openai   # alert stage (the multi-image series) routes to a hosted provider
    alert_model: gpt-4o      # or e.g. claude-opus-4-8 for anthropic
    # max_tokens: 1024       # anthropic only, optional -- shared across both stages, see below
    event_prompt: >-
      ...
    alert_prompt: >-
      ...
```

This is a **per-object-type** choice, exactly like `chat_path` already is — one type can stay on
your local model while another routes to a hosted one, in the same file. A type with no `provider`
key behaves exactly as before (`llama_proxy`, selected via `chat_path`); nothing changes for an
existing deployment that never sets this.

**The alert stage can route to a different provider than the event stage, for the same type** —
`alert_provider`/`alert_model`/`alert_chat_path` are optional overrides checked only by
`alert_ai_worker`, falling back to the plain `provider`/`model`/`chat_path` when omitted. This
matters specifically because the alert stage sends *several* high-res images in one call (see
"Internal AI stages" above) while the event stage only ever sends one — `llama_proxy` only ever
uses the first image handed to it and logs a warning once if given more (multi-image support on a
self-hosted backend is unverified), so a type wanting genuine multi-image reasoning for its alert
analysis needs `alert_provider: openai` or `alert_provider: anthropic` specifically — both natively
support multiple images in one message. A type that never sets any `alert_*` key behaves
identically for both stages, same "omitting the key preserves old behavior" convention `provider`
itself established.

| `provider` | Needs in `.env` | Needs in `profiles.yaml` (per type) |
|---|---|---|
| `llama_proxy` (default) | `LLAMA_PROXY_BASE_URL` (+ optional `LLAMA_PROXY_TOKEN`) | `chat_path` |
| `openai` | `OPENAI_API_KEY` | `model` (e.g. `gpt-4o`) |
| `anthropic` | `ANTHROPIC_API_KEY` | `model` (e.g. `claude-opus-4-8`), optional `max_tokens` (default `1024`, via `AI_STAGE_DEFAULT_MAX_TOKENS`) |

`chat_path` is only read for `llama_proxy`; `model`/`max_tokens` are only read for `openai`/
`anthropic`. Mixing them on the same entry is harmless (the unused one is simply ignored) but only
set the ones your chosen provider actually needs — see `frigate/profiles.yaml.example`'s `car`
entry for both hosted shapes written out in full.

**Embeddings (semantic search) are configured separately, and never follow `provider` above** —
`EMBEDDING_PROVIDER` (`.env`, `llama_proxy` default or `openai`) applies globally, not per object
type. This is a hard constraint, not a design choice: Claude has no embeddings API at all, so a
type routed to `provider: anthropic` for its description still needs `EMBEDDING_PROVIDER` set to
`llama_proxy` (default) or `openai` for that same sighting's embedding vector to get computed at
all. Switching `EMBEDDING_PROVIDER` to `openai` also means setting `EMBEDDING_DIMENSIONS=1536`
(OpenAI's `text-embedding-3-small`, the default `OPENAI_EMBED_MODEL`) and re-running
`POST /embeddings/backfill?confirm=true` — same migration dance described under
`EMBEDDING_DIMENSIONS` above, since a different model's vectors are an incomparable vector space
regardless of dimension.

**Cost and privacy, briefly:** a hosted provider means that type's cropped images (and, for the
alerts stage, the whole gathered series of several high-res crops per visit) leave your network on
every analyzed sighting, billed per request (and per image, for the alert stage) — worth weighing
against `llama_slot_proxy`'s one-time hardware cost and zero marginal cost per sighting. A common
middle ground is routing only your highest-value type (e.g. `car`, for plate/make/model accuracy)
to a hosted provider while everything else stays local.

### Which model should I actually use?

There's no single right answer — it depends on what you're optimizing for. Some starting points,
based on what each provider is actually good at for this project's kind of task (a single cropped
photo for the event stage, or several separate high-res photos in one call for the alert stage,
answered as one or two free-text sentences):

- **Staying local (`llama_proxy`, the default)** — zero marginal cost, zero data leaving your
  network, and genuinely adequate quality for most of this project's prompts (color/body-type/
  plate/clothing description is a much easier task than open-ended reasoning). The trade-off is
  hardware: plate-text legibility and make/model identification noticeably improve with a larger
  local model, which needs more VRAM/compute than a small one. Start here if privacy or ongoing
  cost matters more than squeezing out the last bit of accuracy — it's what this project is
  designed around, and every other section of this doc assumes it.
- **OpenAI (`gpt-4o` or a newer GPT-4-class vision model)** — a reasonable middle ground: cheaper
  per-request than Claude's higher-tier models, fast, and its vision quality on the kind of crops
  this project sends (a single vehicle/person in frame, decent resolution) is solid for color/
  body-type/clothing description. Plate-text OCR accuracy varies more than a dedicated OCR model
  would give you — Frigate's own LPR read (`raw_events.sub_label`) is still captured on every row
  regardless of what the VLM says, specifically as a cross-check for exactly this reason.
- **Claude (`claude-opus-4-8` or `claude-sonnet-5`)** — the more capable option for the *alert*
  prompt specifically (`alert_prompt`, the visit-level "what changed across this series of photos"
  task, natively multi-image) — reading fine detail across several separate high-res photos and
  reasoning about what changed between them is closer to genuine visual reasoning than the
  single-frame `event_prompt` case, and that's where a stronger model's accuracy shows up most.
  `claude-sonnet-5` is the cheaper, faster choice if `claude-opus-4-8`'s cost/latency isn't worth it
  for your volume — both are meaningfully more expensive per request than OpenAI's `gpt-4o` tier or
  a local model's zero marginal cost.

**A practical split**, if you want to try hosted providers without committing everything to one:
route `car`/`truck`'s `event_prompt` (single-frame, plate-legibility-sensitive) to whichever local
or hosted model reads plates best in your own testing, and leave `person`/`dog` on a cheaper/local
model, since clothing-color/breed description doesn't benefit as much from a stronger model. Since
this is all per-object-type in `profiles.yaml`, testing a combination costs nothing but editing the
file and restarting the container — no code change, no redeploy of a different image.
