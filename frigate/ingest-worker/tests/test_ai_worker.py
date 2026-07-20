"""Unit + integration tests for ai_worker.py -- the internal AI stage (alternative to
n8n/metadata-processor.json). Unit tests monkeypatch requests.post and db.* functions, no network
or Postgres required. The one integration test at the bottom needs a reachable Postgres (conn_ok,
skipped otherwise) but still mocks the HTTP calls -- same style as test_semantic_search.py.
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

# Flat, universal profile -- no "sighting_type" concept, each label maps straight to its own
# prompt config. car/truck would normally share one prompt via a YAML anchor in the real
# profiles.yaml; that's just a YAML-authoring convenience, both still resolve to distinct dict
# entries in the parsed profile, so this test fixture defines "car"/"person" directly.
PROFILE = {
    "object_types": {
        "car": {
            "chat_path": "/vehicle-slot/v1/chat/completions",
            "event_prompt": "vehicle event prompt", "alert_prompt": "vehicle alert prompt",
        },
        "person": {
            "chat_path": "/person-slot/v1/chat/completions",
            "event_prompt": "person event prompt", "alert_prompt": "person alert prompt",
        },
    },
}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _chat_response(content):
    return {"choices": [{"message": {"content": content}}]}


def _embed_response(vector):
    return {"data": [{"embedding": vector}]}


# ---- parse_sighting_response ----

def test_parse_sighting_response_returns_description_as_is():
    response = _chat_response("wearing a red hoodie, walking toward the door")
    fields = ai_worker.parse_sighting_response(response, {"id": 7, "objects": "person"})
    assert fields == {
        "raw_event_id": 7,
        "object_label": "person",
        "description": "wearing a red hoodie, walking toward the door",
    }


def test_parse_sighting_response_uses_raw_events_objects_as_label():
    response = _chat_response("red sedan, roof rack, plate 10MG407")
    fields = ai_worker.parse_sighting_response(response, {"id": 42, "objects": "car"})
    assert fields["object_label"] == "car"
    assert fields["description"] == "red sedan, roof rack, plate 10MG407"


# ---- load_profile ----

def test_load_profile_parses_real_file():
    path = os.path.join(os.path.dirname(__file__), "..", "profiles.yaml")
    profile = ai_worker.load_profile(path)
    assert set(profile["object_types"]) == {"car", "truck", "person"}
    assert "dog" not in profile["object_types"]
    assert profile["object_types"]["car"]["event_prompt"]
    assert profile["object_types"]["car"]["alert_prompt"]
    assert profile["object_types"]["person"]["event_prompt"]
    assert profile["object_types"]["person"]["alert_prompt"]
    # truck shares car's prompt via a YAML anchor -- confirms it actually resolved, not just present.
    assert profile["object_types"]["truck"]["event_prompt"] == profile["object_types"]["car"]["event_prompt"]


# ---- run_once ----

def test_run_once_only_claims_mapped_object_types(monkeypatch):
    # Both mapped types are claimed when the global stage default is on and neither overrides it --
    # this is the state the thread would only be running in anyway (see main.py's
    # profile_config.any_ai_events_stage_enabled gate), so run_once's own per-type filtering (see
    # below) has nothing to narrow here.
    monkeypatch.setattr(config, "AI_EVENTS_STAGE_ENABLED", True)
    captured = {}

    def fake_claim(object_types, parallel_limit, stale_minutes, max_age_hours=None, **kwargs):
        captured["object_types"] = object_types
        return []

    monkeypatch.setattr(db, "claim_ai_batch", fake_claim)
    ai_worker.run_once(PROFILE)
    assert set(captured["object_types"]) == {"car", "person"}


def test_run_once_excludes_type_that_opts_out_despite_global_default_on(monkeypatch):
    monkeypatch.setattr(config, "AI_EVENTS_STAGE_ENABLED", True)
    profile = {
        "object_types": {
            "car": {**PROFILE["object_types"]["car"], "ai_events_stage_enabled": False},
            "person": PROFILE["object_types"]["person"],
        },
    }
    captured = {}

    def fake_claim(object_types, *a, **k):
        captured["object_types"] = object_types
        return []

    monkeypatch.setattr(db, "claim_ai_batch", fake_claim)
    ai_worker.run_once(profile)
    assert captured["object_types"] == ["person"]


def test_run_once_includes_type_that_opts_in_despite_global_default_off(monkeypatch):
    monkeypatch.setattr(config, "AI_EVENTS_STAGE_ENABLED", False)
    profile = {
        "object_types": {
            "car": {**PROFILE["object_types"]["car"], "ai_events_stage_enabled": True},
            "person": PROFILE["object_types"]["person"],
        },
    }
    captured = {}

    def fake_claim(object_types, *a, **k):
        captured["object_types"] = object_types
        return []

    monkeypatch.setattr(db, "claim_ai_batch", fake_claim)
    ai_worker.run_once(profile)
    assert captured["object_types"] == ["car"]


# ---- process_claimed_event ----

def test_process_claimed_event_success(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(config, "LLAMA_PROXY_TOKEN", "")

    responses = [
        _Resp(_chat_response("blue suv")),
        _Resp(_embed_response([0.1, 0.2])),
    ]
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(ai_worker.requests, "post", fake_post)

    inserted = []
    monkeypatch.setattr(db, "complete_sighting", lambda *a, **k: inserted.append(a) or 1)
    failed = []
    monkeypatch.setattr(db, "fail_ai_event", lambda *a, **k: failed.append((a, k)))

    row = {"id": 5, "objects": "car", "crop_image_base64": "aGVsbG8=", "sub_label": None, "det_id": "d1"}
    ai_worker.process_claimed_event(row, PROFILE)

    assert len(inserted) == 1
    assert inserted[0][:3] == (5, "car", "blue suv")
    assert not failed
    assert calls[0] == "http://llama.test/vehicle-slot/v1/chat/completions"


def test_process_claimed_event_uses_profile_timeout(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    profile = {
        "object_types": {
            "car": {
                "chat_path": "/vehicle-slot/v1/chat/completions",
                "event_prompt": "vehicle event prompt",
                "timeout_seconds": 42,
            },
        },
    }
    captured_timeouts = []

    def fake_post(url, timeout=None, **kwargs):
        captured_timeouts.append(timeout)
        if "chat/completions" in url:
            return _Resp(_chat_response("black sedan"))
        return _Resp(_embed_response([0.1]))

    monkeypatch.setattr(ai_worker.requests, "post", fake_post)
    monkeypatch.setattr(db, "complete_sighting", lambda *a, **k: 1)

    row = {"id": 10, "objects": "car", "crop_image_base64": "x", "sub_label": None, "det_id": "d5"}
    ai_worker.process_claimed_event(row, profile)

    assert captured_timeouts[0] == 42  # the chat call's timeout came from the profile
    assert captured_timeouts[1] == config.AI_STAGE_EMBED_TIMEOUT_SECONDS


def test_process_claimed_event_falls_back_to_default_timeout_when_unset(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(config, "AI_STAGE_DEFAULT_TIMEOUT_SECONDS", 180)
    # PROFILE's car entry has no timeout_seconds -- must fall back to the config default.
    captured_timeouts = []

    def fake_post(url, timeout=None, **kwargs):
        captured_timeouts.append(timeout)
        if "chat/completions" in url:
            return _Resp(_chat_response("white van"))
        return _Resp(_embed_response([0.1]))

    monkeypatch.setattr(ai_worker.requests, "post", fake_post)
    monkeypatch.setattr(db, "complete_sighting", lambda *a, **k: 1)

    row = {"id": 11, "objects": "car", "crop_image_base64": "x", "sub_label": None, "det_id": "d6"}
    ai_worker.process_claimed_event(row, PROFILE)

    assert captured_timeouts[0] == 180


def test_process_claimed_event_unmapped_type_is_skipped(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_worker.requests, "post", lambda *a, **k: calls.append((a, k)))
    row = {"id": 6, "objects": "dog", "crop_image_base64": "x", "det_id": "d2"}
    ai_worker.process_claimed_event(row, PROFILE)
    assert not calls


def test_process_claimed_event_chat_failure_routes_to_fail_ai_event(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(config, "AI_STAGE_MAX_ATTEMPTS", 3)

    def fake_post(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_worker.requests, "post", fake_post)
    failed = []
    monkeypatch.setattr(db, "fail_ai_event", lambda *a, **k: failed.append((a, k)))
    inserted = []
    monkeypatch.setattr(db, "complete_sighting", lambda *a, **k: inserted.append(a))

    row = {"id": 8, "objects": "car", "crop_image_base64": "x", "det_id": "d3"}
    ai_worker.process_claimed_event(row, PROFILE)

    assert failed == [((8, 3), {})]
    assert not inserted


def test_process_claimed_event_embedding_failure_still_inserts_sighting(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")

    def fake_post(url, **kwargs):
        if "chat/completions" in url:
            return _Resp(_chat_response("green coupe"))
        raise RuntimeError("embedding backend down")

    monkeypatch.setattr(ai_worker.requests, "post", fake_post)
    inserted = []
    monkeypatch.setattr(db, "complete_sighting", lambda *a, **k: inserted.append(a) or 1)
    failed = []
    monkeypatch.setattr(db, "fail_ai_event", lambda *a, **k: failed.append((a, k)))

    row = {"id": 9, "objects": "car", "crop_image_base64": "x", "sub_label": None, "det_id": "d4"}
    ai_worker.process_claimed_event(row, PROFILE)

    assert len(inserted) == 1
    assert inserted[0][-1] is None  # embedding is the last positional arg -- None on failure
    assert not failed


# ---- run_embedding_backfill ----

def test_run_embedding_backfill_requires_llama_proxy_base_url(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "")
    with pytest.raises(RuntimeError):
        ai_worker.run_embedding_backfill(limit=10)


# ---- integration: real Postgres, mocked HTTP ----

@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def _insert_event(camera, objects="car"):
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64)
        VALUES (%s, 'z', %s, now(), now(), %s, true, true, 'done', 'new', 'ZmFrZQ==')
        RETURNING id
        """,
        (camera, objects, det_id), fetch=True,
    )
    return rows[0]["id"]


