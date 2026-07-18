import base64
import logging

import requests

import config

logger = logging.getLogger(__name__)


def build_caption(row: dict) -> str:
    objects = (row.get("objects") or "event").strip()
    camera = (row.get("camera") or "").upper()
    return f"\U0001F6A8 <b>{objects.upper()} DETECTED</b> | {camera}"


def send_photo(image_base64: str, caption: str) -> int | None:
    """POSTs the crop as a photo. Returns the Telegram message_id on success (needed later so a
    matching video send can reply to it), or None on any failure. Never raises -- mirrors the
    n8n workflow's onError: continueErrorOutput on 'Send Photo (Telegram)'."""
    if config.TELEGRAM_EVENTS_MODE not in ("image", "all"):
        return None
    try:
        resp = requests.post(
            f"{config.TELEGRAM_API_BASE_URL}/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto",
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


def send_visit_summary(
    camera: str, objects: str | None, event_count: int,
    gif_base64: str | None = None, image_base64: str | None = None,
) -> int | None:
    """Fires once per visit (Frigate review 'end', see mqtt_ingest.py). Prefers the visit's own
    animated preview GIF (crop.build_visit_preview -- sent via sendAnimation so Telegram actually
    plays it, not sendPhoto/sendDocument which would show it as a static first frame or a file
    attachment) -- falls back to a plain photo of the representative event's own crop if the GIF
    isn't available (still being built, or the preview permanently failed), then a text-only
    summary if neither is ready. Gated by TELEGRAM_ALERTS_MODE being "image" or "all", independent
    of TELEGRAM_EVENTS_MODE above (the existing per-raw_event notifications) -- lets you A/B
    per-event vs. per-visit notifications. Never raises."""
    if config.TELEGRAM_ALERTS_MODE not in ("image", "all"):
        return None
    caption = build_visit_caption(camera, objects, event_count)
    try:
        if gif_base64:
            resp = requests.post(
                f"{config.TELEGRAM_API_BASE_URL}/bot{config.TELEGRAM_BOT_TOKEN}/sendAnimation",
                data={"chat_id": config.TELEGRAM_CHAT_ID, "parse_mode": "HTML", "caption": caption},
                files={"animation": ("visit.gif", base64.b64decode(gif_base64), "image/gif")},
                timeout=30,
            )
        elif image_base64:
            resp = requests.post(
                f"{config.TELEGRAM_API_BASE_URL}/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": config.TELEGRAM_CHAT_ID, "parse_mode": "HTML", "caption": caption},
                files={"photo": ("visit.jpg", base64.b64decode(image_base64), "image/jpeg")},
                timeout=30,
            )
        else:
            resp = requests.post(
                f"{config.TELEGRAM_API_BASE_URL}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
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
                f"{config.TELEGRAM_API_BASE_URL}/bot{config.TELEGRAM_BOT_TOKEN}/sendVideo",
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
    if config.TELEGRAM_EVENTS_MODE not in ("video", "all"):
        return False
    return _post_video(video_path, caption, reply_to_message_id)


def send_visit_video(video_path: str, caption: str, reply_to_message_id: int | None) -> bool:
    """Alerts-flow counterpart to send_video -- gated by TELEGRAM_ALERTS_MODE being "video" or
    "all" instead of TELEGRAM_EVENTS_MODE, otherwise identical (same reply-threading onto the
    earlier visit-summary message, same never-raises failure handling). Called by
    alert_video_worker once a visit's clip finishes downloading (STORE_VIDEO_ALERTS)."""
    if config.TELEGRAM_ALERTS_MODE not in ("video", "all"):
        return False
    return _post_video(video_path, caption, reply_to_message_id)
