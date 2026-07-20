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


def test_build_visit_preview_falls_through_to_independent_timestamps_when_local_video_is_too_short(
    monkeypatch, tmp_path,
):
    # A stored visit video can be a short/bad clip too -- alert_video_worker only validates byte
    # size (VIDEO_MIN_VALID_BYTES=1000 in production, confirmed live -- far below what even a
    # genuinely short few-second clip weighs), so a bad download can pass that check and get
    # stored as "done". video_path never changes once set, so treating this as a permanent dead
    # end would mean the visit's preview could never succeed on any retry -- must fall through to
    # sampling each of the 4 moments independently against Frigate instead.
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 3.9335)
    fake_run, calls = _fake_run_factory_for_preview()
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    local_video = tmp_path / "visit-163.mp4"
    local_video.write_bytes(b"fake-clip-bytes")
    visit = {
        "id": 163,
        "start_ts": 1784219191.0,
        "end_ts": 1784219201.0,  # nominal requested span ~20s -- local "video" claims only 3.9s
        "cameras": "outside",
        "video_path": str(local_video),
    }
    representative_event = {"det_id": "fake-det-id"}

    grid_b64, gif_b64 = crop.build_visit_preview(visit, representative_event)

    assert grid_b64 and gif_b64
    grab_calls = [c for c in calls if "-ss" in c]
    assert len(grab_calls) == 4
    # Every grab hit Frigate directly, not the (too-short) local file.
    assert all(c[c.index("-i") + 1] != str(local_video) for c in grab_calls)


def test_build_visit_preview_raises_when_local_video_too_short_and_no_independent_frame_found(
    monkeypatch, tmp_path,
):
    # Both fallback layers exhausted: the stored video is too short AND Frigate has nothing at any
    # of the 4 sampled moments either -- must still raise (routing into the normal retry path).
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 3.9335)

    def fake_run(cmd, check, capture_output):
        if "-ss" in cmd:
            return  # every independent-timestamp request also comes back with nothing.
        raise AssertionError("grid/gif assembly should never run if every frame grab failed")

    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    local_video = tmp_path / "visit-163.mp4"
    local_video.write_bytes(b"fake-clip-bytes")
    visit = {
        "id": 163, "start_ts": 1784219191.0, "end_ts": 1784219201.0,
        "cameras": "outside", "video_path": str(local_video),
    }
    representative_event = {"det_id": "fake-det-id"}

    try:
        crop.build_visit_preview(visit, representative_event)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "Could not grab a frame at any" in str(exc)


def test_build_visit_preview_uses_local_video_and_samples_actual_duration(monkeypatch, tmp_path):
    # Once a visit video is already downloaded, proportional sampling is based on its own measured
    # duration, not the nominal requested window -- Frigate's continuous-recording endpoint pads
    # an unpredictable amount of extra footage onto EITHER edge of a requested window (confirmed
    # live in production both ways -- see CLAUDE.md), so anchoring to the file's own duration
    # sidesteps that regardless of which edge got padded.
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 20.6)
    fake_run, calls = _fake_run_factory_for_preview()
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    local_video = tmp_path / "visit-1.mp4"
    local_video.write_bytes(b"fake-clip-bytes")
    visit = {
        "start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2",
        "video_path": str(local_video),
    }
    representative_event = {"det_id": "1784219191.5-abc123"}

    grid_b64, gif_b64 = crop.build_visit_preview(visit, representative_event)

    assert grid_b64 and gif_b64
    grab_calls = [c for c in calls if "-ss" in c]
    assert len(grab_calls) == 4
    assert {c[c.index("-i") + 1] for c in grab_calls} == {str(local_video)}
    # margin=0.3s, usable=20.6-0.6=20.0 -> 0.3 + pct/100*20.0 for pct in (0,25,50,100).
    offsets = sorted(float(c[c.index("-ss") + 1]) for c in grab_calls)
    assert offsets == [0.3, 5.3, 10.3, 20.3]


