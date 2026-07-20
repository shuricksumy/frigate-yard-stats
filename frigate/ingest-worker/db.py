import logging
import os
from datetime import datetime

import psycopg2
import psycopg2.extras

import config

logger = logging.getLogger(__name__)

_conn = None


def _vector_literal(embedding: list[float] | None) -> str | None:
    # Formats a Python list as a pgvector input literal ("[0.1,0.2,...]") passed through psycopg2
    # as a plain string param and cast with ::vector in SQL -- avoids depending on the separate
    # `pgvector` package's connection-level type adapter (which itself needs the extension already
    # created before it can register, an ordering hazard not worth taking on for a write-mostly,
    # read-never-as-a-list column).
    if embedding is None:
        return None
    if len(embedding) != config.EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"embedding must have {config.EMBEDDING_DIMENSIONS} dimensions, got {len(embedding)}"
        )
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


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


def _current_embedding_dimension(table: str) -> int | None:
    # pgvector stores a vector(N) column's dimension directly as the column's typmod (unlike e.g.
    # varchar, which offsets it) -- -1 means an unconstrained `vector` with no declared dimension,
    # which schema.sql never creates, but is treated as "needs fixing" below rather than assumed.
    rows = _execute(
        f"""
        SELECT atttypmod FROM pg_attribute
        WHERE attrelid = 'yard_stats.{table}'::regclass
          AND attname = 'embedding' AND NOT attisdropped
        """,
        fetch=True,
    )
    if not rows or rows[0]["atttypmod"] <= 0:
        return None
    return rows[0]["atttypmod"]


def _ensure_embedding_dimension() -> None:
    # schema.sql's ADD COLUMN IF NOT EXISTS only sizes a brand-new column correctly -- it's a
    # no-op against a column that already exists at a different dimension (e.g. after switching
    # EMBEDDING_DIMENSIONS to a new embedding model). Widening is deliberately NOT folded into
    # schema.sql as an unconditional ALTER COLUMN TYPE: that statement must clear the column's data
    # (a different model's vectors are an incomparable vector space, so old values can't be kept
    # regardless), which is only safe to run when the dimension is actually changing -- running it
    # unconditionally on every startup would silently wipe every embedding on every restart, even
    # when nothing changed.
    for table in ("vehicle_sightings", "person_sightings"):
        current = _current_embedding_dimension(table)
        if current is not None and current != config.EMBEDDING_DIMENSIONS:
            logger.warning(
                "yard_stats.%s.embedding is vector(%d), EMBEDDING_DIMENSIONS is now %d -- "
                "widening the column and clearing existing embeddings (re-run "
                "POST /embeddings/backfill?confirm=true afterwards)",
                table,
                current,
                config.EMBEDDING_DIMENSIONS,
            )
            _execute(
                f"ALTER TABLE yard_stats.{table} ALTER COLUMN embedding "
                f"TYPE vector({config.EMBEDDING_DIMENSIONS}) USING NULL"
            )


def ensure_schema() -> None:
    # schema.sql lives alongside this file and is baked into the image by the Dockerfile's
    # `COPY . .` -- runs on every startup. Every statement in it is CREATE ... IF NOT EXISTS, so
    # this is safe to re-run against an already-initialized database -- a brand new instance just
    # needs `docker compose up`, no manual `psql -f schema.sql` step.
    with open(config.SCHEMA_SQL_PATH) as f:
        schema_sql = f.read()
    # The embedding columns' dimension is a single template placeholder rather than a literal, so
    # switching EMBEDDING_DIMENSIONS (config.py) is a one-line .env change instead of also editing
    # this file -- only affects a brand-new column, see _ensure_embedding_dimension() for widening
    # an existing one.
    schema_sql = schema_sql.replace("__EMBEDDING_DIMENSIONS__", str(config.EMBEDDING_DIMENSIONS))
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(schema_sql)
    _ensure_embedding_dimension()
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


def get_retention_info() -> dict:
    # Lets a caller (the Q&A agent, via /status) tell "nothing happened in that range" apart from
    # "that range was already purged" -- config.RETENTION_MONTHS alone doesn't say how much data
    # actually survives right now (the scheduled sweep runs on its own slow cadence, so the true
    # oldest row can be somewhat newer than the nominal cutoff).
    rows = _execute(
        "SELECT min(start_ts) AS oldest_start_ts FROM yard_stats.raw_events", fetch=True,
    )
    return {
        "retention_months": config.RETENTION_MONTHS,
        "oldest_available_start_ts": rows[0]["oldest_start_ts"] if rows else None,
    }


def count_sightings_missing_embedding() -> dict:
    # Dry-run counterpart for POST /embeddings/backfill, same shape as purge_older_than's own
    # always-count-first approach -- a sighting from before semantic search existed (or from any
    # run that didn't attach one) has embedding IS NULL, same condition
    # semantic_search_sightings already excludes on the read side.
    return {
        "vehicle_sightings": _execute(
            "SELECT count(*)::int AS c FROM yard_stats.vehicle_sightings WHERE embedding IS NULL",
            fetch=True,
        )[0]["c"],
        "person_sightings": _execute(
            "SELECT count(*)::int AS c FROM yard_stats.person_sightings WHERE embedding IS NULL",
            fetch=True,
        )[0]["c"],
    }


def get_vehicle_sightings_missing_embedding(limit: int) -> list[dict]:
    # Oldest first (plain id order) -- a backfill has no "freshness" concept the way live queue
    # claims do (see claim_ai_batch's newest-first comment), so working through the backlog in a
    # stable, predictable order is simplest; repeated calls make steady progress either way.
    return _execute(
        """
        SELECT id, raw_event_id, color, body_type, make_guess, model_guess, notable_features,
               plate_text_llm, plate_text_frigate
        FROM yard_stats.vehicle_sightings
        WHERE embedding IS NULL
        ORDER BY id
        LIMIT %s
        """,
        (limit,), fetch=True,
    )


def get_person_sightings_missing_embedding(limit: int) -> list[dict]:
    return _execute(
        """
        SELECT id, raw_event_id, description
        FROM yard_stats.person_sightings
        WHERE embedding IS NULL
        ORDER BY id
        LIMIT %s
        """,
        (limit,), fetch=True,
    )


def update_vehicle_sighting_embedding(sighting_id: int, embedding: list[float]) -> None:
    _execute(
        "UPDATE yard_stats.vehicle_sightings SET embedding = %s::vector WHERE id = %s",
        (_vector_literal(embedding), sighting_id),
    )


