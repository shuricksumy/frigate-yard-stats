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


def test_crop_visit_thumbnail_uses_visit_scoped_clip_and_thumb_time_offset(monkeypatch):
    # thumb_time is an absolute epoch timestamp (unlike CROP_FRAME_OFFSET_PCT, a percentage of one
    # raw_event's own span) and can fall outside the representative event's own window -- so this
    # must fetch the same visit-scoped continuous-recording clip alert_video_worker.py downloads
    # (video.build_clip_url, -5s/+5s padding), not the representative event's own
    # /api/events/{det_id}/clip.mp4 endpoint crop_event uses.
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})

    captured = {}

    def fake_crop_and_scale(clip_url, offset, box):
        captured["clip_url"] = clip_url
        captured["offset"] = offset
        captured["box"] = box
        return "base64result"

    monkeypatch.setattr(crop, "crop_and_scale", fake_crop_and_scale)

    visit = {
        "start_ts": 1784219191.0,
        "end_ts": 1784219201.0,
        "cameras": "outside2",
        "thumb_time": 1784219196.5,
    }
    representative_event = {"det_id": "1784219191.5-abc123"}

    result = crop.crop_visit_thumbnail(visit, representative_event)

    assert result == "base64result"
    # clip fetched for the visit's own start/end window (-5s/+5s), not the event's own clip.mp4.
    assert captured["clip_url"] == "http://frigate.test:5000/api/outside2/start/1784219186/end/1784219206/clip.mp4"
    # offset relative to that clip's start (start_ts - 5), matching thumb_time in Frigate's own
    # absolute timeline exactly.
    assert captured["offset"] == 1784219196.5 - 1784219186
