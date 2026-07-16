import base64
import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import config
import crop
import crop_worker
import db
import report
import schemas
from auth import require_api_key, require_api_key_header_or_query

app = FastAPI(
    title="ingest-worker",
    description=(
        "Frigate event ingest + crop worker, a read-only query/report API, and the AI-stage queue "
        "mechanics (claim/complete/fail) n8n's Metadata Processor calls. /health, /status, "
        "/crop/{id}, /retention/run are unauthenticated admin/debug endpoints for manual testing "
        "-- not part of the normal pipeline. Everything under /events, /sightings, /stats, "
        "/reports, /ai-queue, /retention/purge requires an X-API-Key header (use the Authorize "
        "button below). "
        "ingest-worker never calls an LLM itself -- the actual VLM call and prompt still live in "
        "n8n; this only executes the claim/retry-with-cap mechanics and stores whatever result "
        "n8n posts back. Swagger UI at /docs, ReDoc at /redoc."
    ),
)


def _resolve_window(
    start: datetime | None, end: datetime | None, hours: float
) -> tuple[datetime, datetime]:
    resolved_end = end or datetime.now(timezone.utc)
    resolved_start = start or (resolved_end - timedelta(hours=hours))
    return resolved_start, resolved_end


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    return {"breakdown": db.get_status_breakdown()}


@app.post("/crop/{event_id}")
def crop_one(event_id: int):
    """Manually run the crop step for one raw_event, bypassing the queue/claim logic entirely.
    Useful to test a single event without waiting for the poll loop or fighting PARALLEL_LIMIT."""
    row = db.get_raw_event(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"raw_event {event_id} not found")
    crop_worker.process_claimed_event(row)
    updated = db.get_raw_event(event_id)
    return {"event_id": event_id, "crop_status": updated["crop_status"]}


@app.post("/retention/run")
def run_retention_now():
    """Manually trigger the same retention sweep the poll loop runs on its own schedule."""
    db.run_retention_cleanup(config.RETENTION_MONTHS)
    return {"retention_months": config.RETENTION_MONTHS, "ran": True}


@app.post("/retention/purge", tags=["retention"], dependencies=[Depends(require_api_key)])
def purge_old_records(
    older_than_days: int = Query(..., ge=1, description="Delete raw_events (and their dependent sightings/visits) with start_ts older than this many days"),
    confirm: bool = Query(False, description="Must be true to actually delete. Omitted/false previews counts only -- no rows are removed"),
):
    """Ad-hoc bulk purge with a caller-chosen cutoff, independent of the scheduled
    RETENTION_MONTHS sweep -- e.g. to clear out a backlog of old test data or reclaim space sooner
    than the configured retention window. Unlike /retention/run, the cutoff here is
    caller-controlled and the delete has no undo, so this requires X-API-Key and defaults to a
    dry run: call once without confirm=true to see how many rows of each type would be deleted,
    then again with confirm=true to actually delete them."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    counts = db.purge_older_than(cutoff, execute=confirm)
    return {"cutoff": cutoff, "dry_run": not confirm, "counts": counts}


@app.get("/events", response_model=list[schemas.EventSummary], tags=["events"], dependencies=[Depends(require_api_key)])
def get_events(
    object_type: str | None = Query(None, description="Comma-separated Frigate object labels, e.g. 'car,truck'. Omit or pass 'all' for no filter"),
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    crop_status: str | None = None,
    ai_status: str | None = None,
    video_status: str | None = None,
    has_image: bool = Query(True, description="Only return rows with a stored crop image -- default true, since a row with none (crop_status not yet 'done', including 'skipped') has nothing to show in a thumbnail. Pass false to see every row regardless."),
    hours: float = Query(1, gt=0, description="Used when start/end aren't both given -- window is the last N hours (default: last 1 hour)"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List raw_events, most recent first. Defaults to the last 1 hour, every object type, every
    ai_status, images-only -- matching the web report's default view. No image field -- keeps list
    responses small; use GET /events/{id} for full detail or GET /events/{id}/thumbnail for a
    small preview image."""
    resolved_start, resolved_end = _resolve_window(start, end, hours)
    return db.list_events(
        object_type, camera, resolved_start, resolved_end,
        crop_status, ai_status, video_status, has_image, limit, offset,
    )


