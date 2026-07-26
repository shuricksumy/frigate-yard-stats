"""Unit tests for event_images.py -- persisting the events stage's full-resolution crop to disk
(STORE_EVENT_IMAGES). Pure filesystem tests (pytest's tmp_path), no Postgres/network needed --
event_images.store_event_image itself never touches the database.
"""
import base64
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import config  # noqa: E402
import event_images  # noqa: E402

_FAKE_JPEG = base64.b64encode(b"fake-jpeg-bytes").decode()


def test_store_event_image_writes_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EVENT_IMAGES_STORAGE_PATH", str(tmp_path))
    row = {"id": 100, "objects": "car", "camera": "outside", "start_ts": "2026-01-15T10:30:00+00:00"}

    path = event_images.store_event_image(row, _FAKE_JPEG)

    assert os.path.isfile(path)
    with open(path, "rb") as f:
        assert f.read() == b"fake-jpeg-bytes"


def test_store_event_image_camera_first_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EVENT_IMAGES_STORAGE_PATH", str(tmp_path))
    row = {"id": 5, "objects": "person", "camera": "outside2", "start_ts": "2026-03-01T05:00:00+00:00"}

    path = event_images.store_event_image(row, _FAKE_JPEG)

    expected_dir = os.path.join(str(tmp_path), "outside2", "2026", "03", "01")
    assert os.path.dirname(path) == expected_dir


def test_store_event_image_filename_uses_the_events_own_object_type(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EVENT_IMAGES_STORAGE_PATH", str(tmp_path))
    row = {"id": 200, "objects": "car", "camera": "outside", "start_ts": "2026-01-01T00:00:00+00:00"}

    path = event_images.store_event_image(row, _FAKE_JPEG)

    assert os.path.basename(path).startswith("car-200-")


def test_store_event_image_missing_camera_buckets_under_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EVENT_IMAGES_STORAGE_PATH", str(tmp_path))
    row = {"id": 1, "objects": "car", "camera": None, "start_ts": "2026-01-01T00:00:00+00:00"}

    path = event_images.store_event_image(row, _FAKE_JPEG)

    assert os.path.join(str(tmp_path), "unknown") in path


def test_store_event_image_retry_overwrites_same_file(tmp_path, monkeypatch):
    # Deterministic filename (object type + event id + start_ts, not a fresh timestamp) means a
    # retried attempt overwrites rather than accumulating duplicates on disk.
    monkeypatch.setattr(config, "EVENT_IMAGES_STORAGE_PATH", str(tmp_path))
    row = {"id": 9, "objects": "car", "camera": "outside", "start_ts": "2026-01-01T00:00:00+00:00"}

    first = event_images.store_event_image(row, _FAKE_JPEG)
    second = event_images.store_event_image(row, _FAKE_JPEG)

    assert first == second
    day_dir = os.path.dirname(first)
    assert len(os.listdir(day_dir)) == 1
