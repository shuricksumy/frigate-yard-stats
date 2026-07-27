"""Tests for send-to-Telegram-without-storing -- when storage is disabled for a type but Telegram
still wants a video, video_worker.py/alert_video_worker.py send the clip straight from the
in-memory buffer download_clip already returns, never touching store_clip/store_visit_clip, and
mark the row/visit 'done' with a NULL video_path on success. A failed send with nothing stored is a
genuine failure (unlike the storage-enabled path, where a Telegram hiccup is logged only) and goes
through the same retry-or-fail-with-cap handling as a download failure. Unit tests only
(monkeypatches db/video/telegram) -- no Postgres or network required.
"""
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import alert_video_worker  # noqa: E402
import config  # noqa: E402
import video_worker  # noqa: E402


# ---- video_worker.process_claimed_event ----

def test_storage_off_telegram_on_sends_from_memory_and_marks_done_with_no_path(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "video")
    monkeypatch.setattr(video_worker.video, "download_clip", lambda row: b"fake-bytes")

    def fail_store_clip(*a, **k):
        raise AssertionError("store_clip should never be called when storage is disabled")
    monkeypatch.setattr(video_worker.video, "store_clip", fail_store_clip)

    sent = {}
    def fake_send_video(content, filename, caption, reply_to_message_id=None, mode=None):
        sent["content"] = content
        sent["filename"] = filename
        return True
    monkeypatch.setattr(video_worker.telegram, "send_video", fake_send_video)

    marked = {}
    monkeypatch.setattr(video_worker.db, "mark_video_done", lambda event_id, path: marked.update(id=event_id, path=path))
    monkeypatch.setattr(video_worker.db, "mark_video_retry_or_failed", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not retry/fail on a successful send")))

    row = {"id": 7, "objects": "car", "video_attempt_count": 1, "det_id": "d1"}
    video_worker.process_claimed_event(row, profile=None)

    assert sent["content"] == b"fake-bytes"
    assert marked == {"id": 7, "path": None}


def test_storage_off_telegram_send_failure_goes_to_retry(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "video")
    monkeypatch.setattr(config, "VIDEO_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(video_worker.video, "download_clip", lambda row: b"fake-bytes")
    monkeypatch.setattr(video_worker.telegram, "send_video", lambda *a, **k: False)  # send failed

    marked = {}
    monkeypatch.setattr(video_worker.db, "mark_video_retry_or_failed", lambda event_id, max_attempts: marked.update(
        id=event_id, max_attempts=max_attempts,
    ))
    monkeypatch.setattr(video_worker.db, "mark_video_done", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not mark done when the send failed and nothing was stored")))
    monkeypatch.setattr(video_worker.time, "sleep", lambda *_: None)

    row = {"id": 8, "objects": "car", "video_attempt_count": 1, "det_id": "d1"}
    video_worker.process_claimed_event(row, profile=None)

    assert marked == {"id": 8, "max_attempts": 5}


def test_storage_on_still_stores_and_sends_from_the_same_buffer(monkeypatch):
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", True)
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "video")
    monkeypatch.setattr(video_worker.video, "download_clip", lambda row: b"fake-bytes")
    monkeypatch.setattr(video_worker.video, "store_clip", lambda row, content: "/data/video/car-9.mp4")

    sent = {}
    monkeypatch.setattr(video_worker.telegram, "send_video", lambda content, *a, **k: sent.update(content=content) or True)

    marked = {}
    monkeypatch.setattr(video_worker.db, "mark_video_done", lambda event_id, path: marked.update(id=event_id, path=path))

    row = {"id": 9, "objects": "car", "video_attempt_count": 1, "det_id": "d1"}
    video_worker.process_claimed_event(row, profile=None)

    assert marked == {"id": 9, "path": "/data/video/car-9.mp4"}
    assert sent["content"] == b"fake-bytes"


def test_storage_on_telegram_send_failure_does_not_undo_the_successful_storage(monkeypatch):
    # Storage already succeeded -- a Telegram hiccup here is logged only, matching the pre-
    # existing behavior from before send-without-store existed (see video_worker.py's comment).
    monkeypatch.setattr(config, "STORE_VIDEO_EVENTS", True)
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "video")
    monkeypatch.setattr(video_worker.video, "download_clip", lambda row: b"fake-bytes")
    monkeypatch.setattr(video_worker.video, "store_clip", lambda row, content: "/data/video/car-10.mp4")
    monkeypatch.setattr(video_worker.telegram, "send_video", lambda *a, **k: False)

    marked = {}
    monkeypatch.setattr(video_worker.db, "mark_video_done", lambda event_id, path: marked.update(id=event_id, path=path))
    monkeypatch.setattr(video_worker.db, "mark_video_retry_or_failed", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("storage already succeeded -- must not retry/fail the row")))

    row = {"id": 10, "objects": "car", "video_attempt_count": 1, "det_id": "d1"}
    video_worker.process_claimed_event(row, profile=None)

    assert marked == {"id": 10, "path": "/data/video/car-10.mp4"}


