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


# ---- fetch_frigate_snapshot_base64 / crop_event's FRIGATE_SNAPSHOT_ENABLED branch ----

def test_fetch_frigate_snapshot_base64_returns_encoded_bytes(monkeypatch):
    class FakeResponse:
        content = b"fake-jpeg-bytes"

        def raise_for_status(self):
            pass

    captured_url = []
    monkeypatch.setattr(crop.requests, "get", lambda url, **k: captured_url.append(url) or FakeResponse())

    result = crop.fetch_frigate_snapshot_base64("1784554838.654667-xag8k1")

    assert result == base64.b64encode(b"fake-jpeg-bytes").decode()
    assert captured_url[0] == f"{config.FRIGATE_API_BASE}/api/events/1784554838.654667-xag8k1/snapshot.jpg"


def test_crop_event_uses_frigate_snapshot_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "FRIGATE_SNAPSHOT_ENABLED", True)
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {
        "data": {"region": [0.1, 0.1, 0.2, 0.2], "score": 0.91}, "sub_label": "10MG407",
    })
    monkeypatch.setattr(crop, "fetch_frigate_snapshot_base64", lambda det_id: "snapshot-base64")

    # crop_and_scale must never be called in this mode -- assert by making it raise if it is.
    def _fail_if_called(*a, **k):
        raise AssertionError("crop_and_scale should not run when FRIGATE_SNAPSHOT_ENABLED is true")
    monkeypatch.setattr(crop, "crop_and_scale", _fail_if_called)

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    result = crop.crop_event(raw_event)

    assert result == {"crop_image_base64": "snapshot-base64", "sub_label": "10MG407", "score": 0.91}


def test_crop_event_uses_record_stream_crop_when_snapshot_disabled(monkeypatch):
    monkeypatch.setattr(config, "FRIGATE_SNAPSHOT_ENABLED", False)
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {
        "data": {"region": [0.1, 0.1, 0.2, 0.2], "score": 0.5}, "sub_label": None,
    })

    def _fail_if_called(det_id):
        raise AssertionError("fetch_frigate_snapshot_base64 should not run when FRIGATE_SNAPSHOT_ENABLED is false")
    monkeypatch.setattr(crop, "fetch_frigate_snapshot_base64", _fail_if_called)
    monkeypatch.setattr(crop, "crop_and_scale", lambda clip_url, offset, box, *a, **k: "record-stream-crop-base64")

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    result = crop.crop_event(raw_event)

    assert result == {"crop_image_base64": "record-stream-crop-base64", "sub_label": None, "score": 0.5}


def test_crop_event_high_res_fetches_event_and_crops_from_record_stream(monkeypatch):
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {
        "data": {"region": [0.1, 0.1, 0.2, 0.2], "score": 0.5}, "sub_label": None,
    })
    captured = {}

    def fake_crop_and_scale(clip_url, offset, box, crop_disabled=None, crop_padding_pct=None):
        captured["clip_url"] = clip_url
        captured["offset"] = offset
        return "high-res-crop-base64"
    monkeypatch.setattr(crop, "crop_and_scale", fake_crop_and_scale)

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    result = crop.crop_event_high_res(raw_event)

    assert result == "high-res-crop-base64"
    assert captured["clip_url"] == f"{config.FRIGATE_API_BASE}/api/events/abc123/clip.mp4"
    assert captured["offset"] == 50.0  # default CROP_FRAME_OFFSET_PCT (0.5) midpoint


def test_crop_event_high_res_reuses_already_fetched_event(monkeypatch):
    # crop_event's own non-snapshot branch already fetched the Frigate event once (for
    # sub_label/score) -- passing it through here must skip a second, redundant fetch.
    def _fail_if_called(det_id):
        raise AssertionError("fetch_frigate_event should not run when event= is already provided")
    monkeypatch.setattr(crop, "fetch_frigate_event", _fail_if_called)
    monkeypatch.setattr(crop, "crop_and_scale", lambda *a, **k: "high-res-crop-base64")

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    event = {"data": {"region": [0.1, 0.1, 0.2, 0.2]}}
    result = crop.crop_event_high_res(raw_event, event=event)

    assert result == "high-res-crop-base64"


def test_crop_event_uses_record_stream_crop_calls_frigate_event_only_once(monkeypatch):
    # crop_event's non-snapshot branch must fetch the Frigate event exactly once and reuse it for
    # both crop_event_high_res's box computation and its own sub_label/score fields -- not fetch
    # it twice.
    monkeypatch.setattr(config, "FRIGATE_SNAPSHOT_ENABLED", False)
    call_count = {"n": 0}

    def counting_fetch(det_id):
        call_count["n"] += 1
        return {"data": {"region": [0.1, 0.1, 0.2, 0.2], "score": 0.5}, "sub_label": None}
    monkeypatch.setattr(crop, "fetch_frigate_event", counting_fetch)
    monkeypatch.setattr(crop, "crop_and_scale", lambda *a, **k: "record-stream-crop-base64")

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    crop.crop_event(raw_event)

    assert call_count["n"] == 1


def test_crop_event_frigate_snapshot_enabled_param_overrides_global_config(monkeypatch):
    # frigate_snapshot_enabled=False passed explicitly (as crop_worker.py does once resolved via
    # profile_config.frigate_snapshot_enabled) wins over the global default being True -- a
    # per-type override, e.g. one object type wants the seek-based crop while others use Frigate's
    # own snapshot.
    monkeypatch.setattr(config, "FRIGATE_SNAPSHOT_ENABLED", True)
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {
        "data": {"region": [0.1, 0.1, 0.2, 0.2], "score": 0.5}, "sub_label": None,
    })

    def _fail_if_called(det_id):
        raise AssertionError("fetch_frigate_snapshot_base64 should not run when overridden off")
    monkeypatch.setattr(crop, "fetch_frigate_snapshot_base64", _fail_if_called)

    captured = {}

    def fake_crop_and_scale(clip_url, offset, box, crop_disabled=None, crop_padding_pct=None):
        captured["crop_disabled"] = crop_disabled
        captured["crop_padding_pct"] = crop_padding_pct
        captured["offset"] = offset
        return "record-stream-crop-base64"
    monkeypatch.setattr(crop, "crop_and_scale", fake_crop_and_scale)

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    result = crop.crop_event(
        raw_event, frigate_snapshot_enabled=False, crop_disabled=True,
        crop_frame_offset_pct=0.9, crop_padding_pct=0.05,
    )

    assert result["crop_image_base64"] == "record-stream-crop-base64"
    assert captured == {"crop_disabled": True, "crop_padding_pct": 0.05, "offset": 90.0}
