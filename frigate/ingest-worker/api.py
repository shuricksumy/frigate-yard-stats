import base64
import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import admin
import ai_worker
import config
import crop
import crop_worker
import db
import report
import schemas
import video
from auth import require_api_key, require_api_key_header_or_query

app = FastAPI(
    title="ingest-worker",
    description=(
        "Frigate event ingest + crop worker, a read-only query/report API, and the AI-stage queue "
        "mechanics (claim/complete/fail) n8n's Metadata Processor calls. /health, /status, "
        "/crop/{id}, /retention/run are unauthenticated admin/debug endpoints for manual testing "
        "-- not part of the normal pipeline. Everything under /events, /sightings, /stats, "
        "/reports, /ai-queue, /retention/purge, /admin requires an X-API-Key header (use the "
        "Authorize button below). A web dashboard over the /admin/* endpoints is at /ui/admin. "
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
    # retention/oldest_available_start_ts let a caller (the Q&A agent) tell "nothing happened in
    # that range" apart from "that range was already purged" -- see db.get_retention_info.
    # version/build_sha/build_date/github_url are purely informational (the web UI's footer, see
    # static/app.js's fetchVersionInfo) -- baked into the image at build time, see config.py.
    return {
        "breakdown": db.get_status_breakdown(),
        **db.get_retention_info(),
        "version": config.APP_VERSION,
        "build_sha": config.APP_BUILD_SHA,
        "build_date": config.APP_BUILD_DATE,
        "github_url": config.GITHUB_REPO_URL,
    }


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
    older_than_days: int = Query(..., ge=1, description="Delete/clear data with start_ts older than this many days"),
    confirm: bool = Query(False, description="Must be true to actually delete. Omitted/false previews counts only -- no rows are removed"),
    only_media: bool = Query(True, description="Default true: keeps every row and all its text/structured AI analysis (including embeddings) -- only deletes stored video files off disk and clears stored images/GIFs (crop_image_base64/preview_gif_base64), so old data stays searchable with just the media gone. false: deletes the rows entirely (raw_events, visits, and their dependent sightings) -- today's original full purge -- and rebuilds the vector search index afterward against whatever data remains."),
    object_label: str | None = Query(None, description="Restrict this purge to a single Frigate object label (e.g. 'car'). Only ever affects raw_events and their sightings -- visits/visit_sightings are never touched when this is set, since a visit can span multiple distinct object types and there's no single-type-safe way to decide the visit row (or its own composite-grid media) belongs to just one type's purge. Omit for the existing all-types behavior, which does cover visits/visit_sightings same as before this param existed."),
):
    """Ad-hoc bulk purge with a caller-chosen cutoff, independent of the scheduled
    RETENTION_MONTHS sweep -- e.g. to clear out a backlog of old test data, reclaim space sooner
    than the configured retention window, or (only_media=true, the default) strip old
    video/image/GIF payloads while keeping every row's AI analysis text and plate reads
    searchable indefinitely. Unlike /retention/run, the cutoff here is caller-controlled and the
    delete has no undo, so this requires X-API-Key and defaults to a dry run: call once without
    confirm=true to see how many rows/files would be affected, then again with confirm=true to
    actually apply it."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    if only_media:
        counts = db.purge_media_older_than(cutoff, execute=confirm, object_label=object_label)
        return {"cutoff": cutoff, "dry_run": not confirm, "only_media": True, "object_label": object_label, "counts": counts}
    counts = db.purge_older_than(cutoff, execute=confirm, object_label=object_label)
    result = {"cutoff": cutoff, "dry_run": not confirm, "only_media": False, "object_label": object_label, "counts": counts}
    if confirm:
        # A full purge can remove a large fraction of the embedded rows the HNSW index was built
        # over -- rebuilding it against whatever survives keeps the index accurate/compact rather
        # than leaving it sized for data that's now gone.
        result["reindexed"] = db.reindex_vector_indexes()
    return result


@app.post("/embeddings/backfill", tags=["sightings"], dependencies=[Depends(require_api_key)])
def backfill_embeddings(
    limit: int = Query(50, ge=1, le=500, description="Max rows per sighting type to process in this call -- call repeatedly (each with confirm=true) until the counts reach zero for a larger backlog."),
    confirm: bool = Query(False, description="Must be true to actually call the embedding model and update rows. Omitted/false previews counts only -- no embedding calls are made."),
):
    """Fills in the embedding column for sightings that existed before semantic search was added
    (or came from any run that didn't attach one) -- calls llama_slot_proxy directly
    (LLAMA_PROXY_BASE_URL/LLAMA_PROXY_EMBED_PATH) to re-embed each sighting's own already-stored
    fields, independent of AI_EVENTS_STAGE_ENABLED and without re-running the VLM. Defaults to a dry run
    like /retention/purge: call once without confirm=true to see how many rows are missing an
    embedding, then repeatedly with confirm=true (bounded by limit per call) until both counts
    reach zero."""
    if not confirm:
        return {"confirm": False, **db.count_sightings_missing_embedding()}
    if not config.LLAMA_PROXY_BASE_URL:
        raise HTTPException(status_code=400, detail="LLAMA_PROXY_BASE_URL is not configured")
    return {"confirm": True, **ai_worker.run_embedding_backfill(limit)}


@app.get("/object-types", tags=["events"], dependencies=[Depends(require_api_key)])
def get_object_types():
    """Configured object labels (OBJECT_TYPES, comma-separated in .env) -- lets the web UI's Type
    filter dropdown stay in sync with whatever labels your Frigate config actually produces
    (e.g. car/truck/person/dog) without a frontend code change."""
    return {"object_types": config.OBJECT_TYPES}


@app.get("/events", response_model=list[schemas.EventSummary], tags=["events"], dependencies=[Depends(require_api_key)])
def get_events(
    response: Response,
    object_type: str | None = Query(None, description="Comma-separated Frigate object labels, e.g. 'car,truck'. Omit or pass 'all' for no filter"),
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    crop_status: str | None = None,
    ai_status: str | None = None,
    video_status: str | None = None,
    has_media: bool = Query(True, description="Only return rows with a stored crop image and/or video -- default true, since a row with neither (crop_status not yet 'done', including 'skipped') has nothing to show. Pass false to see every row regardless."),
    event_id: int | None = Query(None, description="Exact-match a single event by id -- ignores the start/end/hours window entirely, since you're looking for one specific known event, not browsing a range."),
    visit_id: int | None = Query(None, description="Every raw_event linked to one specific visit -- ignores the start/end/hours window and has_media default entirely, same reasoning as event_id: you're looking for every det_id a visit grouped, not browsing a range. Lets the web UI's visit lightbox show all connected events, not just the deduped AI-analyzed ones GET /visits/{id}/sightings returns."),
    q: str | None = Query(None, description="Free-text search (substring, case-insensitive) across the AI analysis result -- vehicle color/body_type/make/model/notable_features/plate/notes, or person description/notes. Only matches rows that already have a sighting (ai_status='done'). Combines with start/end/hours (and every other filter) rather than bypassing them -- searches within the selected window, not your whole history."),
    hours: float = Query(1, gt=0, description="Used when start/end aren't both given -- window is the last N hours (default: last 1 hour). Ignored if event_id/visit_id is given; still applies alongside q."),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List raw_events, most recent first. Defaults to the last 1 hour, every object type, every
    ai_status, media-only -- matching the web report's default view. No image field -- keeps list
    responses small; use GET /events/{id} for full detail or GET /events/{id}/thumbnail for a
    small preview image. Sets an X-Total-Count response header (total rows matching the same
    filters, ignoring limit/offset) so a caller can compute a page count without a second request."""
    if event_id is not None or visit_id is not None:
        resolved_start = resolved_end = None
    else:
        resolved_start, resolved_end = _resolve_window(start, end, hours)
    total = db.count_events(
        object_type, camera, resolved_start, resolved_end,
        crop_status, ai_status, video_status, has_media, event_id, q, visit_id,
    )
    response.headers["X-Total-Count"] = str(total)
    return db.list_events(
        object_type, camera, resolved_start, resolved_end,
        crop_status, ai_status, video_status, has_media, event_id, q, limit, offset, visit_id,
    )


@app.get("/visits", response_model=list[schemas.VisitSummary], tags=["events"], dependencies=[Depends(require_api_key)])
def get_visits(
    response: Response,
    object_type: str | None = Query(None, description="Comma-separated Frigate object labels, e.g. 'car,truck'. Matches if the visit contains any of the given types. Omit or pass 'all' for no filter"),
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    q: str | None = Query(None, description="Free-text search (substring, case-insensitive) across every linked raw_event's AI analysis result -- matches the visit if ANY of its grouped events matches, same fields GET /events' own q searches. Combines with start/end/hours (and every other filter) rather than bypassing them -- searches within the selected window, not your whole history."),
    hours: float = Query(1, gt=0, description="Used when start/end aren't both given -- window is the last N hours (default: last 1 hour). Still applies alongside q."),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List visits (Frigate review/alert-grouped raw_events), most recent first -- a comparison
    view alongside GET /events: one row per real-world activity segment instead of one per
    tracked-object det_id, so duplicate det_ids from tracker re-ID/label flicker collapse into a
    single row. representative_event_id is the visit's earliest-linked raw_event (used for the
    thumbnail/lightbox); event_count is how many det_ids were grouped into it. Read-only and
    purely additive -- doesn't affect GET /events, the AI queue, or Telegram notifications. Sets
    an X-Total-Count response header (total visits matching the same filters, ignoring
    limit/offset) so a caller can compute a page count without a second request."""
    resolved_start, resolved_end = _resolve_window(start, end, hours)
    total = db.count_visits(object_type, camera, resolved_start, resolved_end, q)
    response.headers["X-Total-Count"] = str(total)
    return db.list_visits(object_type, camera, resolved_start, resolved_end, q, limit, offset)


@app.get("/visits/{visit_id}/sightings", response_model=schemas.VisitSightings, tags=["events"], dependencies=[Depends(require_api_key)])
def get_visit_sightings(visit_id: int):
    """Every AI analysis result linked to this visit, not just the representative event's --
    claim_ai_batch analyzes one representative event per distinct object type a visit grouped
    together (see its only_visit_representative comment), so a visit spanning e.g. a car and a
    person has one sighting each here, not just whichever was analyzed first. GET /events/{id}
    still only ever returns a single event's own sighting; this is the visit-scoped combined view
    the web UI's lightbox uses instead. alert_sighting is the visit's own AI_ALERTS_ENABLED
    analysis of its composite grid, independent of sightings above -- null until that stage has
    produced one for this visit."""
    if db.get_visit(visit_id) is None:
        raise HTTPException(status_code=404, detail=f"visit {visit_id} not found")
    return {"sightings": db.get_sightings_for_visit(visit_id), "alert_sighting": db.get_visit_alert_sighting(visit_id)}


@app.get("/events/{event_id}", response_model=schemas.EventDetail, tags=["events"], dependencies=[Depends(require_api_key)])
def get_event(event_id: int):
    """Single event's full detail, including its stored crop_image_base64 and, once
    ai_status='done', the AI analysis result."""
    row = db.get_raw_event(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"raw_event {event_id} not found")
    row = dict(row)
    row["sighting"] = db.get_sighting_for_event(event_id)
    return row


@app.get("/events/{event_id}/thumbnail", tags=["events"], dependencies=[Depends(require_api_key_header_or_query)])
def get_event_thumbnail(event_id: int):
    """A small on-the-fly JPEG (THUMBNAIL_MAX_DIMENSION, reuses report.py's same scale-down
    helper) for the web report's grid view -- keeps GET /events list-sized responses light by
    never embedding the full crop_image_base64 there. Falls back to a frame pulled from the stored
    video if there's no crop image but there is a video (belt and suspenders -- in practice a video
    always implies a crop image already exists). Accepts X-API-Key header or ?api_key= query param
    since this is loaded directly by an <img> tag."""
    row = db.get_raw_event(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"raw_event {event_id} not found")
    if row.get("crop_image_base64"):
        thumbnail_base64 = crop.scale_image_base64(row["crop_image_base64"], config.THUMBNAIL_MAX_DIMENSION)
        return Response(content=base64.b64decode(thumbnail_base64), media_type="image/jpeg")
    if row.get("video_path") and os.path.isfile(row["video_path"]):
        return Response(content=video.extract_frame_jpeg(row["video_path"], config.THUMBNAIL_MAX_DIMENSION), media_type="image/jpeg")
    raise HTTPException(status_code=404, detail=f"No crop image or video for raw_event {event_id}")


@app.get("/events/{event_id}/image", tags=["events"], dependencies=[Depends(require_api_key_header_or_query)])
def get_event_image(event_id: int):
    """Full-size crop as raw JPEG bytes (decodes the stored crop_image_base64) -- used by the web
    report's lightbox when an event has no video, or when viewing the still image side of an event
    that has both. Falls back to a frame pulled from the stored video if there's no crop image but
    there is a video. Accepts X-API-Key header or ?api_key= query param since this is loaded
    directly by an <img> tag."""
    row = db.get_raw_event(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"raw_event {event_id} not found")
    if row.get("crop_image_base64"):
        return Response(content=base64.b64decode(row["crop_image_base64"]), media_type="image/jpeg")
    if row.get("video_path") and os.path.isfile(row["video_path"]):
        return Response(content=video.extract_frame_jpeg(row["video_path"]), media_type="image/jpeg")
    raise HTTPException(status_code=404, detail=f"No crop image or video for raw_event {event_id}")


@app.get("/visits/{visit_id}/thumbnail", tags=["events"], dependencies=[Depends(require_api_key_header_or_query)])
def get_visit_thumbnail(visit_id: int):
    """A small on-the-fly image for the Visits view grid -- prefers the visit's own animated
    preview GIF (VISIT_THUMB_CROP_ENABLED, crop.build_visit_preview) so the grid card itself plays
    the sampled-frames sequence, already built at a modest size so it's served as-is; falls back to
    a scaled-down JPEG of the composite grid image, then the representative event's own crop
    (available almost immediately, long before the preview can be), then a frame from the visit's
    own stored video, same belt-and-suspenders reasoning as GET /events/{id}/thumbnail. Accepts
    X-API-Key header or ?api_key= query param since this is loaded directly by an <img> tag."""
    visit = db.get_visit(visit_id)
    if visit is None:
        raise HTTPException(status_code=404, detail=f"visit {visit_id} not found")
    if visit.get("preview_gif_base64"):
        return Response(content=base64.b64decode(visit["preview_gif_base64"]), media_type="image/gif")
    image_base64 = visit.get("crop_image_base64")
    if not image_base64:
        representative = db.get_representative_event_for_visit(visit_id)
        image_base64 = representative.get("crop_image_base64") if representative else None
    if image_base64:
        thumbnail_base64 = crop.scale_image_base64(image_base64, config.THUMBNAIL_MAX_DIMENSION)
        return Response(content=base64.b64decode(thumbnail_base64), media_type="image/jpeg")
    if visit.get("video_path") and os.path.isfile(visit["video_path"]):
        return Response(content=video.extract_frame_jpeg(visit["video_path"], config.THUMBNAIL_MAX_DIMENSION), media_type="image/jpeg")
    raise HTTPException(status_code=404, detail=f"No crop image or video for visit {visit_id}")


@app.get("/visits/{visit_id}/image", tags=["events"], dependencies=[Depends(require_api_key_header_or_query)])
def get_visit_image(visit_id: int):
    """Full-size image as raw JPEG bytes -- same source preference as GET /visits/{id}/thumbnail
    (visit's own composite preview grid, then the representative event's crop, then a frame from
    the visit's stored video), used by the web report's lightbox when viewing a Visits-view card.
    Accepts X-API-Key header or ?api_key= query param since this is loaded directly by an <img>
    tag."""
    visit = db.get_visit(visit_id)
    if visit is None:
        raise HTTPException(status_code=404, detail=f"visit {visit_id} not found")
    image_base64 = visit.get("crop_image_base64")
    if not image_base64:
        representative = db.get_representative_event_for_visit(visit_id)
        image_base64 = representative.get("crop_image_base64") if representative else None
    if image_base64:
        return Response(content=base64.b64decode(image_base64), media_type="image/jpeg")
    if visit.get("video_path") and os.path.isfile(visit["video_path"]):
        return Response(content=video.extract_frame_jpeg(visit["video_path"]), media_type="image/jpeg")
    raise HTTPException(status_code=404, detail=f"No crop image or video for visit {visit_id}")


@app.get("/visits/{visit_id}/preview.gif", tags=["events"], dependencies=[Depends(require_api_key_header_or_query)])
def get_visit_preview_gif(visit_id: int):
    """The visit's animated preview GIF (crop.build_visit_preview's slideshow of frames sampled
    proportionally across the visit's own clip) -- human preview only, a separate artifact from
    crop_image_base64 (the single composite grid image used for AI analysis/thumbnails/reports).
    404s until VISIT_THUMB_CROP_ENABLED and thumb_crop_status='done'. Accepts X-API-Key header or
    ?api_key= query param since this would be loaded directly by an <img> tag."""
    visit = db.get_visit(visit_id)
    if visit is None:
        raise HTTPException(status_code=404, detail=f"visit {visit_id} not found")
    if not visit.get("preview_gif_base64"):
        raise HTTPException(status_code=404, detail=f"No preview GIF for visit {visit_id}")
    return Response(content=base64.b64decode(visit["preview_gif_base64"]), media_type="image/gif")


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


@app.get("/media/video/visit/{visit_id}", tags=["events"], dependencies=[Depends(require_api_key_header_or_query)])
def get_visit_video(visit_id: int):
    """Alerts-flow counterpart to GET /media/video/{event_id} -- a visit's own clip
    (STORE_VIDEO_ALERTS/alert_video_worker.py) lives under VIDEO_STORAGE_PATH_ALERTS, a completely
    separate storage location from any raw_event's video_path, so it needs its own endpoint rather
    than overloading the event one with two different id spaces."""
    row = db.get_visit(visit_id)
    if row is None or not row.get("video_path"):
        raise HTTPException(status_code=404, detail=f"No video for visit {visit_id}")
    video_path = row["video_path"]
    if not os.path.isfile(video_path):
        raise HTTPException(status_code=404, detail=f"Video file missing on disk for visit {visit_id}")
    return FileResponse(video_path, media_type="video/mp4", filename=os.path.basename(video_path))


@app.get("/sightings", response_model=list[schemas.Sighting], tags=["sightings"], dependencies=[Depends(require_api_key)])
def get_sightings(
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    object_label: str | None = Query(None, description="Exact match against the Frigate object label (e.g. 'car', 'dog') -- omit for every type"),
    q: str | None = Query(None, description="Substring match against the sighting's description"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Sightings, most recent first -- e.g. ?object_label=car&limit=10 for the last 10 cars.
    Replaces the former /sightings/vehicles and /sightings/persons (one universal list now, no
    per-type endpoints)."""
    return db.get_sightings(camera, start, end, object_label, q, limit, offset)


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
    source: str = Query("events", pattern="^(events|visits)$", description="'events' (default) includes every sighting independently -- today's exact behavior. 'visits' dedups the same way POST /ai-queue/claim's source=visits does: only the sighting for a visit's earliest-linked raw_event, plus every sighting whose raw_event was never grouped into a visit -- for an alerts-scoped report where one real-world visit shouldn't show up once per det_id."),
    include_preview: str = Query("gif", pattern="^(none|image|gif)$", description="'gif' (default) embeds a visit's animated preview GIF inline when it's ready (today's behavior; only differs from 'image' under source=visits). 'image' skips fetching/embedding the GIF entirely, falling back to the static composite grid/event crop. 'none' skips the row image entirely (crop included), for both source=events and source=visits. Each narrower mode is a real payload-size reduction, not just a rendering change -- the dropped field(s) are never even fetched from Postgres."),
    object_label: str | None = Query(None, description="Restrict the report to a single Frigate object label (e.g. 'car') -- for a per-type report alongside the default report covering every type. Omit for no filter (today's behavior)."),
):
    """Builds the same HTML report daily-report.json used to build itself in a Code node --
    n8n now just calls this and emails/Telegrams the result. Each row's inline image is a small
    on-the-fly thumbnail (never touching the stored full-quality crop); the full-size image is
    still available via the report's click-to-enlarge lightbox, embedded once, not twice."""
    resolved_start, resolved_end = _resolve_window(start, end, hours)
    return report.generate_report(resolved_start, resolved_end, source, include_preview, object_label)


@app.post("/ai-queue/claim", response_model=schemas.ClaimResponse, tags=["ai-queue"], dependencies=[Depends(require_api_key)])
def claim_ai_batch(
    object_types: str = Query("car,truck,person", description="Comma-separated Frigate object labels to claim"),
    parallel_limit: int = Query(3, ge=1, description="Max rows allowed ai_status='processing' at once"),
    stale_minutes: int = Query(5, ge=1, description="Reap rows stuck 'processing' longer than this"),
    max_age_hours: float | None = Query(None, gt=0, description="If set, never claim rows older than this many hours -- lets a backlog age out instead of being processed once it's stale. Omit for no age limit (default)."),
    require_video: bool = Query(False, description="If true, only claim rows that also have a stored video ready (video_status='done'), not just a crop image. Default false -- an image is always guaranteed regardless (crop_status='done' is required either way); this only narrows further for a workflow that wants both artifacts before processing. The VLM call itself still only ever uses the image."),
    source: str = Query("events", pattern="^(events|visits)$", description="'events' (default) analyzes every eligible raw_event independently -- today's exact behavior. 'visits' skips duplicate det_ids already grouped into a visit by Frigate's review/alert stream -- only the earliest (representative) raw_event per visit is claimed, plus every raw_event never grouped into a visit at all (unless visits_only is also set). Lets you A/B whether per-event or per-visit analysis produces better/less-redundant results; completion (POST /sightings/*) is identical either way, since this only changes which rows are eligible to claim, not ai_status semantics."),
    visits_only: bool = Query(False, description="Only meaningful when source=visits. If true, never claim a raw_event that Frigate's review/alert stream never grouped into a visit at all (visit_id IS NULL) -- strictly limits this call to actual alert/visit activity. Default false keeps source=visits' existing fallback: ungrouped events are still claimed so they don't sit unanalyzed forever if only a visits-scoped workflow is active."),
    require_thumb_crop: bool = Query(False, description="Only meaningful when source=visits. If true, only claim a visit's representative event once that visit's own high-res re-crop at Frigate's thumb_time has finished (VISIT_THUMB_CROP_ENABLED, thumb_crop_status='done') -- guarantees the claimed crop_image_base64 is always the well-timed thumb-crop, never the representative event's own possibly-badly-timed one, at the cost of real latency (the re-crop only starts once the review closes). Default false: still opportunistically prefers the thumb-crop whenever it already happens to be done by claim time, just doesn't wait for it."),
):
    """Replaces n8n's old Reap Stale Processing Items / Count In-Progress Items / Check Capacity /
    Claim Next Batch nodes with one call: reaps stale rows, computes available capacity, and
    atomically claims up to that many crop_status='done' rows of the given object types, newest
    first (one shared queue across all requested types, not claimed separately per type) --
    older rows only get swept up once the backlog of newer ones drops below available capacity.
    `events` is an empty list if there's no capacity or no work -- n8n Split Out's the array then
    loops over whatever comes back."""
    types = [t.strip() for t in object_types.split(",") if t.strip()]
    events = db.claim_ai_batch(
        types, parallel_limit, stale_minutes, max_age_hours, require_video,
        only_visit_representative=(source == "visits"),
        visits_only=visits_only,
        require_thumb_crop=require_thumb_crop,
    )
    return {"events": events}


@app.post("/sightings", response_model=schemas.SightingCreated, tags=["sightings"], dependencies=[Depends(require_api_key)])
def create_sighting(sighting: schemas.SightingCreate):
    """Inserts a sighting and marks the raw_event's ai_status='done' in one transaction --
    replaces the old Insert Vehicle/Person Sighting + Mark Done pair of n8n Postgres nodes, and
    the former separate /sightings/vehicles and /sightings/persons endpoints (one universal
    completion shape now, object_label is just data on the row)."""
    sighting_id = db.complete_sighting(
        sighting.raw_event_id, sighting.object_label, sighting.description, sighting.embedding,
    )
    return {"id": sighting_id, "ai_status": "done"}


@app.post("/search/semantic", response_model=list[schemas.SemanticSearchResult], tags=["sightings"], dependencies=[Depends(require_api_key)])
def semantic_search(search: schemas.SemanticSearchRequest):
    """Cosine-similarity search over already-analyzed sightings' embeddings, filtered by the
    caller-resolved time range -- a POST (not GET) since the embedding vector doesn't belong in a
    query string. Built for the Q&A agent's fuzzy-content asks ("anything unusual"), as opposed to
    /events'/'/sightings/*'s structured filters."""
    if len(search.embedding) != config.EMBEDDING_DIMENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"embedding must have {config.EMBEDDING_DIMENSIONS} dimensions, got {len(search.embedding)}",
        )
    return db.semantic_search_sightings(
        search.embedding, search.start, search.end, search.object_types, search.limit,
    )


