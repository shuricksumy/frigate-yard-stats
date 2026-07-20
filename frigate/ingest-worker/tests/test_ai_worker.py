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

PROFILE = {
    "object_types": {"car": {"sighting_type": "vehicle"}, "person": {"sighting_type": "person"}},
    "vehicle": {"chat_path": "/vehicle-slot/v1/chat/completions", "prompt": "vehicle prompt"},
    "person": {"chat_path": "/person-slot/v1/chat/completions", "prompt": "person prompt"},
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


# ---- _sanitize_plate ----

@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    ("   ", None),
    ("10MG407", "10MG407"),
    ("The plate reads ABC1234 on a white background", "ABC1234"),
])
def test_sanitize_plate(raw, expected):
    assert ai_worker._sanitize_plate(raw) == expected


# ---- parse_vehicle_response / parse_person_response ----

def test_parse_vehicle_response_extracts_json_and_sanitizes_plate():
    response = _chat_response(
        'Sure, here is the JSON: {"color": "red", "body_type": "sedan", "make": "Toyota", '
        '"make_confidence": "high", "model": "Camry", "model_confidence": "medium", '
        '"notable_features": "roof rack", "plate_text": "10MG407"}'
    )
    row = {"id": 42, "sub_label": "10MG407-frigate"}
    fields = ai_worker.parse_vehicle_response(response, row)
    assert fields["raw_event_id"] == 42
    assert fields["color"] == "red"
    assert fields["make_guess"] == "Toyota"
    assert fields["plate_text_llm"] == "10MG407"
    assert fields["plate_text_frigate"] == "10MG407-frigate"


def test_parse_vehicle_response_handles_unparseable_content():
    response = _chat_response("no json here at all")
    fields = ai_worker.parse_vehicle_response(response, {"id": 1})
    assert fields["color"] is None
    assert fields["plate_text_llm"] is None


def test_parse_person_response_returns_description_as_is():
    response = _chat_response("wearing a red hoodie, walking toward the door")
    fields = ai_worker.parse_person_response(response, {"id": 7})
    assert fields == {
        "raw_event_id": 7,
        "description": "wearing a red hoodie, walking toward the door",
        "notes": None,
    }


# ---- load_profile ----

def test_load_profile_parses_real_file():
    path = os.path.join(os.path.dirname(__file__), "..", "profiles.yaml")
    profile = ai_worker.load_profile(path)
    assert set(profile["object_types"]) == {"car", "truck", "person"}
    assert "dog" not in profile["object_types"]
    assert profile["vehicle"]["prompt"]
    assert profile["person"]["prompt"]


# ---- run_once ----

def test_run_once_only_claims_mapped_object_types(monkeypatch):
    captured = {}

    def fake_claim(object_types, parallel_limit, stale_minutes, max_age_hours=None, **kwargs):
        captured["object_types"] = object_types
        return []

    monkeypatch.setattr(db, "claim_ai_batch", fake_claim)
    ai_worker.run_once(PROFILE)
    assert set(captured["object_types"]) == {"car", "person"}


# ---- process_claimed_event ----

