"""Unit tests for crop.py's clip-duration-truncation fallback.

Reproduced against real production data: a tracked object with a ~20-minute logical
start/end span had a saved Frigate clip only ~7 minutes long -- ffmpeg's `-ss <midpoint>` seek
landed past the real end of the file and exited 0 with no output (not a raised error), so the
first ffmpeg call succeeding-but-empty can't be caught via subprocess exit code alone.
"""
import base64
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


def test_crop_and_scale_skips_crop_filter_when_disabled(monkeypatch):
    # CROP_DISABLED=true -- crop_image_base64 becomes the full original frame (still scaled to
    # MAX_CROP_DIMENSION), not a region around the object. Same field feeds both the web UI and
    # the VLM call, so this one flag changes what gets displayed AND analyzed.
    monkeypatch.setattr(config, "CROP_DISABLED", True)
    captured_vf = []

    def fake_run(cmd, check, capture_output):
        if "-vf" in cmd:
            captured_vf.append(cmd[cmd.index("-vf") + 1])
        if "-ss" in cmd:
            with open(cmd[-1], "wb") as f:
                f.write(b"fake-frame-bytes")
        else:
            with open(cmd[-1], "wb") as f:
                f.write(b"fake-cropped-bytes")

    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    result = crop.crop_and_scale("http://frigate.test/api/events/abc/clip.mp4", 5.0, [0, 0, 100, 100])

    assert result
    assert len(captured_vf) == 1
    assert "crop=" not in captured_vf[0]
    assert "scale=" in captured_vf[0]


def test_crop_and_scale_disabled_ignores_an_invalid_box(monkeypatch):
    # box is unused when CROP_DISABLED is set, so an otherwise-invalid box must not raise here --
    # it never affects the result in this mode.
    monkeypatch.setattr(config, "CROP_DISABLED", True)
    fake_run, _ = _fake_run_factory(offsets_that_produce_no_output=set())
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    result = crop.crop_and_scale("http://frigate.test/api/events/abc/clip.mp4", 5.0, [0, 0, 0, 100])

    assert result


def _fake_run_factory_for_preview(offsets_that_produce_no_output=frozenset()):
    # Distinguishes the three kinds of ffmpeg calls build_visit_preview makes by their distinctive
    # flags: "-ss" for a raw frame grab (may produce no output, same truncated-clip behavior
    # crop_and_scale guards against), "-framerate" for the final GIF assembly, and anything else
    # (per-panel crop/scale, or the grid xstack assembly) just writes fake bytes to its own output
    # path (always the last argv element for an ffmpeg invocation).
    calls = []

    def fake_run(cmd, check, capture_output):
        calls.append(list(cmd))
        if "-ss" in cmd:
            offset = cmd[cmd.index("-ss") + 1]
            out_path = cmd[-1]
            if offset in offsets_that_produce_no_output:
                return  # ffmpeg's real behavior here: exit 0, no file written
            with open(out_path, "wb") as f:
                f.write(b"fake-frame-bytes")
        elif "-framerate" in cmd:
            with open(cmd[-1], "wb") as f:
                f.write(b"fake-gif-bytes")
        else:
            with open(cmd[-1], "wb") as f:
                f.write(b"fake-image-bytes")

    return fake_run, calls


def test_build_visit_preview_raises_when_clip_is_far_shorter_than_requested(monkeypatch):
    # Regression test: confirmed live in production that a visit's nominal window (~44.6s) got
    # back a clip only ~3.9s long -- Frigate's continuous-recording endpoint hadn't finished
    # writing the segment yet (the same "not ready" condition VIDEO_MIN_VALID_BYTES guards against
    # on the byte-size axis, video.download_clip). Without this check, build_visit_preview sampled
    # percentages of that tiny duration instead, producing 4 frames all crammed within the same
    # ~3.9s window instead of spanning the visit's real ~34.6s span -- silently wrong, thumb_crop_
    # status still went 'done'. This must raise instead, routing into the normal retry-then-
    # fallback path (visit_thumb_worker.py).
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 3.9335)
    monkeypatch.setattr(
        crop.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no frame grab should be attempted")),
    )

    visit = {
        "id": 163,
        "start_ts": 1784283716.537530,
        "end_ts": 1784283751.139967,  # nominal requested span ~44.6s
        "cameras": "outside",
    }
    representative_event = {"det_id": "fake-det-id"}

    try:
        crop.build_visit_preview(visit, representative_event)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "far shorter than the requested window" in str(exc)