@app.post("/search", response_model=schemas.TextSearchResponse, tags=["sightings"], dependencies=[Depends(require_api_key)])
def text_search(search: schemas.TextSearchRequest):
    """The web UI Search tab's own entry point -- takes plain query text (not a pre-computed
    embedding, unlike POST /search/semantic above, which is n8n's contract and stays untouched),
    embeds it server-side, then ranks by cosine similarity across sightings and/or visit_sightings
    (db.semantic_search_combined) depending on `source`. A browser can't call the embedding
    backend directly, so this is the one endpoint that does the embed-then-search round trip in a
    single call for it."""
    try:
        embedding = ai_worker.embed_query_text(search.query)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Embedding backend unavailable or misconfigured: {exc}",
        )
    resolved_start, resolved_end = _resolve_window(search.start, search.end, search.hours)
    results = db.semantic_search_combined(
        embedding, resolved_start, resolved_end, search.object_types, search.limit,
        source=search.source, max_distance=search.max_distance, query_text=search.query,
    )
    return {"results": results}


@app.post("/ai-queue/{event_id}/fail", response_model=schemas.FailResponse, tags=["ai-queue"], dependencies=[Depends(require_api_key)])
def fail_ai_event(
    event_id: int,
    max_attempts: int = Query(3, ge=1, description="Attempt count at/above which the event goes terminal 'failed'"),
):
    """Same retry-or-fail-with-cap logic as n8n's old Handle Failure (Retry or Fail) node --
    below max_attempts this goes back to ai_status='retry' (picked up on a future claim), at/above
    it goes terminal 'failed'."""
    return db.fail_ai_event(event_id, max_attempts)


