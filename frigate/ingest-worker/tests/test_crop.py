"""Unit tests for crop.py.

crop_and_scale/_build_vf_filter (the record-stream seek+crop primitives, including the
clip-duration-truncation fallback -- reproduced against real production data: a tracked object
with a ~20-minute logical start/end span had a saved Frigate clip only ~7 minutes long, so ffmpeg's
`-ss <midpoint>` seek landed past the real end of the file and exited 0 with no output rather than
raising) are still exercised directly here, but are no longer invoked by crop_event -- see that
function's own tests below for why: crop_event uses ONLY Frigate's own best-moment snapshot now.
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


def test_crop_and_scale_defaults_to_no_scale_filter_at_native_resolution(monkeypatch):
    # max_dimension defaults to None -- the full-resolution storage copy, no scale filter at all.
    # The AI-facing (capped) copy is produced afterward via scale_image_base64, not a second scale
    # filter here.
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
    assert "crop=" in captured_vf[0]
    assert "scale=" not in captured_vf[0]


def test_crop_and_scale_skips_crop_filter_when_disabled(monkeypatch):
    # CROP_DISABLED=true -- crop_image_base64 becomes the full original frame (scaled to whatever
    # max_dimension is passed), not a region around the object. Same field feeds both the web UI
    # and the VLM call, so this one flag changes what gets displayed AND analyzed.
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

    result = crop.crop_and_scale(
        "http://frigate.test/api/events/abc/clip.mp4", 5.0, [0, 0, 100, 100], max_dimension=1280,
    )

    assert result
    assert len(captured_vf) == 1
    assert "crop=" not in captured_vf[0]
    assert "scale=" in captured_vf[0]


def test_crop_and_scale_crop_disabled_param_overrides_global_config(monkeypatch):
    # crop_disabled passed explicitly wins over config.CROP_DISABLED (as crop_worker.py does once
    # resolved via profile_config.crop_disabled) -- a per-type override, not just the global flag.
    monkeypatch.setattr(config, "CROP_DISABLED", False)
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

    result = crop.crop_and_scale(
        "http://frigate.test/api/events/abc/clip.mp4", 5.0, [0, 0, 100, 100], crop_disabled=True,
    )

    assert result
    assert "crop=" not in captured_vf[0]


def test_crop_and_scale_crop_padding_pct_param_overrides_global_config(monkeypatch):
    monkeypatch.setattr(config, "CROP_PADDING_PCT", 0.2)
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

    # box=[0,0,100,100] (100x100) with crop_padding_pct=0.0 -- no padding at all, so the crop
    # filter's width/height should stay exactly 100x100 rather than the default 0.2 padding.
    crop.crop_and_scale(
        "http://frigate.test/api/events/abc/clip.mp4", 5.0, [0, 0, 100, 100], crop_padding_pct=0.0,
    )

    assert "crop=100.0:100.0:0:0" in captured_vf[0]


def test_crop_and_scale_disabled_ignores_an_invalid_box(monkeypatch):
    # box is unused when CROP_DISABLED is set, so an otherwise-invalid box must not raise here --
    # it never affects the result in this mode.
    monkeypatch.setattr(config, "CROP_DISABLED", True)
    fake_run, _ = _fake_run_factory(offsets_that_produce_no_output=set())
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    result = crop.crop_and_scale("http://frigate.test/api/events/abc/clip.mp4", 5.0, [0, 0, 0, 100])

    assert result


# ---- crop_event: Frigate's own best-moment snapshot, ONLY -- never a record-stream seek ----

def test_crop_event_uses_frigate_snapshot_not_record_stream_seek(monkeypatch):
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {
        "data": {"region": [0.1, 0.1, 0.2, 0.2], "score": 0.5}, "sub_label": None,
    })
    captured = {}

    def fake_fetch_snapshot(det_id):
        captured["det_id"] = det_id
        return "snapshot-base64"
    monkeypatch.setattr(crop, "fetch_frigate_snapshot_base64", fake_fetch_snapshot)
    # crop_and_scale (the record-stream seek+crop path) must never be called -- if it is, this
    # test fails loudly rather than silently falling back to the old behavior.
    monkeypatch.setattr(crop, "crop_and_scale", lambda *a, **k: (_ for _ in ()).throw(AssertionError("crop_and_scale must not be called")))
    monkeypatch.setattr(crop, "scale_image_base64", lambda image_base64, max_dimension: f"ai-{image_base64}")

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    result = crop.crop_event(raw_event)

    assert result == {
        "crop_image_base64": "ai-snapshot-base64",
        "full_res_image_base64": "snapshot-base64",
        "sub_label": None,
        "score": 0.5,
    }
    assert captured["det_id"] == "abc123"


def test_crop_event_scales_the_snapshot_down_for_the_ai_facing_copy(monkeypatch):
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {
        "data": {"region": [0.1, 0.1, 0.2, 0.2], "score": None}, "sub_label": None,
    })
    monkeypatch.setattr(crop, "fetch_frigate_snapshot_base64", lambda det_id: "snapshot-base64")
    captured = {}

    def fake_scale(image_base64, max_dimension):
        captured["image_base64"] = image_base64
        captured["max_dimension"] = max_dimension
        return "ai-crop-base64"
    monkeypatch.setattr(crop, "scale_image_base64", fake_scale)

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    result = crop.crop_event(raw_event, ai_image_max_dimension=640)

    assert result["crop_image_base64"] == "ai-crop-base64"
    assert result["full_res_image_base64"] == "snapshot-base64"
    assert captured == {"image_base64": "snapshot-base64", "max_dimension": 640}


def test_crop_event_ai_image_max_dimension_falls_back_to_global_config(monkeypatch):
    monkeypatch.setattr(config, "MAX_CROP_DIMENSION", 999)
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {
        "data": {"region": [0.1, 0.1, 0.2, 0.2]}, "sub_label": None,
    })
    monkeypatch.setattr(crop, "fetch_frigate_snapshot_base64", lambda det_id: "snapshot-base64")
    captured = {}
    monkeypatch.setattr(crop, "scale_image_base64", lambda image_base64, max_dimension: captured.setdefault("max_dimension", max_dimension) or "ai-crop-base64")

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    crop.crop_event(raw_event)

    assert captured["max_dimension"] == 999


def test_crop_event_calls_frigate_event_only_once(monkeypatch):
    # crop_event must fetch the Frigate event exactly once and reuse it for its own sub_label/score
    # fields -- not fetch it twice.
    call_count = {"n": 0}

    def counting_fetch(det_id):
        call_count["n"] += 1
        return {"data": {"region": [0.1, 0.1, 0.2, 0.2], "score": 0.5}, "sub_label": None}
    monkeypatch.setattr(crop, "fetch_frigate_event", counting_fetch)
    monkeypatch.setattr(crop, "fetch_frigate_snapshot_base64", lambda det_id: "snapshot-base64")
    monkeypatch.setattr(crop, "scale_image_base64", lambda *a, **k: "ai-crop-base64")

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    crop.crop_event(raw_event)

    assert call_count["n"] == 1


def test_crop_event_ignores_crop_disabled_and_padding_and_offset_params(monkeypatch):
    # These params are accepted for call-site/signature compatibility with crop_worker.py (which
    # resolves and passes them per-object-type) but have no effect now -- there's no region-crop
    # math applied on top of Frigate's own already-rendered snapshot.
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {
        "data": {"region": [0.1, 0.1, 0.2, 0.2], "score": 0.5}, "sub_label": None,
    })
    monkeypatch.setattr(crop, "fetch_frigate_snapshot_base64", lambda det_id: "snapshot-base64")
    monkeypatch.setattr(crop, "scale_image_base64", lambda *a, **k: "ai-crop-base64")

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    result = crop.crop_event(
        raw_event, crop_disabled=True, crop_frame_offset_pct=0.9, crop_padding_pct=0.05,
    )

    assert result["crop_image_base64"] == "ai-crop-base64"
    assert result["full_res_image_base64"] == "snapshot-base64"
