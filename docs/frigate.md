# Frigate configuration, explained for this project

This project doesn't replace or modify Frigate — it listens to what Frigate already reports over
MQTT and pulls clips/crops from Frigate's own REST API. This page explains the parts of
`frigate.conf` that this project actually depends on, in plain language, so you can adapt your own
config with confidence instead of copying blindly.

If you're new to Frigate itself, read
[Frigate's own configuration docs](https://docs.frigate.video/configuration/) first — this page
only covers the subset that matters for *this* project, not Frigate in general.

## Two streams per camera: "detect" vs "record"

Every camera in `frigate.conf` has (at least) two RTSP feeds wired to two different `roles`:

```yaml
inputs:
  - path: rtsp://127.0.0.1:8554/out
    roles: [audio, record]
  - path: rtsp://127.0.0.1:8554/out_sub
    roles: [detect]
```

- **`detect`** — a low-resolution, low-bandwidth stream Frigate's AI actually looks at to find
  objects. Doesn't need to look good, just needs to be fast and cheap to process continuously.
  This is also the stream Frigate's own event snapshot (`GET /api/events/<id>/snapshot.jpg`) comes
  from — `ingest-worker`'s `crop.crop_event` fetches that snapshot directly rather than cropping
  its own region out of either stream (see CLAUDE.md's "Cropping" section), so this stream's
  resolution (set via each camera's `detect: {width, height}` in `frigate.conf`) is what actually
  determines the stored/analyzed image's resolution.
- **`record`** — the full-resolution stream that gets saved and is what `ingest-worker` downloads
  clips from (`STORE_VIDEO_EVENTS`/`STORE_VIDEO_ALERTS`). This project needs this to be genuinely full
  resolution (this repo's own cameras run at 3840x2160) for legible playback, independent of
  whatever the crop/analysis path above uses.

## Recording retention — this genuinely matters, not just a tuning knob

```yaml
record:
  enabled: true
  alerts:
    pre_capture: 10
    post_capture: 10
    retain:
      days: 5
  detections:
    pre_capture: 10
    post_capture: 10
    retain:
      days: 10
  continuous:
    days: 0
  motion:
    days: 30
```

Frigate doesn't retain footage as one long continuous recording — it keeps each short recording
segment (a few seconds each) tagged by *why* that segment was worth keeping, and each tag has its
own retention window:

- **`alerts`** / **`detections`** — a segment where a tracked object crossed your `required_zones`
  gets tagged here, retained for `retain.days`.
- **`motion`** — a segment where Frigate's separate, more basic motion-pixel-change detector fired,
  retained separately (usually the longest window, since it's the cheapest signal).
- **`continuous`** — anything else. `days: 0` here means Frigate keeps essentially none of it once
  its own cleanup pass gets to it — it isn't a "keep everything, just briefly" buffer the way the
  name might suggest.

**Why you should care**: this project's `STORE_VIDEO_ALERTS` flow asks Frigate for a clip covering
an *arbitrary time range* (a whole visit's span), not a specific already-tagged event. If part of
that range was never anything but `continuous`-tagged (e.g. a parked car sitting still for a
stretch, not moving enough to re-trigger motion), Frigate may have almost nothing left for that
portion within seconds of it happening — confirmed directly in production, not a hypothetical.
This project used to have a second feature with the identical problem (a visit-level preview image
built from this same whole-span request, worked around with several successive fixes, then a
separate alert AI stage that gathered its own per-event crops instead — see `CLAUDE.md`'s "Visit
preview" and "Alert AI stage" sections for that history) but both have since been removed entirely.
`STORE_VIDEO_ALERTS` is the only thing left that depends on this whole-span endpoint — if you want
more headroom for it specifically, raising `continuous.days` to `1` or more gives Frigate an actual
short-lived rolling buffer to serve those requests from, at the cost of extra disk usage for the
full record stream.

The single-event crop (`raw_events.crop_image_base64`, every event gets one) doesn't have this
problem at all — it isn't a clip/frame grab from the record stream in the first place. `crop.
crop_event` fetches Frigate's own already-rendered best-detection-score snapshot
(`GET /api/events/<id>/snapshot.jpg`, from the `detect` stream — see "Two streams per camera"
above), the same image Frigate's own Explore UI shows for that event, and downscales it for the
AI-facing/DB-stored copy. `STORE_VIDEO_EVENTS` (the per-event *video clip*, independent of the crop) does
read from Frigate's per-event clip endpoint (`/api/events/<id>/clip.mp4`), which is tied to that
event's own `alerts`/`detections` retention, not the generic continuous-recording endpoint above.

## Zones and `required_zones`

```yaml
zones:
  yard:
    coordinates: 0.978,0.103,0.664,0,0,0,0,1,0.452,1,0.837,1,0.95,0.448
    loitering_time: 0
    inertia: 3

review:
  alerts:
    required_zones: [yard, yard_car_zone]
  detections:
    required_zones: [yard, yard_car_zone]
```

A zone is a polygon drawn over the camera frame (normalized 0–1 coordinates — easiest to draw
using Frigate's own web UI's zone editor rather than hand-typing coordinates). `required_zones`
under `review` means: only count a tracked object as an alert/detection (and therefore something
this project's MQTT listener reacts to) if it actually entered one of these zones — a car passing
on the street outside your driveway, never entering the zone, is tracked by Frigate but never
becomes a review/alert. If both `alerts.required_zones` and `detections.required_zones` list the
exact same zones (as in this repo's own config), *everything* in-zone comes back classified as
`alert`, never `detection` — meaning `severity` isn't currently a useful noise filter here. Give
`detections` a narrower zone list than `alerts` if you want that distinction to mean something.

## License plate recognition (LPR)

```yaml
lpr:
  enabled: true
  device: CPU
  detection_threshold: 0.7
  known_plates:
    Example_Vehicle:
      - ABC-1234
      - ABC1234
```

Global `lpr.enabled: true` plus `lpr.enabled: true` again under each camera (both are required —
the per-camera one doesn't inherit from the global one) turns on Frigate's own plate OCR, which
this project reads as `raw_events.sub_label` (kept alongside this project's own VLM-based plate
read as a cross-check — see the main `README.md`). `known_plates` is optional and lets Frigate
label a recognized plate with a friendly name of your choosing instead of just the raw plate text.
Requires **Frigate 0.16+**.

## Motion detection tuning

```yaml
motion:
  threshold: 35
  contour_area: 15
  improve_contrast: true
  lightning_threshold: 0.8
  mask: 0.203,0.05,0.204,0,0.002,0.002,0,0.05
```

This is Frigate's own generic "did pixels change" detector, separate from object tracking — it's
what feeds the `motion` recording-retention category above, and also gates whether Frigate even
bothers running object detection on a given frame. `mask` excludes a region from motion
consideration entirely (e.g. a spot with headlight glare or moving foliage that isn't yard
activity) — normalized polygon coordinates, same format as zones. If you're seeing false-positive
events from glare/reflections/shadows, this is the first thing to tune, before touching
`objects.filters`.

## Avoiding duplicate events for a stationary object

```yaml
detect:
  stationary:
    classifier: true
    interval: 25
    threshold: 15
    max_frames:
      objects:
        car: 50000
```

Once an object stops moving for `threshold` frames, Frigate marks it "stationary" — but it still
needs to track it as the *same* object indefinitely, or it becomes a brand new event (a fresh row
in this project's `raw_events`) every time Frigate's own internal memory limit
(`max_frames`) is hit, even though nothing actually moved. If you see a parked car repeatedly
generating near-duplicate events every hour or so, raise `max_frames.objects.<label>` — at
`fps: 5`, `50000` frames is about 2.75 hours; scale proportionally to your own `detect.fps` and how
long things typically sit still on your property.

## Where this project actually plugs in

None of the above is something this project changes for you — it's Frigate's own config, and this
project only ever *listens* (`frigate/events`, `frigate/reviews` over MQTT) and *reads* (Frigate's
REST API, for the snapshot/clip bytes). See [`configuration.md`](configuration.md) for how this
project's own settings (crop sizing, video storage, the internal AI stage) relate to what Frigate
gives it.