@app.get("/admin/overview", tags=["admin"], dependencies=[Depends(require_api_key)])
def admin_overview():
    """Everything the /ui/admin dashboard needs for its fast-loading section -- row counts,
    per-stage queue status breakdown, embedding coverage, DB size, vector index health, retention
    info, and the feature-flag switches currently on. Deliberately excludes anything that's a real
    network/filesystem call (see /admin/disk-usage, /admin/embedding-backend/check) so this stays
    cheap enough to load on every dashboard visit."""
    missing_embeddings = db.count_sightings_missing_embedding()
    row_counts = db.get_table_row_counts()
    return {
        "row_counts": row_counts,
        "row_counts_by_object_type": db.get_row_counts_by_object_type(),
        "stage_counts": db.get_stage_counts(),
        "embeddings": {
            "sightings_missing": missing_embeddings["sightings"],
            "sightings_total": row_counts["sightings"],
            "visit_sightings_missing": missing_embeddings["visit_sightings"],
            "visit_sightings_total": row_counts["visit_sightings"],
        },
        "db_size": db.get_db_size_info(),
        "db_size_by_object_type": db.get_db_size_by_object_type(),
        "vector_index": db.get_vector_index_status(),
        "retention": db.get_retention_info(),
        "feature_flags": {
            "ai_events_stage_enabled": config.AI_EVENTS_STAGE_ENABLED,
            "ai_alerts_enabled": config.AI_ALERTS_ENABLED,
            "store_video": config.STORE_VIDEO,
            "store_video_alerts": config.STORE_VIDEO_ALERTS,
            "visit_thumb_crop_enabled": config.VISIT_THUMB_CROP_ENABLED,
            "crop_disabled": config.CROP_DISABLED,
            "frigate_snapshot_enabled": config.FRIGATE_SNAPSHOT_ENABLED,
            "telegram_events_mode": config.TELEGRAM_EVENTS_MODE,
            "telegram_alerts_mode": config.TELEGRAM_ALERTS_MODE,
        },
    }


