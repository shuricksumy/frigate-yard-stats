import logging
import os
from datetime import datetime

import psycopg2
import psycopg2.extras

import config

logger = logging.getLogger(__name__)

_conn = None


def get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(
            host=config.POSTGRES_HOST,
            port=config.POSTGRES_PORT,
            dbname=config.POSTGRES_DB,
            user=config.POSTGRES_USER,
            password=config.POSTGRES_PASSWORD,
        )
        _conn.autocommit = True
    return _conn


def _execute(query, params=None, fetch=False):
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        if fetch:
            return cur.fetchall()
    return []


def ensure_schema() -> None:
    # schema.sql lives alongside this file and is baked into the image by the Dockerfile's
    # `COPY . .` -- runs on every startup. Every statement in it is CREATE ... IF NOT EXISTS, so
    # this is safe to re-run against an already-initialized database -- a brand new instance just
    # needs `docker compose up`, no manual `psql -f schema.sql` step.
    with open(config.SCHEMA_SQL_PATH) as f:
        schema_sql = f.read()
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(schema_sql)
    logger.info("Schema ensured from %s", config.SCHEMA_SQL_PATH)


def get_status_breakdown() -> list:
    return _execute(
        """
        SELECT objects, crop_status, ai_status, count(*) AS count
        FROM yard_stats.raw_events
        GROUP BY objects, crop_status, ai_status
        ORDER BY objects, crop_status, ai_status
        """,
        fetch=True,
    )


def get_raw_event(event_id: int) -> dict | None:
    rows = _execute(
        "SELECT *, (video_path IS NOT NULL) AS has_video FROM yard_stats.raw_events WHERE id = %s",
        (event_id,), fetch=True,
    )
    return rows[0] if rows else None


def insert_raw_event(event: dict) -> None:
    # video_status starts 'skipped' (not 'new') when STORE_VIDEO is off -- a cheap flag set once
    # at ingest, so the video queue's WHERE video_status IN ('new','retry') never even considers
    # these rows, rather than special-casing a disabled feature inside the poll loop.
    initial_video_status = "new" if config.STORE_VIDEO else "skipped"
    _execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot, video_status)
        VALUES (%s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s, %s, %s, %s)
        """,
        (
            event["camera"], event["zone"], event["objects"],
            event["start_time"], event["end_time"], event["det_id"],
            event["has_clip"], event["has_snapshot"], initial_video_status,
        ),
    )


def reap_stale_processing() -> None:
    # Mirrors the n8n processors' "Reap Stale Processing Items" node, scoped to crop_status
    # instead of the (now n8n-only) ai_status.
    _execute(
        """
        UPDATE yard_stats.raw_events
        SET crop_status = 'retry', crop_status_changed_at = now()
        WHERE crop_status = 'processing'
          AND crop_status_changed_at < now() - (%s * interval '1 minute')
        """,
        (config.STALE_MINUTES,),
    )


def count_in_progress() -> int:
    rows = _execute(
        "SELECT count(*)::int AS in_progress_count FROM yard_stats.raw_events WHERE crop_status = 'processing'",
        fetch=True,
    )
    return rows[0]["in_progress_count"] if rows else 0


def claim_next_batch(limit: int) -> list:
    # FOR UPDATE SKIP LOCKED -- same atomic multi-row claim pattern as n8n's "Claim Next Batch"
    # node, just running in one process instead of possibly-overlapping n8n executions. Ingests
    # every Frigate label (car, truck, person, dog, ...), not just car/person.
    return _execute(
        """
        UPDATE yard_stats.raw_events
        SET crop_status = 'processing', crop_status_changed_at = now()
        WHERE id IN (
            SELECT id FROM yard_stats.raw_events
            WHERE has_snapshot = true AND crop_status IN ('new', 'retry')
            ORDER BY created_at ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        (limit,),
        fetch=True,
    )


