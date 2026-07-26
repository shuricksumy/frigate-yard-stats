"""Unit tests for visit_summary_worker.py -- the visit-level text-summary stage. Once every
raw_event a visit grouped has settled its own ai_status, this sends the visit's already-produced
sightings.description text to an LLM for one synthesized account of the whole visit. Unit tests
monkeypatch ai_worker._chat_request/_embed_text and db.* functions, no network or Postgres
required -- same style as test_ai_worker.py.
"""
import os

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

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
