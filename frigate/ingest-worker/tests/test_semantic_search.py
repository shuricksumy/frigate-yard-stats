"""Integration tests for the pgvector-backed embedding columns and db.semantic_search_sightings /
db.get_retention_info.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Additionally requires the pgvector extension (the pgvector/pgvector:pg16
image, not plain postgres:16) since schema.sql's CREATE EXTENSION IF NOT EXISTS vector and the
embedding columns (sized off config.EMBEDDING_DIMENSIONS) depend on it.
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def _vec(seed: float) -> list[float]:
    # A cheap, deterministic vector sized to whatever EMBEDDING_DIMENSIONS is currently configured
    # to -- exact semantic meaning doesn't matter here, only that two vectors built from a close
    # seed land close in cosine distance and a far seed doesn't.
    return [seed] + [0.0] * (config.EMBEDDING_DIMENSIONS - 1)


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
    db._execute("DELETE FROM yard_stats.sightings WHERE raw_event_id = %s", (event_id,))
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = %s", (event_id,))


def test_vector_literal_rejects_wrong_dimensions():
    with pytest.raises(ValueError):
        db._vector_literal([0.1, 0.2, 0.3])


def test_vector_literal_passes_through_none():
    assert db._vector_literal(None) is None


def test_complete_sighting_stores_embedding(conn_ok):
    event_id = _insert_event(camera="pytest-cam")
    try:
        db.complete_sighting(event_id, "car", "red sedan, roof rack, plate ABC123", embedding=_vec(1.0))
        rows = db._execute(
            "SELECT embedding IS NOT NULL AS has_embedding FROM yard_stats.sightings "
            "WHERE raw_event_id = %s",
            (event_id,), fetch=True,
        )
        assert rows[0]["has_embedding"] is True
    finally:
        _cleanup_event(event_id)


def test_complete_sighting_without_embedding_stays_null(conn_ok):
    event_id = _insert_event(camera="pytest-cam", objects="person")
    try:
        db.complete_sighting(event_id, "person", "wearing a red hoodie")
        rows = db._execute(
            "SELECT embedding IS NULL AS no_embedding FROM yard_stats.sightings "
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
        db.complete_sighting(close_id, "car", "red sedan", embedding=_vec(1.0))
        db.complete_sighting(far_id, "car", "blue hatchback", embedding=_vec(-1.0))
        results = db.semantic_search_sightings(_vec(1.0), object_types=["car"], limit=10)
        result_ids = [r["raw_event_id"] for r in results if r["raw_event_id"] in (close_id, far_id)]
        assert result_ids == [close_id, far_id]
    finally:
        _cleanup_event(close_id)
        _cleanup_event(far_id)


def test_semantic_search_filters_by_object_type(conn_ok):
    # object_types now filters by the actual Frigate label directly, not a "vehicle"/"person"
    # pseudo-category -- there's only one table to search regardless.
    camera = f"pytest-sem-{uuid.uuid4()}"
    vehicle_id = _insert_event(camera=camera, objects="car")
    person_id = _insert_event(camera=camera, objects="person")
    try:
        db.complete_sighting(vehicle_id, "car", "red sedan", embedding=_vec(1.0))
        db.complete_sighting(person_id, "person", "red jacket", embedding=_vec(1.0))

        vehicles_only = db.semantic_search_sightings(_vec(1.0), object_types=["car"], limit=10)
        assert {r["object_label"] for r in vehicles_only} == {"car"}

        persons_only = db.semantic_search_sightings(_vec(1.0), object_types=["person"], limit=10)
        assert {r["object_label"] for r in persons_only} == {"person"}
    finally:
        _cleanup_event(vehicle_id)
        _cleanup_event(person_id)


def test_semantic_search_excludes_sightings_without_embedding(conn_ok):
    camera = f"pytest-sem-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera)
    try:
        db.complete_sighting(event_id, "car", "green coupe")
        results = db.semantic_search_sightings(_vec(1.0), object_types=["car"], limit=50)
        assert event_id not in {r["raw_event_id"] for r in results}
    finally:
        _cleanup_event(event_id)


def _insert_visit(camera, objects="car", det_ids=None):
    visit_id = db.record_visit({
        "camera": camera, "zone": "z", "objects": objects,
        "start_time": 1784198451.0, "end_time": 1784198470.0, "det_ids": det_ids or [],
    })
    return visit_id


def _cleanup_visit(visit_id):
    db._execute("DELETE FROM yard_stats.visit_sightings WHERE visit_id = %s", (visit_id,))
    db._execute("DELETE FROM yard_stats.visits WHERE id = %s", (visit_id,))


# ---- semantic_search_combined (web UI Search tab's own combined events+visits lookup) ----

def test_semantic_search_combined_events_only_matches_semantic_search_sightings(conn_ok):
    camera = f"pytest-combo-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera)
    try:
        db.complete_sighting(event_id, "car", "red sedan", embedding=_vec(1.0))
        results = db.semantic_search_combined(_vec(1.0), object_types=["car"], limit=10, source="events")
        assert all(r["kind"] == "event" for r in results)
        assert event_id in {r["id"] for r in results}
        # Lightbox-ready fields -- has_image reflects _insert_event's own crop_image_base64,
        # ai_status reflects raw_events.ai_status ('done', set by _insert_event).
        row = next(r for r in results if r["id"] == event_id)
        assert row["has_image"] is True
        assert row["has_video"] is False
        assert row["has_preview_gif"] is False
        assert row["ai_status"] == "done"
    finally:
        _cleanup_event(event_id)


def test_semantic_search_combined_visits_only(conn_ok):
    camera = f"pytest-combo-{uuid.uuid4()}"
    visit_id = _insert_visit(camera=camera)
    try:
        db.complete_visit_sighting(visit_id, "car", "silver suv pulling into the driveway", embedding=_vec(1.0))
        results = db.semantic_search_combined(_vec(1.0), object_types=["car"], limit=10, source="visits")
        assert all(r["kind"] == "visit" for r in results)
        assert visit_id in {r["id"] for r in results}
        # ai_status reflects visits.alert_ai_status ('done', set by complete_visit_sighting) --
        # has_image/has_video/has_preview_gif are false here since this test never runs the actual
        # thumb-crop/video workers that would populate those visit columns.
        row = next(r for r in results if r["id"] == visit_id)
        assert row["ai_status"] == "done"
        assert row["has_image"] is False
        assert row["has_video"] is False
        assert row["has_preview_gif"] is False
    finally:
        _cleanup_visit(visit_id)


def test_semantic_search_combined_default_source_ranks_both_together(conn_ok):
    camera = f"pytest-combo-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera)
    visit_id = _insert_visit(camera=camera)
    try:
        db.complete_sighting(event_id, "car", "red sedan", embedding=_vec(1.0))
        db.complete_visit_sighting(visit_id, "car", "red sedan pulling in", embedding=_vec(1.0))
        results = db.semantic_search_combined(_vec(1.0), object_types=["car"], limit=50)
        kinds_and_ids = {(r["kind"], r["id"]) for r in results}
        assert ("event", event_id) in kinds_and_ids
        assert ("visit", visit_id) in kinds_and_ids
    finally:
        _cleanup_event(event_id)
        _cleanup_visit(visit_id)


def test_semantic_search_combined_orders_by_distance_across_both_tables(conn_ok):
    camera = f"pytest-combo-{uuid.uuid4()}"
    close_event_id = _insert_event(camera=camera)
    far_visit_id = _insert_visit(camera=camera)
    try:
        db.complete_sighting(close_event_id, "car", "red sedan", embedding=_vec(1.0))
        db.complete_visit_sighting(far_visit_id, "car", "blue hatchback", embedding=_vec(-1.0))
        results = db.semantic_search_combined(_vec(1.0), object_types=["car"], limit=50)
        ids_in_order = [(r["kind"], r["id"]) for r in results]
        assert ids_in_order.index(("event", close_event_id)) < ids_in_order.index(("visit", far_visit_id))
    finally:
        _cleanup_event(close_event_id)
        _cleanup_visit(far_visit_id)


def test_semantic_search_combined_camera_filter(conn_ok):
    camera_a = f"pytest-combo-a-{uuid.uuid4()}"
    camera_b = f"pytest-combo-b-{uuid.uuid4()}"
    event_a = _insert_event(camera=camera_a)
    event_b = _insert_event(camera=camera_b)
    visit_a = _insert_visit(camera=camera_a)
    visit_b = _insert_visit(camera=camera_b)
    try:
        db.complete_sighting(event_a, "car", "red sedan", embedding=_vec(1.0))
        db.complete_sighting(event_b, "car", "red sedan", embedding=_vec(1.0))
        db.complete_visit_sighting(visit_a, "car", "red sedan", embedding=_vec(1.0))
        db.complete_visit_sighting(visit_b, "car", "red sedan", embedding=_vec(1.0))
        results = db.semantic_search_combined(_vec(1.0), object_types=["car"], limit=50, camera=camera_a)
        kinds_and_ids = {(r["kind"], r["id"]) for r in results}
        assert ("event", event_a) in kinds_and_ids
        assert ("visit", visit_a) in kinds_and_ids
        assert ("event", event_b) not in kinds_and_ids
        assert ("visit", visit_b) not in kinds_and_ids
    finally:
        _cleanup_event(event_a)
        _cleanup_event(event_b)
        _cleanup_visit(visit_a)
        _cleanup_visit(visit_b)


def test_semantic_search_combined_respects_time_window(conn_ok):
    camera = f"pytest-combo-{uuid.uuid4()}"
    old_event_id = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64)
        VALUES (%s, 'z', 'car', now() - interval '2 days', now() - interval '2 days', %s,
                true, true, 'done', 'done', 'ZmFrZQ==')
        RETURNING id
        """,
        (camera, f"pytest-{uuid.uuid4()}"), fetch=True,
    )[0]["id"]
    try:
        db.complete_sighting(old_event_id, "car", "red sedan", embedding=_vec(1.0))
        from datetime import datetime, timedelta, timezone
        results = db.semantic_search_combined(
            _vec(1.0), start=datetime.now(timezone.utc) - timedelta(hours=1),
            object_types=["car"], limit=50, source="events",
        )
        assert old_event_id not in {r["id"] for r in results}
    finally:
        _cleanup_event(old_event_id)