def test_process_claimed_event_vehicle_success(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(config, "LLAMA_PROXY_TOKEN", "")

    responses = [
        _Resp(_chat_response('{"color": "blue", "body_type": "suv"}')),
        _Resp(_embed_response([0.1, 0.2])),
    ]
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(ai_worker.requests, "post", fake_post)

    inserted = []
    monkeypatch.setattr(db, "complete_vehicle_sighting", lambda *a, **k: inserted.append(a) or 1)
    failed = []
    monkeypatch.setattr(db, "fail_ai_event", lambda *a, **k: failed.append((a, k)))

    row = {"id": 5, "objects": "car", "crop_image_base64": "aGVsbG8=", "sub_label": None, "det_id": "d1"}
    ai_worker.process_claimed_event(row, PROFILE)

    assert len(inserted) == 1
    assert not failed
    assert calls[0] == "http://llama.test/vehicle-slot/v1/chat/completions"


def test_process_claimed_event_uses_profile_timeout(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    profile = {
        "object_types": {"car": {"sighting_type": "vehicle"}},
        "vehicle": {
            "chat_path": "/vehicle-slot/v1/chat/completions",
            "prompt": "vehicle prompt",
            "timeout_seconds": 42,
        },
    }
    captured_timeouts = []

    def fake_post(url, timeout=None, **kwargs):
        captured_timeouts.append(timeout)
        if "chat/completions" in url:
            return _Resp(_chat_response('{"color": "black"}'))
        return _Resp(_embed_response([0.1]))

    monkeypatch.setattr(ai_worker.requests, "post", fake_post)
    monkeypatch.setattr(db, "complete_vehicle_sighting", lambda *a, **k: 1)

    row = {"id": 10, "objects": "car", "crop_image_base64": "x", "sub_label": None, "det_id": "d5"}
    ai_worker.process_claimed_event(row, profile)

    assert captured_timeouts[0] == 42  # the chat call's timeout came from the profile
    assert captured_timeouts[1] == config.AI_STAGE_EMBED_TIMEOUT_SECONDS


def test_process_claimed_event_falls_back_to_default_timeout_when_unset(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(config, "AI_STAGE_DEFAULT_TIMEOUT_SECONDS", 180)
    # PROFILE's vehicle entry has no timeout_seconds -- must fall back to the config default.
    captured_timeouts = []

    def fake_post(url, timeout=None, **kwargs):
        captured_timeouts.append(timeout)
        if "chat/completions" in url:
            return _Resp(_chat_response('{"color": "white"}'))
        return _Resp(_embed_response([0.1]))

    monkeypatch.setattr(ai_worker.requests, "post", fake_post)
    monkeypatch.setattr(db, "complete_vehicle_sighting", lambda *a, **k: 1)

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
    monkeypatch.setattr(db, "complete_vehicle_sighting", lambda *a, **k: inserted.append(a))

    row = {"id": 8, "objects": "car", "crop_image_base64": "x", "det_id": "d3"}
    ai_worker.process_claimed_event(row, PROFILE)

    assert failed == [((8, 3), {})]
    assert not inserted


def test_process_claimed_event_embedding_failure_still_inserts_sighting(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")

    def fake_post(url, **kwargs):
        if "chat/completions" in url:
            return _Resp(_chat_response('{"color": "green"}'))
        raise RuntimeError("embedding backend down")

    monkeypatch.setattr(ai_worker.requests, "post", fake_post)
    inserted = []
    monkeypatch.setattr(db, "complete_vehicle_sighting", lambda *a, **k: inserted.append(a) or 1)
    failed = []
    monkeypatch.setattr(db, "fail_ai_event", lambda *a, **k: failed.append((a, k)))

    row = {"id": 9, "objects": "car", "crop_image_base64": "x", "sub_label": None, "det_id": "d4"}
    ai_worker.process_claimed_event(row, PROFILE)

    assert len(inserted) == 1
    assert inserted[0][-1] is None  # embedding is the last positional arg -- None on failure
    assert not failed


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
    db._execute("DELETE FROM yard_stats.vehicle_sightings WHERE raw_event_id = %s", (event_id,))
    db._execute("DELETE FROM yard_stats.person_sightings WHERE raw_event_id = %s", (event_id,))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = %s", (event_id,))


def test_process_claimed_event_end_to_end_marks_ai_status_done(conn_ok, monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")

    def fake_post(url, **kwargs):
        if "chat/completions" in url:
            return _Resp(_chat_response('{"color": "silver", "body_type": "hatchback"}'))
        return _Resp(_embed_response([0.1] + [0.0] * 767))

    monkeypatch.setattr(ai_worker.requests, "post", fake_post)

    event_id = _insert_event(camera="pytest-ai-worker")
    try:
        row = db.get_raw_event(event_id)
        ai_worker.process_claimed_event(row, PROFILE)

        updated = db.get_raw_event(event_id)
        assert updated["ai_status"] == "done"

        sighting_rows = db._execute(
            "SELECT color, embedding IS NOT NULL AS has_embedding FROM yard_stats.vehicle_sightings "
            "WHERE raw_event_id = %s",
            (event_id,), fetch=True,
        )
        assert sighting_rows[0]["color"] == "silver"
        assert sighting_rows[0]["has_embedding"] is True
    finally:
        _cleanup_event(event_id)