def mark_crop_done(event_id: int, crop_image_base64: str, sub_label: str | None, score: float | None) -> None:
    _execute(
        """
        UPDATE yard_stats.raw_events
        SET crop_status = 'done', crop_status_changed_at = now(),
            crop_image_base64 = %s, sub_label = %s, score = %s
        WHERE id = %s
        """,
        (crop_image_base64, sub_label, score, event_id),
    )


def mark_crop_failed(event_id: int) -> None:
    # Same retry-or-fail-with-cap logic as n8n's "Handle Failure (Retry or Fail)" node.
    _execute(
        """
        UPDATE yard_stats.raw_events
        SET crop_attempt_count = crop_attempt_count + 1,
            crop_status = CASE WHEN crop_attempt_count + 1 >= %s THEN 'failed' ELSE 'retry' END,
            crop_status_changed_at = now()
        WHERE id = %s
        """,
        (config.MAX_ATTEMPTS, event_id),
    )


def set_telegram_photo_message_id(event_id: int, message_id: int) -> None:
    _execute(
        "UPDATE yard_stats.raw_events SET telegram_photo_message_id = %s WHERE id = %s",
        (message_id, event_id),
    )


def reap_stale_video_processing() -> None:
    _execute(
        """
        UPDATE yard_stats.raw_events
        SET video_status = 'retry', video_status_changed_at = now()
        WHERE video_status = 'processing'
          AND video_status_changed_at < now() - (%s * interval '1 minute')
        """,
        (config.STALE_MINUTES,),
    )


def count_video_in_progress() -> int:
    rows = _execute(
        "SELECT count(*)::int AS in_progress_count FROM yard_stats.raw_events WHERE video_status = 'processing'",
        fetch=True,
    )
    return rows[0]["in_progress_count"] if rows else 0