@app.get("/admin/disk-usage", tags=["admin"], dependencies=[Depends(require_api_key)])
def admin_disk_usage():
    """Walks VIDEO_STORAGE_PATH/VIDEO_STORAGE_PATH_ALERTS on disk to report real bytes used --
    kept separate from /admin/overview since this is a real filesystem walk (can be slow with a
    large video backlog), not a cheap SQL query."""
    return {
        "video_storage": admin.dir_size_bytes(config.VIDEO_STORAGE_PATH),
        "video_storage_alerts": admin.dir_size_bytes(config.VIDEO_STORAGE_PATH_ALERTS),
        "video_storage_by_object_type": admin.dir_size_by_object_type(config.VIDEO_STORAGE_PATH),
        "video_storage_alerts_by_object_type": admin.dir_size_by_object_type(config.VIDEO_STORAGE_PATH_ALERTS),
    }


@app.get("/admin/embedding-backend/check", tags=["admin"], dependencies=[Depends(require_api_key)])
def admin_check_embedding_backend():
    """On-demand live smoke test against LLAMA_PROXY_BASE_URL/LLAMA_PROXY_EMBED_PATH -- confirms
    it answers at all and returns EMBEDDING_DIMENSIONS-sized vectors, the same check every real
    embedding call already makes. A real network call, so this is button-triggered from the
    dashboard rather than loaded automatically."""
    return admin.check_embedding_backend()


