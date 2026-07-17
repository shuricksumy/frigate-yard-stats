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
  model" mode.

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

Two more **independent** switches, both off by default:

- `TELEGRAM_EVENTS_ENABLED` — a photo per event (right after cropping), followed by a video reply
  once the clip is stored (if `STORE_VIDEO` is also on).
- `TELEGRAM_ALERTS_ENABLED` — one summary message per *visit* instead (photo/GIF once the preview
  is ready, or text-only immediately if `VISIT_THUMB_CROP_ENABLED` is off) — a reply video follows
  if `STORE_VIDEO_ALERTS` is also on.

To use either, you need a Telegram bot and your own chat ID:

1. Message [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`, follow the prompts — it
   gives you a bot token. That's `TELEGRAM_BOT_TOKEN`.
2. Message your new bot anything once (so it can see your chat), then visit
   `https://api.telegram.org/bot<your-token>/getUpdates` in a browser — your numeric chat ID is in
   the JSON response under `message.chat.id`. That's `TELEGRAM_CHAT_ID`.

Both event- and alert-level notifications can be on at once, either alone, or neither — this is
deliberately a place to A/B which granularity is actually useful for your traffic rather than a
choice you're expected to get right upfront.

## Retention

- `RETENTION_MONTHS` (default `12`) — how long data (DB rows, and any stored video files) is kept
  before an automatic sweep deletes it.
- `RETENTION_CHECK_INTERVAL_SECONDS` (default `86400`, once a day) — how often that sweep runs.

`POST /retention/purge` (Swagger UI) is a separate, ad-hoc counterpart if you want to purge on a
cutoff of your own choosing right now rather than waiting for or reconfiguring the scheduled sweep
— defaults to a dry run (just shows you counts) until you pass `confirm=true`.

## Web UI

`OBJECT_TYPES` (default `car,truck,person,dog`) — the labels your own Frigate config actually
tracks, so the web UI's Type filter dropdown matches reality. Add a label here (matching what you
added to `frigate.conf`'s `objects.track`) and it appears in the dropdown on next restart, no code
change needed. See [`web-ui.md`](web-ui.md) for a tour of the UI itself.