def test_build_visit_preview_reuses_already_downloaded_visit_video(monkeypatch, tmp_path):
    # Regression test: confirmed live that alert_video_worker's own clip download can succeed
    # (full-length clip) mere seconds before build_visit_preview's independent re-request of the
    # exact same Frigate URL comes back near-empty -- Frigate's continuous-recording endpoint is a
    # race against its own segment cleanup, not a stable source you can re-query freely. Once a
    # visit's video is already stored on disk (video_path set), reuse that file instead of
    # re-entering the race.
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(crop, "_probe_duration_seconds", lambda clip_url: 20.6)
    fake_run, calls = _fake_run_factory_for_preview()
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    local_video = tmp_path / "visit-171.mp4"
    local_video.write_bytes(b"fake-clip-bytes")

    visit = {
        "start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2",
        "video_path": str(local_video),
    }
    representative_event = {"det_id": "1784219191.5-abc123"}

    grid_b64, gif_b64 = crop.build_visit_preview(visit, representative_event)

    assert grid_b64 and gif_b64
    grab_calls = [c for c in calls if "-ss" in c]
    assert {c[c.index("-i") + 1] for c in grab_calls} == {str(local_video)}


def test_build_visit_preview_fetches_each_sampled_moment_independently_when_no_video_stored_yet(monkeypatch):
    # video_path absent (STORE_VIDEO_ALERTS off, or the video worker hasn't succeeded yet) --
    # each of the 4 sampled moments (0/25/50/100% of the visit's own start_ts->end_ts span) is now
    # requested independently (_panels_from_independent_timestamps), not as one whole-visit-span
    # request -- a gap at one moment then can't take the other three down with it (see
    # test_build_visit_preview_reuses_neighbor_frame_when_one_timestamp_has_no_footage).
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    fake_run, calls = _fake_run_factory_for_preview()
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    visit = {"start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2", "video_path": None}
    representative_event = {"det_id": "1784219191.5-abc123"}

    crop.build_visit_preview(visit, representative_event)

    grab_calls = [c for c in calls if "-ss" in c]
    assert len(grab_calls) == 4
    # 4 distinct URLs -- each sampled moment gets its own -5s/+5s window (build_clip_url) around
    # that specific timestamp, not one request spanning the whole visit.
    assert {c[c.index("-i") + 1] for c in grab_calls} == {
        "http://frigate.test:5000/api/outside2/start/1784219186/end/1784219196/clip.mp4",  # 0%
        "http://frigate.test:5000/api/outside2/start/1784219188/end/1784219198/clip.mp4",  # 25%
        "http://frigate.test:5000/api/outside2/start/1784219191/end/1784219201/clip.mp4",  # 50%
        "http://frigate.test:5000/api/outside2/start/1784219196/end/1784219206/clip.mp4",  # 100%
    }
    # Each grab targets the middle of its own small window -- the sampled moment itself.
    assert {float(c[c.index("-ss") + 1]) for c in grab_calls} == {5.0}


def test_build_visit_preview_respects_configured_frame_percentages(monkeypatch):
    # VISIT_PREVIEW_FRAME_PERCENTAGES is deployment-tunable (e.g. "5,35,65,90" to stay a bit clear
    # of both edges instead of landing exactly on them) -- confirms changing it actually changes
    # which moments get sampled, not just the default (0,25,50,100).
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    monkeypatch.setattr(config, "VISIT_PREVIEW_FRAME_PERCENTAGES", [5, 35, 65, 90])
    fake_run, calls = _fake_run_factory_for_preview()
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    visit = {"start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2"}
    representative_event = {"det_id": "1784219191.5-abc123"}

    crop.build_visit_preview(visit, representative_event)

    grab_calls = [c for c in calls if "-ss" in c]
    urls = {c[c.index("-i") + 1] for c in grab_calls}
    assert urls == {
        "http://frigate.test:5000/api/outside2/start/1784219186/end/1784219196/clip.mp4",  # 5%
        "http://frigate.test:5000/api/outside2/start/1784219189/end/1784219199/clip.mp4",  # 35%
        "http://frigate.test:5000/api/outside2/start/1784219192/end/1784219202/clip.mp4",  # 65%
        "http://frigate.test:5000/api/outside2/start/1784219195/end/1784219205/clip.mp4",  # 90%
    }


def test_build_visit_preview_returns_distinct_grid_and_gif_images(monkeypatch):
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
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
    fake_run, calls = _fake_run_factory_for_preview()
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    visit = {"start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2"}
    representative_event = {"det_id": "1784219191.5-abc123"}

    crop.build_visit_preview(visit, representative_event)

    # 4 grid panels + 4 full-size GIF frames, all derived from the same 4 raw moments.
    panel_calls = [c for c in calls if "-vf" in c and "-ss" not in c and "-framerate" not in c]
    assert len(panel_calls) == 8
    for c in panel_calls:
        assert "crop=" not in c[c.index("-vf") + 1]
        assert "scale=" in c[c.index("-vf") + 1]