def update_person_sighting_embedding(sighting_id: int, embedding: list[float]) -> None:
    _execute(
        "UPDATE yard_stats.person_sightings SET embedding = %s::vector WHERE id = %s",
        (_vector_literal(embedding), sighting_id),
    )


def get_raw_event(event_id: int) -> dict | None:
    rows = _execute(
        """
        SELECT *, (video_path IS NOT NULL) AS has_video,
               (crop_image_base64 IS NOT NULL) AS has_image
        FROM yard_stats.raw_events WHERE id = %s
        """,
        (event_id,), fetch=True,
    )
    return rows[0] if rows else None


def get_vehicle_sighting_for_event(raw_event_id: int) -> dict | None:
    # For GET /events/{id} -- surfaces the AI analysis result (plate, color, description) in the
    # web UI's lightbox once ai_status='done'. At most one row ever exists per raw_event_id.
    rows = _execute(
        """
        SELECT vs.id, vs.raw_event_id, re.camera, re.zone, re.start_ts,
               vs.color, vs.body_type, vs.make_guess, vs.make_confidence,
               vs.model_guess, vs.model_confidence, vs.notable_features,
               vs.plate_text_llm, vs.plate_text_frigate, vs.plate_confidence, vs.notes
        FROM yard_stats.vehicle_sightings vs
        JOIN yard_stats.raw_events re ON re.id = vs.raw_event_id
        WHERE vs.raw_event_id = %s
        """,
        (raw_event_id,), fetch=True,
    )
    return rows[0] if rows else None


def get_person_sighting_for_event(raw_event_id: int) -> dict | None:
    rows = _execute(
        """
        SELECT ps.id, ps.raw_event_id, re.camera, re.zone, re.start_ts, ps.description, ps.notes
        FROM yard_stats.person_sightings ps
        JOIN yard_stats.raw_events re ON re.id = ps.raw_event_id
        WHERE ps.raw_event_id = %s
        """,
        (raw_event_id,), fetch=True,
    )
    return rows[0] if rows else None


def get_sightings_for_visit(visit_id: int) -> dict:
    # Every sighting linked to this visit, not just the representative event's -- claim_ai_batch's
    # only_visit_representative now partitions by (visit_id, objects) rather than visit_id alone
    # (see there for why), so a visit can have more than one analyzed event: one representative per
    # distinct object type (a car and a person in the same visit each get their own sighting), not
    # just one per visit. Used by the web UI's visit lightbox to show all of them together instead
    # of only the single representative event's AI result GET /events/{id} would return.
    vehicles = _execute(
        """
        SELECT vs.id, vs.raw_event_id, re.camera, re.zone, re.start_ts,
               vs.color, vs.body_type, vs.make_guess, vs.make_confidence,
               vs.model_guess, vs.model_confidence, vs.notable_features,
               vs.plate_text_llm, vs.plate_text_frigate, vs.plate_confidence, vs.notes
        FROM yard_stats.vehicle_sightings vs
        JOIN yard_stats.raw_events re ON re.id = vs.raw_event_id
        WHERE re.visit_id = %s
        ORDER BY re.start_ts ASC
        """,
        (visit_id,), fetch=True,
    )
    persons = _execute(
        """
        SELECT ps.id, ps.raw_event_id, re.camera, re.zone, re.start_ts, ps.description, ps.notes
        FROM yard_stats.person_sightings ps
        JOIN yard_stats.raw_events re ON re.id = ps.raw_event_id
        WHERE re.visit_id = %s
        ORDER BY re.start_ts ASC
        """,
        (visit_id,), fetch=True,
    )
    return {"vehicles": vehicles, "persons": persons}


