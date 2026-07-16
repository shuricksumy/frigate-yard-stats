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

import config  # noqa: E402
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
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 20.0)

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
    # offset is anchored from the clip's END (end_ts+5 minus thumb_time, subtracted from the
    # measured duration) -- here the mocked duration (20.0) exactly matches the nominal padded
    # window length (end_ts+5 - (start_ts-5) == 20.0), so this lands on the same instant
    # thumb_time refers to in Frigate's own absolute timeline, same as a naive start-anchored
    # calculation would give when there's no drift.
    assert captured["offset"] == 20.0 - (1784219206.0 - 1784219196.5)


def test_crop_visit_thumbnail_raises_when_offset_exceeds_actual_clip_duration(monkeypatch):
    # Regression test: confirmed in production that Frigate's continuous-recording clip endpoint
    # can silently return far less footage than requested (a 13s request came back only ~4.06s
    # long, likely a motion-based recording gap) -- the computed thumb_time offset (~6.1s) was past
    # that real duration, and without this check ffmpeg just clamped to a frame near the tail
    # instead of erroring, silently returning a wrong-moment crop. This must raise instead, so
    # visit_thumb_worker's normal retry-then-fallback path takes over.
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 4.06)
    monkeypatch.setattr(crop, "crop_and_scale", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))

    visit = {
        "start_ts": 1784226538.0 + 5,  # visit.start_ts, matching build_clip_url's own -5s math
        "end_ts": 1784226551.0 - 5,
        "cameras": "outside2",
        "thumb_time": 1784226544.126003,
    }
    representative_event = {"det_id": "1784226543.203275-s0hvuw"}

    try:
        crop.crop_visit_thumbnail(visit, representative_event)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "clip's bounds" in str(exc)


def test_crop_visit_thumbnail_succeeds_when_offset_is_within_actual_duration(monkeypatch):
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 12.0)
    monkeypatch.setattr(crop, "crop_and_scale", lambda clip_url, offset, box: "base64result")

    visit = {
        "start_ts": 1784219191.0,
        "end_ts": 1784219201.0,
        "cameras": "outside2",
        "thumb_time": 1784219196.5,
    }
    representative_event = {"det_id": "1784219191.5-abc123"}

    assert crop.crop_visit_thumbnail(visit, representative_event) == "base64result"


def test_crop_visit_thumbnail_duration_safety_margin_is_a_fixed_internal_constant(monkeypatch):
    # _DURATION_SAFETY_MARGIN_SECONDS is intentionally NOT a deployment setting (it only guards
    # encoder/keyframe edge cases right at a clip's tail, not real recording gaps -- no env var
    # controls it) -- confirms it's still applied, and that changing the internal constant
    # directly changes the behavior (proving it's actually used, not dead code).
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 11.0)
    monkeypatch.setattr(crop, "crop_and_scale", lambda clip_url, offset, box: "base64result")

    visit = {
        "start_ts": 1784219191.0,
        "end_ts": 1784219201.0,
        "cameras": "outside2",
        # end_padding_epoch (end_ts+5) = 1784219206.0; thumb_time 0.5s before that puts
        # offset_from_end at 0.5s -> offset = duration(11.0) - 0.5 = 10.5s; duration = 11.0s ->
        # 0.5s of headroom.
        "thumb_time": 1784219205.5,
    }
    representative_event = {"det_id": "1784219191.5-abc123"}

    # Default margin (0.5s) leaves exactly zero headroom (10.5 >= 11.0 - 0.5) -- must raise.
    monkeypatch.setattr(crop, "_DURATION_SAFETY_MARGIN_SECONDS", 0.5)
    try:
        crop.crop_visit_thumbnail(visit, representative_event)
        assert False, "expected ValueError"
    except ValueError:
        pass

    # A smaller margin (0.1s) leaves enough headroom -- must succeed.
    monkeypatch.setattr(crop, "_DURATION_SAFETY_MARGIN_SECONDS", 0.1)
    assert crop.crop_visit_thumbnail(visit, representative_event) == "base64result"


def test_crop_visit_thumbnail_end_anchored_offset_survives_extra_lead_in(monkeypatch):
    # Regression test for a real production visit: requested window was start_ts-5 to end_ts+5
    # (~15.2s), but Frigate's continuous-recording endpoint returned a 21.3s clip -- ~6.1s of
    # extra footage prepended before the requested start (confirmed live: Frigate's own review
    # thumbnail showed a person near a van; a start-anchored offset (thumb_time - (start_ts-5),
    # ~5.2s into the file) landed on an empty frame, while the person was actually visible
    # ~11-13s in -- matching this end-anchored formula almost exactly). Likely Frigate snapping
    # the clip start backward to a fixed-length recording-segment boundary while the end lines up
    # with what was actually requested.
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 21.335022)
    monkeypatch.setattr(config, "VISIT_THUMB_CROP_OFFSET_ADJUST_SECONDS", 0.0)

    captured = {}

    def fake_crop_and_scale(clip_url, offset, box):
        captured["offset"] = offset
        return "base64result"

    monkeypatch.setattr(crop, "crop_and_scale", fake_crop_and_scale)

    visit = {
        "start_ts": 1784240053.11415,
        "end_ts": 1784240058.310633,
        "cameras": "outside2",
        "thumb_time": 1784240053.286873,
    }
    representative_event = {"det_id": "1784240051.947106-1ou5oc"}

    assert crop.crop_visit_thumbnail(visit, representative_event) == "base64result"
    # end_padding_epoch = int(1784240058.310633) + 5 = 1784240063
    expected_offset = 21.335022 - (1784240063 - 1784240053.286873)
    assert captured["offset"] == expected_offset
    assert 11 < captured["offset"] < 13  # matches the person's confirmed on-screen position


def test_crop_visit_thumbnail_offset_adjust_shifts_the_seek_target(monkeypatch):
    # VISIT_THUMB_CROP_OFFSET_ADJUST_SECONDS is the real, deployment-tunable knob for "my crops
    # consistently land a bit off from thumb_time" -- positive shifts later/forward, negative
    # shifts earlier/backward. Default (0) must leave the offset exactly at thumb_time.
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 20.0)

    captured = {}

    def fake_crop_and_scale(clip_url, offset, box):
        captured["offset"] = offset
        return "base64result"

    monkeypatch.setattr(crop, "crop_and_scale", fake_crop_and_scale)

    visit = {
        "start_ts": 1784219191.0,
        "end_ts": 1784219201.0,
        "cameras": "outside2",
        "thumb_time": 1784219196.5,  # base offset = 10.5s
    }
    representative_event = {"det_id": "1784219191.5-abc123"}

    monkeypatch.setattr(config, "VISIT_THUMB_CROP_OFFSET_ADJUST_SECONDS", 0.0)
    crop.crop_visit_thumbnail(visit, representative_event)
    assert captured["offset"] == 10.5

    monkeypatch.setattr(config, "VISIT_THUMB_CROP_OFFSET_ADJUST_SECONDS", 1.2)
    crop.crop_visit_thumbnail(visit, representative_event)
    assert captured["offset"] == 10.5 + 1.2

    monkeypatch.setattr(config, "VISIT_THUMB_CROP_OFFSET_ADJUST_SECONDS", -1.2)
    crop.crop_visit_thumbnail(visit, representative_event)
    assert captured["offset"] == 10.5 - 1.2
