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
def test_send_video_gated_by_events_mode(monkeypatch, mode, expect_sent, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_EVENTS_MODE", mode)
    calls = []
    monkeypatch.setattr(telegram, "_post_video", lambda *a, **k: calls.append((a, k)) or True)
    result = telegram.send_video(str(tmp_path / "clip.mp4"), "caption", reply_to_message_id=None)
    assert result == expect_sent
    assert (len(calls) == 1) == expect_sent


@pytest.mark.parametrize("mode,expect_sent", [("none", False), ("image", True), ("video", False), ("all", True)])
def test_send_visit_summary_gated_by_alerts_mode(monkeypatch, fake_post, mode, expect_sent):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", mode)
    result = telegram.send_visit_summary("outside", "car", 1, image_base64="aGVsbG8=")
    assert (result is not None) == expect_sent
    assert (len(fake_post) == 1) == expect_sent


@pytest.mark.parametrize("mode,expect_sent", [("none", False), ("image", False), ("video", True), ("all", True)])
def test_send_visit_video_gated_by_alerts_mode(monkeypatch, mode, expect_sent, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_ALERTS_MODE", mode)
    calls = []
    monkeypatch.setattr(telegram, "_post_video", lambda *a, **k: calls.append((a, k)) or True)
    result = telegram.send_visit_video(str(tmp_path / "clip.mp4"), "caption", reply_to_message_id=None)
    assert result == expect_sent
    assert (len(calls) == 1) == expect_sent


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
