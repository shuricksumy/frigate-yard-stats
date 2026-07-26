"""Unit tests for alert_images.py -- persisting the alert stage's already-gathered high-res crops
to disk (STORE_ALERT_IMAGES). Pure filesystem tests (pytest's tmp_path), no Postgres/network
needed -- alert_images.store_alert_images itself never touches the database.
"""
import base64
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import alert_images  # noqa: E402
import config  # noqa: E402

_FAKE_JPEG = base64.b64encode(b"fake-jpeg-bytes").decode()


def test_store_alert_images_writes_one_file_per_image(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALERT_IMAGES_STORAGE_PATH", str(tmp_path))
    visit = {"id": 42, "cameras": "outside", "start_ts": "2026-01-15T10:30:00+00:00"}
    events = [
        {"id": 100, "objects": "car"},
        {"id": 101, "objects": "car"},
    ]
    images = [_FAKE_JPEG, _FAKE_JPEG]

    paths = alert_images.store_alert_images(visit, events, images)

    assert len(paths) == 2
    for path in paths:
        assert os.path.isfile(path)
        with open(path, "rb") as f:
            assert f.read() == b"fake-jpeg-bytes"


def test_store_alert_images_camera_first_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALERT_IMAGES_STORAGE_PATH", str(tmp_path))
    visit = {"id": 42, "cameras": "outside2", "start_ts": "2026-03-01T05:00:00+00:00"}
    paths = alert_images.store_alert_images(visit, [{"id": 5, "objects": "person"}], [_FAKE_JPEG])

    expected_dir = os.path.join(str(tmp_path), "outside2", "2026", "03", "01")
    assert os.path.dirname(paths[0]) == expected_dir


def test_store_alert_images_filenames_use_each_events_own_object_type(tmp_path, monkeypatch):
    # A mixed-type visit (e.g. a car and a person) must name each file after its own source
    # event's type, not the visit's overall representative type -- otherwise the admin dashboard's
    # by-object-type disk-usage breakdown would misattribute bytes.
    monkeypatch.setattr(config, "ALERT_IMAGES_STORAGE_PATH", str(tmp_path))
    visit = {"id": 7, "cameras": "outside", "start_ts": "2026-01-01T00:00:00+00:00"}
    events = [{"id": 200, "objects": "car"}, {"id": 201, "objects": "person"}]
    paths = alert_images.store_alert_images(visit, events, [_FAKE_JPEG, _FAKE_JPEG])

    names = [os.path.basename(p) for p in paths]
    assert names[0].startswith("visit-car-7-0-200")
    assert names[1].startswith("visit-person-7-1-201")


def test_store_alert_images_missing_camera_buckets_under_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALERT_IMAGES_STORAGE_PATH", str(tmp_path))
    visit = {"id": 1, "cameras": None, "start_ts": "2026-01-01T00:00:00+00:00"}
    paths = alert_images.store_alert_images(visit, [{"id": 1, "objects": "car"}], [_FAKE_JPEG])
    assert os.path.join(str(tmp_path), "unknown") in paths[0]


def test_store_alert_images_retry_overwrites_same_files(tmp_path, monkeypatch):
    # Deterministic filenames (visit id + index + event id, not a timestamp) mean a retried
    # attempt overwrites rather than accumulating duplicates on disk.
    monkeypatch.setattr(config, "ALERT_IMAGES_STORAGE_PATH", str(tmp_path))
    visit = {"id": 3, "cameras": "outside", "start_ts": "2026-01-01T00:00:00+00:00"}
    events = [{"id": 9, "objects": "car"}]

    first = alert_images.store_alert_images(visit, events, [_FAKE_JPEG])
    second = alert_images.store_alert_images(visit, events, [_FAKE_JPEG])

    assert first == second
    day_dir = os.path.dirname(first[0])
    assert len(os.listdir(day_dir)) == 1
