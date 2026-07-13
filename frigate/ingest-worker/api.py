from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query

import config
import crop_worker
import db
import report
import schemas
from auth import require_api_key

app = FastAPI(
    title="ingest-worker",
    description=(
        "Frigate event ingest + crop worker, a read-only query/report API, and the AI-stage queue "
        "mechanics (claim/complete/fail) n8n's Metadata Processor calls. /health, /status, "
        "/crop/{id}, /retention/run are unauthenticated admin/debug endpoints for manual testing "
        "-- not part of the normal pipeline. Everything under /events, /sightings, /stats, "
        "/reports, /ai-queue requires an X-API-Key header (use the Authorize button below). "
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


@app.get("/events", response_model=list[schemas.EventSummary], tags=["events"], dependencies=[Depends(require_api_key)])
def get_events(
    object_type: str | None = Query(None, description="Exact match on Frigate's object label, e.g. 'car', 'person', 'truck'"),
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    crop_status: str | None = None,
    ai_status: str | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List raw_events, most recent first. No image field -- keeps list responses small; use
    GET /events/{id} for a single event's full detail including its cropped image."""
    return db.list_events(object_type, camera, start, end, crop_status, ai_status, limit, offset)


@app.get("/events/{event_id}", response_model=schemas.EventDetail, tags=["events"], dependencies=[Depends(require_api_key)])
def get_event(event_id: int):
    """Single event's full detail, including its stored crop_image_base64."""
    row = db.get_raw_event(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"raw_event {event_id} not found")
    return row


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
):
    """Replaces n8n's old Reap Stale Processing Items / Count In-Progress Items / Check Capacity /
    Claim Next Batch nodes with one call: reaps stale rows, computes available capacity, and
    atomically claims up to that many crop_status='done' rows of the given object types. `events`
    is an empty list if there's no capacity or no work -- n8n Split Out's the array then loops
    over whatever comes back."""
    types = [t.strip() for t in object_types.split(",") if t.strip()]
    return {"events": db.claim_ai_batch(types, parallel_limit, stale_minutes)}


@app.post("/sightings/vehicles", response_model=schemas.SightingCreated, tags=["sightings"], dependencies=[Depends(require_api_key)])
def create_vehicle_sighting(sighting: schemas.VehicleSightingCreate):
    """Inserts a vehicle sighting and marks the raw_event's ai_status='done' in one transaction
    -- replaces the old Insert Vehicle Sighting + Mark Done pair of n8n Postgres nodes."""
    sighting_id = db.complete_vehicle_sighting(
        sighting.raw_event_id, sighting.color, sighting.body_type, sighting.make_guess,
        sighting.make_confidence, sighting.plate_text_llm, sighting.plate_text_frigate,
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