def test_build_visit_preview_reuses_neighbor_frame_when_one_timestamp_has_no_footage(monkeypatch):
    # One sampled moment (here, the 50% timestamp's own -5s/+5s window) has genuinely nothing
    # retained -- Frigate's per-segment retention (record.continuous.days: 0, see CLAUDE.md) means
    # this is a real, not-transient outcome for some moments, not just an ffmpeg glitch. The gap
    # reuses the nearest earlier successful frame instead of failing the whole grid over one
    # missing percentage point.
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})
    # The 50% sample's window is start/1784219191/end/1784219201 (see the URL math in
    # test_build_visit_preview_fetches_each_sampled_moment_independently_when_no_video_stored_yet).
    no_footage_url = "http://frigate.test:5000/api/outside2/start/1784219191/end/1784219201/clip.mp4"
    calls = []

    def fake_run(cmd, check, capture_output):
        calls.append(list(cmd))
        if "-ss" in cmd:
            url = cmd[cmd.index("-i") + 1]
            if url == no_footage_url:
                return  # ffmpeg's real behavior here: exit 0, no file written -- both attempts.
            with open(cmd[-1], "wb") as f:
                f.write(b"fake-frame-bytes")
        elif "-framerate" in cmd:
            with open(cmd[-1], "wb") as f:
                f.write(b"fake-gif-bytes")
        else:
            with open(cmd[-1], "wb") as f:
                f.write(b"fake-image-bytes")

    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    visit = {"start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2"}
    representative_event = {"det_id": "1784219191.5-abc123"}

    grid_b64, gif_b64 = crop.build_visit_preview(visit, representative_event)

    assert grid_b64 and gif_b64
    # Still 4 panel-crop calls -- the gap (index 2, the 50% sample) was filled by reusing index 1's
    # (25%) already-successful raw frame rather than being dropped or left empty.
    panel_calls = [c for c in calls if c[-1].endswith(("panel_1.jpg", "panel_2.jpg"))]
    assert len(panel_calls) == 2
    inputs = {c[c.index("-i") + 1] for c in panel_calls}
    assert len(inputs) == 1  # both panels 1 and 2 were cropped from the same reused raw frame


def test_build_visit_preview_raises_when_no_sampled_moment_has_any_footage(monkeypatch):
    # If literally none of the 4 sampled moments have any retained footage, there's nothing to
    # reuse -- this must still raise (routing into the normal retry-then-fallback path), not
    # silently produce an empty/garbage grid.
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0.1, 0.1, 0.2, 0.2]}})

    def fake_run(cmd, check, capture_output):
        if "-ss" in cmd:
            return  # every sampled moment comes back with nothing at all.
        raise AssertionError("grid/gif assembly should never run if every frame grab failed")

    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    visit = {"start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2"}
    representative_event = {"det_id": "1784219191.5-abc123"}

    try:
        crop.build_visit_preview(visit, representative_event)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "Could not grab a frame at any" in str(exc)


def test_build_visit_preview_raises_on_invalid_box_when_crop_enabled(monkeypatch):
    monkeypatch.setattr(crop, "fetch_frigate_event", lambda det_id: {"data": {"region": [0, 0, 0, 0.2]}})
    fake_run, _ = _fake_run_factory_for_preview()
    monkeypatch.setattr(crop.subprocess, "run", fake_run)

    visit = {"start_ts": 1784219191.0, "end_ts": 1784219201.0, "cameras": "outside2"}
    representative_event = {"det_id": "1784219191.5-abc123"}

    try:
        crop.build_visit_preview(visit, representative_event)
        assert False, "expected ValueError"
    except ValueError:
        pass


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
    monkeypatch.setattr(crop, "crop_and_scale", lambda clip_url, offset, box: "record-stream-crop-base64")

    raw_event = {"det_id": "abc123", "start_ts": 0, "end_ts": 100}
    result = crop.crop_event(raw_event)

    assert result == {"crop_image_base64": "record-stream-crop-base64", "sub_label": None, "score": 0.5}
