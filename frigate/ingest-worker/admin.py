import logging
import os

import requests

import config

logger = logging.getLogger(__name__)


def dir_size_bytes(path: str) -> dict:
    # Walks the tree summing real file sizes -- used for the admin dashboard's disk-usage section
    # (VIDEO_STORAGE_PATH/VIDEO_STORAGE_PATH_ALERTS). A path that doesn't exist (e.g.
    # VIDEO_STORAGE_PATH_ALERTS when STORE_VIDEO_ALERTS has never been turned on) is reported as
    # zero rather than an error -- an unused optional storage location isn't a fault.
    if not path or not os.path.isdir(path):
        return {"path": path, "exists": False, "bytes": 0, "file_count": 0}
    total = 0
    count = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
                count += 1
            except OSError:
                # File removed mid-walk (e.g. retention cleanup running concurrently) -- skip it
                # rather than failing the whole scan over one already-gone file.
                pass
    return {"path": path, "exists": True, "bytes": total, "file_count": count}


def _object_type_from_filename(name: str) -> str:
    # video.py's store_clip/store_visit_clip name every file "{object_type}-{id}-..." or
    # "visit-{object_type}-{id}-..." -- the object label is always the token right after a leading
    # "visit-" if present, otherwise the very first token. A name that doesn't match this pattern
    # at all (e.g. some other file someone dropped into the storage dir by hand) buckets under
    # "unknown" rather than raising or being silently skipped.
    parts = name.split("-")
    if not parts or not parts[0]:
        return "unknown"
    if parts[0] == "visit" and len(parts) > 1 and parts[1]:
        return parts[1]
    return parts[0]


def dir_size_by_object_type(path: str) -> dict:
    # Same walk as dir_size_bytes, but bucketed by object type parsed from each file's own name --
    # for the admin dashboard's "By object type" disk-usage breakdown. A missing/nonexistent path
    # (same as dir_size_bytes) reports an empty breakdown rather than an error.
    if not path or not os.path.isdir(path):
        return {}
    totals: dict[str, dict[str, int]] = {}
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                size = os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            object_type = _object_type_from_filename(name)
            bucket = totals.setdefault(object_type, {"bytes": 0, "file_count": 0})
            bucket["bytes"] += size
            bucket["file_count"] += 1
    return totals


def check_embedding_backend(timeout_seconds: float = 8.0) -> dict:
    # Live smoke test against LLAMA_PROXY_BASE_URL/LLAMA_PROXY_EMBED_PATH -- confirms both that
    # something answers at all and that it returns the dimension this deployment is configured
    # for (config.EMBEDDING_DIMENSIONS), the same check ai_worker._embed_text applies on every
    # real call. Deliberately a separate, on-demand admin action rather than folded into the
    # overview endpoint -- it's a real network call, not a cheap SQL query.
    if not config.LLAMA_PROXY_BASE_URL:
        return {"ok": False, "detail": "LLAMA_PROXY_BASE_URL is not configured"}
    headers = {}
    if config.LLAMA_PROXY_TOKEN:
        headers["Authorization"] = f"Bearer {config.LLAMA_PROXY_TOKEN}"
    try:
        resp = requests.post(
            f"{config.LLAMA_PROXY_BASE_URL}{config.LLAMA_PROXY_EMBED_PATH}",
            json={"input": "admin dashboard health check"},
            headers=headers,
            timeout=timeout_seconds,
        )
        resp.raise_for_status()
        embedding = resp.json()["data"][0]["embedding"]
        dims = len(embedding)
        if dims != config.EMBEDDING_DIMENSIONS:
            return {
                "ok": False,
                "dimensions": dims,
                "expected_dimensions": config.EMBEDDING_DIMENSIONS,
                "detail": f"Backend returned {dims} dims, EMBEDDING_DIMENSIONS is {config.EMBEDDING_DIMENSIONS} -- wrong model loaded at LLAMA_PROXY_EMBED_PATH?",
            }
        return {"ok": True, "dimensions": dims, "expected_dimensions": config.EMBEDDING_DIMENSIONS, "detail": None}
    except Exception as exc:
        logger.warning("Embedding backend health check failed", exc_info=True)
        return {"ok": False, "detail": str(exc)}
