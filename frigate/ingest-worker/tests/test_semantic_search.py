"""Integration tests for the pgvector-backed embedding columns and db.semantic_search_sightings /
db.get_retention_info.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Additionally requires the pgvector extension (the pgvector/pgvector:pg16
image, not plain postgres:16) since schema.sql's CREATE EXTENSION IF NOT EXISTS vector and the
vector(768) columns depend on it.
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import db  # noqa: E402


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def _vec(seed: float) -> list[float]:
    # A cheap, deterministic 768-dim vector -- exact semantic meaning doesn't matter here, only
    # that two vectors built from a close seed land close in cosine distance and a far seed doesn't.
    return [seed] + [0.0] * (db.EMBEDDING_DIMENSIONS - 1)


def _insert_event(camera, objects="car"):
    det_id = f"pytest-{uuid.uuid4()}"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64)
        VALUES (%s, 'z', %s, now(), now(), %s, true, true, 'done', 'done', 'ZmFrZQ==')
        RETURNING id
        """,
        (camera, objects, det_id), fetch=True,
    )
    return rows[0]["id"]


def _cleanup_event(event_id):
    db._execute("DELETE FROM yard_stats.vehicle_sightings WHERE raw_event_id = %s", (event_id,))
    db._execute("DELETE FROM yard_stats.person_sightings WHERE raw_event_id = %s", (event_id,))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = %s", (event_id,))


def test_vector_literal_rejects_wrong_dimensions():
    with pytest.raises(ValueError):
        db._vector_literal([0.1, 0.2, 0.3])


def test_vector_literal_passes_through_none():
    assert db._vector_literal(None) is None


def test_complete_vehicle_sighting_stores_embedding(conn_ok):
    event_id = _insert_event(camera="pytest-cam")
    try:
        db.complete_vehicle_sighting(
            event_id, color="red", body_type="sedan", make_guess="Toyota",
            make_confidence="high", model_guess="Camry", model_confidence="medium",
            notable_features="roof rack", plate_text_llm="ABC123", plate_text_frigate=None,
            plate_confidence="high", notes=None, embedding=_vec(1.0),
        )
        rows = db._execute(
            "SELECT embedding IS NOT NULL AS has_embedding FROM yard_stats.vehicle_sightings "
            "WHERE raw_event_id = %s",
            (event_id,), fetch=True,
        )
        assert rows[0]["has_embedding"] is True
    finally:
        _cleanup_event(event_id)


def test_complete_person_sighting_without_embedding_stays_null(conn_ok):
    event_id = _insert_event(camera="pytest-cam", objects="person")
    try:
        db.complete_person_sighting(event_id, description="wearing a red hoodie", notes=None)
        rows = db._execute(
            "SELECT embedding IS NULL AS no_embedding FROM yard_stats.person_sightings "
            "WHERE raw_event_id = %s",
            (event_id,), fetch=True,
        )
        assert rows[0]["no_embedding"] is True
    finally:
        _cleanup_event(event_id)


def test_semantic_search_orders_by_distance(conn_ok):
    camera = f"pytest-sem-{uuid.uuid4()}"
    close_id = _insert_event(camera=camera)
    far_id = _insert_event(camera=camera)
    try:
        db.complete_vehicle_sighting(
            close_id, color="red", body_type="sedan", make_guess=None, make_confidence=None,
            model_guess=None, model_confidence=None, notable_features=None, plate_text_llm=None,
            plate_text_frigate=None, plate_confidence=None, notes=None, embedding=_vec(1.0),
        )
        db.complete_vehicle_sighting(
            far_id, color="blue", body_type="hatchback", make_guess=None, make_confidence=None,
            model_guess=None, model_confidence=None, notable_features=None, plate_text_llm=None,
            plate_text_frigate=None, plate_confidence=None, notes=None, embedding=_vec(-1.0),
        )
        results = db.semantic_search_sightings(
            _vec(1.0), object_types=["vehicle"], limit=10,
        )
        result_ids = [r["raw_event_id"] for r in results if r["raw_event_id"] in (close_id, far_id)]
        assert result_ids == [close_id, far_id]
    finally:
        _cleanup_event(close_id)
        _cleanup_event(far_id)


