"""Tests for report.py's alerts-report grouping (source="visits") -- a visit's sightings (any
mix of object types) are combined into one alert row (image + one labeled line per sighting)
instead of separate per-type tables, since they belong to the same real-world activity.

_group_by_visit/_img_cell/_build_alert_rows are pure functions (no DB), so most of this runs
without Postgres. The end-to-end generate_report() test at the bottom does need a reachable
Postgres with schema.sql applied -- see test_db_video_queue.py's module docstring.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import db  # noqa: E402
import report  # noqa: E402

# A real, tiny (4x4) decodable JPEG -- needed wherever _img_cell actually reaches
# crop.scale_image_base64 (its ffmpeg call fails on a fake non-image string like "grid-image-b64").
_TINY_JPEG_BASE64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcp"
    "LDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjL/wAARCAAEAAQDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
    "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
    "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
    "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
    "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
    "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDi6KKK+ZP3E//Z"
)


def test_group_by_visit_combines_car_and_person_of_same_visit():
    t0 = datetime(2026, 7, 17, 10, 0, 0)
    t1 = datetime(2026, 7, 17, 10, 0, 5)
    car = {
        "visit_id": 42, "raw_event_id": 1, "start_ts": t1, "camera": "outside2",
        "crop_image_base64": "car-crop", "object_label": "car", "description": "orange suv",
    }
    person = {
        "visit_id": 42, "raw_event_id": 2, "start_ts": t0, "camera": "outside2",
        "crop_image_base64": "person-crop", "object_label": "person", "description": "dark jacket",
    }
    groups = report._group_by_visit([car, person])
    assert len(groups) == 1
    group = groups[0]
    assert group["sightings"] == [car, person]
    # Earliest sighting (person, t0) represents the group's time/image.
    assert group["start_ts"] == t0
    assert group["crop_image_base64"] == "person-crop"


def test_group_by_visit_keeps_ungrouped_sightings_separate():
    t = datetime(2026, 7, 17, 10, 0, 0)
    a = {"visit_id": None, "raw_event_id": 1, "start_ts": t, "camera": "outside2", "crop_image_base64": "a"}
    b = {"visit_id": None, "raw_event_id": 2, "start_ts": t, "camera": "outside2", "crop_image_base64": "b"}
    groups = report._group_by_visit([a, b])
    assert len(groups) == 2


def test_img_cell_prefers_gif_over_grid_image():
    cell = report._img_cell(_TINY_JPEG_BASE64, [], [0], "preview-gif-b64")
    assert "data:image/gif;base64,preview-gif-b64" in cell
    assert _TINY_JPEG_BASE64 not in cell
    # No lightbox for the GIF case -- embedding the same bytes a second time would reintroduce the
    # double-embed bloat this report already avoids for the JPEG case.
    assert cell.count("<img") == 1


def test_img_cell_falls_back_to_grid_image_without_gif():
    lightboxes = []
    cell = report._img_cell(_TINY_JPEG_BASE64, lightboxes, [0])
    assert f"data:image/jpeg;base64,{_TINY_JPEG_BASE64}" in lightboxes[0]
    assert "image/gif" not in cell


def test_group_by_visit_carries_preview_gif_from_earliest_sighting():
    t0 = datetime(2026, 7, 17, 10, 0, 0)
    t1 = datetime(2026, 7, 17, 10, 0, 5)
    car = {
        "visit_id": 42, "raw_event_id": 1, "start_ts": t1, "camera": "outside2",
        "crop_image_base64": "car-crop", "preview_gif_base64": "visit-gif",
        "object_label": "car", "description": "orange suv",
    }
    person = {
        "visit_id": 42, "raw_event_id": 2, "start_ts": t0, "camera": "outside2",
        "crop_image_base64": "person-crop", "preview_gif_base64": "visit-gif",
        "object_label": "person", "description": "dark jacket",
    }
    group = report._group_by_visit([car, person])[0]
    assert group["preview_gif_base64"] == "visit-gif"


def test_build_alert_rows_orders_newest_first():
    older = {
        "visit_id": None, "raw_event_id": 1, "start_ts": datetime(2026, 7, 17, 9, 0, 0),
        "camera": "outside2", "crop_image_base64": None, "object_label": "car", "description": "red",
    }
    newer = {
        "visit_id": None, "raw_event_id": 2, "start_ts": datetime(2026, 7, 17, 10, 0, 0),
        "camera": "outside2", "crop_image_base64": None, "object_label": "car", "description": "blue",
    }
    html = report._build_alert_rows([older, newer], [], [0])
    assert html.index("blue") < html.index("red")


def test_build_alert_rows_renders_both_summaries_in_one_row():
    t = datetime(2026, 7, 17, 10, 0, 0)
    car = {
        "visit_id": 1, "raw_event_id": 1, "start_ts": t, "camera": "outside2",
        "crop_image_base64": None, "object_label": "car", "description": "silver sedan, plate 10MO407",
    }
    person = {
        "visit_id": 1, "raw_event_id": 2, "start_ts": t, "camera": "outside2",
        "crop_image_base64": None, "object_label": "person", "description": "dark jacket",
    }
    html = report._build_alert_rows([car, person], [], [0])
    assert "silver sedan" in html
    assert "10MO407" in html
    assert "dark jacket" in html
    # Both summaries land in the same <tr> -- one alert row, not two.
    assert html.count("<tr>") == 1


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def _insert_raw_event(start_ts_expr="now()", objects="car"):
    # No crop_image_base64 -- _img_cell's "(no image)" branch, same as test_report.py's fixture,
    # avoids needing a real decodable JPEG just to exercise the grouping/summary logic here.
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        f"""
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status)
        VALUES ('pytest-cam', 'pytest-zone', %s, {start_ts_expr}, {start_ts_expr}, %s, true, true,
                'done', 'done')
        RETURNING id, det_id
        """,
        (objects, det_id), fetch=True,
    )
    return rows[0]["id"], rows[0]["det_id"]


def _insert_sighting(raw_event_id: int, object_label: str, description: str) -> int:
    rows = db._execute(
        "INSERT INTO yard_stats.sightings (raw_event_id, object_label, description) VALUES (%s, %s, %s) RETURNING id",
        (raw_event_id, object_label, description), fetch=True,
    )
    return rows[0]["id"]


def _cleanup(*raw_event_ids, visit_id=None):
    db._execute("DELETE FROM yard_stats.sightings WHERE raw_event_id = ANY(%s)", (list(raw_event_ids),))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(raw_event_ids),))
    if visit_id is not None:
        db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


def test_generate_report_visits_combines_car_and_person_into_one_alert(conn_ok):
    car_id, car_det = _insert_raw_event(objects="car")
    person_id, person_det = _insert_raw_event(objects="person")
    _insert_sighting(car_id, "car", "silver sedan")
    _insert_sighting(person_id, "person", "dark jacket")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car,person",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [car_det, person_det],
    })
    try:
        now = datetime.now(timezone.utc)
        result = report.generate_report(now - timedelta(hours=1), now + timedelta(hours=1), source="visits")
        assert "1</b> alert(s)" in result["html"]
        assert "silver sedan" in result["html"]
        assert "dark jacket" in result["html"]
        assert result["sighting_count"] == 2
    finally:
        _cleanup(car_id, person_id, visit_id=visit_id)


def test_generate_report_include_preview_image_omits_gif(conn_ok):
    raw_id, det_id = _insert_raw_event(objects="car")
    _insert_sighting(raw_id, "car", "silver sedan")
    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": [det_id],
    })
    db.mark_visit_thumb_crop_done(visit_id, _TINY_JPEG_BASE64, "visit-gif")
    try:
        now = datetime.now(timezone.utc)
        window = (now - timedelta(hours=1), now + timedelta(hours=1))
        with_gif = report.generate_report(*window, source="visits", include_preview="gif")
        image_only = report.generate_report(*window, source="visits", include_preview="image")
        none_mode = report.generate_report(*window, source="visits", include_preview="none")
        assert "image/gif" in with_gif["html"]
        assert "image/gif" not in image_only["html"]
        # Falls back to the visit's own static grid crop, not "(no image)" -- "image" mode only
        # drops the GIF, not the crop preference source=visits already applies.
        assert _TINY_JPEG_BASE64 in image_only["html"]
        # "none" drops the image entirely -- neither the GIF nor the static crop appear.
        assert "image/gif" not in none_mode["html"]
        assert _TINY_JPEG_BASE64 not in none_mode["html"]
        assert "(no image)" in none_mode["html"]
    finally:
        _cleanup(raw_id, visit_id=visit_id)
