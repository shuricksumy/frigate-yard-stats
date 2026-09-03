"""Integration tests for selective event deletion (db.preview_event_deletion / db.delete_events).

Built for clearing out false alarms -- repeated re-detections of one parked car, which cluster at a
few seconds each (see CLAUDE.md's min_event_duration_seconds notes). Distinct from the age-based
/retention/purge, so it has its own selection rules and its own visit handling.

The behaviour that needs the most protection is the visit sweep: deleting every event a visit
grouped would otherwise leave the visit itself behind as an empty shell -- still listed on the
Visits tab, still counted, with nothing behind it. A visit that keeps at least one event must NOT
be touched.

Requires a reachable Postgres with schema.sql applied -- see test_db_video_queue.py's module
docstring for setup notes.
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
        db.check_connection()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable for integration test: {exc}")


def _event(camera, duration_seconds=5, objects="car", visit_id=None, video_path=None, image_path=None):
    row = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64, visit_id, video_path, image_path)
        VALUES (%s, 'z', %s, now() - interval '1 hour',
                now() - interval '1 hour' + (%s * interval '1 second'),
                %s, true, true, 'done', 'done', 'ZmFrZQ==', %s, %s, %s)
        RETURNING id
        """,
        (camera, objects, duration_seconds, f"pytest-{uuid.uuid4()}", visit_id, video_path, image_path),
        fetch=True,
    )[0]["id"]
    db._execute(
        "INSERT INTO yard_stats.sightings (raw_event_id, object_label, description) VALUES (%s, %s, %s)",
        (row, objects, "a parked car"),
    )
    return row


def _visit(camera):
    return db.record_visit({
        "camera": camera, "zone": "z", "objects": "car",
        "start_time": 1785700000.0, "end_time": 1785700010.0, "det_ids": [],
    })


def _cleanup(camera, visit_ids=()):
    db._execute(
        "DELETE FROM yard_stats.sightings WHERE raw_event_id IN "
        "(SELECT id FROM yard_stats.raw_events WHERE camera = %s)", (camera,),
    )
    db._execute("DELETE FROM yard_stats.raw_events WHERE camera = %s", (camera,))
    if visit_ids:
        db._execute("DELETE FROM yard_stats.visit_summaries WHERE visit_id = ANY(%s)", (list(visit_ids),))
        db._execute("DELETE FROM yard_stats.visits WHERE id = ANY(%s)", (list(visit_ids),))


def _event_exists(event_id):
    return db._execute(
        "SELECT count(*)::int AS c FROM yard_stats.raw_events WHERE id = %s", (event_id,), fetch=True,
    )[0]["c"] == 1


def _visit_exists(visit_id):
    return db._execute(
        "SELECT count(*)::int AS c FROM yard_stats.visits WHERE id = %s", (visit_id,), fetch=True,
    )[0]["c"] == 1


# ---- selection rules ----

def test_selection_requires_at_least_one_filter():
    # An unbounded match would offer to delete the entire table, one missing parameter away.
    with pytest.raises(ValueError):
        db.build_event_selection()


def test_explicit_ids_override_every_other_filter():
    # The admin UI previews by filter, then confirms with the ids left ticked. Re-applying the
    # filters at confirm time could pick up a row that appeared in between, or drop one that aged
    # out of the window -- so ids win outright.
    where, params = db.build_event_selection(event_ids=[1, 2], camera="ignored", max_duration_seconds=1)
    # Columns are qualified with the `re` alias because this clause is interpolated into queries
    # that join sightings (which also has an `id`), where a bare column would be ambiguous.
    assert where == "re.id = ANY(%s)"
    assert params == [[1, 2]]