def test_build_visit_preview_uses_visit_scoped_clip_and_samples_actual_duration(monkeypatch):
    # Frigate's continuous-recording clip endpoint pads an unpredictable amount of extra footage
    # onto EITHER edge of the requested window, inconsistently request to request (confirmed live
    # in production both ways -- see CLAUDE.md) -- so instead of seeking to one precise "best
    # moment" relative to an assumed clip boundary, this samples
    # config.VISIT_PREVIEW_FRAME_PERCENTAGES proportionally across the clip's own measured
    # duration, sidestepping that whole problem.
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 20.6)
    fake_run, calls = _fake_run_factory_for_preview()
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    visit = {"start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2"}
    representative_event = {"det_id": "1784219191.5-abc123"}

    grid_b64, gif_b64 = crop.build_visit_preview(visit, representative_event)

    assert grid_b64 and gif_b64
    grab_calls = [c for c in calls if "-ss" in c]
    assert len(grab_calls) == 4
    # Same visit-scoped continuous-recording clip alert_video_worker.py downloads (-5s/+5s
    # padding), not the representative event's own /api/events/{det_id}/clip.mp4 endpoint.
    assert {c[c.index("-i") + 1] for c in grab_calls} == {
        "http://frigate.test:5000/api/outside2/start/1784219186/end/1784219206/clip.mp4"
    }
    # margin=0.3s, usable=20.6-0.6=20.0 -> 0.3 + pct/100*20.0 for pct in (0,25,50,100).
    offsets = sorted(float(c[c.index("-ss") + 1]) for c in grab_calls)
    assert offsets == [0.3, 5.3, 10.3, 20.3]


def test_build_visit_preview_respects_configured_frame_percentages(monkeypatch):
    # VISIT_PREVIEW_FRAME_PERCENTAGES is deployment-tunable (e.g. "5,35,65,90" to stay a bit clear
    # of both edges instead of landing exactly on them) -- confirms changing it actually changes
    # which offsets get sampled, not just the default (0,25,50,100).
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 20.6)
    monkeypatch.setattr(config, "VISIT_PREVIEW_FRAME_PERCENTAGES", [5, 35, 65, 90])
    fake_run, calls = _fake_run_factory_for_preview()
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    visit = {"start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2"}
    representative_event = {"det_id": "1784219191.5-abc123"}

    crop.build_visit_preview(visit, representative_event)

    # margin=0.3s, usable=20.6-0.6=20.0 -> 0.3 + pct/100*20.0 for pct in (5,35,65,90).
    grab_calls = [c for c in calls if "-ss" in c]
    offsets = sorted(float(c[c.index("-ss") + 1]) for c in grab_calls)
    assert offsets == [1.3, 7.3, 13.3, 18.3]


def test_build_visit_preview_returns_distinct_grid_and_gif_images(monkeypatch):
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 20.0)
    fake_run, _ = _fake_run_factory_for_preview()
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    visit = {"start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2"}
    representative_event = {"det_id": "1784219191.5-abc123"}

    grid_b64, gif_b64 = crop.build_visit_preview(visit, representative_event)

    assert base64.b64decode(grid_b64) == b"fake-image-bytes"
    assert base64.b64decode(gif_b64) == b"fake-gif-bytes"


def test_build_visit_preview_respects_crop_disabled_for_every_panel(monkeypatch):
    monkeypatch.setattr(config, "CROP_DISABLED", True)
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 20.0)
    fake_run, calls = _fake_run_factory_for_preview()
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    visit = {"start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2"}
    representative_event = {"det_id": "1784219191.5-abc123"}

    crop.build_visit_preview(visit, representative_event)

    panel_calls = [c for c in calls if "-vf" in c and "-ss" not in c and "-framerate" not in c]
    assert len(panel_calls) == 4
    for c in panel_calls:
        assert "crop=" not in c[c.index("-vf") + 1]
        assert "scale=" in c[c.index("-vf") + 1]


def test_build_visit_preview_falls_back_when_a_frame_grab_produces_no_output(monkeypatch):
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 20.6)
    # The 0% sample (offset 0.3) produces no output, same ffmpeg-exits-0-with-no-file behavior
    # crop_and_scale's own fallback guards against.
    fake_run, calls = _fake_run_factory_for_preview(offsets_that_produce_no_output={"0.3"})
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    visit = {"start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2"}
    representative_event = {"det_id": "1784219191.5-abc123"}

    grid_b64, gif_b64 = crop.build_visit_preview(visit, representative_event)

    assert grid_b64 and gif_b64
    grab_offsets = [c[c.index("-ss") + 1] for c in calls if "-ss" in c]
    assert grab_offsets.count(str(crop._FALLBACK_FRAME_OFFSET_SECONDS)) == 1


def test_build_visit_preview_raises_on_invalid_box_when_crop_enabled(monkeypatch):
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0, 0, 0, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 20.0)
    fake_run, _ = _fake_run_factory_for_preview()
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    visit = {"start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2"}
    representative_event = {"det_id": "1784219191.5-abc123"}

    try:
        crop.build_visit_preview(visit, representative_event)
        assert False, "expected ValueError"
    except ValueError:
        pass
