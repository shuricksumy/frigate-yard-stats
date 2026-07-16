from datetime import datetime

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


class VehicleSighting(BaseModel):
    id: int
    raw_event_id: int
    camera: str
    zone: str | None
    start_ts: datetime
    color: str | None
    body_type: str | None
    make_guess: str | None
    make_confidence: str | None
    model_guess: str | None
    model_confidence: str | None
    notable_features: str | None
    plate_text_llm: str | None
    plate_text_frigate: str | None
    plate_confidence: str | None
    notes: str | None


class PersonSighting(BaseModel):
    id: int
    raw_event_id: int
    camera: str
    zone: str | None
    start_ts: datetime
    description: str | None
    notes: str | None


class EventDetail(EventSummary):
    det_id: str | None
    crop_image_base64: str | None
    created_at: datetime
    # Populated only once ai_status='done' and a matching row exists -- at most one of the two is
    # ever non-null (a raw_event is either a vehicle or a person, never both).
    vehicle_sighting: VehicleSighting | None = None
    person_sighting: PersonSighting | None = None


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
    has_image: bool
    has_video: bool


class VisitSightings(BaseModel):
    # One representative sighting per distinct object type the visit grouped together (see
    # claim_ai_batch's only_visit_representative comment in db.py) -- e.g. a car and a person in
    # the same visit each show up here, rather than just whichever was analyzed first.
    vehicles: list[VehicleSighting]
    persons: list[PersonSighting]


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
    total_vehicle_sightings: int
    total_person_sightings: int
    by_camera: list[CameraCount]
    by_object_type: list[ObjectTypeCount]
    by_day: list[DayCount]


class ReportResponse(BaseModel):
    start: datetime
    end: datetime
    html: str
    caption: str
    car_count: int
    person_count: int


class VehicleSightingCreate(BaseModel):
    raw_event_id: int
    color: str | None = None
    body_type: str | None = None
    make_guess: str | None = None
    make_confidence: str | None = None
    model_guess: str | None = None
    model_confidence: str | None = None
    notable_features: str | None = None
    plate_text_llm: str | None = None
    plate_text_frigate: str | None = None
    plate_confidence: str | None = None
    notes: str | None = None


class PersonSightingCreate(BaseModel):
    raw_event_id: int
    description: str | None = None
    notes: str | None = None


class SightingCreated(BaseModel):
    id: int
    ai_status: str = "done"


class FailResponse(BaseModel):
    ai_status: str
    ai_attempt_count: int


class ClaimResponse(BaseModel):
    # Wrapped rather than a bare array -- an HTTP Request node's raw JSON-array response doesn't
    # reliably auto-split into n8n items across versions. n8n uses an explicit Split Out node on
    # the "events" field instead, which is unambiguous, version-stable behavior.
    events: list[EventDetail]
