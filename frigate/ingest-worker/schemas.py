from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class EventSummary(BaseModel):
    id: int
    camera: str
    zone: str | None
    objects: str | None
    start_ts: datetime
    end_ts: datetime
    crop_status: str
    ai_status: str
    video_status: str
    has_video: bool
    has_image: bool
    sub_label: str | None
    score: float | None


class Sighting(BaseModel):
    # Universal shape -- one row per AI-analyzed event, any object type. object_label carries the
    # actual Frigate label (car/truck/person/dog/whatever profiles.yaml has a prompt for);
    # description is the VLM's own free-text answer, whatever profiles.yaml's event_prompt asked
    # it to describe for that label. No per-type fields (color/plate/etc.) -- see CLAUDE.md's
    # "Universal sightings" section for why.
    id: int
    raw_event_id: int
    camera: str
    zone: str | None
    start_ts: datetime
    object_label: str | None
    description: str | None


class EventDetail(EventSummary):
    det_id: str | None
    crop_image_base64: str | None
    created_at: datetime
    # Populated only once ai_status='done' and a matching row exists.
    sighting: Sighting | None = None


class VisitSummary(BaseModel):
    id: int
    zone: str | None
    objects: str | None
    cameras: str | None
    camera_count: int | None
    start_ts: datetime
    end_ts: datetime
    event_count: int
    representative_event_id: int
    ai_status: str
    crop_status: str
    video_status: str
    thumb_crop_status: str
    has_thumb_crop: bool
    has_preview_gif: bool
    has_image: bool
    has_video: bool


class AlertSighting(BaseModel):
    # The visit's own alert-stage (composite grid) analysis -- same universal shape as Sighting,
    # just keyed by visit_id instead of raw_event_id.
    id: int
    visit_id: int
    object_label: str | None
    description: str | None


class VisitSightings(BaseModel):
    # Every sighting linked to this visit, not just the representative event's -- claim_ai_batch's
    # only_visit_representative partitions by (visit_id, objects), so a visit can have more than
    # one analyzed event: one representative per distinct object type (a car and a person in the
    # same visit each get their own sighting), not just one per visit. One universal list now --
    # there's no vehicles/persons split anywhere in this model.
    sightings: list[Sighting]
    # The visit's own alert-stage analysis (AI_ALERTS_ENABLED), independent of sightings above --
    # null until that stage has produced one (feature off, or this visit's grid isn't ready yet).
    # The web UI prefers this when present, falling back to sightings otherwise.
    alert_sighting: AlertSighting | None = None


class CameraCount(BaseModel):
    camera: str
    count: int


class ObjectTypeCount(BaseModel):
    objects: str
    count: int


class DayCount(BaseModel):
    day: str
    count: int


class StatsSummary(BaseModel):
    start: datetime
    end: datetime
    total_events: int
    total_sightings: int
    by_camera: list[CameraCount]
    by_object_type: list[ObjectTypeCount]
    by_day: list[DayCount]


class ReportResponse(BaseModel):
    start: datetime
    end: datetime
    html: str
    caption: str
    sighting_count: int


class SightingCreate(BaseModel):
    # Replaces the former VehicleSightingCreate/PersonSightingCreate split -- one universal
    # completion shape, POSTed by n8n's own AI-analysis workflow (or the internal ai_worker.py,
    # which calls db.complete_sighting directly rather than over HTTP).
    raw_event_id: int
    object_label: str | None = None
    description: str | None = None
    # Optional: n8n (or the internal AI stage's own _embed_text) computes this (currently
    # Qwen3-Embedding-0.6B-GGUF, 1024 dims -- must match config.EMBEDDING_DIMENSIONS/schema.sql's
    # embedding columns) before calling this endpoint. Omitted or null means this sighting just
    # isn't semantically searchable, not an error.
    embedding: list[float] | None = None


class SightingCreated(BaseModel):
    id: int
    ai_status: str = "done"


