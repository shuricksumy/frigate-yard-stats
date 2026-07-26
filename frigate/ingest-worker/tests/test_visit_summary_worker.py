"""Unit tests for visit_summary_worker.py -- the visit-level text-summary stage. Once every
raw_event a visit grouped has settled its own ai_status, this sends the visit's already-produced
sightings.description text to an LLM for one synthesized account of the whole visit. Unit tests
monkeypatch ai_worker._chat_request/_embed_text and db.* functions, no network or Postgres
required -- same style as test_ai_worker.py.
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import ai_worker  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import visit_summary_worker  # noqa: E402

VISIT_SUMMARY_CONFIG = {
    "enabled": True,
    "chat_path": "/summary-slot/v1/chat/completions",
    "prompt": "Summarize this visit.",
}


def _chat_response(content):
    return {"choices": [{"message": {"content": content}}]}


# ---- build_summary_input ----

def test_build_summary_input_joins_object_label_and_description():
    sightings = [
        {"object_label": "car", "description": "red sedan, plate 10MO407"},
        {"object_label": "person", "description": "wearing a blue jacket"},
    ]
    text = visit_summary_worker.build_summary_input(sightings)
    assert text == "car: red sedan, plate 10MO407\nperson: wearing a blue jacket"


def test_build_summary_input_skips_sightings_with_no_description():
    sightings = [
        {"object_label": "car", "description": "red sedan"},
        {"object_label": "person", "description": None},
    ]
    text = visit_summary_worker.build_summary_input(sightings)
    assert text == "car: red sedan"


def test_build_summary_input_empty_list_returns_empty_string():
    assert visit_summary_worker.build_summary_input([]) == ""


# ---- process_claimed_visit ----

def test_process_claimed_visit_skips_when_no_sighting_text(monkeypatch):
    monkeypatch.setattr(db, "get_sightings_for_visit", lambda visit_id: [])
    skipped = []
    monkeypatch.setattr(db, "mark_visit_summary_skipped", lambda visit_id: skipped.append(visit_id))
    called = []
    monkeypatch.setattr(ai_worker, "_chat_request", lambda *a, **k: called.append(a))

    visit_summary_worker.process_claimed_visit({"id": 7}, VISIT_SUMMARY_CONFIG)

    assert skipped == [7]
    assert not called


def test_process_claimed_visit_success(monkeypatch):
    monkeypatch.setattr(
        db, "get_sightings_for_visit",
        lambda visit_id: [{"object_label": "car", "description": "red sedan"}],
    )
    captured_chat = {}

    def fake_chat_request(type_config, prompt, images, timeout):
        captured_chat.update(type_config=type_config, prompt=prompt, images=images, timeout=timeout)
        return _chat_response("A car arrived and parked in the driveway.")

    monkeypatch.setattr(ai_worker, "_chat_request", fake_chat_request)
    monkeypatch.setattr(ai_worker, "_embed_text", lambda text: [0.1, 0.2])

    completed = []
    monkeypatch.setattr(
        db, "complete_visit_summary",
        lambda visit_id, summary, embedding=None: completed.append((visit_id, summary, embedding)) or 1,
    )
    failed = []
    monkeypatch.setattr(db, "fail_visit_summary", lambda *a, **k: failed.append((a, k)))

    visit_summary_worker.process_claimed_visit({"id": 42}, VISIT_SUMMARY_CONFIG)

    assert not failed
    assert completed == [(42, "A car arrived and parked in the driveway.", [0.1, 0.2])]
    # images=[] -- this stage never sends an image, only the gathered text.
    assert captured_chat["images"] == []
    assert "car: red sedan" in captured_chat["prompt"]
    assert captured_chat["prompt"].startswith(VISIT_SUMMARY_CONFIG["prompt"])


def test_process_claimed_visit_uses_own_timeout(monkeypatch):
    monkeypatch.setattr(
        db, "get_sightings_for_visit",
        lambda visit_id: [{"object_label": "car", "description": "red sedan"}],
    )
    captured_timeouts = []

    def fake_chat_request(type_config, prompt, images, timeout):
        captured_timeouts.append(timeout)
        return _chat_response("summary")

    monkeypatch.setattr(ai_worker, "_chat_request", fake_chat_request)
    monkeypatch.setattr(ai_worker, "_embed_text", lambda text: None)
    monkeypatch.setattr(db, "complete_visit_summary", lambda *a, **k: 1)

    profile_config_with_timeout = {**VISIT_SUMMARY_CONFIG, "timeout_seconds": 42}
    visit_summary_worker.process_claimed_visit({"id": 1}, profile_config_with_timeout)

    assert captured_timeouts == [42]


def test_process_claimed_visit_falls_back_to_default_timeout_when_unset(monkeypatch):
    monkeypatch.setattr(config, "AI_STAGE_DEFAULT_TIMEOUT_SECONDS", 180)
    monkeypatch.setattr(
        db, "get_sightings_for_visit",
        lambda visit_id: [{"object_label": "car", "description": "red sedan"}],
    )
    captured_timeouts = []

    def fake_chat_request(type_config, prompt, images, timeout):
        captured_timeouts.append(timeout)
        return _chat_response("summary")

    monkeypatch.setattr(ai_worker, "_chat_request", fake_chat_request)
    monkeypatch.setattr(ai_worker, "_embed_text", lambda text: None)
    monkeypatch.setattr(db, "complete_visit_summary", lambda *a, **k: 1)

    visit_summary_worker.process_claimed_visit({"id": 1}, VISIT_SUMMARY_CONFIG)

    assert captured_timeouts == [180]


def test_process_claimed_visit_chat_failure_routes_to_fail_visit_summary(monkeypatch):
    monkeypatch.setattr(config, "AI_STAGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(
        db, "get_sightings_for_visit",
        lambda visit_id: [{"object_label": "car", "description": "red sedan"}],
    )

    def fake_chat_request(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_worker, "_chat_request", fake_chat_request)
    failed = []
    monkeypatch.setattr(db, "fail_visit_summary", lambda *a, **k: failed.append((a, k)))
    completed = []
    monkeypatch.setattr(db, "complete_visit_summary", lambda *a, **k: completed.append(a))

    visit_summary_worker.process_claimed_visit({"id": 8}, VISIT_SUMMARY_CONFIG)

    assert failed == [((8, 3), {})]
    assert not completed


def test_process_claimed_visit_embedding_failure_still_completes_summary(monkeypatch):
    monkeypatch.setattr(
        db, "get_sightings_for_visit",
        lambda visit_id: [{"object_label": "car", "description": "red sedan"}],
    )
    monkeypatch.setattr(ai_worker, "_chat_request", lambda *a, **k: _chat_response("summary text"))
    monkeypatch.setattr(ai_worker, "_embed_text", lambda text: None)
    completed = []
    monkeypatch.setattr(db, "complete_visit_summary", lambda *a, **k: completed.append(a) or 1)
    failed = []
    monkeypatch.setattr(db, "fail_visit_summary", lambda *a, **k: failed.append((a, k)))

    visit_summary_worker.process_claimed_visit({"id": 9}, VISIT_SUMMARY_CONFIG)

    assert not failed
    assert completed == [(9, "summary text", None)]


def test_process_claimed_visit_routes_to_anthropic_provider(monkeypatch):
    # End-to-end (mocked HTTP) with visit_summary routed to Claude instead of llama_proxy --
    # confirms process_claimed_visit actually threads its own config through _chat_request, not
    # just that _chat_request's own provider dispatch works in isolation (already covered by
    # test_ai_worker.py).
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")

    def fake_post(url, **kwargs):
        assert url.endswith("/v1/messages")
        return type("Resp", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {"content": [{"type": "text", "text": "A car arrived and left."}]},
        })()

    monkeypatch.setattr(ai_worker.requests, "post", fake_post)
    monkeypatch.setattr(ai_worker, "_embed_text", lambda text: None)
    monkeypatch.setattr(
        db, "get_sightings_for_visit",
        lambda visit_id: [{"object_label": "car", "description": "red sedan"}],
    )
    completed = []
    monkeypatch.setattr(db, "complete_visit_summary", lambda *a, **k: completed.append(a) or 1)

    profile_visit_summary = {
        "enabled": True, "provider": "anthropic", "model": "claude-opus-4-8", "prompt": "Summarize.",
    }
    visit_summary_worker.process_claimed_visit({"id": 30}, profile_visit_summary)

    assert completed == [(30, "A car arrived and left.", None)]


# ---- run_once ----

def test_run_once_does_nothing_when_disabled(monkeypatch):
    called = []
    monkeypatch.setattr(db, "claim_visit_summary_batch", lambda *a, **k: called.append(a) or [])
    visit_summary_worker.run_once({"visit_summary": {"enabled": False}})
    assert not called


def test_run_once_does_nothing_when_visit_summary_key_absent(monkeypatch):
    called = []
    monkeypatch.setattr(db, "claim_visit_summary_batch", lambda *a, **k: called.append(a) or [])
    visit_summary_worker.run_once({})
    assert not called


def test_run_once_claims_and_processes_when_enabled(monkeypatch):
    monkeypatch.setattr(db, "claim_visit_summary_batch", lambda *a, **k: [{"id": 1}, {"id": 2}])
    processed = []
    monkeypatch.setattr(
        visit_summary_worker, "process_claimed_visit",
        lambda row, cfg: processed.append(row["id"]),
    )
    visit_summary_worker.run_once({"visit_summary": VISIT_SUMMARY_CONFIG})
    assert processed == [1, 2]


def test_run_once_passes_configured_tuning_knobs(monkeypatch):
    captured = {}

    def fake_claim(parallel_limit, stale_minutes, max_age_hours=None):
        captured.update(parallel_limit=parallel_limit, stale_minutes=stale_minutes, max_age_hours=max_age_hours)
        return []

    monkeypatch.setattr(db, "claim_visit_summary_batch", fake_claim)
    profile = {
        "visit_summary": {
            "enabled": True, "parallel_limit": 5, "stale_minutes": 10, "max_age_hours": 24,
        },
    }
    visit_summary_worker.run_once(profile)
    assert captured == {"parallel_limit": 5, "stale_minutes": 10, "max_age_hours": 24}


# ---- integration: a stale visit summary is invalidated once a previously-failed event succeeds ----
# A visit whose linked events include one or more permanently-failed ones is still summarized from
# whatever sightings exist (failed is a terminal state for claim_visit_summary_batch's own purposes
# -- it doesn't wait forever). If that failed event later gets requeued (e.g. via the admin
# dashboard's "Requeue failed" button) and succeeds, the visit's already-computed summary is now
# stale (built from an incomplete set) -- db.complete_sighting resets summary_status back to 'new'
# so visit_summary_worker recomputes it from the fuller set, overriding the stale result.

@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def _insert_raw_event(det_id, ai_status, objects="car"):
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status)
        VALUES ('pytest-cam', 'pytest-zone', %s, now(), now(), %s, true, true, 'done', %s)
        RETURNING id
        """,
        (objects, det_id, ai_status), fetch=True,
    )
    return rows[0]["id"]


