import logging
import time

import config
import db

logger = logging.getLogger(__name__)

_last_run_monotonic = None


def maybe_run_retention() -> None:
    # Cheap no-op check on every crop_worker poll iteration -- the actual DELETE sweep only
    # fires once per RETENTION_CHECK_INTERVAL_SECONDS, not every poll tick.
    global _last_run_monotonic
    now = time.monotonic()
    if _last_run_monotonic is not None and now - _last_run_monotonic < config.RETENTION_CHECK_INTERVAL_SECONDS:
        return

    try:
        db.run_retention_cleanup(config.RETENTION_MONTHS)
    except Exception:
        logger.exception("Retention cleanup failed")
    finally:
        _last_run_monotonic = now
