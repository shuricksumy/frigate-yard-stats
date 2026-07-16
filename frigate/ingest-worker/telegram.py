import base64
import logging

import requests

import config

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"


def build_caption(row: dict) -> str:
    objects = (row.get("objects") or "event").strip()
    camera = (row.get("camera") or "").upper()
    return f"\U0001F6A8 <b>{objects.upper()} DETECTED</b> | {camera}"


def send_photo(image_base64: str, caption: str) -> int | None:
    """POSTs the crop as a photo. Returns the Telegram message_id on success (needed later so a
    matching video send can reply to it), or None on any failure. Never raises -- mirrors the
    n8n workflow's onError: continueErrorOutput on 'Send Photo (Telegram)'."""
    if not config.TELEGRAM_ENABLED:
        return None
    try:
        resp = requests.post(
            f"{_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={"chat_id": config.TELEGRAM_CHAT_ID, "parse_mode": "HTML", "caption": caption},
            files={"photo": ("crop.jpg", base64.b64decode(image_base64), "image/jpeg")},
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        return result.get("message_id")
    except Exception:
        logger.warning("Telegram sendPhoto failed", exc_info=True)
        return None


def build_visit_caption(camera: str, objects: str | None, event_count: int) -> str:
    label = (objects or "activity").strip()
    grouped_note = f" ({event_count} events grouped)" if event_count and event_count > 1 else ""
    return f"\U0001F514 <b>VISIT: {label.upper()}</b> | {(camera or '').upper()}{grouped_note}"


def send_visit_summary(camera: str, objects: str | None, event_count: int, image_base64: str | None) -> int | None:
    """Fires once per visit (Frigate review 'end', see mqtt_ingest.py) -- a photo of the visit's
    representative event if its crop is already available, else a text-only summary (the crop
    stage may not have finished analyzing that event by the time the review closes). Gated by
    TELEGRAM_ALERTS_ENABLED, independent of TELEGRAM_ENABLED above (the existing per-raw_event
    notifications) -- lets you A/B per-event vs. per-visit notifications. Never raises."""
    if not config.TELEGRAM_ALERTS_ENABLED:
        return None
    caption = build_visit_caption(camera, objects, event_count)
    try:
        if image_base64:
            resp = requests.post(
                f"{_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": config.TELEGRAM_CHAT_ID, "parse_mode": "HTML", "caption": caption},
                files={"photo": ("visit.jpg", base64.b64decode(image_base64), "image/jpeg")},
                timeout=30,
            )
        else:
            resp = requests.post(
                f"{_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": config.TELEGRAM_CHAT_ID, "parse_mode": "HTML", "text": caption},
                timeout=30,
            )
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        return result.get("message_id")
    except Exception:
        logger.warning("Telegram visit summary send failed", exc_info=True)
        return None


def _post_video(video_path: str, caption: str, reply_to_message_id: int | None) -> bool:
    try:
        data = {"chat_id": config.TELEGRAM_CHAT_ID, "parse_mode": "HTML", "caption": caption}
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = reply_to_message_id
        with open(video_path, "rb") as f:
            resp = requests.post(
                f"{_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendVideo",
                data=data,
                files={"video": (video_path.rsplit("/", 1)[-1], f, "video/mp4")},
                timeout=120,
            )
        resp.raise_for_status()
        return True
    except Exception:
        logger.warning("Telegram sendVideo failed for %s", video_path, exc_info=True)
        return False


def send_video(video_path: str, caption: str, reply_to_message_id: int | None) -> bool:
    """POSTs the stored clip as a video, replying to reply_to_message_id if given (mirrors the
    n8n workflow's 'Has Reply Target?' branch -- 'Send Video (Reply)' vs 'Send Video (No Reply)').
    Never raises; logs a warning and returns False on failure so the caller can carry on."""
    if not config.TELEGRAM_ENABLED:
        return False
    return _post_video(video_path, caption, reply_to_message_id)


def send_visit_video(video_path: str, caption: str, reply_to_message_id: int | None) -> bool:
    """Alerts-flow counterpart to send_video -- gated by TELEGRAM_ALERTS_ENABLED instead of
    TELEGRAM_ENABLED, otherwise identical (same reply-threading onto the earlier visit-summary
    message, same never-raises failure handling). Called by alert_video_worker once a visit's
    clip finishes downloading (STORE_VIDEO_ALERTS)."""
    if not config.TELEGRAM_ALERTS_ENABLED:
        return False
    return _post_video(video_path, caption, reply_to_message_id)
