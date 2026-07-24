# The web report UI, a tour

`ingest-worker` serves a small static web page at `http://<host>:8080/ui` — no separate service,
no build step (plain HTML/CSS + [Alpine.js](https://alpinejs.dev), vendored locally so nothing
loads from a CDN). It reads the exact same API n8n and everything else uses; it isn't a special
privileged view into the database.

## Logging in

The first time you open `/ui`, it asks for your API key (the same `API_KEY` value from `.env`).
It validates that key against the API once, then stores it in a cookie so you don't need to
re-enter it — "Change API key" in the header logs out and clears that cookie.

## Events, Visits, and Search

A toggle at the top switches the whole page between three views:

- **Events** — one card per Frigate detection (`raw_events`), the most granular view.
- **Visits** — one card per Frigate review/alert (`visits`) — multiple detections Frigate's own
  tracker considers the same real-world activity (occlusion, re-ID, label flicker) collapsed into
  one card, with an "N events grouped" badge when it bundled more than one.
- **Search** — ask a free-text question ("red pickup truck backing into the driveway") and get
  back a ranked grid of the most semantically similar sightings, across both events and visits at
  once. See "Search" below.

Switching views resets the filter bar back to defaults — a filter that only makes sense in one
view (see below) doesn't silently keep applying once you can't see it anymore.

## Filtering

The simple filter bar has:

- **Search AI analysis** — free-text search across whatever the VLM wrote (color, plate, notable
  features, description...) for any already-analyzed sighting. Works in both views.
- **Time range** — a quick preset (last 1/3/6/12/24 hours).
- **Camera** — populated live from whatever cameras actually have data, not a fixed config list.

**Advanced filters** (toggle to reveal) adds From/To date pickers (override the Time range preset
when set), Type (object label), and — Events view only — AI status and "Only with media" (checked
by default: hides rows that don't have an image or video yet, since there's nothing to show for
them).

Every filter except the free-text Search box applies the instant you change it — no separate
"Search" click needed for a dropdown or date picker.

## Search

The Search tab reuses the same "Search AI analysis" text box — its label changes to "Ask about
your yard" while this tab is active — but instead of an exact substring match, it embeds your
query text server-side (`POST /search`, via `ingest-worker`'s configured embedding backend) and
ranks sightings by semantic similarity (cosine distance), same idea as the `semantic_search`
tool the n8n Q&A agent already uses, just reachable directly from the browser with no agent in
the loop. Time range/Camera/From-To/Type still apply exactly as they do on the other two tabs;
AI status/"Only with media" don't apply here (hidden, same as on the Visits tab) since a
search result already implies AI analysis exists.

Results are a flat, ranked grid — most relevant first, no "page 2" concept — spanning both
per-event sightings and per-visit alert-stage sightings together (there's no separate toggle for
"just events" or "just visits" in the UI). Click a result to open the exact same lightbox the
Events/Visits tabs use, whether it's an event or a visit under the hood. If the embedding backend
is unreachable or misconfigured, an error banner explains why instead of silently showing an empty
grid. Each card shows a rough **match %** badge (hover for the exact cosine distance) — a
human-friendly stand-in for how confident the match is, not a calibrated probability.

A query with fewer genuinely relevant sightings than the page size can otherwise pad itself out
with weak, barely-related filler once it runs out of real matches. The **Precision** dropdown
(simple view) controls a relevance cutoff: **High precision** (default) drops anything past a
fairly strict distance threshold, **Balanced** is more lenient, and **Show everything** disables
the cutoff entirely (today's original behavior). A cutoff never hides a sighting that literally
contains your query word, even past the threshold — so searching "dog" still surfaces a mostly
unrelated sentence that happens to mention a dog in passing. Advanced mode swaps the dropdown for
a **Precision (exact)** number field if you want to dial in the exact cutoff value yourself.

## Opening a card

Click any card with media to open the lightbox. If more than one artifact is available for that
row, toggle buttons switch between them:

- **Preview** — a visit's animated GIF (4 sampled moments played as a slideshow) — only shown for
  visits, and only once `VISIT_THUMB_CROP_ENABLED`'s preview has actually finished building.
- **Video** — the stored clip, if `STORE_VIDEO`/`STORE_VIDEO_ALERTS` downloaded one — full
  scrubber support (drag to any point), since it's served with range-request support.
- **Image** — the still crop (or, for a visit, the composite 4-frame grid — same image that's
  actually sent to the VLM).

Whichever is richest and already available opens by default (Preview, then Video, then Image) —
the toggle buttons only appear when there's actually more than one to switch between.

Below the media, once AI analysis has finished, you'll see the AI's description as a single line of
plain text (whatever the VLM said in response to that object type's prompt — color/body
type/plate for a car, clothing/activity for a person, or anything at all for any other label you've
configured — there's no per-field table, just the model's own words). On the Events tab this is
always the event's own single-frame analysis
(`AI_EVENTS_STAGE_ENABLED`). On the Visits tab, it prefers that visit's own alert-stage analysis
of the composite grid instead (`AI_ALERTS_ENABLED`, labeled "... (alert analysis)") — a richer
result that also describes what changed across the visit, not just a static snapshot — falling
back to the per-event analysis if the alert stage is off or hasn't finished that visit yet. A
visit that grouped several distinct object types (e.g. a car and a person) shows each one's
sighting (per-event fallback only), labeled separately, rather than picking just one.

On the Visits tab specifically, below that a "Connected events" strip shows every individual
det_id Frigate's own tracker grouped into that visit (not just the deduped sighting(s) above) —
small thumbnails in chronological order, each clickable to jump straight into that specific
event's own lightbox.

A download button next to the close button grabs whichever of video/image is currently on screen.

## What the badges mean

- **`ai: <status>`** — `new` (not analyzed yet), `processing` (an n8n run has claimed it right
  now), `retry` (a previous attempt didn't finish cleanly, will be picked up again), `failed`
  (gave up after repeated errors), `done` (a sighting exists — click the card to see it), `skipped`
  (this event never had a snapshot to crop in the first place — Frigate detected it but never
  persisted a real event for it, so there's nothing to analyze regardless of how long you wait).
- **`video`** — this row has a stored clip available.
- **`N events grouped`** (Visits view only) — how many individual detections Frigate's tracker
  bundled into this one visit.

## Paging

Prev/Next buttons below the grid step through results; the label between them shows
`<page> / <total pages>` (e.g. `2 / 5`), computed from the total row count matching your current
filters — not just "there might be more data" from a full page of results. The Search tab has no
pager — it's a fixed-size ranked top-N grid, not a browsable list.

## Auto-refresh

The checkbox next to the Search button keeps the current page's data refreshing on its own,
without you needing to hit Search repeatedly while watching activity come in live.

## Admin dashboard

A separate page at `/ui/admin` (linked from the main report UI's header) for operational
health/maintenance rather than browsing sightings — same login (the same API key/cookie works on
both pages). It shows:

- **Health** — feature flags currently on (AI stage, video storage, Telegram modes, etc.), pgvector
  extension/index status, and an on-demand "Check now" button that live-tests your embedding
  backend (`LLAMA_PROXY_EMBED_PATH`) and reports whether it's reachable and returning the right
  vector size. This flags summary only ever reflects `ingest-worker`'s hardcoded fallback defaults
  — it doesn't parse `profiles.yaml` at all, so it won't show a `defaults:` section value or any
  per-object-type override (see "Per-object-type overrides" in [`configuration.md`](configuration.md))
  even though it's actually in effect for that type. For AI stage/video storage/Telegram/crop
  settings specifically, treat this summary as unreliable — check `profiles.yaml` directly instead.
  The "By object type" row counts below do reflect whatever actually happened, since those come
  from real data, not the static flag summary.
- **Counts** — total events, visits, sightings (any object type), and retention info (how many months
  you're keeping, and the oldest event still in the database).
- **By object type** — one row per Frigate object label (car/truck/person/dog/...) showing its own
  event/sighting row counts, an approximate Postgres byte footprint, and real on-disk video bytes
  (parsed from stored clip filenames, which always start with the object type). Lets you see at a
  glance which type is actually driving disk/DB growth instead of only a pipeline-wide total.
- **Semantic search coverage** — how many sightings have an embedding vs. don't, with buttons to
  backfill missing ones or reindex the vector database.
- **Queue health** — a status breakdown (new/processing/retry/failed/done) for every queue stage
  (crop/video/AI on events, video/preview on visits). Any stage with failed rows gets a "Requeue N
  failed" button — the same fix `frigate/sql/queue-debug.sql` documents for manual psql use, now a
  real button instead of requiring shell access.
- **Storage** — disk usage for stored video (main and alerts), plus Postgres database size broken
  down per table.
- **Retention purge** — pick a cutoff in days, then hit Preview to see exactly what would happen,
  and Delete/Clear now, which asks for an explicit confirmation spelling out those same numbers
  before anything actually changes. Nothing happens from a single click. A "Media only" checkbox
  (on by default) controls what "purge" actually means:
  - **Checked (default)** — keeps every row and all its AI analysis text/plate reads searchable
    forever; only deletes the stored video files and clears the stored crop images/preview GIFs
    for anything older than the cutoff. Use this to reclaim disk/database space while keeping your
    full history searchable.
  - **Unchecked** — deletes the matching events/visits (and their sightings) entirely, the
    original full purge, then rebuilds the semantic search index against whatever remains.

  An "Object type" dropdown (defaults to "All types") restricts either mode to one Frigate label
  at a time -- e.g. clean up just `dog` events without touching everything else's retention. Only
  ever affects events/sightings of that type: visits (which can span multiple distinct object
  types in one row) are never touched by a type-scoped purge, so leave "All types" selected to
  also cover those.
- **Reports** — generate a report on demand (Events or Visits/alerts, any object type or all,
  a time window, and the same GIF/image/none preview modes `/reports/generate` accepts) and open
  it in a new tab -- the exact same HTML n8n's scheduled report workflows email/Telegram, without
  waiting for the next scheduled run.
