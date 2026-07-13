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
    sub_label: str | None
    score: float | None


class EventDetail(EventSummary):
    det_id: str | None
    crop_image_base64: str | None
    created_at: datetime


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