def claim_video_batch(limit: int) -> list:
    # Only claims rows the crop stage has already finished with -- video download uses the same
    # camera/start/end window regardless of crop_status, but there's no reason to spend download
    # bandwidth on a row that might still fail crop-stage validation upstream, and this keeps the
    # video stage a strict downstream consumer of the crop stage, same relationship the AI stage
    # already has with crop_status='done'.
    return _execute(
        """
        UPDATE yard_stats.raw_events
        SET video_status = 'processing', video_status_changed_at = now()
        WHERE id IN (
            SELECT id FROM yard_stats.raw_events
            WHERE crop_status = 'done' AND video_status IN ('new', 'retry')
            ORDER BY created_at ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        (limit,),
        fetch=True,
    )


def mark_video_done(event_id: int, video_path: str) -> None:
    _execute(
        """
        UPDATE yard_stats.raw_events
        SET video_status = 'done', video_status_changed_at = now(), video_path = %s
        WHERE id = %s
        """,
        (video_path, event_id),
    )


def mark_video_retry_or_failed(event_id: int, max_attempts: int) -> None:
    # Same retry-or-fail-with-cap CASE logic as mark_crop_failed/fail_ai_event.
    _execute(
        """
        UPDATE yard_stats.raw_events
        SET video_attempt_count = video_attempt_count + 1,
            video_status = CASE WHEN video_attempt_count + 1 >= %s THEN 'failed' ELSE 'retry' END,
            video_status_changed_at = now()
        WHERE id = %s
        """,
        (max_attempts, event_id),
    )


def _delete_video_files(paths: list[str]) -> int:
    # Filesystem side-effect, deliberately run outside any DB transaction -- a delete here can't
    # be rolled back, so it happens after the caller already knows which rows/paths matched, not
    # nested inside the DELETE statements themselves. Missing files (already deleted, or a path
    # from before VIDEO_STORAGE_PATH was ever configured) are not treated as errors.
    deleted = 0
    for path in paths:
        if not path:
            continue
        try:
            os.remove(path)
            deleted += 1
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Failed to delete video file %s during retention cleanup", path, exc_info=True)
    return deleted


def run_retention_cleanup(retention_months: int) -> None:
    # Same FK-safe child-before-parent delete order as the (now superseded) n8n
    # "retention-cleanup.json" workflow -- video files are collected and removed first (their
    # paths only exist on the raw_events rows about to be deleted), then the usual DB sweep runs.
    video_paths = [
        row["video_path"] for row in _execute(
            """
            SELECT video_path FROM yard_stats.raw_events
            WHERE start_ts < now() - (%s || ' months')::interval AND video_path IS NOT NULL
            """,
            (retention_months,), fetch=True,
        )
    ]
    deleted_files = _delete_video_files(video_paths)

    _execute(
        """
        DELETE FROM yard_stats.vehicle_sightings WHERE raw_event_id IN (
            SELECT id FROM yard_stats.raw_events WHERE start_ts < now() - (%s || ' months')::interval
        )
        """,
        (retention_months,),
    )
    _execute(
        """
        DELETE FROM yard_stats.person_sightings WHERE raw_event_id IN (
            SELECT id FROM yard_stats.raw_events WHERE start_ts < now() - (%s || ' months')::interval
        )
        """,
        (retention_months,),
    )
    _execute(
        "DELETE FROM yard_stats.visits WHERE start_ts < now() - (%s || ' months')::interval",
        (retention_months,),
    )
    _execute(
        "DELETE FROM yard_stats.raw_events WHERE start_ts < now() - (%s || ' months')::interval",
        (retention_months,),
    )
    logger.info(
        "Retention cleanup applied (retention_months=%s, video_files_deleted=%s)",
        retention_months, deleted_files,
    )


def purge_older_than(cutoff: datetime, execute: bool) -> dict:
    # Ad-hoc counterpart to run_retention_cleanup above -- same FK-safe child-before-parent
    # delete order, but keyed on a caller-supplied cutoff timestamp instead of the fixed
    # config.RETENTION_MONTHS, and always counts first so a dry run (execute=False) and a real
    # run report the identical shape of result.
    counts = {
        "vehicle_sightings": _execute(
            """
            SELECT count(*)::int AS c FROM yard_stats.vehicle_sightings vs
            JOIN yard_stats.raw_events re ON re.id = vs.raw_event_id
            WHERE re.start_ts < %s
            """,
            (cutoff,), fetch=True,
        )[0]["c"],
        "person_sightings": _execute(
            """
            SELECT count(*)::int AS c FROM yard_stats.person_sightings ps
            JOIN yard_stats.raw_events re ON re.id = ps.raw_event_id
            WHERE re.start_ts < %s
            """,
            (cutoff,), fetch=True,
        )[0]["c"],
        "visits": _execute(
            "SELECT count(*)::int AS c FROM yard_stats.visits WHERE start_ts < %s",
            (cutoff,), fetch=True,
        )[0]["c"],
        "raw_events": _execute(
            "SELECT count(*)::int AS c FROM yard_stats.raw_events WHERE start_ts < %s",
            (cutoff,), fetch=True,
        )[0]["c"],
    }

    video_paths = [
        row["video_path"] for row in _execute(
            "SELECT video_path FROM yard_stats.raw_events WHERE start_ts < %s AND video_path IS NOT NULL",
            (cutoff,), fetch=True,
        )
    ]
    counts["video_files"] = len(video_paths)

    if execute:
        deleted_files = _delete_video_files(video_paths)
        _execute(
            """
            DELETE FROM yard_stats.vehicle_sightings WHERE raw_event_id IN (
                SELECT id FROM yard_stats.raw_events WHERE start_ts < %s
            )
            """,
            (cutoff,),
        )
        _execute(
            """
            DELETE FROM yard_stats.person_sightings WHERE raw_event_id IN (
                SELECT id FROM yard_stats.raw_events WHERE start_ts < %s
            )
            """,
            (cutoff,),
        )
        _execute("DELETE FROM yard_stats.visits WHERE start_ts < %s", (cutoff,))
        _execute("DELETE FROM yard_stats.raw_events WHERE start_ts < %s", (cutoff,))
        logger.info("Ad-hoc purge executed (cutoff=%s, counts=%s, video_files_deleted=%s)", cutoff, counts, deleted_files)

    return counts


def list_events(
    object_type: str | None = None,
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    crop_status: str | None = None,
    ai_status: str | None = None,
    video_status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list:
    clauses = []
    params: list = []
    if object_type:
        # Comma-separated ("car,truck") or a single value -- "all"/omitted means no filter.
        # `objects` is a free-text label (see mqtt_ingest.parse_payload: Frigate's single
        # `after.label` per row today, not an actual joined multi-label list), so this matches
        # on exact equality against any of the requested types rather than an array/substring op.
        types = [t.strip() for t in object_type.split(",") if t.strip() and t.strip().lower() != "all"]
        if types:
            clauses.append("objects = ANY(%s)")
            params.append(types)
    if camera:
        clauses.append("camera = %s")
        params.append(camera)
    if start:
        clauses.append("start_ts >= %s")
        params.append(start)
    if end:
        clauses.append("start_ts <= %s")
        params.append(end)
    if crop_status:
        clauses.append("crop_status = %s")
        params.append(crop_status)
    if ai_status:
        clauses.append("ai_status = %s")
        params.append(ai_status)
    if video_status:
        clauses.append("video_status = %s")
        params.append(video_status)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    return _execute(
        f"""
        SELECT id, camera, zone, objects, start_ts, end_ts, crop_status, ai_status, video_status,
               sub_label, score, (video_path IS NOT NULL) AS has_video
        FROM yard_stats.raw_events
        {where}
        ORDER BY start_ts DESC
        LIMIT %s OFFSET %s
        """,
        params,
        fetch=True,
    )


def get_vehicle_sightings(
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    plate_text: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list:
    clauses = []
    params: list = []
    if camera:
        clauses.append("re.camera = %s")
        params.append(camera)
    if start:
        clauses.append("re.start_ts >= %s")
        params.append(start)
    if end:
        clauses.append("re.start_ts <= %s")
        params.append(end)
    if plate_text:
        clauses.append("(vs.plate_text_llm ILIKE %s OR vs.plate_text_frigate ILIKE %s)")
        params.extend([f"%{plate_text}%", f"%{plate_text}%"])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    return _execute(
        f"""
        SELECT vs.id, vs.raw_event_id, re.camera, re.zone, re.start_ts,
               vs.color, vs.body_type, vs.make_guess, vs.make_confidence,
               vs.model_guess, vs.model_confidence, vs.notable_features,
               vs.plate_text_llm, vs.plate_text_frigate, vs.plate_confidence, vs.notes
        FROM yard_stats.vehicle_sightings vs
        JOIN yard_stats.raw_events re ON re.id = vs.raw_event_id
        {where}
        ORDER BY re.start_ts DESC
        LIMIT %s OFFSET %s
        """,
        params,
        fetch=True,
    )


def get_person_sightings(
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list:
    clauses = []
    params: list = []
    if camera:
        clauses.append("re.camera = %s")
        params.append(camera)
    if start:
        clauses.append("re.start_ts >= %s")
        params.append(start)
    if end:
        clauses.append("re.start_ts <= %s")
        params.append(end)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    return _execute(
        f"""
        SELECT ps.id, ps.raw_event_id, re.camera, re.zone, re.start_ts, ps.description, ps.notes
        FROM yard_stats.person_sightings ps
        JOIN yard_stats.raw_events re ON re.id = ps.raw_event_id
        {where}
        ORDER BY re.start_ts DESC
        LIMIT %s OFFSET %s
        """,
        params,
        fetch=True,
    )


def get_stats_summary(start: datetime, end: datetime) -> dict:
    total_events = _execute(
        "SELECT count(*)::int AS c FROM yard_stats.raw_events WHERE start_ts >= %s AND start_ts <= %s",
        (start, end), fetch=True,
    )[0]["c"]
    total_vehicle_sightings = _execute(
        """
        SELECT count(*)::int AS c FROM yard_stats.vehicle_sightings vs
        JOIN yard_stats.raw_events re ON re.id = vs.raw_event_id
        WHERE re.start_ts >= %s AND re.start_ts <= %s
        """,
        (start, end), fetch=True,
    )[0]["c"]
    total_person_sightings = _execute(
        """
        SELECT count(*)::int AS c FROM yard_stats.person_sightings ps
        JOIN yard_stats.raw_events re ON re.id = ps.raw_event_id
        WHERE re.start_ts >= %s AND re.start_ts <= %s
        """,
        (start, end), fetch=True,
    )[0]["c"]
    by_camera = _execute(
        """
        SELECT camera, count(*)::int AS count FROM yard_stats.raw_events
        WHERE start_ts >= %s AND start_ts <= %s
        GROUP BY camera ORDER BY count DESC
        """,
        (start, end), fetch=True,
    )
    by_object_type = _execute(
        """
        SELECT objects, count(*)::int AS count FROM yard_stats.raw_events
        WHERE start_ts >= %s AND start_ts <= %s
        GROUP BY objects ORDER BY count DESC
        """,
        (start, end), fetch=True,
    )
    by_day = _execute(
        """
        SELECT to_char(date_trunc('day', start_ts), 'YYYY-MM-DD') AS day, count(*)::int AS count
        FROM yard_stats.raw_events
        WHERE start_ts >= %s AND start_ts <= %s
        GROUP BY 1 ORDER BY 1
        """,
        (start, end), fetch=True,
    )
    return {
        "start": start,
        "end": end,
        "total_events": total_events,
        "total_vehicle_sightings": total_vehicle_sightings,
        "total_person_sightings": total_person_sightings,
        "by_camera": by_camera,
        "by_object_type": by_object_type,
        "by_day": by_day,
    }


def claim_ai_batch(
    object_types: list[str],
    parallel_limit: int,
    stale_minutes: int,
    max_age_hours: float | None = None,
) -> list:
    # Replaces what used to be four separate n8n nodes (Reap Stale Processing Items, Count
    # In-Progress Items, Check Capacity, Claim Next Batch) with one call. Same FOR UPDATE SKIP
    # LOCKED race-safety as every other claim in this project.
    _execute(
        """
        UPDATE yard_stats.raw_events
        SET ai_status = 'retry', ai_status_changed_at = now()
        WHERE ai_status = 'processing'
          AND ai_status_changed_at < now() - (%s * interval '1 minute')
        """,
        (stale_minutes,),
    )
    in_progress = _execute(
        "SELECT count(*)::int AS c FROM yard_stats.raw_events WHERE ai_status = 'processing'",
        fetch=True,
    )[0]["c"]
    available_capacity = max(0, parallel_limit - in_progress)
    if available_capacity == 0:
        return []

    # ORDER BY created_at DESC (not ASC) -- newest-eligible-first, one shared queue across every
    # requested object_type (no separate car/person ordering). This is a deliberate priority
    # inversion from a plain FIFO queue: when there are more eligible rows than capacity, the
    # most recent ones win and older rows keep waiting; only once the backlog drops below
    # available_capacity do older rows get swept up too (the LIMIT stops cutting them off). Bursty
    # incoming traffic (e.g. a bunch of car events) then naturally deprioritizes stale backlog
    # instead of processing strictly in arrival order.
    age_clause = ""
    params: list = [object_types]
    if max_age_hours is not None:
        age_clause = "AND created_at >= now() - (%s * interval '1 hour')"
        params.append(max_age_hours)
    params.append(available_capacity)
    return _execute(
        f"""
        UPDATE yard_stats.raw_events
        SET ai_status = 'processing', ai_status_changed_at = now()
        WHERE id IN (
            SELECT id FROM yard_stats.raw_events
            WHERE objects = ANY(%s) AND crop_status = 'done' AND ai_status IN ('new', 'retry')
            {age_clause}
            ORDER BY created_at DESC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        params,
        fetch=True,
    )


def complete_vehicle_sighting(
    raw_event_id: int,
    color: str | None,
    body_type: str | None,
    make_guess: str | None,
    make_confidence: str | None,
    model_guess: str | None,
    model_confidence: str | None,
    notable_features: str | None,
    plate_text_llm: str | None,
    plate_text_frigate: str | None,
    plate_confidence: str | None,
    notes: str | None,
) -> int:
    # Insert + mark ai_status='done' in one transaction -- replaces the old Insert Vehicle
    # Sighting + Mark Done pair of n8n Postgres nodes, closing the gap where a crash between the
    # two left the row stuck 'processing' until the next reap.
    conn = get_conn()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO yard_stats.vehicle_sightings
                    (raw_event_id, color, body_type, make_guess, make_confidence,
                     model_guess, model_confidence, notable_features,
                     plate_text_llm, plate_text_frigate, plate_confidence, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (raw_event_id, color, body_type, make_guess, make_confidence,
                 model_guess, model_confidence, notable_features,
                 plate_text_llm, plate_text_frigate, plate_confidence, notes),
            )
            sighting_id = cur.fetchone()["id"]
            cur.execute(
                "UPDATE yard_stats.raw_events SET ai_status = 'done', ai_status_changed_at = now() WHERE id = %s",
                (raw_event_id,),
            )
        conn.commit()
        return sighting_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def complete_person_sighting(raw_event_id: int, description: str | None, notes: str | None) -> int:
    conn = get_conn()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO yard_stats.person_sightings (raw_event_id, description, notes)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (raw_event_id, description, notes),
            )
            sighting_id = cur.fetchone()["id"]
            cur.execute(
                "UPDATE yard_stats.raw_events SET ai_status = 'done', ai_status_changed_at = now() WHERE id = %s",
                (raw_event_id,),
            )
        conn.commit()
        return sighting_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def fail_ai_event(event_id: int, max_attempts: int) -> dict:
    # Same retry-or-fail-with-cap CASE logic as n8n's old "Handle Failure (Retry or Fail)" node.
    rows = _execute(
        """
        UPDATE yard_stats.raw_events
        SET ai_attempt_count = ai_attempt_count + 1,
            ai_status = CASE WHEN ai_attempt_count + 1 >= %s THEN 'failed' ELSE 'retry' END,
            ai_status_changed_at = now()
        WHERE id = %s
        RETURNING ai_status, ai_attempt_count
        """,
        (max_attempts, event_id),
        fetch=True,
    )
    return rows[0]