def test_duration_filter_targets_the_flicker_signature(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    short, long = _event(camera, 3), _event(camera, 240)
    try:
        preview = db.preview_event_deletion(camera=camera, max_duration_seconds=10)
        assert preview["events"] == 1
        assert [r["id"] for r in preview["sample"]] == [short]
        assert long not in [r["id"] for r in preview["sample"]]
    finally:
        _cleanup(camera)


# ---- the visit sweep ----

def test_visit_emptied_by_the_deletion_is_removed(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    visit = _visit(camera)
    _event(camera, 3, visit_id=visit)
    _event(camera, 4, visit_id=visit)
    try:
        assert db.preview_event_deletion(camera=camera, max_duration_seconds=10)["visits"] == 1
        result = db.delete_events(camera=camera, max_duration_seconds=10)
        assert result["events"] == 2
        assert result["visits"] == 1
        # Without the sweep this visit would survive as an empty shell -- still listed on the
        # Visits tab, with no events behind it.
        assert not _visit_exists(visit)
    finally:
        _cleanup(camera, [visit])


def test_visit_keeping_at_least_one_event_survives(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    visit = _visit(camera)
    short = _event(camera, 2, visit_id=visit)
    kept = _event(camera, 300, visit_id=visit)
    try:
        assert db.preview_event_deletion(camera=camera, max_duration_seconds=10)["visits"] == 0
        result = db.delete_events(camera=camera, max_duration_seconds=10)
        assert result["events"] == 1 and result["visits"] == 0
        assert not _event_exists(short)
        assert _event_exists(kept)
        assert _visit_exists(visit), "a visit that still has events must never be swept"
    finally:
        _cleanup(camera, [visit])


def test_only_the_emptied_visit_is_swept_when_several_are_touched(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    emptied, kept_visit = _visit(camera), _visit(camera)
    _event(camera, 3, visit_id=emptied)
    _event(camera, 4, visit_id=kept_visit)
    survivor = _event(camera, 400, visit_id=kept_visit)
    try:
        result = db.delete_events(camera=camera, max_duration_seconds=10)
        assert result["events"] == 2 and result["visits"] == 1
        assert not _visit_exists(emptied)
        assert _visit_exists(kept_visit)
        assert _event_exists(survivor)
    finally:
        _cleanup(camera, [emptied, kept_visit])


def test_events_with_no_visit_at_all_delete_cleanly(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    unlinked = _event(camera, 3)
    try:
        result = db.delete_events(camera=camera, max_duration_seconds=10)
        assert result["events"] == 1 and result["visits"] == 0
        assert not _event_exists(unlinked)
    finally:
        _cleanup(camera)


# ---- dependent rows and files ----

def test_sightings_go_with_their_events(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    event_id = _event(camera, 3)
    try:
        assert db.preview_event_deletion(camera=camera, max_duration_seconds=10)["sightings"] == 1
        db.delete_events(camera=camera, max_duration_seconds=10)
        left = db._execute(
            "SELECT count(*)::int AS c FROM yard_stats.sightings WHERE raw_event_id = %s",
            (event_id,), fetch=True,
        )[0]["c"]
        # sightings.raw_event_id has no ON DELETE CASCADE -- leaving these behind would both
        # violate the FK and strand rows pointing at nothing.
        assert left == 0
    finally:
        _cleanup(camera)


def test_stored_media_files_are_deleted_from_disk(conn_ok, tmp_path):
    camera = f"pytest-del-{uuid.uuid4()}"
    clip = tmp_path / "clip.mp4"
    still = tmp_path / "still.jpg"
    clip.write_bytes(b"video")
    still.write_bytes(b"image")
    _event(camera, 3, video_path=str(clip), image_path=str(still))
    try:
        preview = db.preview_event_deletion(camera=camera, max_duration_seconds=10)
        assert preview["video_files"] == 1 and preview["image_files"] == 1
        result = db.delete_events(camera=camera, max_duration_seconds=10)
        assert result["files_deleted"] == 2
        assert not clip.exists() and not still.exists()
    finally:
        _cleanup(camera)


def test_a_missing_file_on_disk_is_not_an_error(conn_ok, tmp_path):
    # A path from before storage was reconfigured, or a file already removed by hand.
    camera = f"pytest-del-{uuid.uuid4()}"
    _event(camera, 3, video_path=str(tmp_path / "never-existed.mp4"))
    try:
        result = db.delete_events(camera=camera, max_duration_seconds=10)
        assert result["events"] == 1
        assert result["files_deleted"] == 0
    finally:
        _cleanup(camera)


# ---- preview shape ----

def test_preview_deletes_nothing(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    event_id = _event(camera, 3)
    try:
        db.preview_event_deletion(camera=camera, max_duration_seconds=10)
        assert _event_exists(event_id), "a preview must never delete anything"
    finally:
        _cleanup(camera)


def test_preview_sample_carries_what_the_grid_renders(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    _event(camera, 3)
    try:
        row = db.preview_event_deletion(camera=camera, max_duration_seconds=10)["sample"][0]
        # The admin preview grid renders a thumbnail, label/camera, time + duration, and the AI
        # description -- deciding "is this a false alarm?" needs all of them.
        for field in ("id", "camera", "objects", "start_ts", "duration_seconds",
                      "has_image", "has_video", "description", "visit_id"):
            assert field in row, f"preview sample missing {field}"
        assert row["duration_seconds"] == pytest.approx(3, abs=0.5)
    finally:
        _cleanup(camera)


def test_preview_marks_a_truncated_sample(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    for _ in range(4):
        _event(camera, 3)
    try:
        preview = db.preview_event_deletion(camera=camera, max_duration_seconds=10, limit=2)
        assert preview["events"] == 4
        assert len(preview["sample"]) == 2
        # Without this the operator would think they had seen everything that matched.
        assert preview["sample_truncated"] is True
    finally:
        _cleanup(camera)


# ---- advanced filters ----

def test_q_filter_matches_the_ai_description(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    keep = _event(camera, 3)
    db._execute("UPDATE yard_stats.sightings SET description = %s WHERE raw_event_id = %s",
                ("a red delivery van", keep))
    match = _event(camera, 3)  # description defaults to "a parked car"
    try:
        preview = db.preview_event_deletion(camera=camera, q="parked car")
        assert preview["events"] == 1
        assert [r["id"] for r in preview["sample"]] == [match]
    finally:
        _cleanup(camera)


def test_q_filter_deletes_the_events_not_just_their_sightings(conn_ok):
    # Regression: the q filter matches THROUGH the sightings table, and sightings are deleted
    # before their events. Re-evaluating the filter for the raw_events DELETE therefore matched
    # nothing by that point -- confirmed against a real database as 7 sightings deleted and 0
    # events, leaving the events behind with their analysis stripped. delete_events resolves the
    # selection to concrete ids up front so no later statement can invalidate it.
    camera = f"pytest-del-{uuid.uuid4()}"
    ids = [_event(camera, 3) for _ in range(3)]
    try:
        result = db.delete_events(camera=camera, q="parked car")
        assert result["events"] == 3, "events must be deleted, not just their sightings"
        assert result["sightings"] == 3
        for event_id in ids:
            assert not _event_exists(event_id)
    finally:
        _cleanup(camera)


def test_ai_status_filter(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    done = _event(camera, 3)
    failed = _event(camera, 3)
    db._execute("UPDATE yard_stats.raw_events SET ai_status = 'failed' WHERE id = %s", (failed,))
    try:
        preview = db.preview_event_deletion(camera=camera, ai_status="failed")
        assert [r["id"] for r in preview["sample"]] == [failed]
        assert done not in [r["id"] for r in preview["sample"]]
    finally:
        _cleanup(camera)


# ---- pagination ----

def test_pages_do_not_overlap_and_cover_everything(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    created = {_event(camera, 3) for _ in range(7)}
    try:
        seen, offset = set(), 0
        while True:
            page = db.preview_event_deletion(camera=camera, max_duration_seconds=10, limit=3, offset=offset)
            assert page["events"] == 7, "the total is the full match, not the page size"
            ids = [r["id"] for r in page["sample"]]
            assert not (seen & set(ids)), "pages must not repeat a row"
            seen.update(ids)
            if not page["sample_truncated"]:
                break
            offset += 3
            assert offset < 30, "paging failed to terminate"
        assert seen == created, "paging must cover every matching row exactly once"
    finally:
        _cleanup(camera)


def test_last_page_is_not_marked_truncated(conn_ok):
    camera = f"pytest-del-{uuid.uuid4()}"
    for _ in range(4):
        _event(camera, 3)
    try:
        assert db.preview_event_deletion(camera=camera, max_duration_seconds=10, limit=3, offset=0)["sample_truncated"] is True
        last = db.preview_event_deletion(camera=camera, max_duration_seconds=10, limit=3, offset=3)
        assert len(last["sample"]) == 1
        # Off-by-one here would leave the operator paging forever past the end.
        assert last["sample_truncated"] is False
    finally:
        _cleanup(camera)


def test_ordering_is_stable_across_pages(conn_ok):
    # Flicker re-detections of one parked car can share a start_ts to the microsecond, so ordering
    # by start_ts alone would let rows swap between pages and be seen twice or never.
    camera = f"pytest-del-{uuid.uuid4()}"
    db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64)
        SELECT %s, 'z', 'car', now(), now() + interval '3 seconds',
               'pytest-tie-' || g, true, true, 'done', 'done', 'ZmFrZQ=='
        FROM generate_series(1, 6) g
        """,
        (camera,),
    )
    try:
        first = [r["id"] for r in db.preview_event_deletion(camera=camera, limit=3, offset=0)["sample"]]
        second = [r["id"] for r in db.preview_event_deletion(camera=camera, limit=3, offset=3)["sample"]]
        assert not set(first) & set(second)
        assert len(set(first) | set(second)) == 6
    finally:
        _cleanup(camera)
