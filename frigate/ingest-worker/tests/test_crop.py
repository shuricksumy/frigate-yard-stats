"""Unit tests for crop.py's clip-duration-truncation fallback.

Reproduced against real production data: a tracked object with a ~20-minute logical
start/end span had a saved Frigate clip only ~7 minutes long -- ffmpeg's `-ss <midpoint>` seek
landed past the real end of the file and exited 0 with no output (not a raised error), so the
first ffmpeg call succeeding-but-empty can't be caught via subprocess exit code alone.
"""
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import crop  # noqa: E402


def test_compute_frame_offset_seconds_defaults_to_midpoint():
    # offset_pct=0.5 (config.CROP_FRAME_OFFSET_PCT's default) is this project's original fixed
    # behavior -- exact midpoint of the event's start_ts->end_ts span.
    offset = crop.compute_frame_offset_seconds(0, 100)
    assert offset == 50.0


def test_compute_frame_offset_seconds_respects_custom_pct():
    assert crop.compute_frame_offset_seconds(0, 100, offset_pct=0.0) == 0.0
    assert crop.compute_frame_offset_seconds(0, 100, offset_pct=0.3) == 30.0
    assert crop.compute_frame_offset_seconds(0, 100, offset_pct=1.0) == 100.0


def _fake_run_factory(offsets_that_produce_no_output):
    calls = []

    def fake_run(cmd, check, capture_output):
        calls.append(list(cmd))
        if "-ss" in cmd:
            offset = cmd[cmd.index("-ss") + 1]
            frame_path = cmd[-1]
            if offset in offsets_that_produce_no_output:
                return  # ffmpeg's real behavior here: exit 0, no file written
            with open(frame_path, "wb") as f:
                f.write(b"fake-frame-bytes")
        else:
            crop_path = cmd[-1]
            with open(crop_path, "wb") as f:
                f.write(b"fake-cropped-bytes")

    return fake_run, calls


def test_crop_and_scale_falls_back_when_midpoint_offset_produces_no_frame(monkeypatch):
    fake_run, calls = _fake_run_factory(offsets_that_produce_no_output={"622.9"})
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    result = crop.crop_and_scale("http://frigate.test/api/events/abc/clip.mp4", 622.9, [0, 0, 100, 100])

    assert result  # base64 of "fake-cropped-bytes"
    grab_calls = [c for c in calls if "-ss" in c]
    assert [c[c.index("-ss") + 1] for c in grab_calls] == ["622.9", str(crop._FALLBACK_FRAME_OFFSET_SECONDS)]


def test_crop_and_scale_does_not_fall_back_when_first_grab_succeeds(monkeypatch):
    fake_run, calls = _fake_run_factory(offsets_that_produce_no_output=set())
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    result = crop.crop_and_scale("http://frigate.test/api/events/abc/clip.mp4", 5.0, [0, 0, 100, 100])

    assert result
    grab_calls = [c for c in calls if "-ss" in c]
    assert len(grab_calls) == 1


def test_crop_and_scale_raises_on_invalid_box(monkeypatch):
    fake_run, _ = _fake_run_factory(offsets_that_produce_no_output=set())
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    try:
        crop.crop_and_scale("http://frigate.test/api/events/abc/clip.mp4", 5.0, [0, 0, 0, 100])
        assert False, "expected ValueError"
    except ValueError:
        pass