def test_semantic_search_filters_by_object_type(conn_ok):
    camera = f"pytest-sem-{uuid.uuid4()}"
    vehicle_id = _insert_event(camera=camera, objects="car")
    person_id = _insert_event(camera=camera, objects="person")
    try:
        db.complete_vehicle_sighting(
            vehicle_id, color="red", body_type="sedan", make_guess=None, make_confidence=None,
            model_guess=None, model_confidence=None, notable_features=None, plate_text_llm=None,
            plate_text_frigate=None, plate_confidence=None, notes=None, embedding=_vec(1.0),
        )
        db.complete_person_sighting(person_id, description="red jacket", notes=None, embedding=_vec(1.0))

        vehicles_only = db.semantic_search_sightings(_vec(1.0), object_types=["vehicle"], limit=10)
        assert {r["sighting_type"] for r in vehicles_only} == {"vehicle"}

        persons_only = db.semantic_search_sightings(_vec(1.0), object_types=["person"], limit=10)
        assert {r["sighting_type"] for r in persons_only} == {"person"}
    finally:
        _cleanup_event(vehicle_id)
        _cleanup_event(person_id)


def test_semantic_search_excludes_sightings_without_embedding(conn_ok):
    camera = f"pytest-sem-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera)
    try:
        db.complete_vehicle_sighting(
            event_id, color="green", body_type="coupe", make_guess=None, make_confidence=None,
            model_guess=None, model_confidence=None, notable_features=None, plate_text_llm=None,
            plate_text_frigate=None, plate_confidence=None, notes=None,
        )
        results = db.semantic_search_sightings(_vec(1.0), object_types=["vehicle"], limit=50)
        assert event_id not in {r["raw_event_id"] for r in results}
    finally:
        _cleanup_event(event_id)


def test_get_retention_info_returns_configured_months_and_oldest_ts(conn_ok):
    info = db.get_retention_info()
    assert info["retention_months"] == db.config.RETENTION_MONTHS
    assert "oldest_available_start_ts" in info


def test_get_vehicle_sightings_missing_embedding_excludes_rows_with_one(conn_ok):
    camera = f"pytest-backfill-{uuid.uuid4()}"
    missing_id = _insert_event(camera=camera)
    has_one_id = _insert_event(camera=camera)
    try:
        db.complete_vehicle_sighting(
            missing_id, color="red", body_type="sedan", make_guess=None, make_confidence=None,
            model_guess=None, model_confidence=None, notable_features=None, plate_text_llm=None,
            plate_text_frigate=None, plate_confidence=None, notes=None,
        )
        db.complete_vehicle_sighting(
            has_one_id, color="blue", body_type="suv", make_guess=None, make_confidence=None,
            model_guess=None, model_confidence=None, notable_features=None, plate_text_llm=None,
            plate_text_frigate=None, plate_confidence=None, notes=None, embedding=_vec(1.0),
        )
        rows = db.get_vehicle_sightings_missing_embedding(limit=100)
        raw_event_ids = {r["raw_event_id"] for r in rows}
        assert missing_id in raw_event_ids
        assert has_one_id not in raw_event_ids
    finally:
        _cleanup_event(missing_id)
        _cleanup_event(has_one_id)


def test_update_vehicle_sighting_embedding_sets_column(conn_ok):
    event_id = _insert_event(camera="pytest-backfill")
    try:
        sighting_id = db.complete_vehicle_sighting(
            event_id, color="black", body_type="hatchback", make_guess=None, make_confidence=None,
            model_guess=None, model_confidence=None, notable_features=None, plate_text_llm=None,
            plate_text_frigate=None, plate_confidence=None, notes=None,
        )
        db.update_vehicle_sighting_embedding(sighting_id, _vec(1.0))
        rows = db._execute(
            "SELECT embedding IS NOT NULL AS has_embedding FROM yard_stats.vehicle_sightings WHERE id = %s",
            (sighting_id,), fetch=True,
        )
        assert rows[0]["has_embedding"] is True
    finally:
        _cleanup_event(event_id)


def test_update_person_sighting_embedding_sets_column(conn_ok):
    event_id = _insert_event(camera="pytest-backfill", objects="person")
    try:
        sighting_id = db.complete_person_sighting(event_id, description="wearing a blue jacket", notes=None)
        db.update_person_sighting_embedding(sighting_id, _vec(1.0))
        rows = db._execute(
            "SELECT embedding IS NOT NULL AS has_embedding FROM yard_stats.person_sightings WHERE id = %s",
            (sighting_id,), fetch=True,
        )
        assert rows[0]["has_embedding"] is True
    finally:
        _cleanup_event(event_id)


def test_count_sightings_missing_embedding_reflects_null_rows(conn_ok):
    camera = f"pytest-backfill-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera)
    try:
        db.complete_vehicle_sighting(
            event_id, color="white", body_type="van", make_guess=None, make_confidence=None,
            model_guess=None, model_confidence=None, notable_features=None, plate_text_llm=None,
            plate_text_frigate=None, plate_confidence=None, notes=None,
        )
        before = db.count_sightings_missing_embedding()
        assert before["vehicle_sightings"] >= 1
    finally:
        _cleanup_event(event_id)
