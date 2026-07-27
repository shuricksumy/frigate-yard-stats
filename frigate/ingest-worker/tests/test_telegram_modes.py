"""Tests for TELEGRAM_EVENTS_MODE/TELEGRAM_ALERTS_MODE -- each is none/image/video/all, not a
bool, and "image"/"video" are independent halves (neither implies the other; only "all" sends
both). Unit tests only (monkeypatches requests.post) -- no Postgres or network required.
"""
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import config  # noqa: E402
import telegram  # noqa: E402


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"result": {"message_id": 999}}


@pytest.fixture
def fake_post(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: calls.append((a, k)) or _Resp())
    return calls


@pytest.mark.parametrize("mode,expect_sent", [("none", False), ("image", True), ("video", False), ("all", True)])
def test_send_photo_gated_by_events_mode(monkeypatch, fake_post, mode, expect_sent):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", mode)
    result = telegram.send_photo("aGVsbG8=", "caption")
    assert (result is not None) == expect_sent
    assert (len(fake_post) == 1) == expect_sent


@pytest.mark.parametrize("mode,expect_sent", [("none", False), ("image", False), ("video", True), ("all", True)])
def test_send_video_gated_by_events_mode(monkeypatch, mode, expect_sent):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", mode)
    calls = []
    monkeypatch.setattr(telegram, "_post_video", lambda *a, **k: calls.append((a, k)) or True)
    result = telegram.send_video(b"fake-clip-bytes", "clip.mp4", "caption", reply_to_message_id=None)
    assert result == expect_sent
    assert (len(calls) == 1) == expect_sent


@pytest.mark.parametrize("mode,expect_sent", [("none", False), ("image", True), ("video", False), ("all", True)])
def test_send_visit_summary_gated_by_alerts_mode(monkeypatch, fake_post, mode, expect_sent):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", mode)
    result = telegram.send_visit_summary("outside", "car", 1, image_base64="aGVsbG8=")
    assert (result is not None) == expect_sent
    assert (len(fake_post) == 1) == expect_sent


@pytest.mark.parametrize("mode,expect_sent", [("none", False), ("image", False), ("video", True), ("all", True)])
def test_send_visit_video_gated_by_alerts_mode(monkeypatch, mode, expect_sent):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", mode)
    calls = []
    monkeypatch.setattr(telegram, "_post_video", lambda *a, **k: calls.append((a, k)) or True)
    result = telegram.send_visit_video(b"fake-clip-bytes", "visit.mp4", "caption", reply_to_message_id=None)
    assert result == expect_sent
    assert (len(calls) == 1) == expect_sent


# ---- mode= override (per-object-type resolution, see profile_config.py) ----

def test_send_photo_mode_override_wins_over_global_config(monkeypatch, fake_post):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "none")
    result = telegram.send_photo("aGVsbG8=", "caption", mode="image")
    assert result is not None
    assert len(fake_post) == 1


def test_send_photo_mode_override_can_suppress_despite_global_config(monkeypatch, fake_post):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "all")
    result = telegram.send_photo("aGVsbG8=", "caption", mode="none")
    assert result is None
    assert len(fake_post) == 0


def test_send_video_mode_none_falls_back_to_global_config(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "video")
    calls = []
    monkeypatch.setattr(telegram, "_post_video", lambda *a, **k: calls.append((a, k)) or True)
    result = telegram.send_video(b"fake-clip-bytes", "clip.mp4", "caption", reply_to_message_id=None, mode=None)
    assert result is True
    assert len(calls) == 1


def test_send_visit_summary_mode_override_wins_over_global_config(monkeypatch, fake_post):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "none")
    result = telegram.send_visit_summary("outside", "car", 1, image_base64="aGVsbG8=", mode="image")
    assert result is not None
    assert len(fake_post) == 1


def test_send_visit_video_mode_override_wins_over_global_config(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "none")
    calls = []
    monkeypatch.setattr(telegram, "_post_video", lambda *a, **k: calls.append((a, k)) or True)
    result = telegram.send_visit_video(b"fake-clip-bytes", "visit.mp4", "caption", reply_to_message_id=None, mode="video")
    assert result is True
    assert len(calls) == 1


# ---- send_visit_summary's two-way artifact branch: photo > text-only ----

def test_send_visit_summary_sends_photo_when_image_available(monkeypatch, fake_post):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "image")
    telegram.send_visit_summary("outside", "car", 1, image_base64="aW1n")
    assert len(fake_post) == 1
    url, kwargs = fake_post[0]
    assert url[0].endswith("/sendPhoto")
    assert "photo" in kwargs["files"]


def test_send_visit_summary_falls_back_to_text_only_without_image(monkeypatch, fake_post):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "image")
    telegram.send_visit_summary("outside", "car", 1, image_base64=None)
    assert len(fake_post) == 1
    url, kwargs = fake_post[0]
    assert url[0].endswith("/sendMessage")
    assert "files" not in kwargs


def test_send_photo_uses_configured_api_base_url(monkeypatch, fake_post):
    # TELEGRAM_API_BASE_URL lets a self-hosted Local Bot API server (telegram-bot-api Compose
    # profile) stand in for api.telegram.org -- every request must go through it, not a
    # hardcoded cloud-API URL.
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "image")
    monkeypatch.setattr(config, "TELEGRAM_API_BASE_URL", "http://telegram-bot-api:8081")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    telegram.send_photo("aGVsbG8=", "caption")
    assert len(fake_post) == 1
    url = fake_post[0][0][0]
    assert url == "http://telegram-bot-api:8081/bottest-token/sendPhoto"


# ---- _post_video sends straight from an in-memory buffer, no filesystem involved (needed for
# send-without-store, where there's no stored video_path to read back from at all) ----

def test_post_video_sends_raw_bytes_with_no_filesystem_access(monkeypatch, fake_post):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "video")
    result = telegram.send_video(b"fake-clip-bytes", "event-42.mp4", "caption", reply_to_message_id=None)
    assert result is True
    assert len(fake_post) == 1
    _, kwargs = fake_post[0]
    filename, content, content_type = kwargs["files"]["video"]
    assert filename == "event-42.mp4"
    assert content == b"fake-clip-bytes"
    assert content_type == "video/mp4"


def test_post_video_threads_reply_to_message_id(monkeypatch, fake_post):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", "video")
    telegram.send_visit_video(b"fake-clip-bytes", "visit-7.mp4", "caption", reply_to_message_id=555)
    assert fake_post[0][1]["data"]["reply_to_message_id"] == 555


def test_post_video_returns_false_on_request_failure(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", "video")

    def _raise(*a, **k):
        raise ConnectionError("boom")

    monkeypatch.setattr(telegram.requests, "post", _raise)
    result = telegram.send_video(b"fake-clip-bytes", "event-1.mp4", "caption", reply_to_message_id=None)
    assert result is False