@app.post("/admin/vector/reindex", tags=["admin"], dependencies=[Depends(require_api_key)])
def admin_reindex_vector():
    """Rebuilds both HNSW embedding indexes in place -- fixes an invalid index (e.g. left behind
    by an interrupted build) and is a reasonable "tidy up" action after a large
    /embeddings/backfill run. Non-destructive to the underlying data either way."""
    return {"reindexed": db.reindex_vector_indexes()}


@app.post("/admin/queue/requeue-failed", tags=["admin"], dependencies=[Depends(require_api_key)])
def admin_requeue_failed(
    table: str = Query(..., description="'raw_events' or 'visits'"),
    stage: str = Query(..., description="raw_events: 'crop'/'video'/'ai'. visits: 'video'/'thumb_crop'."),
):
    """Resets every row currently stuck at {stage}_status='failed' back to 'retry' with a fresh
    attempt count, so the next poll tick/claim picks it back up -- the exact fix
    sql/queue-debug.sql's "retry every ai-failed item" query applies by hand, exposed as a real
    button instead of requiring direct psql access."""
    try:
        count = db.requeue_failed(table, stage)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"table": table, "stage": stage, "requeued": count}


# Static web report UI (index.html/app.js/style.css/vendor/*) -- all local files, no CDN
# requests. Calls back into GET /events, /events/{id}/thumbnail, /media/video/{id} above using an
# API key the user enters once and stores in a cookie. Baked into the image by the Dockerfile
# (COPY static/ ./static/); mounted last so it doesn't shadow any API route above.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/ui/admin", tags=["admin"], include_in_schema=False)
def admin_ui():
    """Clean URL for the admin dashboard (static/admin.html) -- registered before the /ui
    StaticFiles mount below so it isn't shadowed; StaticFiles(html=True) only auto-resolves
    index.html for a directory path, not arbitrary/admin -> admin.html, so this route is what
    makes /ui/admin (no .html) work. Same unauthenticated-page-plus-client-side-key pattern as
    /ui/index.html -- the page itself carries no data, every fetch() it makes still sends
    X-API-Key and is protected server-side same as any other admin endpoint."""
    return FileResponse(os.path.join(_STATIC_DIR, "admin.html"))


if os.path.isdir(_STATIC_DIR):
    app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")