def _cleanup_visit(*raw_event_ids, visit_id):
    db._execute("DELETE FROM yard_stats.sightings WHERE raw_event_id = ANY(%s)", (list(raw_event_ids),))
    db._execute("DELETE FROM yard_stats.visit_summaries WHERE visit_id = %s", (visit_id,))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = ANY(%s)", (list(raw_event_ids),))
    db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


def test_complete_sighting_resets_stale_visit_summary_after_a_retried_event_succeeds(conn_ok):
    det_id_done = f"pytest-{uuid.uuid4()}"
    det_id_failed = f"pytest-{uuid.uuid4()}"
    raw_id_done = _insert_raw_event(det_id_done, "done")
    raw_id_failed = _insert_raw_event(det_id_failed, "failed")
    db.complete_sighting(raw_id_done, "car", "red sedan")

    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id_done, det_id_failed],
    })
    try:
        # The visit already ran its summary once, using only the "done" event's sighting -- the
        # "failed" one contributed nothing.
        db.complete_visit_summary(visit_id, "A red sedan visited.")
        assert db.get_visit(visit_id)["summary_status"] == "done"

        # The previously-failed event gets requeued and this time succeeds.
        db.complete_sighting(raw_id_failed, "car", "actually a different red sedan, plate ABC123")

        visit = db.get_visit(visit_id)
        assert visit["summary_status"] == "new"
        assert visit["summary_attempt_count"] == 0
        # The stale summary row is left in place as history -- get_visit_summary still returns it
        # (the newest/only one) until visit_summary_worker recomputes and inserts a fresh one.
        assert db.get_visit_summary(visit_id)["summary"] == "A red sedan visited."
    finally:
        _cleanup_visit(raw_id_done, raw_id_failed, visit_id=visit_id)


