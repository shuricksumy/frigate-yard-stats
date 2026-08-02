"""The shared poll-loop skeleton every background stage runs on.

crop_worker, video_worker, alert_video_worker, ai_worker and visit_summary_worker all had a
byte-for-byte identical run_forever: log the stage's settings once, then loop forever calling
run_once, swallowing and logging any exception so one bad iteration can never kill the thread, and
sleeping between ticks. That skeleton lives here once instead of five times.

Deliberately NOT abstracted any further than this. Each stage's run_once is genuinely different
(different claim function, different capacity source, different per-row work), and the previous
round of this project's history shows what over-generalizing a queue stage costs -- see CLAUDE.md's
"Superseded design, reverted entirely" note. This module owns only the part that was actually
identical.
"""
import logging
import time

logger = logging.getLogger(__name__)


def run_forever(name: str, run_once, poll_interval_seconds, settings: dict | None = None) -> None:
    """Run `run_once` on a fixed interval forever, surviving any exception it raises.

    name -- the stage name, used for the startup line and every error line.
    run_once -- zero-argument callable; each stage binds its own profile/config via a closure or
        functools.partial rather than this module knowing anything about either.
    poll_interval_seconds -- either a number, or a zero-argument callable returning one, for stages
        whose interval comes from profiles.yaml rather than a config.py constant.
    settings -- optional {label: value} logged once at startup, so the same "here's what this stage
        is running with" line each worker used to build by hand is still there.
    """
    if settings:
        logger.info(
            "%s starting: %s", name, " ".join(f"{k}={v}" for k, v in settings.items()),
        )
    else:
        logger.info("%s starting", name)
    while True:
        try:
            run_once()
        except Exception:
            # Never let one bad iteration kill the thread -- the next tick retries from scratch,
            # and any row left mid-flight is picked back up by that stage's own reap-stale pass.
            logger.exception("%s poll iteration failed", name)
        interval = poll_interval_seconds() if callable(poll_interval_seconds) else poll_interval_seconds
        time.sleep(interval)