@app.get("/events/{event_id}", response_model=schemas.EventDetail, tags=["events"], dependencies=[Depends(require_api_key)])
def get_event(event_id: int):
    """Single event's full detail, including its stored crop_image_base64."""
    row = db.get_raw_event(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"raw_event {event_id} not found")
    return row


@app.get("/events/{event_id}/thumbnail", tags=["events"], dependencies=[Depends(require_api_key_header_or_query)])
def get_event_thumbnail(event_id: int):
    """A small on-the-fly JPEG (THUMBNAIL_MAX_DIMENSION, reuses report.py's same scale-down
    helper) for the web report's grid view -- keeps GET /events list-sized responses light by
    never embedding the full crop_image_base64 there. Accepts X-API-Key header or ?api_key= query
    param since this is loaded directly by an <img> tag."""
    row = db.get_raw_event(event_id)
    if row is None or not row.get("crop_image_base64"):
        raise HTTPException(status_code=404, detail=f"No crop image for raw_event {event_id}")
    thumbnail_base64 = crop.scale_image_base64(row["crop_image_base64"], config.THUMBNAIL_MAX_DIMENSION)
    return Response(content=base64.b64decode(thumbnail_base64), media_type="image/jpeg")


@app.get("/events/{event_id}/image", tags=["events"], dependencies=[Depends(require_api_key_header_or_query)])
def get_event_image(event_id: int):
    """Full-size crop as raw JPEG bytes (decodes the stored crop_image_base64) -- used by the web
    report's lightbox when an event has no video to show instead. Accepts X-API-Key header or
    ?api_key= query param since this is loaded directly by an <img> tag."""
    row = db.get_raw_event(event_id)
    if row is None or not row.get("crop_image_base64"):
        raise HTTPException(status_code=404, detail=f"No crop image for raw_event {event_id}")
    return Response(content=base64.b64decode(row["crop_image_base64"]), media_type="image/jpeg")


@app.get("/media/video/{event_id}", tags=["events"], dependencies=[Depends(require_api_key_header_or_query)])
def get_event_video(event_id: int):
    """Streams the stored clip off disk (range requests supported via Starlette's FileResponse,
    so the browser's video scrubber works) -- never queried through Postgres, video_path only
    ever points at a file under VIDEO_STORAGE_PATH. Accepts X-API-Key header or ?api_key= query
    param since this is loaded directly by a <video> tag."""
    row = db.get_raw_event(event_id)
    if row is None or not row.get("video_path"):
        raise HTTPException(status_code=404, detail=f"No video for raw_event {event_id}")
    video_path = row["video_path"]
    if not os.path.isfile(video_path):
        raise HTTPException(status_code=404, detail=f"Video file missing on disk for raw_event {event_id}")
    return FileResponse(video_path, media_type="video/mp4", filename=os.path.basename(video_path))