def test_complete_sighting_does_not_disturb_a_visit_summary_still_in_progress(conn_ok):
    det_id_done = f"pytest-{uuid.uuid4()}"
    det_id_failed = f"pytest-{uuid.uuid4()}"
    raw_id_done = _insert_raw_event(det_id_done, "done")
    raw_id_failed = _insert_raw_event(det_id_failed, "failed")
    db.complete_sighting(raw_id_done, "car", "red sedan")

    visit_id = db.record_visit({
        "camera": "pytest-cam", "zone": "pytest-zone", "objects": "car",
        "start_time": 1784198451.0, "end_time": 1784198470.0,
        "det_ids": [det_id_done, det_id_failed],
    })
    try:
        # Visit summary hasn't run yet (still 'new', the freshly-inserted default) -- completing an
        # unrelated event's sighting must not touch it, only a *stale* (done/failed/skipped)
        # summary should ever be reset.
        assert db.get_visit(visit_id)["summary_status"] == "new"

        db.complete_sighting(raw_id_failed, "car", "actually visible after all")

        visit = db.get_visit(visit_id)
        assert visit["summary_status"] == "new"
        assert visit["summary_attempt_count"] == 0
    finally:
        _cleanup_visit(raw_id_done, raw_id_failed, visit_id=visit_id)
