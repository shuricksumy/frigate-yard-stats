import base64
import os

import config
from video import _as_datetime


def _object_type(event: dict) -> str:
    # Each gathered image comes from one specific raw_event, which (today) always carries a
    # single Frigate label -- but be defensive the same way video._primary_object_type is, in case
    # `objects` is ever comma-joined here too.
    objects = (event.get("objects") or "").strip()
    if not objects:
        return "event"
    return objects.split(",")[0].strip() or "event"


def store_alert_images(visit: dict, events: list[dict], images: list[str]) -> list[str]:
    """Persists the alert stage's already-gathered high-res crops to disk -- ephemeral in memory
    only until this point, this is the one opt-in place (STORE_ALERT_IMAGES/store_alert_images)
    they're written out. `events`/`images` must be the same length and order (one raw_event per
    gathered image, as returned by alert_ai_worker._gather_alert_images) so each file can be named
    after its own source event's object type/id rather than the visit's overall representative
    type. Camera-first layout matching video.store_visit_clip -- deterministic filenames (visit id
    + index + event id, not a timestamp) mean a retried attempt overwrites the same files instead
    of accumulating duplicates on disk."""
    camera = visit.get("cameras") or "unknown"
    start = _as_datetime(visit["start_ts"])
    day_dir = os.path.join(
        config.ALERT_IMAGES_STORAGE_PATH, camera, f"{start:%Y}", f"{start:%m}", f"{start:%d}",
    )
    os.makedirs(day_dir, exist_ok=True)

    paths = []
    for index, (event, image_base64) in enumerate(zip(events, images)):
        object_type = _object_type(event)
        filename = f"visit-{object_type}-{visit['id']}-{index}-{event.get('id')}.jpg"
        path = os.path.join(day_dir, filename)
        with open(path, "wb") as f:
            f.write(base64.b64decode(image_base64))
        paths.append(path)
    return paths