def test_semantic_search_combined_excludes_rows_without_embedding(conn_ok):
    camera = f"pytest-combo-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera)
    visit_id = _insert_visit(camera=camera)
    try:
        db.complete_sighting(event_id, "car", "no embedding here")
        db.complete_visit_sighting(visit_id, "car", "no embedding here either")
        results = db.semantic_search_combined(_vec(1.0), object_types=["car"], limit=50)
        kinds_and_ids = {(r["kind"], r["id"]) for r in results}
        assert ("event", event_id) not in kinds_and_ids
        assert ("visit", visit_id) not in kinds_and_ids
    finally:
        _cleanup_event(event_id)
        _cleanup_visit(visit_id)


def _orthogonal_vec(seed: float) -> list[float]:
    # Orthogonal to _vec(seed) (nonzero only in the second dimension) -- cosine distance from
    # _vec(1.0) is exactly 1.0, vs. 0.0 for an identical vector, giving a clean threshold to test
    # max_distance against without relying on any particular embedding's real-world distance.
    return [0.0, seed] + [0.0] * (config.EMBEDDING_DIMENSIONS - 2)


def test_semantic_search_combined_max_distance_excludes_weak_matches(conn_ok):
    camera = f"pytest-combo-{uuid.uuid4()}"
    close_event_id = _insert_event(camera=camera)
    far_visit_id = _insert_visit(camera=camera)
    try:
        db.complete_sighting(close_event_id, "car", "red sedan", embedding=_vec(1.0))
        db.complete_visit_sighting(far_visit_id, "car", "unrelated", embedding=_orthogonal_vec(1.0))
        # No cutoff -- both come back, same as today's behavior.
        results = db.semantic_search_combined(_vec(1.0), object_types=["car"], limit=50)
        kinds_and_ids = {(r["kind"], r["id"]) for r in results}
        assert ("event", close_event_id) in kinds_and_ids
        assert ("visit", far_visit_id) in kinds_and_ids
        # A cutoff strictly between the two distances (0.0 and 1.0) keeps the close match and
        # drops the orthogonal one.
        filtered = db.semantic_search_combined(_vec(1.0), object_types=["car"], limit=50, max_distance=0.5)
        kinds_and_ids = {(r["kind"], r["id"]) for r in filtered}
        assert ("event", close_event_id) in kinds_and_ids
        assert ("visit", far_visit_id) not in kinds_and_ids
    finally:
        _cleanup_event(close_event_id)
        _cleanup_visit(far_visit_id)