def _cleanup_event(event_id):
    db._execute("DELETE FROM yard_stats.sightings WHERE raw_event_id = %s", (event_id,))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = %s", (event_id,))


def test_process_claimed_event_end_to_end_marks_ai_status_done(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")

    def fake_post(url, **kwargs):
        if "chat/completions" in url:
            return _Resp(_chat_response("silver hatchback"))
        return _Resp(_embed_response([0.1] + [0.0] * (config.EMBEDDING_DIMENSIONS - 1)))

    monkeypatch.setattr(ai_worker.requests, "post", fake_post)

    event_id = _insert_event(camera="pytest-ai-worker")
    try:
        row = db.get_raw_event(event_id)
        ai_worker.process_claimed_event(row, PROFILE)

        updated = db.get_raw_event(event_id)
        assert updated["ai_status"] == "done"

        sighting_rows = db._execute(
            "SELECT description, embedding IS NOT NULL AS has_embedding FROM yard_stats.sightings "
            "WHERE raw_event_id = %s",
            (event_id,), fetch=True,
        )
        assert sighting_rows[0]["description"] == "silver hatchback"
        assert sighting_rows[0]["has_embedding"] is True
    finally:
        _cleanup_event(event_id)


def test_run_embedding_backfill_updates_rows_missing_embedding(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(
        ai_worker.requests, "post",
        lambda *a, **k: _Resp(_embed_response([0.1] + [0.0] * (config.EMBEDDING_DIMENSIONS - 1))),
    )

    vehicle_event_id = _insert_event(camera="pytest-backfill")
    person_event_id = _insert_event(camera="pytest-backfill", objects="person")
    try:
        db.complete_sighting(vehicle_event_id, "car", "red sedan")
        db.complete_sighting(person_event_id, "person", "wearing a green hat")

        result = ai_worker.run_embedding_backfill(limit=100)

        assert result["sightings_updated"] >= 2

        vehicle_rows = db._execute(
            "SELECT embedding IS NOT NULL AS has_embedding FROM yard_stats.sightings "
            "WHERE raw_event_id = %s",
            (vehicle_event_id,), fetch=True,
        )
        person_rows = db._execute(
            "SELECT embedding IS NOT NULL AS has_embedding FROM yard_stats.sightings "
            "WHERE raw_event_id = %s",
            (person_event_id,), fetch=True,
        )
        assert vehicle_rows[0]["has_embedding"] is True
        assert person_rows[0]["has_embedding"] is True
    finally:
        _cleanup_event(vehicle_event_id)
        _cleanup_event(person_event_id)