def insert_raw_event(event: dict) -> None:
    # video_status starts 'skipped' (not 'new') when STORE_VIDEO is off -- a cheap flag set once
    # at ingest, so the video queue's WHERE video_status IN ('new','retry') never even considers
    # these rows, rather than special-casing a disabled feature inside the poll loop.
    initial_video_status = "new" if config.STORE_VIDEO else "skipped"
    # crop_status starts 'skipped' (not 'new') when has_snapshot is false -- Frigate can emit a
    # full "end" MQTT lifecycle for a tracked object it never actually saved a snapshot for (seen
    # in production: such det_ids 404 against Frigate's own /api/events/<id>), so cropping can
    # never succeed for these regardless of retries. Skipping at ingest keeps the row (accurate
    # yard-activity counts) without it piling up as an eternally-unprocessed 'new'.
    initial_crop_status = "new" if event["has_snapshot"] else "skipped"
    _execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, video_status)
        VALUES (%s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s, %s, %s, %s, %s)
        """,
        (
            event["camera"], event["zone"], event["objects"],
            event["start_time"], event["end_time"], event["det_id"],
            event["has_clip"], event["has_snapshot"], initial_crop_status, initial_video_status,
        ),
    )


def get_visit(visit_id: int) -> dict | None:
    # Mirrors get_raw_event -- used by GET /media/video/visit/{id} to serve a visit's own stored
    # clip (STORE_VIDEO_ALERTS), a completely separate video/storage location from any raw_event's.
    rows = _execute(
        "SELECT *, (video_path IS NOT NULL) AS has_video FROM yard_stats.visits WHERE id = %s",
        (visit_id,), fetch=True,
    )
    return rows[0] if rows else None


def visit_thumb_crop_will_be_attempted(review: dict) -> bool:
    # Shared by record_visit (to set the initial thumb_crop_status) and mqtt_ingest's
    # _handle_review_message (to decide whether the Telegram visit-summary send should fire
    # immediately or be deferred to visit_thumb_worker -- see there for why). Used to also require
    # review.get("thumb_time") is not None, back when crop.build_visit_preview's predecessor
    # (crop_visit_thumbnail) seeked to thumb_time specifically -- that approach was abandoned (see
    # CLAUDE.md) in favor of sampling frames proportionally across the visit's own clip duration,
    # which needs only start_ts/end_ts/cameras, not thumb_time at all. thumb_time is still stored
    # on the visit row (informational -- Frigate's own opinion of the best moment) but no longer
    # gates whether a preview can be built.
    return config.VISIT_THUMB_CROP_ENABLED


def record_visit(review: dict) -> int | None:
    # Populates the previously-unwired visits table / raw_events.visit_id+reconciled from
    # Frigate's own review/alert grouping (frigate/reviews MQTT topic) -- one review segment
    # already bundles the det_ids Frigate's tracker considers the same real-world activity
    # (occlusion/re-ID, label flicker e.g. car -> truck mid-track), so this reuses that grouping
    # instead of reimplementing a merge heuristic ourselves. Grouping is per-camera only --
    # confirmed live against production Frigate that a review's "camera" is a single value, never
    # a list -- so cameras/camera_count are set for just that one camera; a cross-camera merge on
    # top of this (same zone, overlapping time window, different camera) is a separate, not-yet-
    # built layer. Insert + link in one transaction, same pattern as complete_vehicle_sighting.
    # video_status starts 'skipped' (not 'new') when STORE_VIDEO_ALERTS is off -- same reasoning
    # as insert_raw_event's initial_video_status: a cheap flag set once at insert, so the visit
    # video queue's WHERE clause never even considers these rows while the feature is disabled.
    initial_video_status = "new" if config.STORE_VIDEO_ALERTS else "skipped"
    # Same reasoning for thumb_crop_status -- skipped whenever the feature itself is off
    # (VISIT_THUMB_CROP_ENABLED); no longer conditioned on thumb_time being present (see
    # visit_thumb_crop_will_be_attempted).
    initial_thumb_crop_status = "new" if visit_thumb_crop_will_be_attempted(review) else "skipped"
    conn = get_conn()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO yard_stats.visits
                    (zone, objects, start_ts, end_ts, cameras, camera_count, video_status,
                     thumb_time, thumb_crop_status)
                VALUES (%s, %s, to_timestamp(%s), to_timestamp(%s), %s, 1, %s, %s, %s)
                RETURNING id
                """,
                (review["zone"], review["objects"], review["start_time"], review["end_time"],
                 review["camera"], initial_video_status, review.get("thumb_time"),
                 initial_thumb_crop_status),
            )
            visit_id = cur.fetchone()["id"]
            if review["det_ids"]:
                cur.execute(
                    """
                    UPDATE yard_stats.raw_events
                    SET visit_id = %s, reconciled = true
                    WHERE det_id = ANY(%s)
                    """,
                    (visit_id, review["det_ids"]),
                )
        conn.commit()
        return visit_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def get_representative_event_for_visit(visit_id: int) -> dict | None:
    # The visit's earliest-linked raw_event (same "representative" definition as list_visits) --
    # used right after record_visit to grab a crop image for the Telegram visit-summary
    # notification, if one's already available (the crop stage may not have finished by the time
    # the review closes -- see telegram.send_visit_summary), and by visit_thumb_worker.py, which
    # additionally needs det_id to look up that event's region/box for the re-crop.
    rows = _execute(
        """
        SELECT id, camera, objects, crop_image_base64, det_id
        FROM yard_stats.raw_events
        WHERE visit_id = %s
        ORDER BY start_ts ASC, id ASC
        LIMIT 1
        """,
        (visit_id,), fetch=True,
    )
    return rows[0] if rows else None


def set_visit_telegram_photo_message_id(visit_id: int, message_id: int) -> None:
    _execute(
        "UPDATE yard_stats.visits SET telegram_photo_message_id = %s WHERE id = %s",
        (message_id, visit_id),
    )


def count_events_for_visit(visit_id: int) -> int:
    rows = _execute(
        "SELECT count(*)::int AS c FROM yard_stats.raw_events WHERE visit_id = %s",
        (visit_id,), fetch=True,
    )
    return rows[0]["c"] if rows else 0


def reap_stale_visit_video_processing() -> None:
    _execute(
        """
        UPDATE yard_stats.visits
        SET video_status = 'retry', video_status_changed_at = now()
        WHERE video_status = 'processing'
          AND video_status_changed_at < now() - (%s * interval '1 minute')
        """,
        (config.STALE_MINUTES,),
    )


def count_visit_video_in_progress() -> int:
    rows = _execute(
        "SELECT count(*)::int AS c FROM yard_stats.visits WHERE video_status = 'processing'",
        fetch=True,
    )
    return rows[0]["c"] if rows else 0


def claim_visit_video_batch(limit: int, max_age_hours: float | None = None) -> list:
    # Mirrors claim_video_batch exactly (CTE form for the same FOR UPDATE SKIP LOCKED reason, and
    # the same newest-first + max_age_hours safety valve), just against visits instead of
    # raw_events -- one clip per visit's whole start_ts->end_ts span, independent of any per-event
    # video. See alert_video_worker.py.
    age_clause = ""
    params: list = []
    if max_age_hours is not None:
        age_clause = "AND start_ts >= now() - (%s * interval '1 hour')"
        params.append(max_age_hours)
    params.append(limit)
    return _execute(
        f"""
        WITH claimable AS (
            SELECT id FROM yard_stats.visits
            WHERE video_status IN ('new', 'retry')
            {age_clause}
            ORDER BY start_ts DESC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE yard_stats.visits
        SET video_status = 'processing', video_status_changed_at = now()
        FROM claimable
        WHERE yard_stats.visits.id = claimable.id
        RETURNING yard_stats.visits.*
        """,
        params,
        fetch=True,
    )


def mark_visit_video_done(visit_id: int, video_path: str) -> None:
    _execute(
        """
        UPDATE yard_stats.visits
        SET video_status = 'done', video_status_changed_at = now(), video_path = %s
        WHERE id = %s
        """,
        (video_path, visit_id),
    )


def mark_visit_video_retry_or_failed(visit_id: int, max_attempts: int) -> None:
    _execute(
        """
        UPDATE yard_stats.visits
        SET video_attempt_count = video_attempt_count + 1,
            video_status = CASE WHEN video_attempt_count + 1 >= %s THEN 'failed' ELSE 'retry' END,
            video_status_changed_at = now()
        WHERE id = %s
        """,
        (max_attempts, visit_id),
    )


def reap_stale_visit_thumb_crop_processing() -> None:
    _execute(
        """
        UPDATE yard_stats.visits
        SET thumb_crop_status = 'retry', thumb_crop_status_changed_at = now()
        WHERE thumb_crop_status = 'processing'
          AND thumb_crop_status_changed_at < now() - (%s * interval '1 minute')
        """,
        (config.STALE_MINUTES,),
    )


def count_visit_thumb_crop_in_progress() -> int:
    rows = _execute(
        "SELECT count(*)::int AS c FROM yard_stats.visits WHERE thumb_crop_status = 'processing'",
        fetch=True,
    )
    return rows[0]["c"] if rows else 0