def test_semantic_search_combined_max_distance_keeps_literal_keyword_match(conn_ok):
    # A sighting can literally contain the query word yet still land outside a distance cutoff,
    # since a general-purpose embedding weighs a sentence's dominant subject more than a short
    # trailing detail (confirmed in production: a person-focused description mentioning "a small
    # dog nearby" only in passing scored just past a 0.45 cutoff for query "dog"). The cutoff
    # should never hide a literal keyword match regardless of its embedding distance.
    camera = f"pytest-combo-{uuid.uuid4()}"
    far_event_id = _insert_event(camera=camera)
    try:
        db.complete_sighting(
            far_event_id, "person",
            "An adult in grey clothing walks briskly while looking at a phone, with a small dog nearby.",
            embedding=_orthogonal_vec(1.0),
        )
        # Without the query text, a strict cutoff excludes this distant-but-relevant sighting.
        filtered = db.semantic_search_combined(_vec(1.0), object_types=["person"], limit=50, max_distance=0.5)
        assert far_event_id not in {r["id"] for r in filtered}
        # With the query text, the literal "dog" word match keeps it in, despite the same cutoff.
        filtered = db.semantic_search_combined(
            _vec(1.0), object_types=["person"], limit=50, max_distance=0.5, query_text="dog",
        )
        assert far_event_id in {r["id"] for r in filtered}
    finally:
        _cleanup_event(far_event_id)


