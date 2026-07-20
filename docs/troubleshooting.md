# Troubleshooting

A first-things-first checklist for the most common "it's not doing what I expected" situations,
plus how to look under the hood when you need more than this page covers.

## Your two best tools before anything else

```bash
# What is ingest-worker actually doing / logging right now?
docker compose --profile pipeline logs -f ingest-worker

# Is the pipeline even seeing events, and are the three queue stages moving?
curl http://<host>:8080/status
```

`/status` (also linked from Swagger at `/docs`) gives a quick breakdown of how many rows are in
each queue state — if everything's stuck on `new` across the board, the issue is upstream
(MQTT/Frigate); if things are stuck on `processing` or `retry`/`failed`, the issue is in
`ingest-worker` itself or whatever it's calling out to (Frigate's API, the VLM, Telegram).

`frigate/sql/queue-debug.sql` has ready-made queries for a closer look (status breakdowns,
what's in flight, recently failed, force-retry) against both `raw_events` and `visits` — run these
directly against Postgres (`docker compose --profile pipeline exec postgres-projects psql -U
n8n_projects -d home_automation`) when the API-level view isn't enough.

## No events showing up at all

- Confirm Frigate is actually publishing: subscribe to the topic directly
  (`mosquitto_sub -h <broker> -t 'frigate/events'`) and trigger a real detection — if nothing
  arrives here, the problem is Frigate/MQTT, not this project.
- Check `MQTT_HOST`/`MQTT_USERNAME`/`MQTT_PASSWORD` in `.env` match your actual broker, and that
  `ingest-worker`'s logs show a successful MQTT connection on startup, not repeated reconnect
  attempts.
- If you set `CAMERAS` in `.env`, double check the camera name matches Frigate's own name for it
  exactly (case-sensitive) — a camera not on that list is silently never ingested at all, not
  hidden from some later view.

## Events show up but never get a crop image

- Check `/status` or `queue-debug.sql`'s "what's in flight" query — if rows are stuck on
  `crop_status = 'retry'` or `'failed'`, check the logs around that time for the actual error
  (usually a Frigate API call failing).
- Confirm `FRIGATE_API_BASE` is reachable *from the ingest-worker container*, not just from your
  own machine — `docker compose --profile pipeline exec ingest-worker curl -s
  $FRIGATE_API_BASE/api/version` is a quick check.
- A row stuck on `crop_status = 'skipped'` forever is usually correct, not a bug — Frigate can emit
  a full event lifecycle for an object it never actually persisted a snapshot for; there's nothing
  to crop for these no matter how long you wait.
- If crops look wrong for just *one* object type (full frame instead of cropped, or vice versa;
  wrong offset/framing) while others look fine, check that type's own entry in `profiles.yaml` —
  `crop_disabled`/`crop_frame_offset_pct`/`crop_padding_pct`/`frigate_snapshot_enabled` can all be
  overridden per type there, so a type-specific override (or a profile-wide `defaults:` entry) can
  be the actual cause even when the `.env` values look right.

## Events are cropped but never analyzed (`ai_status` stuck on `new`)

This stage is owned by n8n, not `ingest-worker` — see [`n8n.md`](n8n.md):

- Is the Metadata Processor workflow actually **Active** (not just imported)?
- Open its last few executions in n8n and check for errors on the **Claim Next Batch (API)** node
  first (wrong `INGEST_WORKER_HOST`/`PORT`, or a missing/wrong `X-API-Key`) — if that node succeeds
  but returns an empty list every time, nothing is eligible yet (still cropping) or your
  `max_age_hours` query param is excluding a backlog that's older than expected.
- If that node succeeds but a later VLM call node fails, the issue is your VLM endpoint
  (`REPLACE_WITH_VLM_HOST`/`PORT`), not this project.
- If you're using the internal AI stage instead (`AI_EVENTS_STAGE_ENABLED`/`AI_ALERTS_ENABLED`) and
  one object type never gets analyzed while others do, check that type's `profiles.yaml` entry for
  an `ai_events_stage_enabled`/`ai_alerts_enabled` override — or that it has an entry in
  `profiles.yaml` at all; a label with no entry is never claimed by either stage, by design.

## Video never gets stored

- Confirm `STORE_VIDEO` (per-event) or `STORE_VIDEO_ALERTS` (per-visit) is actually `true` in
  `.env`, and that you restarted the container after changing it.
- If it's only missing for one object type, check that type's `profiles.yaml` entry (and any
  profile-wide `defaults:` section) for a `store_video`/`store_video_alerts` override — either can
  disable it for that one type even while the `.env` default is `true`.
- Check the relevant bind-mount directory actually exists and is writable
  (`VIDEO_STORAGE_HOST_PATH`/`VIDEO_STORAGE_ALERTS_HOST_PATH` on the host).
- A row permanently stuck on `video_status = 'failed'` after using up its retry attempts is often
  Frigate's recording buffer having already rolled the clip off before `ingest-worker` got to it —
  see [`frigate.md`](frigate.md)'s retention section. Raising `record.continuous.days` (per-camera
  in `frigate.conf`) or lowering `VIDEO_MAX_AGE_HOURS` (so a backlogged row gives up sooner instead
  of burning attempts on a clip that's already gone) are the two real levers here.

## Visit preview (grid/GIF) keeps failing

Same underlying cause as the video case above, more often — this feature asks Frigate for an
arbitrary time range, which is exactly the request pattern that's sensitive to your
`record.continuous.days` setting. See [`frigate.md`](frigate.md#recording-retention--this-genuinely-matters-not-just-a-tuning-knob)
for the full explanation and `CLAUDE.md`'s "Visit preview" section for the production history
behind this feature's design. If it's failing consistently rather than occasionally, that setting
is almost always why.

## Telegram messages never arrive

- Confirm `TELEGRAM_EVENTS_MODE`/`TELEGRAM_ALERTS_MODE` (whichever you expect) is set to `image`,
  `video`, or `all` — not left at the default `none` — and that it actually covers what you're
  waiting for (`video` alone does not also send the photo/summary; only `all` sends both). Also
  confirm `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are both real values, not the `changeme`
  placeholder.
- If it's silent for just one object type, check that type's `profiles.yaml` entry (and any
  profile-wide `defaults:` section) for a `telegram_events_mode`/`telegram_alerts_mode` override —
  a type can be silenced (or enabled) independently of the global `.env` mode.
- Message your bot at least once first — Telegram bots can't message a chat that's never
  messaged them (see [`configuration.md`](configuration.md#telegram-notifications) for the
  bot-creation steps).
- A Telegram failure never blocks or fails the crop/video/preview pipeline itself by design — check
  `ingest-worker`'s logs for a warning around the time you expected a message, rather than assuming
  the whole pipeline is broken because a notification didn't show up.

## Starting over

If you want to wipe everything and let `ingest-worker` rebuild its schema from scratch (e.g. after
significant testing, or moving to a fresh production instance):

```bash
cd frigate
docker compose --profile pipeline down
rm -rf ./postgres-projects   # this is a bind-mounted host directory, not a named Docker volume
docker compose --profile pipeline up -d
```

`ingest-worker` applies `schema.sql` on every startup (idempotent — safe to run against either a
brand new or an already-populated database), so a completely empty Postgres just gets initialized
fresh with no extra steps. This is destructive — it deletes all recorded history, not just queue
state — so only do this on data you're fine losing.