# ---- alert_video_worker.process_claimed_visit (same shape, alerts flow) ----

def test_visit_storage_off_telegram_on_sends_from_memory_and_marks_done_with_no_path(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "video")
    monkeypatch.setattr(alert_video_worker.db, "get_representative_event_for_visit", lambda visit_id: {"objects": "car"})
    monkeypatch.setattr(alert_video_worker.db, "count_events_for_visit", lambda visit_id: 1)
    monkeypatch.setattr(alert_video_worker.video, "download_clip", lambda row: b"fake-bytes")

    def fail_store(*a, **k):
        raise AssertionError("store_visit_clip should never be called when storage is disabled")
    monkeypatch.setattr(alert_video_worker.video, "store_visit_clip", fail_store)

    sent = {}
    monkeypatch.setattr(alert_video_worker.telegram, "send_visit_video", lambda content, *a, **k: sent.update(content=content) or True)

    marked = {}
    monkeypatch.setattr(alert_video_worker.db, "mark_visit_video_done", lambda visit_id, path: marked.update(id=visit_id, path=path))
    monkeypatch.setattr(alert_video_worker.db, "mark_visit_video_retry_or_failed", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not retry/fail on a successful send")))

    visit = {"id": 42, "start_ts": "2026-01-01T00:00:00", "end_ts": "2026-01-01T00:01:00", "cameras": "outside", "objects": "car", "video_attempt_count": 1}
    alert_video_worker.process_claimed_visit(visit, profile=None)

    assert sent["content"] == b"fake-bytes"
    assert marked == {"id": 42, "path": None}


def test_visit_storage_off_telegram_send_failure_goes_to_retry(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "video")
    monkeypatch.setattr(config, "VIDEO_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(alert_video_worker.db, "get_representative_event_for_visit", lambda visit_id: {"objects": "car"})
    monkeypatch.setattr(alert_video_worker.db, "count_events_for_visit", lambda visit_id: 1)
    monkeypatch.setattr(alert_video_worker.video, "download_clip", lambda row: b"fake-bytes")
    monkeypatch.setattr(alert_video_worker.telegram, "send_visit_video", lambda *a, **k: False)

    marked = {}
    monkeypatch.setattr(alert_video_worker.db, "mark_visit_video_retry_or_failed", lambda visit_id, max_attempts: marked.update(
        id=visit_id, max_attempts=max_attempts,
    ))
    monkeypatch.setattr(alert_video_worker.db, "mark_visit_video_done", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not mark done when the send failed and nothing was stored")))
    monkeypatch.setattr(alert_video_worker.time, "sleep", lambda *_: None)

    visit = {"id": 43, "start_ts": "2026-01-01T00:00:00", "end_ts": "2026-01-01T00:01:00", "cameras": "outside", "objects": "car", "video_attempt_count": 1}
    alert_video_worker.process_claimed_visit(visit, profile=None)

    assert marked == {"id": 43, "max_attempts": 5}