def get_report_data(start: datetime, end: datetime) -> dict:
    # Same joins daily-report.json's two query nodes used to run directly -- filtered by
    # created_at (when the AI stage produced the sighting), not start_ts, matching that behavior.
    vehicles = _execute(
        """
        SELECT re.camera, re.zone, re.start_ts, re.crop_image_base64,
               vs.color, vs.body_type, vs.make_guess, vs.make_confidence,
               vs.model_guess, vs.model_confidence, vs.notable_features,
               vs.plate_text_llm, vs.plate_text_frigate, vs.notes
        FROM yard_stats.vehicle_sightings vs
        JOIN yard_stats.raw_events re ON re.id = vs.raw_event_id
        WHERE vs.created_at >= %s AND vs.created_at <= %s
        ORDER BY re.start_ts ASC
        """,
        (start, end), fetch=True,
    )
    persons = _execute(
        """
        SELECT re.camera, re.zone, re.start_ts, re.crop_image_base64, ps.description, ps.notes
        FROM yard_stats.person_sightings ps
        JOIN yard_stats.raw_events re ON re.id = ps.raw_event_id
        WHERE ps.created_at >= %s AND ps.created_at <= %s
        ORDER BY re.start_ts ASC
        """,
        (start, end), fetch=True,
    )
    return {"vehicles": vehicles, "persons": persons}