class SemanticSearchRequest(BaseModel):
    # A vector, not free text, because ingest-worker never calls an embedding model itself -- the
    # caller (n8n) already resolved the query text to a vector using the same model that wrote the
    # stored sightings' embeddings before calling this.
    embedding: list[float]
    start: datetime | None = None
    end: datetime | None = None
    # Filters by the actual Frigate label (object_label) directly now -- e.g. ["car", "dog"] --
    # rather than the old pseudo-categories ("vehicle"/"person") the two-table split required.
    object_types: list[str] | None = None
    limit: int = 10


class SemanticSearchResult(BaseModel):
    sighting_id: int
    raw_event_id: int
    start_ts: datetime
    camera: str
    objects: str | None
    object_label: str | None
    description: str | None = None
    distance: float


class FailResponse(BaseModel):
    ai_status: str
    ai_attempt_count: int


class TextSearchRequest(BaseModel):
    # Free text, not a vector -- unlike SemanticSearchRequest above (the n8n-facing contract,
    # which already resolved its own embedding before calling that endpoint), this is the web UI
    # Search tab's own entry point: ingest-worker embeds this text itself (POST /search, via
    # ai_worker.embed_query_text) before searching, since a browser can't call the embedding
    # backend directly.
    query: str
    start: datetime | None = None
    end: datetime | None = None
    # Convenience alternative to start/end, same "last N hours" preset the web UI's other tabs
    # already use -- ignored if start/end are both given.
    hours: float = 24
    object_types: list[str] | None = None
    # None (default) searches both sightings and visit_sightings, unioned and re-ranked together.
    source: Literal["events", "visits"] | None = None
    limit: int = 20
    # Cosine-distance cutoff (lower = stricter/more confident) -- without this, a query with fewer
    # than `limit` genuinely relevant sightings still pads the response out to `limit` with
    # whatever's next-closest, which can be barely-related filler once the embedding model's own
    # discriminative power runs out (confirmed in practice: for a small/general embedding model,
    # a true match and an unrelated one can land within ~0.05 of each other). None (default) keeps
    # today's behavior -- no cutoff, always exactly `limit` results when that many exist.
    # db.semantic_search_combined also always includes any sighting whose description contains
    # `query` as a whole word (case-insensitive), even past this cutoff -- a sighting can mention
    # the exact query word only as a minor/trailing detail in an otherwise-unrelated sentence,
    # landing just outside a strict cutoff on distance alone despite the literal word being present
    # (confirmed: "...an adult in a grey t-shirt... with a small dog nearby" scored 0.457 for query
    # "dog", just past a 0.45 cutoff) -- a cutoff should never hide a literal keyword match. This is
    # a whole-word match, not a plain substring one -- a plain substring match on a short query like
    # "cat" would wrongly match "indication"/"location"/"vacation" (confirmed live: it returned 24
    # completely unrelated results, none actually about a cat).
    max_distance: float | None = None


class TextSearchResult(BaseModel):
    # kind + id (not a single flat id) since raw_event ids and visit ids are independent
    # sequences that can collide -- the web UI needs kind to know which lightbox to open.
    kind: Literal["event", "visit"]
    id: int
    sighting_id: int
    start_ts: datetime
    camera: str | None
    objects: str | None
    object_label: str | None
    description: str | None = None
    distance: float
    # Same fields EventSummary/VisitSummary already expose -- lets the web UI open a result
    # straight into the existing lightbox with no follow-up fetch (there's no GET /visits/{id}
    # single-item endpoint to fetch these for a visit-kind result on demand).
    has_image: bool
    has_video: bool
    has_preview_gif: bool
    ai_status: str


class TextSearchResponse(BaseModel):
    results: list[TextSearchResult]


class ClaimResponse(BaseModel):
    # Wrapped rather than a bare array -- an HTTP Request node's raw JSON-array response doesn't
    # reliably auto-split into n8n items across versions. n8n uses an explicit Split Out node on
    # the "events" field instead, which is unambiguous, version-stable behavior.
    events: list[EventDetail]
