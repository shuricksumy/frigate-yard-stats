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


def send_video(video_path: str, caption: str, reply_to_message_id: int | None) -> bool:
    """POSTs the stored clip as a video, replying to reply_to_message_id if given (mirrors the
    n8n workflow's 'Has Reply Target?' branch -- 'Send Video (Reply)' vs 'Send Video (No Reply)').
    Never raises; logs a warning and returns False on failure so the caller can carry on."""
    if not config.TELEGRAM_ENABLED:
        return False
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