def test_semantic_search_combined_max_distance_keyword_fallback_is_whole_word_only(conn_ok):
    # Confirmed live in production: query "cat" against a plain ILIKE '%cat%' fallback matched
    # "indi-CAT-ion"/"lo-CAT-ion" inside completely unrelated car/person descriptions -- 24 results,
    # none actually about a cat, every one already past its own distance cutoff. The fallback must
    # require a whole word, not any substring occurrence.
    camera = f"pytest-combo-{uuid.uuid4()}"
    far_event_id = _insert_event(camera=camera)
    try:
        db.complete_sighting(
            far_event_id, "person",
            "A person in dark clothing standing near parked cars, with no indication of distress.",
            embedding=_orthogonal_vec(1.0),
        )
        filtered = db.semantic_search_combined(
            _vec(1.0), object_types=["person"], limit=50, max_distance=0.5, query_text="cat",
        )
        assert far_event_id not in {r["id"] for r in filtered}
    finally:
        _cleanup_event(far_event_id)


def test_get_retention_info_returns_configured_months_and_oldest_ts(conn_ok):
    info = db.get_retention_info()
    assert info["retention_months"] == db.config.RETENTION_MONTHS
    assert "oldest_available_start_ts" in info


def test_get_sightings_missing_embedding_excludes_rows_with_one(conn_ok):
    camera = f"pytest-backfill-{uuid.uuid4()}"
    missing_id = _insert_event(camera=camera)
    has_one_id = _insert_event(camera=camera)
    try:
        db.complete_sighting(missing_id, "car", "red sedan")
        db.complete_sighting(has_one_id, "car", "blue suv", embedding=_vec(1.0))
        rows = db.get_sightings_missing_embedding(limit=100)
        raw_event_ids = {r["raw_event_id"] for r in rows}
        assert missing_id in raw_event_ids
        assert has_one_id not in raw_event_ids
    finally:
        _cleanup_event(missing_id)
        _cleanup_event(has_one_id)


def test_update_sighting_embedding_sets_column(conn_ok):
    event_id = _insert_event(camera="pytest-backfill")
    try:
        sighting_id = db.complete_sighting(event_id, "car", "black hatchback")
        db.update_sighting_embedding(sighting_id, _vec(1.0))
        rows = db._execute(
            "SELECT embedding IS NOT NULL AS has_embedding FROM yard_stats.sightings WHERE id = %s",
            (sighting_id,), fetch=True,
        )
        assert rows[0]["has_embedding"] is True
    finally:
        _cleanup_event(event_id)


def test_count_sightings_missing_embedding_reflects_null_rows(conn_ok):
    camera = f"pytest-backfill-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera)
    try:
        db.complete_sighting(event_id, "car", "white van")
        before = db.count_sightings_missing_embedding()
        assert before["sightings"] >= 1
    finally:
        _cleanup_event(event_id)
