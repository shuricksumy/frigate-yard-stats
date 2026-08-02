"""Tests for poll_loop.run_forever -- the skeleton every background stage's thread runs on.

The property that matters most: one failing iteration must never kill the thread. Each stage's
run_once is the only thing that ever reaps its own stale 'processing' rows, so a dead thread means
that stage silently stops forever while its rows sit claimed-but-abandoned -- the failure mode this
loop's try/except exists to prevent.

No DB or network -- run_once is a plain callable and sleep is stubbed.
"""
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import poll_loop  # noqa: E402


class _StopLoop(Exception):
    """Raised by the stubbed sleep to break out of the infinite loop."""


def _run_n_ticks(monkeypatch, run_once, n, poll_interval=5):
    """Run the loop for exactly n ticks, then break out via the sleep stub."""
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= n:
            raise _StopLoop()

    monkeypatch.setattr(poll_loop.time, "sleep", fake_sleep)
    with pytest.raises(_StopLoop):
        poll_loop.run_forever("test_stage", run_once, poll_interval)
    return sleeps


def test_run_once_is_called_every_tick(monkeypatch):
    calls = []
    _run_n_ticks(monkeypatch, lambda: calls.append(1), n=3)
    assert len(calls) == 3


def test_a_failing_iteration_does_not_kill_the_loop(monkeypatch):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("one bad poll")

    _run_n_ticks(monkeypatch, flaky, n=4)
    # The exception on tick 2 must not have stopped ticks 3 and 4.
    assert len(calls) == 4


def test_every_iteration_failing_still_never_kills_the_loop(monkeypatch):
    calls = []

    def always_fails():
        calls.append(1)
        raise RuntimeError("permanently broken backend")

    _run_n_ticks(monkeypatch, always_fails, n=5)
    assert len(calls) == 5


def test_sleeps_for_the_configured_interval(monkeypatch):
    sleeps = _run_n_ticks(monkeypatch, lambda: None, n=3, poll_interval=7)
    assert sleeps == [7, 7, 7]


def test_callable_interval_is_re_evaluated_each_tick(monkeypatch):
    # visit_summary_worker's interval comes from profiles.yaml rather than a config.py constant --
    # a callable lets a stage pick it up without this module knowing where it came from.
    intervals = iter([1, 2, 3])
    sleeps = _run_n_ticks(monkeypatch, lambda: None, n=3, poll_interval=lambda: next(intervals))
    assert sleeps == [1, 2, 3]


def test_startup_settings_are_logged_once(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="poll_loop")
    _run_n_ticks(
        monkeypatch,
        lambda: None,
        n=2,
    )
    startup_lines = [r for r in caplog.records if "starting" in r.getMessage()]
    assert len(startup_lines) == 1


def test_failure_is_logged_with_the_stage_name(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.ERROR, logger="poll_loop")

    def boom():
        raise RuntimeError("kaboom")

    _run_n_ticks(monkeypatch, boom, n=1)
    assert any("test_stage poll iteration failed" in r.getMessage() for r in caplog.records)