def claim_visit_thumb_crop_batch(limit: int) -> list:
    # Mirrors claim_visit_video_batch's CTE-claim shape (same FOR UPDATE SKIP LOCKED reason,
    # newest-first) -- fifth queue stage, on visits.thumb_crop_status. See visit_thumb_worker.py.
    return _execute(
        """
        WITH claimable AS (
            SELECT id FROM yard_stats.visits
            WHERE thumb_crop_status IN ('new', 'retry')
            ORDER BY start_ts DESC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE yard_stats.visits
        SET thumb_crop_status = 'processing', thumb_crop_status_changed_at = now()
        FROM claimable
        WHERE yard_stats.visits.id = claimable.id
        RETURNING yard_stats.visits.*
        """,
        (limit,),
        fetch=True,
    )


def mark_visit_thumb_crop_done(visit_id: int, crop_image_base64: str, preview_gif_base64: str | None = None) -> None:
    _execute(
        """
        UPDATE yard_stats.visits
        SET thumb_crop_status = 'done', thumb_crop_status_changed_at = now(),
            crop_image_base64 = %s, preview_gif_base64 = %s
        WHERE id = %s
        """,
        (crop_image_base64, preview_gif_base64, visit_id),
    )


def mark_visit_thumb_crop_retry_or_failed(visit_id: int, max_attempts: int) -> dict:
    # Returns the resulting status (not just None like the video-queue equivalent) so
    # visit_thumb_worker can tell whether this was the final attempt -- if so, the deferred
    # Telegram visit-summary send (see mqtt_ingest.visit_thumb_crop_will_be_attempted) needs to
    # fire now, falling back to the representative event's own crop, since the re-crop will never
    # succeed for this visit.
    rows = _execute(
        """
        UPDATE yard_stats.visits
        SET thumb_crop_attempt_count = thumb_crop_attempt_count + 1,
            thumb_crop_status = CASE WHEN thumb_crop_attempt_count + 1 >= %s THEN 'failed' ELSE 'retry' END,
            thumb_crop_status_changed_at = now()
        WHERE id = %s
        RETURNING thumb_crop_status, thumb_crop_attempt_count
        """,
        (max_attempts, visit_id),
        fetch=True,
    )
    return rows[0]


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
    #
    # ORDER BY created_at DESC (not ASC) -- newest-eligible-first, same deliberate priority
    # inversion as claim_ai_batch/claim_video_batch: when a backlog outnumbers available capacity,
    # the most recent rows win and older ones keep waiting, only getting swept up once the backlog
    # drops below capacity. Crop is the very first stage -- everything downstream (video, AI) can
    # only ever become eligible after crop_status='done', so an oldest-first crop queue means fresh
    # events wait behind however large the backlog is before they're even croppable at all. This
    # was flipped after confirming in production the crop backlog was tens of thousands of rows
    # deep and growing faster than PARALLEL_LIMIT could clear it oldest-first.
    #
    # CTE, not a plain `WHERE id IN (SELECT ... LIMIT %s FOR UPDATE SKIP LOCKED)` subquery --
    # confirmed in practice that the subquery form does NOT reliably cap the UPDATE at `limit`
    # rows when the subquery self-references the same table being updated (reproduced: 3 eligible
    # rows, LIMIT 2, claimed all 3). The CTE forms an optimization fence so the claimable-id set is
    # computed exactly once and the outer UPDATE only ever touches those rows.
    return _execute(
        """
        WITH claimable AS (
            SELECT id FROM yard_stats.raw_events
            WHERE has_snapshot = true AND crop_status IN ('new', 'retry')
            ORDER BY created_at DESC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE yard_stats.raw_events
        SET crop_status = 'processing', crop_status_changed_at = now()
        FROM claimable
        WHERE yard_stats.raw_events.id = claimable.id
        RETURNING yard_stats.raw_events.*
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


def claim_video_batch(limit: int, max_age_hours: float | None = None) -> list:
    # Only claims rows the crop stage has already finished with -- video download uses the same
    # camera/start/end window regardless of crop_status, but there's no reason to spend download
    # bandwidth on a row that might still fail crop-stage validation upstream, and this keeps the
    # video stage a strict downstream consumer of the crop stage, same relationship the AI stage
    # already has with crop_status='done'.
    #
    # ORDER BY created_at DESC (not ASC) -- newest-eligible-first, same reasoning as
    # claim_next_batch/claim_ai_batch: under a backlog, keeps the video stage caught up on fresh
    # events instead of working through however old a backlog has piled up first. Pairs with
    # max_age_hours below for backlog that's too old to bother with at all.
    #
    # CTE, not a plain `WHERE id IN (SELECT ... LIMIT %s FOR UPDATE SKIP LOCKED)` subquery -- see
    # claim_next_batch's comment for why (confirmed the subquery form over-claims past `limit`).
    #
    # max_age_hours -- same throughput safety valve as claim_ai_batch's: past this cutoff, a row
    # just stays video_status='new'/'retry' indefinitely rather than spending an attempt on a clip
    # that's very likely already rolled off Frigate's continuous-recording buffer.
    age_clause = ""
    params: list = []
    if max_age_hours is not None:
        age_clause = "AND created_at >= now() - (%s * interval '1 hour')"
        params.append(max_age_hours)
    params.append(limit)
    return _execute(
        f"""
        WITH claimable AS (
            SELECT id FROM yard_stats.raw_events
            WHERE crop_status = 'done' AND video_status IN ('new', 'retry')
            {age_clause}
            ORDER BY created_at DESC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE yard_stats.raw_events
        SET video_status = 'processing', video_status_changed_at = now()
        FROM claimable
        WHERE yard_stats.raw_events.id = claimable.id
        RETURNING yard_stats.raw_events.*
        """,
        params,
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
    # visits can now also have a video_path (see alert_video_worker.py) -- collected the same way,
    # otherwise a visit-level clip would be orphaned on disk once its DB row is deleted below.
    video_paths = [
        row["video_path"] for row in _execute(
            """
            SELECT video_path FROM yard_stats.raw_events
            WHERE start_ts < now() - (%s || ' months')::interval AND video_path IS NOT NULL
            """,
            (retention_months,), fetch=True,
        )
    ]
    video_paths += [
        row["video_path"] for row in _execute(
            """
            SELECT video_path FROM yard_stats.visits
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
    video_paths += [
        row["video_path"] for row in _execute(
            "SELECT video_path FROM yard_stats.visits WHERE start_ts < %s AND video_path IS NOT NULL",
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


def _build_events_query(
    object_type: str | None = None,
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    crop_status: str | None = None,
    ai_status: str | None = None,
    video_status: str | None = None,
    has_media: bool = True,
    event_id: int | None = None,
    q: str | None = None,
) -> tuple[str, list]:
    # Factored out of list_events so count_events can reuse the exact same filters (LIMIT/OFFSET-
    # free) rather than re-deriving them in a parallel function that could silently drift out of
    # sync with list_events' own filtering over time.
    clauses = []
    params: list = []
    join = ""
    if q and q.strip():
        # Free-text search across the AI analysis result, not raw_events itself -- only ever
        # matches rows that already have a vehicle_sighting or person_sighting (i.e. ai_status
        # already 'done'), so it composes fine with has_media's default (those rows always have an
        # image already, by the same crop_status='done' invariant everything else here relies on).
        # LEFT JOIN both sighting tables rather than picking one, since object_type isn't required
        # alongside q -- a raw_event only ever matches at most one of the two.
        join = """
        LEFT JOIN yard_stats.vehicle_sightings vs ON vs.raw_event_id = re.id
        LEFT JOIN yard_stats.person_sightings ps ON ps.raw_event_id = re.id
        """
        term = f"%{q.strip()}%"
        clauses.append("""(
            vs.color ILIKE %s OR vs.body_type ILIKE %s OR vs.make_guess ILIKE %s OR
            vs.model_guess ILIKE %s OR vs.notable_features ILIKE %s OR
            vs.plate_text_llm ILIKE %s OR vs.plate_text_frigate ILIKE %s OR vs.notes ILIKE %s OR
            ps.description ILIKE %s OR ps.notes ILIKE %s
        )""")
        params.extend([term] * 10)
    if has_media and event_id is None:
        # Default view: hide rows with neither a crop image nor a stored video (crop_status not
        # yet 'done' -- including the 'skipped' rows has_snapshot=false produces, which will never
        # get one) so the grid isn't full of cards with nothing to show. In practice video_path is
        # never set without crop_image_base64 already being set too (claim_video_batch only claims
        # crop_status='done' rows), so this is currently equivalent to crop-image-only, but checks
        # both so it stays correct if that invariant ever changes. Pass has_media=false to see
        # everything. Skipped entirely when event_id is given -- searching for one specific known
        # event should find it regardless of whether it has media, same reasoning as event_id
        # bypassing the time window at the API layer.
        clauses.append("(re.crop_image_base64 IS NOT NULL OR re.video_path IS NOT NULL)")
    if event_id is not None:
        clauses.append("re.id = %s")
        params.append(event_id)
    if object_type:
        # Comma-separated ("car,truck") or a single value -- "all"/omitted means no filter.
        # `objects` is a free-text label (see mqtt_ingest.parse_payload: Frigate's single
        # `after.label` per row today, not an actual joined multi-label list), so this matches
        # on exact equality against any of the requested types rather than an array/substring op.
        types = [t.strip() for t in object_type.split(",") if t.strip() and t.strip().lower() != "all"]
        if types:
            clauses.append("re.objects = ANY(%s)")
            params.append(types)
    if camera:
        clauses.append("re.camera = %s")
        params.append(camera)
    if start:
        clauses.append("re.start_ts >= %s")
        params.append(start)
    if end:
        clauses.append("re.start_ts <= %s")
        params.append(end)
    if crop_status:
        clauses.append("re.crop_status = %s")
        params.append(crop_status)
    if ai_status:
        clauses.append("re.ai_status = %s")
        params.append(ai_status)
    if video_status:
        clauses.append("re.video_status = %s")
        params.append(video_status)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT DISTINCT re.id, re.camera, re.zone, re.objects, re.start_ts, re.end_ts,
               re.crop_status, re.ai_status, re.video_status,
               re.sub_label, re.score, (re.video_path IS NOT NULL) AS has_video,
               (re.crop_image_base64 IS NOT NULL) AS has_image
        FROM yard_stats.raw_events re
        {join}
        {where}
        ORDER BY re.start_ts DESC
    """
    return query, params


def list_events(
    object_type: str | None = None,
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    crop_status: str | None = None,
    ai_status: str | None = None,
    video_status: str | None = None,
    has_media: bool = True,
    event_id: int | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list:
    query, params = _build_events_query(
        object_type, camera, start, end, crop_status, ai_status, video_status, has_media, event_id, q,
    )
    return _execute(f"{query} LIMIT %s OFFSET %s", params + [limit, offset], fetch=True)


def count_events(
    object_type: str | None = None,
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    crop_status: str | None = None,
    ai_status: str | None = None,
    video_status: str | None = None,
    has_media: bool = True,
    event_id: int | None = None,
    q: str | None = None,
) -> int:
    # Same filters as list_events, no LIMIT/OFFSET -- lets the web UI show "page X of Y" instead
    # of just "there might be more" (e.g. by comparing len(events) to limit).
    query, params = _build_events_query(
        object_type, camera, start, end, crop_status, ai_status, video_status, has_media, event_id, q,
    )
    rows = _execute(f"SELECT COUNT(*) AS count FROM ({query}) AS sub", params, fetch=True)
    return rows[0]["count"]


def _build_visits_query(
    object_type: str | None = None,
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    q: str | None = None,
) -> tuple[str, list]:
    # Factored out of list_visits so count_visits can reuse the exact same filters (LIMIT/OFFSET-
    # free) rather than re-deriving them in a parallel function that could silently drift out of
    # sync with list_visits' own filtering over time.
    # Comparison view alongside list_events -- one row per Frigate review/alert segment instead
    # of one per raw_event, so duplicate det_ids from tracker re-ID/label flicker collapse into a
    # single row. representative_event_id is the earliest-linked raw_event (row_number() = 1,
    # ordered by start_ts then id) -- the simplest deterministic choice for a first comparison
    # pass, not a "best crop" heuristic (that's a separate, later decision if this view leads to
    # actually deduping AI-queue/Telegram work). event_count via a window COUNT(*) OVER (PARTITION
    # BY v.id), same partition as the row_number, so both come from a single pass over `linked`.
    # video_status/has_video describe the VISIT's own video (STORE_VIDEO_ALERTS/
    # alert_video_worker.py), not the representative raw_event's -- those are two entirely
    # separate video flows/storage locations; get_event_video only ever serves a raw_event's
    # video_path, never a visit's, so the web UI needs a different endpoint for visit playback
    # (see GET /media/video/visit/{visit_id}).
    clauses = []
    params: list = []
    if object_type:
        # && (array overlap) -- true if the visit's objects (a comma-joined list, since a review
        # segment can span more than one label, e.g. "car,truck" from a re-ID label flip) share
        # any element with the requested types, same "match any of the requested types" semantics
        # as list_events' object_type filter.
        types = [t.strip() for t in object_type.split(",") if t.strip() and t.strip().lower() != "all"]
        if types:
            clauses.append("string_to_array(v.objects, ',') && %s")
            params.append(types)
    if camera:
        clauses.append("v.cameras = %s")
        params.append(camera)
    if start:
        clauses.append("v.start_ts >= %s")
        params.append(start)
    if end:
        clauses.append("v.start_ts <= %s")
        params.append(end)
    if q and q.strip():
        # Matches a visit if ANY of its linked raw_events has a vehicle_sighting/person_sighting
        # whose AI analysis text matches -- same fields/ILIKE substring match as GET /events' own
        # q. An EXISTS subquery against a fresh raw_events/sighting join, not a condition on the
        # `re` row already joined into `linked` below -- a visit can group several distinct events
        # (e.g. a car and a person), and the match might come from either one, not necessarily the
        # one row_number picks as the representative, so this can't be a plain per-row filter
        # without breaking event_count/rn (which need every linked event, matching or not).
        term = f"%{q.strip()}%"
        clauses.append("""
        EXISTS (
            SELECT 1 FROM yard_stats.raw_events re2
            LEFT JOIN yard_stats.vehicle_sightings vs2 ON vs2.raw_event_id = re2.id
            LEFT JOIN yard_stats.person_sightings ps2 ON ps2.raw_event_id = re2.id
            WHERE re2.visit_id = v.id
              AND (
                vs2.color ILIKE %s OR vs2.body_type ILIKE %s OR vs2.make_guess ILIKE %s OR
                vs2.model_guess ILIKE %s OR vs2.notable_features ILIKE %s OR
                vs2.plate_text_llm ILIKE %s OR vs2.plate_text_frigate ILIKE %s OR vs2.notes ILIKE %s OR
                ps2.description ILIKE %s OR ps2.notes ILIKE %s
              )
        )
        """)
        params.extend([term] * 10)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        WITH linked AS (
            SELECT
                v.id AS visit_id, v.zone, v.objects, v.cameras, v.camera_count,
                v.start_ts AS visit_start_ts, v.end_ts AS visit_end_ts,
                v.video_status AS visit_video_status,
                (v.video_path IS NOT NULL) AS visit_has_video,
                v.thumb_crop_status,
                (v.crop_image_base64 IS NOT NULL) AS has_thumb_crop,
                (v.preview_gif_base64 IS NOT NULL) AS has_preview_gif,
                re.id AS event_id, re.ai_status, re.crop_status,
                (re.crop_image_base64 IS NOT NULL) AS event_has_image,
                row_number() OVER (PARTITION BY v.id ORDER BY re.start_ts ASC, re.id ASC) AS rn,
                count(*) OVER (PARTITION BY v.id) AS event_count
            FROM yard_stats.visits v
            JOIN yard_stats.raw_events re ON re.visit_id = v.id
            {where}
        )
        SELECT visit_id AS id, zone, objects, cameras, camera_count,
               visit_start_ts AS start_ts, visit_end_ts AS end_ts, event_count,
               event_id AS representative_event_id, ai_status, crop_status,
               visit_video_status AS video_status, thumb_crop_status, has_thumb_crop,
               has_preview_gif,
               (has_thumb_crop OR event_has_image) AS has_image, visit_has_video AS has_video
        FROM linked
        WHERE rn = 1
        ORDER BY visit_start_ts DESC
    """
    return query, params


def list_visits(
    object_type: str | None = None,
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list:
    query, params = _build_visits_query(object_type, camera, start, end, q)
    return _execute(f"{query} LIMIT %s OFFSET %s", params + [limit, offset], fetch=True)


def count_visits(
    object_type: str | None = None,
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    q: str | None = None,
) -> int:
    # Same filters as list_visits, no LIMIT/OFFSET -- lets the web UI show "page X of Y".
    query, params = _build_visits_query(object_type, camera, start, end, q)
    rows = _execute(f"SELECT COUNT(*) AS count FROM ({query}) AS sub", params, fetch=True)
    return rows[0]["count"]


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
    require_video: bool = False,
    only_visit_representative: bool = False,
    visits_only: bool = False,
    require_thumb_crop: bool = False,
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
    #
    # CTE, not a plain `WHERE id IN (SELECT ... LIMIT %s FOR UPDATE SKIP LOCKED)` subquery -- see
    # claim_next_batch's comment for why (confirmed the subquery form over-claims past `limit`,
    # i.e. past available_capacity here -- exactly the cap this function exists to enforce).
    # require_video=true adds AND video_status = 'done' -- claim_video_batch only ever claims
    # crop_status='done' rows, so this is strictly narrower than the default (image guaranteed
    # either way), for a future workflow that wants both artifacts ready before processing rather
    # than just the image. The VLM call itself still only ever uses the image -- no model in this
    # setup analyzes video directly -- this only changes which rows are eligible to claim.
    video_clause = "AND video_status = 'done'" if require_video else ""
    # only_visit_representative=true ("source=visits" on POST /ai-queue/claim) skips analyzing
    # every duplicate det_id a visit grouped together -- only one representative raw_event per
    # distinct object type within a visit is eligible (partitioned by (visit_id, objects), not
    # visit_id alone), plus every raw_event that was never grouped into a visit at all (visit_id IS
    # NULL), so events Frigate's review never bundled still get analyzed one-to-one same as today.
    # Partitioning by object type too (not just visit_id) is deliberate: a visit's det_ids can be
    # several re-tracks of the *same* real object (tracker re-ID, label flicker -- the case this
    # dedup was originally built for) or genuinely distinct simultaneous objects (a car and a
    # person in the same visit). Partitioning by visit_id alone collapsed both cases down to a
    # single analyzed event, silently dropping a whole object type whenever a visit happened to
    # group more than one -- confirmed live: a visit with a car det_id and a person det_id only
    # ever got the earlier of the two analyzed, never both. Partitioning by (visit_id, objects)
    # keeps the original behavior for same-type duplicates (still just one analyzed event) while
    # giving each distinct object type in a visit its own representative. Deliberately doesn't
    # touch ai_status semantics or completion at all -- POST /sightings/vehicles|persons still mark
    # the exact same raw_event's ai_status='done' regardless of source, so this is purely a
    # claim-time filter, not a schema/queue change (no ai_status column added to visits).
    #
    # visits_only=true ("source=visits" + visits_only=true on POST /ai-queue/claim) narrows
    # further: drops the "OR visit_id IS NULL" fallback above entirely, so a raw_event Frigate's
    # review never grouped into a visit is never claimed by this call at all -- confirmed needed
    # in production: with the fallback on, an alerts-only n8n workflow still ends up analyzing
    # plain ungrouped events (in one real window, 201 of 215 raw_events had no visit_id at all,
    # so most of what little the alerts flow claimed was the fallback branch, not actual visits).
    # Only meaningful together with only_visit_representative -- ignored otherwise.
    visit_clause = ""
    if only_visit_representative:
        representative_subquery = """
            id = (
                SELECT re2.id FROM yard_stats.raw_events re2
                WHERE re2.visit_id = raw_events.visit_id
                  AND re2.objects = raw_events.objects
                ORDER BY re2.start_ts ASC, re2.id ASC
                LIMIT 1
            )
        """
        if visits_only:
            visit_clause = f"AND visit_id IS NOT NULL AND {representative_subquery}"
        else:
            visit_clause = f"AND (visit_id IS NULL OR {representative_subquery})"
    # require_thumb_crop=true only ever narrows further when only_visit_representative is also set
    # (thumb-crops only exist on visits, and only the representative row is ever eligible either
    # way) -- waits for the visit's own well-timed re-crop (VISIT_THUMB_CROP_ENABLED,
    # visit_thumb_worker.py) to finish before claiming at all, a genuine latency trade-off (the
    # re-crop only starts once the review closes, well after crop_status='done') in exchange for a
    # guarantee that this claim's crop_image_base64 (see below) is always the high-res thumb-crop,
    # never the representative event's own possibly-badly-timed one.
    thumb_crop_required_clause = ""
    if only_visit_representative and require_thumb_crop:
        thumb_crop_required_clause = """
        AND visit_id IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM yard_stats.visits v WHERE v.id = raw_events.visit_id AND v.thumb_crop_status = 'done'
        )
        """
    age_clause = ""
    params: list = [object_types]
    if max_age_hours is not None:
        age_clause = "AND created_at >= now() - (%s * interval '1 hour')"
        params.append(max_age_hours)
    params.append(available_capacity)
    # Opportunistic upgrade (independent of require_thumb_crop, which only affects eligibility
    # above): whenever the claimed row IS a visit's representative and that visit's thumb-crop
    # already happens to be done by claim time, prefer it over the representative event's own
    # crop_image_base64 -- zero latency cost, since this never changes which rows get claimed or
    # when, only which image ends up in the response. Only computed for source=visits claims (a
    # visit's thumb-crop is one artifact per visit; overriding every duplicate det_id's own crop
    # under plain source=events would mean several distinct raw_events all getting sent to the VLM
    # as the identical image, wasted analysis rather than a real improvement).
    visit_thumb_crop_column = (
        """,
        (
            SELECT v.crop_image_base64 FROM yard_stats.visits v
            WHERE v.id = yard_stats.raw_events.visit_id AND v.thumb_crop_status = 'done'
        ) AS visit_thumb_crop_base64
        """
        if only_visit_representative else ""
    )
    rows = _execute(
        f"""
        WITH claimable AS (
            SELECT id FROM yard_stats.raw_events
            WHERE objects = ANY(%s) AND crop_status = 'done' AND ai_status IN ('new', 'retry')
            {video_clause}
            {visit_clause}
            {thumb_crop_required_clause}
            {age_clause}
            ORDER BY created_at DESC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE yard_stats.raw_events
        SET ai_status = 'processing', ai_status_changed_at = now()
        FROM claimable
        WHERE yard_stats.raw_events.id = claimable.id
        RETURNING yard_stats.raw_events.*,
                  (yard_stats.raw_events.video_path IS NOT NULL) AS has_video,
                  (yard_stats.raw_events.crop_image_base64 IS NOT NULL) AS has_image
                  {visit_thumb_crop_column}
        """,
        params,
        fetch=True,
    )
    for row in rows:
        visit_crop = row.pop("visit_thumb_crop_base64", None)
        if visit_crop:
            row["crop_image_base64"] = visit_crop
    return rows


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
    embedding: list[float] | None = None,
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
                     plate_text_llm, plate_text_frigate, plate_confidence, notes, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                RETURNING id
                """,
                (raw_event_id, color, body_type, make_guess, make_confidence,
                 model_guess, model_confidence, notable_features,
                 plate_text_llm, plate_text_frigate, plate_confidence, notes,
                 _vector_literal(embedding)),
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


def complete_person_sighting(
    raw_event_id: int, description: str | None, notes: str | None,
    embedding: list[float] | None = None,
) -> int:
    conn = get_conn()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO yard_stats.person_sightings (raw_event_id, description, notes, embedding)
                VALUES (%s, %s, %s, %s::vector)
                RETURNING id
                """,
                (raw_event_id, description, notes, _vector_literal(embedding)),
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


def semantic_search_sightings(
    embedding: list[float],
    start: datetime | None = None,
    end: datetime | None = None,
    object_types: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    # Cosine-distance ("<=>") ordered search across vehicle_sightings/person_sightings, filtered by
    # time range -- the agent resolves "last week"/"today" into concrete start/end itself (see
    # CLAUDE.md), this only ever ranks by semantic similarity *within* that already-resolved window,
    # same division of labor claim_ai_batch's own time filters already have. embedding IS NOT NULL
    # naturally excludes sightings from before this feature existed (or any n8n run that didn't
    # attach one) -- they're simply not semantically searchable, not an error.
    vector_literal = _vector_literal(embedding)
    want_vehicles = object_types is None or "vehicle" in object_types
    want_persons = object_types is None or "person" in object_types
    parts = []
    params: list = []
    if want_vehicles:
        parts.append(
            """
            SELECT 'vehicle' AS sighting_type, vs.id AS sighting_id, vs.raw_event_id,
                   re.start_ts, re.camera, re.objects,
                   vs.color, vs.body_type, vs.make_guess, vs.model_guess,
                   vs.notable_features, vs.plate_text_llm, vs.plate_text_frigate,
                   NULL AS description,
                   vs.embedding <=> %s::vector AS distance
            FROM yard_stats.vehicle_sightings vs
            JOIN yard_stats.raw_events re ON re.id = vs.raw_event_id
            WHERE vs.embedding IS NOT NULL
              AND (%s::timestamptz IS NULL OR re.start_ts >= %s)
              AND (%s::timestamptz IS NULL OR re.start_ts <= %s)
            """
        )
        params.extend([vector_literal, start, start, end, end])
    if want_persons:
        parts.append(
            """
            SELECT 'person' AS sighting_type, ps.id AS sighting_id, ps.raw_event_id,
                   re.start_ts, re.camera, re.objects,
                   NULL AS color, NULL AS body_type, NULL AS make_guess, NULL AS model_guess,
                   NULL AS notable_features, NULL AS plate_text_llm, NULL AS plate_text_frigate,
                   ps.description,
                   ps.embedding <=> %s::vector AS distance
            FROM yard_stats.person_sightings ps
            JOIN yard_stats.raw_events re ON re.id = ps.raw_event_id
            WHERE ps.embedding IS NOT NULL
              AND (%s::timestamptz IS NULL OR re.start_ts >= %s)
              AND (%s::timestamptz IS NULL OR re.start_ts <= %s)
            """
        )
        params.extend([vector_literal, start, start, end, end])
    if not parts:
        return []
    query = " UNION ALL ".join(parts) + " ORDER BY distance ASC LIMIT %s"
    params.append(limit)
    return _execute(query, tuple(params), fetch=True)


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


def get_report_data(
    start: datetime, end: datetime, source: str = "events", include_preview: str = "gif",
) -> dict:
    # Same joins daily-report.json's two query nodes used to run directly -- filtered by
    # created_at (when the AI stage produced the sighting), not start_ts, matching that behavior.
    # source="visits" (the alerts-report workflow) applies the same dedup claim_ai_batch's
    # only_visit_representative does -- one representative raw_event's sighting per distinct
    # object type within a visit (partitioned by (visit_id, objects), not visit_id alone -- see
    # claim_ai_batch's comment), plus every sighting whose raw_event was never grouped into a
    # visit at all -- so re-tracked duplicates of the same real object (re-track, label flicker)
    # still collapse to one row, but a visit grouping genuinely distinct objects (e.g. a car and a
    # person) shows both instead of silently dropping one.
    visit_clause = ""
    visit_join = ""
    crop_image_expr = "re.crop_image_base64"
    gif_image_expr = "NULL"
    if source == "visits":
        visit_clause = """
        AND (
            re.visit_id IS NULL
            OR re.id = (
                SELECT re2.id FROM yard_stats.raw_events re2
                WHERE re2.visit_id = re.visit_id
                  AND re2.objects = re.objects
                ORDER BY re2.start_ts ASC, re2.id ASC
                LIMIT 1
            )
        )
        """
        # Prefer the visit's own high-res re-crop at Frigate's thumb_time (VISIT_THUMB_CROP_ENABLED,
        # visit_thumb_worker.py) over the representative event's own crop, when it's finished --
        # reports run well after the fact (a scheduled daily/hourly window), so unlike the AI queue
        # there's no real latency cost to just always preferring it here.
        visit_join = "LEFT JOIN yard_stats.visits v ON v.id = re.visit_id AND v.thumb_crop_status = 'done'"
        crop_image_expr = "COALESCE(v.crop_image_base64, re.crop_image_base64)"
        # The visit's own animated preview GIF (human preview only, never sent to the VLM -- see
        # CLAUDE.md's "Visit preview") -- report.py prefers this over the static grid for the
        # inline row preview, same "richer artifact when available" preference Telegram's visit
        # summary already applies. NULL for a standalone (never visit-grouped) sighting, or while
        # the preview hasn't finished building yet -- report.py falls back to the grid/crop there.
        gif_image_expr = "v.preview_gif_base64"
    # include_preview is a mode, not a bool, same shape as TELEGRAM_EVENTS_MODE -- "gif" (the
    # default) is today's original behavior (prefer the visit's animated GIF, falling back to the
    # static grid/crop); "image" drops only the GIF, at the SQL level, not just report.py's
    # rendering, since it's typically the single largest field in this query (a multi-frame
    # animated GIF vs. one flat JPEG); "none" drops the image entirely (crop included) for a
    # caller that wants the smallest possible payload -- report.py's _img_cell already renders
    # "(no image)" whenever crop_image_base64 comes back NULL, so no separate rendering path is
    # needed for that case.
    if include_preview == "none":
        crop_image_expr = "NULL"
        gif_image_expr = "NULL"
    elif include_preview == "image":
        gif_image_expr = "NULL"
    # visit_id is included so report.py can group a visit's vehicle + person sightings into one
    # combined alert entry (source="visits" only -- always NULL under source="events", where
    # there's no grouping concept and every sighting is its own entry).
    vehicles = _execute(
        f"""
        SELECT re.id AS raw_event_id, re.visit_id, re.camera, re.zone, re.start_ts,
               {crop_image_expr} AS crop_image_base64, {gif_image_expr} AS preview_gif_base64,
               vs.color, vs.body_type, vs.make_guess, vs.make_confidence,
               vs.model_guess, vs.model_confidence, vs.notable_features,
               vs.plate_text_llm, vs.plate_text_frigate, vs.notes
        FROM yard_stats.vehicle_sightings vs
        JOIN yard_stats.raw_events re ON re.id = vs.raw_event_id
        {visit_join}
        WHERE vs.created_at >= %s AND vs.created_at <= %s
        {visit_clause}
        ORDER BY re.start_ts DESC
        """,
        (start, end), fetch=True,
    )
    persons = _execute(
        f"""
        SELECT re.id AS raw_event_id, re.visit_id, re.camera, re.zone, re.start_ts,
               {crop_image_expr} AS crop_image_base64, {gif_image_expr} AS preview_gif_base64,
               ps.description, ps.notes
        FROM yard_stats.person_sightings ps
        JOIN yard_stats.raw_events re ON re.id = ps.raw_event_id
        {visit_join}
        WHERE ps.created_at >= %s AND ps.created_at <= %s
        {visit_clause}
        ORDER BY re.start_ts DESC
        """,
        (start, end), fetch=True,
    )
    return {"vehicles": vehicles, "persons": persons}
