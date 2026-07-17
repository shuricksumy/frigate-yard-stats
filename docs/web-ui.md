# The web report UI, a tour

`ingest-worker` serves a small static web page at `http://<host>:8080/ui` — no separate service,
no build step (plain HTML/CSS + [Alpine.js](https://alpinejs.dev), vendored locally so nothing
loads from a CDN). It reads the exact same API n8n and everything else uses; it isn't a special
privileged view into the database.

## Logging in

The first time you open `/ui`, it asks for your API key (the same `API_KEY` value from `.env`).
It validates that key against the API once, then stores it in a cookie so you don't need to
re-enter it — "Change API key" in the header logs out and clears that cookie.

## Events vs Visits

A toggle at the top switches the whole page between two views of the same underlying data:

- **Events** — one card per Frigate detection (`raw_events`), the most granular view.
- **Visits** — one card per Frigate review/alert (`visits`) — multiple detections Frigate's own
  tracker considers the same real-world activity (occlusion, re-ID, label flicker) collapsed into
  one card, with an "N events grouped" badge when it bundled more than one.

Switching views resets the filter bar back to defaults — a filter that only makes sense in one
view (see below) doesn't silently keep applying once you can't see it anymore.

## Filtering

The simple filter bar has:

- **Search AI analysis** — free-text search across whatever the VLM wrote (color, plate, notable
  features, description...) for any already-analyzed sighting. Works in both views.
- **Time range** — a quick preset (last 1/3/6/12/24 hours).

**Advanced filters** (toggle to reveal) adds From/To date pickers (override the Time range preset
when set), Type (object label), and — Events view only — Event ID, AI status, and "Only with
media" (checked by default: hides rows that don't have an image or video yet, since there's
nothing to show for them).

Every filter except the two free-text boxes (Search, Event ID) applies the instant you change it
— no separate "Search" click needed for a dropdown or date picker.

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

Below the media, once AI analysis has finished (`ai_status: done`), you'll see the actual
extracted fields — color/body type/make/model/notable features/plate for a vehicle, a short
description for a person. A visit that grouped both a vehicle and a person shows both, labeled
separately, rather than picking just one.

A download button next to the close button grabs whichever of video/image is currently on screen.

## What the badges mean

- **`ai: <status>`** — `new` (not analyzed yet), `processing` (an n8n run has claimed it right
  now), `retry` (a previous attempt didn't finish cleanly, will be picked up again), `failed`
  (gave up after repeated errors), `done` (a sighting exists — click the card to see it).
- **`video`** — this row has a stored clip available.
- **`N events grouped`** (Visits view only) — how many individual detections Frigate's tracker
  bundled into this one visit.

## Paging

Prev/Next buttons below the grid step through results; the label between them shows
`<page> / <total pages>` (e.g. `2 / 5`), computed from the total row count matching your current
filters — not just "there might be more data" from a full page of results.

## Auto-refresh

The checkbox next to the Search button keeps the current page's data refreshing on its own,
without you needing to hit Search repeatedly while watching activity come in live.
