import base64
import os

import config
from video import _as_datetime, _primary_object_type


def store_event_image(row: dict, image_base64: str) -> str:
    """Persists the full-resolution crop crop.crop_event already built for this event to disk --
    the AI-facing copy in crop_image_base64 is always a downscale of this same crop, kept in
    Postgres regardless; this is the one opt-in place (STORE_EVENT_IMAGES/store_event_images) the
    full-resolution original is written out. Same camera-first layout/filename convention
    video.store_clip already established (object_type-id-epoch-iso.jpg) so admin.dir_size_bytes/
    dir_size_by_object_type/dir_size_by_camera apply unchanged -- deterministic filenames mean a
    retried attempt overwrites the same file rather than accumulating duplicates on disk."""
    camera = row.get("camera") or "unknown"
    start = _as_datetime(row["start_ts"])
    day_dir = os.path.join(
        config.EVENT_IMAGES_STORAGE_PATH, camera, f"{start:%Y}", f"{start:%m}", f"{start:%d}",
    )
    os.makedirs(day_dir, exist_ok=True)

    object_type = _primary_object_type(row)
    filename = f"{object_type}-{row['id']}-{int(start.timestamp())}-{start:%Y%m%dT%H%M%SZ}.jpg"
    path = os.path.join(day_dir, filename)
    with open(path, "wb") as f:
        f.write(base64.b64decode(image_base64))
    return path