@app.get("/sightings/vehicles", response_model=list[schemas.VehicleSighting], tags=["sightings"], dependencies=[Depends(require_api_key)])
def get_vehicle_sightings(
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    plate_text: str | None = Query(None, description="Substring match against either plate_text_llm or plate_text_frigate"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Vehicle sightings, most recent first -- e.g. ?limit=10 for the last 10 cars."""
    return db.get_vehicle_sightings(camera, start, end, plate_text, limit, offset)


@app.get("/sightings/persons", response_model=list[schemas.PersonSighting], tags=["sightings"], dependencies=[Depends(require_api_key)])
def get_person_sightings(
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Person sightings, most recent first."""
    return db.get_person_sightings(camera, start, end, limit, offset)


@app.get("/stats/summary", response_model=schemas.StatsSummary, tags=["stats"], dependencies=[Depends(require_api_key)])
def get_stats_summary(
    start: datetime | None = None,
    end: datetime | None = None,
    hours: float = Query(24, gt=0, description="Used when start/end aren't both given -- window is the last N hours"),
):
    """Aggregate counts (total events/sightings, breakdown by camera/object type/day) over a
    time window -- defaults to the last 24 hours."""
    resolved_start, resolved_end = _resolve_window(start, end, hours)
    return db.get_stats_summary(resolved_start, resolved_end)


@app.get("/reports/generate", response_model=schemas.ReportResponse, tags=["reports"], dependencies=[Depends(require_api_key)])
def generate_report(
    start: datetime | None = None,
    end: datetime | None = None,
    hours: float = Query(24, gt=0, description="Used when start/end aren't both given -- window is the last N hours"),
):
    """Builds the same HTML report daily-report.json used to build itself in a Code node --
    n8n now just calls this and emails/Telegrams the result. Each row's inline image is a small
    on-the-fly thumbnail (never touching the stored full-quality crop); the full-size image is
    still available via the report's click-to-enlarge lightbox, embedded once, not twice."""
    resolved_start, resolved_end = _resolve_window(start, end, hours)
    return report.generate_report(resolved_start, resolved_end)


@app.post("/ai-queue/claim", response_model=schemas.ClaimResponse, tags=["ai-queue"], dependencies=[Depends(require_api_key)])
def claim_ai_batch(
    object_types: str = Query("car,truck,person", description="Comma-separated Frigate object labels to claim"),
    parallel_limit: int = Query(3, ge=1, description="Max rows allowed ai_status='processing' at once"),
    stale_minutes: int = Query(5, ge=1, description="Reap rows stuck 'processing' longer than this"),
    max_age_hours: float | None = Query(None, gt=0, description="If set, never claim rows older than this many hours -- lets a backlog age out instead of being processed once it's stale. Omit for no age limit (default)."),
):
    """Replaces n8n's old Reap Stale Processing Items / Count In-Progress Items / Check Capacity /
    Claim Next Batch nodes with one call: reaps stale rows, computes available capacity, and
    atomically claims up to that many crop_status='done' rows of the given object types, newest
    first (one shared queue across all requested types, not claimed separately per type) --
    older rows only get swept up once the backlog of newer ones drops below available capacity.
    `events` is an empty list if there's no capacity or no work -- n8n Split Out's the array then
    loops over whatever comes back."""
    types = [t.strip() for t in object_types.split(",") if t.strip()]
    return {"events": db.claim_ai_batch(types, parallel_limit, stale_minutes, max_age_hours)}


@app.post("/sightings/vehicles", response_model=schemas.SightingCreated, tags=["sightings"], dependencies=[Depends(require_api_key)])
def create_vehicle_sighting(sighting: schemas.VehicleSightingCreate):
    """Inserts a vehicle sighting and marks the raw_event's ai_status='done' in one transaction
    -- replaces the old Insert Vehicle Sighting + Mark Done pair of n8n Postgres nodes."""
    sighting_id = db.complete_vehicle_sighting(
        sighting.raw_event_id, sighting.color, sighting.body_type, sighting.make_guess,
        sighting.make_confidence, sighting.model_guess, sighting.model_confidence,
        sighting.notable_features, sighting.plate_text_llm, sighting.plate_text_frigate,
        sighting.plate_confidence, sighting.notes,
    )
    return {"id": sighting_id, "ai_status": "done"}


@app.post("/sightings/persons", response_model=schemas.SightingCreated, tags=["sightings"], dependencies=[Depends(require_api_key)])
def create_person_sighting(sighting: schemas.PersonSightingCreate):
    """Inserts a person sighting and marks the raw_event's ai_status='done' in one transaction."""
    sighting_id = db.complete_person_sighting(sighting.raw_event_id, sighting.description, sighting.notes)
    return {"id": sighting_id, "ai_status": "done"}


@app.post("/ai-queue/{event_id}/fail", response_model=schemas.FailResponse, tags=["ai-queue"], dependencies=[Depends(require_api_key)])
def fail_ai_event(
    event_id: int,
    max_attempts: int = Query(3, ge=1, description="Attempt count at/above which the event goes terminal 'failed'"),
):
    """Same retry-or-fail-with-cap logic as n8n's old Handle Failure (Retry or Fail) node --
    below max_attempts this goes back to ai_status='retry' (picked up on a future claim), at/above
    it goes terminal 'failed'."""
    return db.fail_ai_event(event_id, max_attempts)


# Static web report UI (index.html/app.js/style.css/vendor/*) -- all local files, no CDN
# requests. Calls back into GET /events, /events/{id}/thumbnail, /media/video/{id} above using an
# API key the user enters once and stores in a cookie. Baked into the image by the Dockerfile
# (COPY static/ ./static/); mounted last so it doesn't shadow any API route above.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")
