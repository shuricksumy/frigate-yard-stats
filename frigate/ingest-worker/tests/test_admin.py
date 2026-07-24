"""Tests for admin.py (disk usage, embedding backend health check) and the admin-dashboard-only
db.py functions (get_table_row_counts, get_stage_counts, get_db_size_info,
get_vector_index_status, reindex_vector_indexes, requeue_failed).

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes. Additionally requires the pgvector extension (pgvector/pgvector:pg16),
same as test_semantic_search.py.
"""
import os
import uuid

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import admin  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402


@pytest.fixture
def conn_ok():
    try:
        db.get_conn()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def _insert_event(camera, objects="car", ai_status="new"):
    det_id = f"pytest-admin-{uuid.uuid4()}"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, ai_attempt_count, crop_image_base64)
        VALUES (%s, 'z', %s, now(), now(), %s, true, true, 'done', %s, 3, 'ZmFrZQ==')
        RETURNING id
        """,
        (camera, objects, det_id, ai_status), fetch=True,
    )
    return rows[0]["id"]


def _cleanup_event(event_id):
    db._execute("DELETE FROM yard_stats.raw_events WHERE id = %s", (event_id,))


# ---- db.get_table_row_counts / get_stage_counts ----

def test_get_table_row_counts_reflects_inserted_row(conn_ok):
    before = db.get_table_row_counts()["raw_events"]
    event_id = _insert_event(camera="pytest-admin-counts")
    try:
        after = db.get_table_row_counts()["raw_events"]
        assert after == before + 1
    finally:
        _cleanup_event(event_id)


def test_get_table_row_counts_has_universal_sighting_tables(conn_ok):
    result = db.get_table_row_counts()
    assert set(result.keys()) == {"raw_events", "visits", "sightings", "visit_sightings"}


def test_get_stage_counts_has_expected_shape(conn_ok):
    result = db.get_stage_counts()
    assert set(result.keys()) == {"raw_events", "visits"}
    assert set(result["raw_events"].keys()) == {"crop_status", "video_status", "ai_status"}
    assert set(result["visits"].keys()) == {"video_status", "thumb_crop_status", "alert_ai_status"}


def test_get_stage_counts_counts_a_failed_ai_row(conn_ok):
    event_id = _insert_event(camera="pytest-admin-stage", ai_status="failed")
    try:
        result = db.get_stage_counts()
        assert result["raw_events"]["ai_status"].get("failed", 0) >= 1
    finally:
        _cleanup_event(event_id)


# ---- db.get_row_counts_by_object_type / get_db_size_by_object_type ----

def test_get_row_counts_by_object_type_reflects_inserted_rows(conn_ok):
    camera = f"pytest-admin-bytype-{uuid.uuid4()}"
    car_id = _insert_event(camera=camera, objects="car")
    dog_id = _insert_event(camera=camera, objects="dog")
    try:
        result = db.get_row_counts_by_object_type()
        raw_events_by_type = {r["object_type"]: r["count"] for r in result["raw_events"]}
        assert raw_events_by_type.get("car", 0) >= 1
        assert raw_events_by_type.get("dog", 0) >= 1
        assert set(result.keys()) == {"raw_events", "sightings", "visit_sightings"}
    finally:
        _cleanup_event(car_id)
        _cleanup_event(dog_id)


def test_get_row_counts_by_object_type_includes_sightings(conn_ok):
    camera = f"pytest-admin-bytype-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera, objects="car")
    db._execute(
        "INSERT INTO yard_stats.sightings (raw_event_id, object_label, description) VALUES (%s, 'car', 'red suv')",
        (event_id,),
    )
    try:
        result = db.get_row_counts_by_object_type()
        sightings_by_type = {r["object_type"]: r["count"] for r in result["sightings"]}
        assert sightings_by_type.get("car", 0) >= 1
    finally:
        db._execute("DELETE FROM yard_stats.sightings WHERE raw_event_id = %s", (event_id,))
        _cleanup_event(event_id)


def test_get_db_size_by_object_type_reports_positive_bytes_for_a_type_with_rows(conn_ok):
    camera = f"pytest-admin-bytype-{uuid.uuid4()}"
    event_id = _insert_event(camera=camera, objects="car")
    try:
        result = db.get_db_size_by_object_type()
        raw_events_bytes = {r["object_type"]: r["bytes"] for r in result["raw_events"]}
        assert raw_events_bytes.get("car", 0) > 0
        assert set(result.keys()) == {"raw_events", "sightings", "visit_sightings"}
    finally:
        _cleanup_event(event_id)


# ---- admin.dir_size_by_object_type ----

def test_dir_size_by_object_type_missing_path_reports_empty():
    assert admin.dir_size_by_object_type("/no/such/path/on/this/machine") == {}


def test_dir_size_by_object_type_buckets_event_and_visit_filenames(tmp_path):
    (tmp_path / "car-1-1700000000-20231114T220000Z.mp4").write_bytes(b"x" * 100)
    (tmp_path / "car-2-1700000100-20231114T220140Z.mp4").write_bytes(b"y" * 50)
    (tmp_path / "person-3-1700000200-20231114T220320Z.mp4").write_bytes(b"z" * 25)
    (tmp_path / "visit-car-9-1700000300-20231114T220500Z.mp4").write_bytes(b"w" * 10)
    (tmp_path / "some-unrelated-file.txt").write_bytes(b"q" * 5)

    result = admin.dir_size_by_object_type(str(tmp_path))

    assert result["car"] == {"bytes": 160, "file_count": 3}  # 100 + 50 + 10 (event x2 + visit x1)
    assert result["person"] == {"bytes": 25, "file_count": 1}
    assert result["some"] == {"bytes": 5, "file_count": 1}  # unrelated file -- first hyphen token


# ---- db.requeue_failed ----

def test_requeue_failed_rejects_unknown_combination():
    with pytest.raises(ValueError):
        db.requeue_failed("bogus_table", "ai")
    with pytest.raises(ValueError):
        db.requeue_failed("raw_events", "bogus_stage")


def test_requeue_failed_resets_status_and_attempt_count(conn_ok):
    event_id = _insert_event(camera="pytest-admin-requeue", ai_status="failed")
    try:
        count = db.requeue_failed("raw_events", "ai")
        assert count >= 1
        row = db.get_raw_event(event_id)
        assert row["ai_status"] == "retry"
        assert row["ai_attempt_count"] == 0
    finally:
        _cleanup_event(event_id)


def test_requeue_failed_is_noop_when_nothing_failed(conn_ok):
    event_id = _insert_event(camera="pytest-admin-requeue-noop", ai_status="done")
    try:
        db.requeue_failed("raw_events", "ai")
        row = db.get_raw_event(event_id)
        assert row["ai_status"] == "done"  # untouched -- wasn't 'failed'
    finally:
        _cleanup_event(event_id)


# ---- db.skip_failed_older_than ----

def test_skip_failed_older_than_rejects_unknown_combination():
    with pytest.raises(ValueError):
        db.skip_failed_older_than("bogus_table", "ai", 7)
    with pytest.raises(ValueError):
        db.skip_failed_older_than("raw_events", "bogus_stage", 7)


def test_skip_failed_older_than_marks_old_failures_skipped(conn_ok):
    event_id = _insert_event(camera="pytest-admin-skip-old", ai_status="failed")
    try:
        db._execute(
            "UPDATE yard_stats.raw_events SET ai_status_changed_at = now() - interval '8 days' WHERE id = %s",
            (event_id,),
        )
        count = db.skip_failed_older_than("raw_events", "ai", 7)
        assert count >= 1
        row = db.get_raw_event(event_id)
        assert row["ai_status"] == "skipped"
        assert row["ai_attempt_count"] == 3  # left as-is, unlike requeue_failed's reset to 0
    finally:
        _cleanup_event(event_id)


def test_skip_failed_older_than_leaves_recent_failures_alone(conn_ok):
    # Just failed (ai_status_changed_at defaults to now()) -- still within the 7-day window, so
    # this is a still-worth-retrying failure, not a permanent one to give up on.
    event_id = _insert_event(camera="pytest-admin-skip-recent", ai_status="failed")
    try:
        db.skip_failed_older_than("raw_events", "ai", 7)
        row = db.get_raw_event(event_id)
        assert row["ai_status"] == "failed"
    finally:
        _cleanup_event(event_id)


# ---- db.get_db_size_info / get_vector_index_status / reindex_vector_indexes ----

def test_get_db_size_info_returns_positive_total_and_known_tables(conn_ok):
    result = db.get_db_size_info()
    assert result["database_bytes"] > 0
    table_names = {row["table"] for row in result["tables"]}
    assert {"raw_events", "visits", "sightings", "visit_sightings"} <= table_names


def test_get_vector_index_status_reports_valid_indexes(conn_ok):
    result = db.get_vector_index_status()
    assert result["extension_installed"] is True
    assert result["embedding_dimensions"] == config.EMBEDDING_DIMENSIONS
    index_names = {row["index"] for row in result["indexes"]}
    assert "yard_stats.idx_sightings_embedding" in index_names
    assert "yard_stats.idx_visit_sightings_embedding" in index_names
    assert all(row["indisvalid"] for row in result["indexes"])


def test_reindex_vector_indexes_runs_without_error(conn_ok):
    result = db.reindex_vector_indexes()
    assert set(result) == {"idx_sightings_embedding", "idx_visit_sightings_embedding"}


# ---- admin.dir_size_bytes ----

def test_dir_size_bytes_missing_path_reports_zero():
    result = admin.dir_size_bytes("/no/such/path/on/this/machine")
    assert result == {"path": "/no/such/path/on/this/machine", "exists": False, "bytes": 0, "file_count": 0}


def test_dir_size_bytes_sums_real_files(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x" * 100)
    (tmp_path / "b.mp4").write_bytes(b"y" * 50)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.mp4").write_bytes(b"z" * 25)

    result = admin.dir_size_bytes(str(tmp_path))
    assert result["exists"] is True
    assert result["bytes"] == 175
    assert result["file_count"] == 3


# ---- admin.check_embedding_backend ----

def test_check_embedding_backend_requires_base_url(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "")
    result = admin.check_embedding_backend()
    assert result["ok"] is False
    assert "LLAMA_PROXY_BASE_URL" in result["detail"]


def test_check_embedding_backend_reports_dimension_mismatch(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(config, "EMBEDDING_DIMENSIONS", 1024)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"embedding": [0.1] * 768}]}

    monkeypatch.setattr(admin.requests, "post", lambda *a, **k: _Resp())
    result = admin.check_embedding_backend()
    assert result["ok"] is False
    assert result["dimensions"] == 768
    assert result["expected_dimensions"] == 1024


def test_check_embedding_backend_ok_on_matching_dimension(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")
    monkeypatch.setattr(config, "EMBEDDING_DIMENSIONS", 4)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}

    monkeypatch.setattr(admin.requests, "post", lambda *a, **k: _Resp())
    result = admin.check_embedding_backend()
    assert result == {"ok": True, "dimensions": 4, "expected_dimensions": 4, "detail": None}


def test_check_embedding_backend_reports_request_exception(monkeypatch):
    monkeypatch.setattr(config, "LLAMA_PROXY_BASE_URL", "http://llama.test")

    def _raise(*a, **k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(admin.requests, "post", _raise)
    result = admin.check_embedding_backend()
    assert result["ok"] is False
    assert "connection refused" in result["detail"]
