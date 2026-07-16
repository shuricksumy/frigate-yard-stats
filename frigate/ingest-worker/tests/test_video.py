import os
from datetime import datetime, timezone

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import video  # noqa: E402


def test_build_clip_url_pads_by_5_seconds_each_side():
    row = {"camera": "outside2", "start_ts": datetime(2026, 7, 15, 22, 22, 55, tzinfo=timezone.utc),
           "end_ts": datetime(2026, 7, 15, 22, 22, 57, 961338, tzinfo=timezone.utc)}
    url = video.build_clip_url(row)
    start_epoch = int(row["start_ts"].timestamp())
    end_epoch = int(row["end_ts"].timestamp())
    assert url == (
        f"{video.config.FRIGATE_API_BASE}/api/outside2/start/{start_epoch - 5}/end/{end_epoch + 5}/clip.mp4"
    )


def test_build_clip_url_accepts_iso_strings():
    row = {"camera": "front_yard", "start_ts": "2026-07-15T22:22:55+00:00",
           "end_ts": "2026-07-15T22:22:57+00:00"}
    url = video.build_clip_url(row)
    assert url.startswith(f"{video.config.FRIGATE_API_BASE}/api/front_yard/start/")
    assert "/end/" in url and url.endswith("/clip.mp4")


def test_build_clip_url_accepts_epoch_numbers():
    row = {"camera": "outside", "start_ts": 1784154175.561241, "end_ts": 1784154177.961338}
    url = video.build_clip_url(row)
    assert url == f"{video.config.FRIGATE_API_BASE}/api/outside/start/1784154170/end/1784154182/clip.mp4"


def test_primary_object_type_single_label():
    assert video._primary_object_type({"objects": "car"}) == "car"


def test_primary_object_type_comma_joined_takes_first():
    assert video._primary_object_type({"objects": "car,truck"}) == "car"


def test_primary_object_type_empty_falls_back_to_event():
    assert video._primary_object_type({"objects": ""}) == "event"
    assert video._primary_object_type({"objects": None}) == "event"
    assert video._primary_object_type({}) == "event"


def test_store_clip_writes_date_partitioned_path(tmp_path, monkeypatch):
    monkeypatch.setattr(video.config, "VIDEO_STORAGE_PATH", str(tmp_path))
    row = {"id": 42, "objects": "car", "start_ts": datetime(2026, 7, 15, 22, 22, 55, tzinfo=timezone.utc)}
    content = b"fake-mp4-bytes"

    path = video.store_clip(row, content)

    expected_path = tmp_path / "2026" / "07" / "15" / "car-42-1784154175.mp4"
    assert path == str(expected_path)
    assert expected_path.read_bytes() == content


def test_store_clip_falls_back_to_event_for_empty_objects(tmp_path, monkeypatch):
    monkeypatch.setattr(video.config, "VIDEO_STORAGE_PATH", str(tmp_path))
    row = {"id": 7, "objects": None, "start_ts": datetime(2026, 1, 1, tzinfo=timezone.utc)}

    path = video.store_clip(row, b"x")

    assert os.path.basename(path).startswith("event-7-")


def test_download_clip_raises_clip_not_ready_below_min_bytes(monkeypatch):
    class FakeResponse:
        content = b"x" * 10  # below default VIDEO_MIN_VALID_BYTES (1000)

        def raise_for_status(self):
            pass

    monkeypatch.setattr(video.requests, "get", lambda *a, **k: FakeResponse())
    row = {"camera": "outside2", "start_ts": 1784154175, "end_ts": 1784154177, "det_id": "abc"}

    try:
        video.download_clip(row)
        assert False, "expected ClipNotReadyError"
    except video.ClipNotReadyError:
        pass
