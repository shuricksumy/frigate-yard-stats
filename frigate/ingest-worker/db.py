import logging
import os
import re
from datetime import datetime

import psycopg2
import psycopg2.extras

import config
import profile_config

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
    for table in ("sightings", "visit_sightings"):
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


def get_table_row_counts() -> dict:
    # Total rows per table, for the admin dashboard's headline numbers -- deliberately just counts,
    # not filtered by status/date, since get_stage_counts()/get_retention_info() already cover the
    # "how many are in what state" and "how far back does data go" angles.
    return {
        "raw_events": _execute("SELECT count(*)::int AS c FROM yard_stats.raw_events", fetch=True)[0]["c"],
        "visits": _execute("SELECT count(*)::int AS c FROM yard_stats.visits", fetch=True)[0]["c"],
        "sightings": _execute("SELECT count(*)::int AS c FROM yard_stats.sightings", fetch=True)[0]["c"],
        "visit_sightings": _execute("SELECT count(*)::int AS c FROM yard_stats.visit_sightings", fetch=True)[0]["c"],
    }


def get_stage_counts() -> dict:
    # Per-stage status breakdown (crop/video/ai on raw_events; video/thumb_crop on visits) for the
    # admin dashboard's queue-health section -- table/column names below are always one of the
    # fixed literals passed by this function's own callers below, never caller-supplied, so
    # building the query with an f-string carries no injection risk.
    def _counts(table: str, column: str) -> dict:
        rows = _execute(
            f"SELECT {column} AS status, count(*)::int AS count FROM yard_stats.{table} "
            f"GROUP BY {column} ORDER BY {column}",
            fetch=True,
        )
        return {row["status"]: row["count"] for row in rows}

    return {
        "raw_events": {
            "crop_status": _counts("raw_events", "crop_status"),
            "video_status": _counts("raw_events", "video_status"),
            "ai_status": _counts("raw_events", "ai_status"),
        },
        "visits": {
            "video_status": _counts("visits", "video_status"),
            "thumb_crop_status": _counts("visits", "thumb_crop_status"),
            "alert_ai_status": _counts("visits", "alert_ai_status"),
        },
    }


def get_row_counts_by_object_type() -> dict:
    # Per-type row-count breakdown for the admin dashboard's "By object type" section -- raw_events
    # is grouped by its own single-label `objects` column; sightings/visit_sightings by their own
    # object_label. Deliberately three separate lists rather than one joined table -- a type can
    # have raw_events without a sighting yet (still queued/analyzing), so summing across tables
    # would either double- or under-count depending on how it's done; showing each table's own
    # breakdown side by side is unambiguous.
    return {
        "raw_events": _execute(
            "SELECT objects AS object_type, count(*)::int AS count FROM yard_stats.raw_events "
            "GROUP BY objects ORDER BY count DESC",
            fetch=True,
        ),
        "sightings": _execute(
            "SELECT object_label AS object_type, count(*)::int AS count FROM yard_stats.sightings "
            "GROUP BY object_label ORDER BY count DESC",
            fetch=True,
        ),
        "visit_sightings": _execute(
            "SELECT object_label AS object_type, count(*)::int AS count FROM yard_stats.visit_sightings "
            "GROUP BY object_label ORDER BY count DESC",
            fetch=True,
        ),
    }


def get_db_size_by_object_type() -> dict:
    # Approximate per-type Postgres footprint -- sum(pg_column_size(t.*)) over each table's own
    # rows, grouped by that table's label column. This is a real byte count of each row's stored
    # data (not a rough estimate), but it's still an approximation of the table's true on-disk
    # footprint: it doesn't include per-row overhead (tuple header, alignment padding), TOAST
    # storage for the crop_image_base64/preview_gif_base64 columns' actual out-of-line chunks, or
    # index space at all -- get_db_size_info()'s pg_total_relation_size figures remain the
    # authoritative whole-table sizes; this is for relative "which type is using the most space"
    # comparison, not a precise accounting.
    return {
        "raw_events": _execute(
            "SELECT objects AS object_type, sum(pg_column_size(raw_events.*))::bigint AS bytes "
            "FROM yard_stats.raw_events GROUP BY objects ORDER BY bytes DESC",
            fetch=True,
        ),
        "sightings": _execute(
            "SELECT object_label AS object_type, sum(pg_column_size(sightings.*))::bigint AS bytes "
            "FROM yard_stats.sightings GROUP BY object_label ORDER BY bytes DESC",
            fetch=True,
        ),
        "visit_sightings": _execute(
            "SELECT object_label AS object_type, sum(pg_column_size(visit_sightings.*))::bigint AS bytes "
            "FROM yard_stats.visit_sightings GROUP BY object_label ORDER BY bytes DESC",
            fetch=True,
        ),
    }


# (table, status_column, attempt_column, id_column) for every queue stage requeue_failed()
# supports -- the whitelist requeue_failed() validates against, so a caller-supplied stage/table
# name is never interpolated into SQL unchecked.
_REQUEUE_TARGETS = {
    ("raw_events", "crop"): ("yard_stats.raw_events", "crop_status", "crop_attempt_count", "crop_status_changed_at"),
    ("raw_events", "video"): ("yard_stats.raw_events", "video_status", "video_attempt_count", "video_status_changed_at"),
    ("raw_events", "ai"): ("yard_stats.raw_events", "ai_status", "ai_attempt_count", "ai_status_changed_at"),
    ("visits", "video"): ("yard_stats.visits", "video_status", "video_attempt_count", "video_status_changed_at"),
    ("visits", "thumb_crop"): ("yard_stats.visits", "thumb_crop_status", "thumb_crop_attempt_count", "thumb_crop_status_changed_at"),
    ("visits", "alert_ai"): ("yard_stats.visits", "alert_ai_status", "alert_ai_attempt_count", "alert_ai_status_changed_at"),
}


def requeue_failed(table: str, stage: str) -> int:
    # The exact fix applied by hand via sql/queue-debug.sql's "retry every ai-failed item" query,
    # exposed as a real admin action instead of requiring direct psql access -- reset status back
    # to 'retry' with a fresh attempt count, so the next poll tick/claim picks it back up. table/
    # stage are validated against _REQUEUE_TARGETS before touching SQL, since they'd otherwise be
    # caller-supplied strings.
    target = _REQUEUE_TARGETS.get((table, stage))
    if target is None:
        raise ValueError(f"Unknown table/stage combination: {table}/{stage}")
    sql_table, status_col, attempt_col, changed_col = target
    rows = _execute(
        f"UPDATE {sql_table} SET {status_col} = 'retry', {attempt_col} = 0, {changed_col} = now() "
        f"WHERE {status_col} = 'failed' RETURNING 1",
        fetch=True,
    )
    return len(rows)


def skip_failed_older_than(table: str, stage: str, days: int) -> int:
    # requeue_failed's reset-to-retry assumes the failure was transient -- for a row that's a
    # genuinely permanent failure (corrupt/truncated media, a det_id Frigate no longer has, an
    # image the VLM can never parse), that just re-fails on the very next attempt and piles back
    # into the same 'failed' bucket, indistinguishable from a fresh, still-worth-retrying failure.
    # This is the other lever for that bucket: instead of retrying, mark anything that's been
    # sitting at 'failed' for at least `days` as 'skipped' -- the same terminal state a
    # has_snapshot=false row already gets (see mqtt_ingest.py) -- so it stops being retried or
    # counted as failed, without deleting the row itself. attempt_count is deliberately left as-is
    # (unlike requeue_failed's reset to 0) since it's no longer meaningful once terminal, and
    # keeping it is a small audit trail of how many attempts were made before giving up. Same
    # table/stage whitelist as requeue_failed -- these would otherwise be caller-supplied strings.
    target = _REQUEUE_TARGETS.get((table, stage))
    if target is None:
        raise ValueError(f"Unknown table/stage combination: {table}/{stage}")
    sql_table, status_col, _attempt_col, changed_col = target
    rows = _execute(
        f"UPDATE {sql_table} SET {status_col} = 'skipped', {changed_col} = now() "
        f"WHERE {status_col} = 'failed' AND {changed_col} < now() - make_interval(days => %s) "
        f"RETURNING 1",
        (days,),
        fetch=True,
    )
    return len(rows)


def get_db_size_info() -> dict:
    # Total DB size plus a per-table breakdown (yard_stats' own tables only) for the admin
    # dashboard's disk-usage section -- pg_total_relation_size includes indexes/TOAST, so this
    # matches what actually shows up in `docker exec ... du` on the Postgres data volume, not just
    # raw row bytes.
    total = _execute("SELECT pg_database_size(current_database())::bigint AS b", fetch=True)[0]["b"]
    tables = _execute(
        """
        SELECT relname AS table, pg_total_relation_size(c.oid)::bigint AS bytes
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'yard_stats' AND c.relkind = 'r'
        ORDER BY bytes DESC
        """,
        fetch=True,
    )
    return {"database_bytes": total, "tables": tables}


def get_vector_index_status() -> dict:
    # pgvector extension presence/version plus HNSW index health for the admin dashboard's vector
    # DB section -- indisvalid=false (e.g. after a killed concurrent build) is exactly the
    # condition the dashboard's "Reindex vector DB" button exists to fix.
    ext = _execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'", fetch=True)
    indexes = _execute(
        """
        SELECT indexrelid::regclass::text AS index, indisvalid, indisready
        FROM pg_index WHERE indexrelid::regclass::text IN (
            'yard_stats.idx_sightings_embedding', 'yard_stats.idx_visit_sightings_embedding'
        )
        """,
        fetch=True,
    )
    return {
        "extension_installed": bool(ext),
        "extension_version": ext[0]["extversion"] if ext else None,
        "embedding_dimensions": config.EMBEDDING_DIMENSIONS,
        "indexes": indexes,
    }


def reindex_vector_indexes() -> list[str]:
    # REINDEX rebuilds the HNSW index structure in place -- fixes an indisvalid=false index (e.g.
    # left behind by an interrupted build) and is the natural "tidy up" action after a large
    # /embeddings/backfill run, without the brief index-gap a DROP+CREATE would introduce.
    reindexed = []
    for index in ("idx_sightings_embedding", "idx_visit_sightings_embedding"):
        _execute(f"REINDEX INDEX yard_stats.{index}")
        reindexed.append(index)
    return reindexed


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


def get_distinct_cameras() -> list[str]:
    # Backs the web UI's Camera filter dropdown -- queried directly from raw_events rather than
    # sourced from a config value (unlike OBJECT_TYPES, a manually-maintained env var) since
    # config.CAMERAS is an optional ingest-time allow-list that's usually unset (meaning "no
    # filter", not "here is the list of cameras") and would give an empty/stale dropdown on a
    # deployment that never set it, or silently miss a newly added camera until someone remembers
    # to update it. Querying real data instead means the dropdown always reflects reality.
    rows = _execute(
        "SELECT DISTINCT camera FROM yard_stats.raw_events WHERE camera IS NOT NULL ORDER BY camera",
        fetch=True,
    )
    return [row["camera"] for row in rows]


def count_sightings_missing_embedding() -> dict:
    # Dry-run counterpart for POST /embeddings/backfill, same shape as purge_older_than's own
    # always-count-first approach -- a sighting from before semantic search existed (or from any
    # run that didn't attach one) has embedding IS NULL, same condition
    # semantic_search_sightings already excludes on the read side. Covers both event-level and
    # visit-level sightings -- unlike the old vehicle_sightings/person_sightings-only backfill,
    # there's no reason to leave visit_sightings out now that both share one universal shape.
    return {
        "sightings": _execute(
            "SELECT count(*)::int AS c FROM yard_stats.sightings WHERE embedding IS NULL",
            fetch=True,
        )[0]["c"],
        "visit_sightings": _execute(
            "SELECT count(*)::int AS c FROM yard_stats.visit_sightings WHERE embedding IS NULL",
            fetch=True,
        )[0]["c"],
    }


def get_sightings_missing_embedding(limit: int) -> list[dict]:
    # Oldest first (plain id order) -- a backfill has no "freshness" concept the way live queue
    # claims do (see claim_ai_batch's newest-first comment), so working through the backlog in a
    # stable, predictable order is simplest; repeated calls make steady progress either way.
    return _execute(
        """
        SELECT id, raw_event_id, object_label, description
        FROM yard_stats.sightings
        WHERE embedding IS NULL
        ORDER BY id
        LIMIT %s
        """,
        (limit,), fetch=True,
    )


def get_visit_sightings_missing_embedding(limit: int) -> list[dict]:
    return _execute(
        """
        SELECT id, visit_id, object_label, description
        FROM yard_stats.visit_sightings
        WHERE embedding IS NULL
        ORDER BY id
        LIMIT %s
        """,
        (limit,), fetch=True,
    )


def update_sighting_embedding(sighting_id: int, embedding: list[float]) -> None:
    _execute(
        "UPDATE yard_stats.sightings SET embedding = %s::vector WHERE id = %s",
        (_vector_literal(embedding), sighting_id),
    )


def update_visit_sighting_embedding(sighting_id: int, embedding: list[float]) -> None:
    _execute(
        "UPDATE yard_stats.visit_sightings SET embedding = %s::vector WHERE id = %s",
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


def get_sighting_for_event(raw_event_id: int) -> dict | None:
    # For GET /events/{id} -- surfaces the AI analysis result (object_label + description) in the
    # web UI's lightbox once ai_status='done'. At most one row ever exists per raw_event_id.
    rows = _execute(
        """
        SELECT s.id, s.raw_event_id, re.camera, re.zone, re.start_ts, s.object_label, s.description
        FROM yard_stats.sightings s
        JOIN yard_stats.raw_events re ON re.id = s.raw_event_id
        WHERE s.raw_event_id = %s
        """,
        (raw_event_id,), fetch=True,
    )
    return rows[0] if rows else None


def get_sightings_for_visit(visit_id: int) -> list[dict]:
    # Every sighting linked to this visit, not just the representative event's -- claim_ai_batch's
    # only_visit_representative now partitions by (visit_id, objects) rather than visit_id alone
    # (see there for why), so a visit can have more than one analyzed event: one representative per
    # distinct object type (a car and a person in the same visit each get their own sighting), not
    # just one per visit. Used by the web UI's visit lightbox to show all of them together instead
    # of only the single representative event's AI result GET /events/{id} would return. One flat
    # list now (not split by type -- there's no type split anywhere in this universal model).
    return _execute(
        """
        SELECT s.id, s.raw_event_id, re.camera, re.zone, re.start_ts, s.object_label, s.description
        FROM yard_stats.sightings s
        JOIN yard_stats.raw_events re ON re.id = s.raw_event_id
        WHERE re.visit_id = %s
        ORDER BY re.start_ts ASC
        """,
        (visit_id,), fetch=True,
    )


def insert_raw_event(event: dict, profile: dict | None = None) -> None:
    # video_status starts 'skipped' (not 'new') when store_video resolves to off for this event's
    # own object type -- a cheap flag set once at ingest, so the video queue's WHERE video_status
    # IN ('new','retry') never even considers these rows, rather than special-casing a disabled
    # feature inside the poll loop. Resolved via profile_config (type override -> profiles.yaml
    # `defaults:` -> config.py hardcoded fallback), not a bare config.STORE_VIDEO read -- store_video
    # has no env var backing it at all any more (see config.py), so reading the bare constant here
    # would silently ignore a profiles.yaml override/default and always see the hardcoded False.
    initial_video_status = "new" if profile_config.store_video_enabled(profile, event["objects"]) else "skipped"
    # crop_status starts 'skipped' (not 'new') when has_snapshot is false -- Frigate can emit a
    # full "end" MQTT lifecycle for a tracked object it never actually saved a snapshot for (seen
    # in production: such det_ids 404 against Frigate's own /api/events/<id>), so cropping can
    # never succeed for these regardless of retries. Skipping at ingest keeps the row (accurate
    # yard-activity counts) without it piling up as an eternally-unprocessed 'new'.
    initial_crop_status = "new" if event["has_snapshot"] else "skipped"
    # ai_status gets the identical treatment, for the identical reason: claim_ai_batch hard-requires
    # crop_status='done' (an image is always guaranteed on every claimed row, never configurable),
    # so a row that can never get a crop can also never be claimed for AI analysis -- without this,
    # such a row would sit at ai_status='new' forever, indistinguishable from one genuinely waiting
    # on capacity (confirmed live: this was the majority of a reported ai_status='new' backlog).
    initial_ai_status = "new" if event["has_snapshot"] else "skipped"
    _execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, video_status, ai_status)
        VALUES (%s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s, %s, %s, %s, %s, %s)
        """,
        (
            event["camera"], event["zone"], event["objects"],
            event["start_time"], event["end_time"], event["det_id"],
            event["has_clip"], event["has_snapshot"], initial_crop_status, initial_video_status,
            initial_ai_status,
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


def visit_thumb_crop_will_be_attempted(
    review: dict, profile: dict | None = None, object_label: str | None = None,
) -> bool:
    # Shared by record_visit (to set the initial thumb_crop_status) and mqtt_ingest's
    # _handle_review_message (to decide whether the Telegram visit-summary send should fire
    # immediately or be deferred to visit_thumb_worker -- see there for why). Used to also require
    # review.get("thumb_time") is not None, back when crop.build_visit_preview's predecessor
    # (crop_visit_thumbnail) seeked to thumb_time specifically -- that approach was abandoned (see
    # CLAUDE.md) in favor of sampling frames proportionally across the visit's own clip duration,
    # which needs only start_ts/end_ts/cameras, not thumb_time at all. thumb_time is still stored
    # on the visit row (informational -- Frigate's own opinion of the best moment) but no longer
    # gates whether a preview can be built.
    #
    # Resolved via profile_config (type override -> profiles.yaml `defaults:` -> config.py
    # hardcoded fallback) against the visit's representative object type, not a bare
    # config.VISIT_THUMB_CROP_ENABLED read -- this setting has no env var backing it at all any
    # more (see config.py), so reading the bare constant here would silently ignore a
    # profiles.yaml override/default and always see the hardcoded False. `review` itself is
    # unused now (kept as a parameter for call-site compatibility/API stability) -- the decision
    # is entirely `profile`/`object_label` driven.
    return profile_config.visit_thumb_crop_enabled(profile, object_label)


def _get_representative_object_label_for_det_ids(det_ids: list) -> str | None:
    # Same "earliest linked raw_event" representative definition as get_representative_event_for_
    # visit, but matched by det_id instead of visit_id -- used by record_visit, which needs this
    # *before* the visit row (and its raw_events.visit_id link) exists yet.
    if not det_ids:
        return None
    rows = _execute(
        """
        SELECT objects FROM yard_stats.raw_events
        WHERE det_id = ANY(%s)
        ORDER BY start_ts ASC, id ASC
        LIMIT 1
        """,
        (det_ids,), fetch=True,
    )
    return rows[0]["objects"] if rows else None


def record_visit(review: dict, profile: dict | None = None) -> int | None:
    # Populates the previously-unwired visits table / raw_events.visit_id+reconciled from
    # Frigate's own review/alert grouping (frigate/reviews MQTT topic) -- one review segment
    # already bundles the det_ids Frigate's tracker considers the same real-world activity
    # (occlusion/re-ID, label flicker e.g. car -> truck mid-track), so this reuses that grouping
    # instead of reimplementing a merge heuristic ourselves. Grouping is per-camera only --
    # confirmed live against production Frigate that a review's "camera" is a single value, never
    # a list -- so cameras/camera_count are set for just that one camera; a cross-camera merge on
    # top of this (same zone, overlapping time window, different camera) is a separate, not-yet-
    # built layer. Insert + link in one transaction, same pattern as complete_sighting.
    #
    # store_video_alerts/visit_thumb_crop_enabled are resolved against the visit's own
    # representative object type (same single-type-per-visit convention claim_alert_ai_batch
    # already uses for a visit that can span multiple distinct types) -- computed here via det_ids
    # since the visit row (and its raw_events.visit_id link, which get_representative_event_for_
    # visit relies on) doesn't exist yet at this point.
    representative_label = _get_representative_object_label_for_det_ids(review["det_ids"])
    # video_status starts 'skipped' (not 'new') when store_video_alerts resolves to off for this
    # visit's representative type -- same reasoning as insert_raw_event's initial_video_status: a
    # cheap flag set once at insert, so the visit video queue's WHERE clause never even considers
    # these rows while the feature is disabled.
    initial_video_status = "new" if profile_config.store_video_alerts_enabled(profile, representative_label) else "skipped"
    # Same reasoning for thumb_crop_status -- no longer conditioned on thumb_time being present
    # (see visit_thumb_crop_will_be_attempted).
    initial_thumb_crop_status = (
        "new" if visit_thumb_crop_will_be_attempted(review, profile, representative_label) else "skipped"
    )
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


def claim_visit_video_batch(
    limit: int,
    max_age_hours: float | None = None,
    object_types: list[str] | None = None,
    exclude_object_types: list[str] | None = None,
) -> list:
    # Mirrors claim_video_batch exactly (CTE form for the same FOR UPDATE SKIP LOCKED reason, and
    # the same newest-first + max_age_hours safety valve), just against visits instead of
    # raw_events -- one clip per visit's whole start_ts->end_ts span, independent of any per-event
    # video. See alert_video_worker.py.
    #
    # object_types/exclude_object_types match against the visit's own *representative* event's
    # objects (via a LATERAL join, same convention claim_alert_ai_batch already uses) rather than
    # visits.objects, since a visit's objects column can span more than one distinct type -- see
    # claim_alert_ai_batch's own comment for why. Only joined in at all when a filter is actually
    # requested, so the unfiltered default case (both None) runs the exact same query as before
    # per-type overrides existed.
    age_clause = ""
    type_clause = ""
    join_clause = ""
    params: list = []
    if object_types is not None or exclude_object_types is not None:
        join_clause = """
            JOIN LATERAL (
                SELECT re.objects FROM yard_stats.raw_events re
                WHERE re.visit_id = v.id
                ORDER BY re.start_ts ASC, re.id ASC
                LIMIT 1
            ) rep ON true
        """
        if object_types is not None:
            type_clause = "AND rep.objects = ANY(%s)"
            params.append(object_types)
        else:
            type_clause = "AND NOT (rep.objects = ANY(%s))"
            params.append(exclude_object_types)
    if max_age_hours is not None:
        age_clause = "AND v.start_ts >= now() - (%s * interval '1 hour')"
        params.append(max_age_hours)
    params.append(limit)
    return _execute(
        f"""
        WITH claimable AS (
            SELECT v.id FROM yard_stats.visits v
            {join_clause}
            WHERE v.video_status IN ('new', 'retry')
            {type_clause}
            {age_clause}
            ORDER BY v.start_ts DESC
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


def claim_visit_thumb_crop_batch(
    limit: int,
    object_types: list[str] | None = None,
    exclude_object_types: list[str] | None = None,
) -> list:
    # Mirrors claim_visit_video_batch's CTE-claim shape (same FOR UPDATE SKIP LOCKED reason,
    # newest-first) -- fifth queue stage, on visits.thumb_crop_status. See visit_thumb_worker.py.
    # object_types/exclude_object_types: same representative-event LATERAL-join include/exclude
    # filter as claim_visit_video_batch -- see its comment for why this isn't a plain include-list
    # against every known label.
    type_clause = ""
    join_clause = ""
    params: list = []
    if object_types is not None or exclude_object_types is not None:
        join_clause = """
            JOIN LATERAL (
                SELECT re.objects FROM yard_stats.raw_events re
                WHERE re.visit_id = v.id
                ORDER BY re.start_ts ASC, re.id ASC
                LIMIT 1
            ) rep ON true
        """
        if object_types is not None:
            type_clause = "AND rep.objects = ANY(%s)"
            params.append(object_types)
        else:
            type_clause = "AND NOT (rep.objects = ANY(%s))"
            params.append(exclude_object_types)
    params.append(limit)
    return _execute(
        f"""
        WITH claimable AS (
            SELECT v.id FROM yard_stats.visits v
            {join_clause}
            WHERE v.thumb_crop_status IN ('new', 'retry')
            {type_clause}
            ORDER BY v.start_ts DESC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE yard_stats.visits
        SET thumb_crop_status = 'processing', thumb_crop_status_changed_at = now()
        FROM claimable
        WHERE yard_stats.visits.id = claimable.id
        RETURNING yard_stats.visits.*
        """,
        params,
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


def claim_video_batch(
    limit: int,
    max_age_hours: float | None = None,
    object_types: list[str] | None = None,
    exclude_object_types: list[str] | None = None,
) -> list:
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
    #
    # object_types/exclude_object_types -- an include-or-exclude pair (at most one set, see
    # profile_config.store_video_claim_filter), not a plain include-list against every known label:
    # STORE_VIDEO applies to any Frigate label by default, and OBJECT_TYPES is otherwise a cosmetic
    # list (just the web UI's Type dropdown source) -- an include-only filter against it would
    # silently stop storing video for any real label that isn't in that list. Both None (the
    # default) means no per-type filtering at all, identical to this function's behavior before
    # per-type overrides existed.
    age_clause = ""
    type_clause = ""
    params: list = []
    if object_types is not None:
        type_clause = "AND objects = ANY(%s)"
        params.append(object_types)
    elif exclude_object_types is not None:
        type_clause = "AND NOT (objects = ANY(%s))"
        params.append(exclude_object_types)
    if max_age_hours is not None:
        age_clause = "AND created_at >= now() - (%s * interval '1 hour')"
        params.append(max_age_hours)
    params.append(limit)
    return _execute(
        f"""
        WITH claimable AS (
            SELECT id FROM yard_stats.raw_events
            WHERE crop_status = 'done' AND video_status IN ('new', 'retry')
            {type_clause}
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
        DELETE FROM yard_stats.sightings WHERE raw_event_id IN (
            SELECT id FROM yard_stats.raw_events WHERE start_ts < now() - (%s || ' months')::interval
        )
        """,
        (retention_months,),
    )
    # visit_sightings references visits(id) with no ON DELETE CASCADE -- must go before the
    # visits DELETE below, same child-before-parent reasoning as sightings above
    # (raw_event_id -> raw_events(id)).
    _execute(
        """
        DELETE FROM yard_stats.visit_sightings WHERE visit_id IN (
            SELECT id FROM yard_stats.visits WHERE start_ts < now() - (%s || ' months')::interval
        )
        """,
        (retention_months,),
    )
    # raw_events.visit_id references visits(id) -- the opposite direction from the delete order
    # below (visits before raw_events), so a visit about to be deleted can't still have a
    # raw_event pointing at it, deleted or not (a visit's start_ts is set from its earliest-linked
    # event, but a long-lived visit -- e.g. a car parked for 20+ minutes -- can have later-linked
    # events that individually aren't old enough to be purged in this same pass). Decoupling first
    # makes the delete order below safe regardless of that edge case, rather than relying on every
    # linked raw_event always being at least as old as its visit.
    _execute(
        """
        UPDATE yard_stats.raw_events SET visit_id = NULL WHERE visit_id IN (
            SELECT id FROM yard_stats.visits WHERE start_ts < now() - (%s || ' months')::interval
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


def purge_older_than(cutoff: datetime, execute: bool, object_label: str | None = None) -> dict:
    # Ad-hoc counterpart to run_retention_cleanup above -- same FK-safe child-before-parent
    # delete order, but keyed on a caller-supplied cutoff timestamp instead of the fixed
    # config.RETENTION_MONTHS, and always counts first so a dry run (execute=False) and a real
    # run report the identical shape of result.
    #
    # object_label (optional) scopes this purge to a single Frigate object type -- deliberately
    # raw_events/sightings only. visits/visit_sightings are never touched when object_label is set:
    # a visit can span multiple distinct object types (see "Visit grouping" in CLAUDE.md), so there
    # is no single-type-safe way to decide whether the visit *row itself* (or its own composite-grid
    # media) belongs to just one type's purge -- only the type-scoped raw_events/sightings have an
    # unambiguous single label to filter on. Purging "all types" (object_label=None) still covers
    # visits/visit_sightings exactly as before this param existed.
    type_clause = "AND re.objects = %s" if object_label else ""
    type_clause_bare = "AND objects = %s" if object_label else ""
    type_params = [object_label] if object_label else []

    counts = {
        "sightings": _execute(
            f"""
            SELECT count(*)::int AS c FROM yard_stats.sightings s
            JOIN yard_stats.raw_events re ON re.id = s.raw_event_id
            WHERE re.start_ts < %s {type_clause}
            """,
            [cutoff, *type_params], fetch=True,
        )[0]["c"],
        "visit_sightings": 0 if object_label else _execute(
            """
            SELECT count(*)::int AS c FROM yard_stats.visit_sightings vs
            JOIN yard_stats.visits v ON v.id = vs.visit_id
            WHERE v.start_ts < %s
            """,
            (cutoff,), fetch=True,
        )[0]["c"],
        "visits": 0 if object_label else _execute(
            "SELECT count(*)::int AS c FROM yard_stats.visits WHERE start_ts < %s",
            (cutoff,), fetch=True,
        )[0]["c"],
        "raw_events": _execute(
            f"SELECT count(*)::int AS c FROM yard_stats.raw_events WHERE start_ts < %s {type_clause_bare}",
            [cutoff, *type_params], fetch=True,
        )[0]["c"],
    }

    video_paths = [
        row["video_path"] for row in _execute(
            f"SELECT video_path FROM yard_stats.raw_events WHERE start_ts < %s AND video_path IS NOT NULL {type_clause_bare}",
            [cutoff, *type_params], fetch=True,
        )
    ]
    if not object_label:
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
            f"""
            DELETE FROM yard_stats.sightings WHERE raw_event_id IN (
                SELECT id FROM yard_stats.raw_events WHERE start_ts < %s {type_clause_bare}
            )
            """,
            [cutoff, *type_params],
        )
        if not object_label:
            # visit_sightings references visits(id) with no ON DELETE CASCADE -- must go before
            # the visits DELETE below, same reasoning as sightings above. Skipped entirely under
            # an object_label-scoped purge (visits/visit_sightings are never touched -- see above).
            _execute(
                """
                DELETE FROM yard_stats.visit_sightings WHERE visit_id IN (
                    SELECT id FROM yard_stats.visits WHERE start_ts < %s
                )
                """,
                (cutoff,),
            )
            # raw_events.visit_id references visits(id) -- the opposite direction from the delete
            # order below (visits before raw_events); decouple first so a visit about to be deleted
            # can never still have a raw_event pointing at it, same reasoning as
            # run_retention_cleanup's identical fix.
            _execute(
                """
                UPDATE yard_stats.raw_events SET visit_id = NULL WHERE visit_id IN (
                    SELECT id FROM yard_stats.visits WHERE start_ts < %s
                )
                """,
                (cutoff,),
            )
            _execute("DELETE FROM yard_stats.visits WHERE start_ts < %s", (cutoff,))
        _execute(f"DELETE FROM yard_stats.raw_events WHERE start_ts < %s {type_clause_bare}", [cutoff, *type_params])
        logger.info(
            "Ad-hoc purge executed (cutoff=%s, object_label=%s, counts=%s, video_files_deleted=%s)",
            cutoff, object_label, counts, deleted_files,
        )

    return counts


def purge_media_older_than(cutoff: datetime, execute: bool, object_label: str | None = None) -> dict:
    # POST /retention/purge's only_media=true mode (the default) -- deletes stored video files off
    # disk and clears the stored image/GIF columns (crop_image_base64/preview_gif_base64) for rows
    # older than cutoff, but keeps the rows themselves and every text field on them (AI analysis,
    # embeddings) intact and searchable. Unlike purge_older_than, this never touches
    # sightings/visit_sightings at all -- those tables carry no media columns of their own, only
    # text.
    #
    # object_label (optional), same scoping decision as purge_older_than: only raw_events (a
    # single-type-per-row concept) are filtered by it -- visits (which can span multiple object
    # types in one row) are never touched at all when object_label is set, since there's no
    # single-type-safe way to decide a multi-type visit's own media belongs to just one purge.
    type_clause = "AND objects = %s" if object_label else ""
    type_params = [object_label] if object_label else []

    counts = {
        "raw_events_video_files": _execute(
            f"SELECT count(*)::int AS c FROM yard_stats.raw_events WHERE start_ts < %s AND video_path IS NOT NULL {type_clause}",
            [cutoff, *type_params], fetch=True,
        )[0]["c"],
        "raw_events_images": _execute(
            f"SELECT count(*)::int AS c FROM yard_stats.raw_events WHERE start_ts < %s AND crop_image_base64 IS NOT NULL {type_clause}",
            [cutoff, *type_params], fetch=True,
        )[0]["c"],
        "visits_video_files": 0 if object_label else _execute(
            "SELECT count(*)::int AS c FROM yard_stats.visits WHERE start_ts < %s AND video_path IS NOT NULL",
            (cutoff,), fetch=True,
        )[0]["c"],
        "visits_images_or_gifs": 0 if object_label else _execute(
            "SELECT count(*)::int AS c FROM yard_stats.visits WHERE start_ts < %s "
            "AND (crop_image_base64 IS NOT NULL OR preview_gif_base64 IS NOT NULL)",
            (cutoff,), fetch=True,
        )[0]["c"],
    }

    if execute:
        video_paths = [
            row["video_path"] for row in _execute(
                f"SELECT video_path FROM yard_stats.raw_events WHERE start_ts < %s AND video_path IS NOT NULL {type_clause}",
                [cutoff, *type_params], fetch=True,
            )
        ]
        if not object_label:
            video_paths += [
                row["video_path"] for row in _execute(
                    "SELECT video_path FROM yard_stats.visits WHERE start_ts < %s AND video_path IS NOT NULL",
                    (cutoff,), fetch=True,
                )
            ]
        deleted_files = _delete_video_files(video_paths)
        _execute(
            f"""
            UPDATE yard_stats.raw_events SET video_path = NULL, crop_image_base64 = NULL
            WHERE start_ts < %s AND (video_path IS NOT NULL OR crop_image_base64 IS NOT NULL) {type_clause}
            """,
            [cutoff, *type_params],
        )
        if not object_label:
            _execute(
                """
                UPDATE yard_stats.visits SET video_path = NULL, crop_image_base64 = NULL, preview_gif_base64 = NULL
                WHERE start_ts < %s
                  AND (video_path IS NOT NULL OR crop_image_base64 IS NOT NULL OR preview_gif_base64 IS NOT NULL)
                """,
                (cutoff,),
            )
        counts["video_files_deleted"] = deleted_files
        logger.info("Media-only purge executed (cutoff=%s, object_label=%s, counts=%s)", cutoff, object_label, counts)

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
    visit_id: int | None = None,
) -> tuple[str, list]:
    # Factored out of list_events so count_events can reuse the exact same filters (LIMIT/OFFSET-
    # free) rather than re-deriving them in a parallel function that could silently drift out of
    # sync with list_events' own filtering over time.
    clauses = []
    params: list = []
    join = ""
    if q and q.strip():
        # Free-text search across the AI analysis result, not raw_events itself -- only ever
        # matches rows that already have a sighting (i.e. ai_status already 'done'), so it
        # composes fine with has_media's default (those rows always have an image already, by the
        # same crop_status='done' invariant everything else here relies on).
        join = "LEFT JOIN yard_stats.sightings s ON s.raw_event_id = re.id"
        term = f"%{q.strip()}%"
        clauses.append("s.description ILIKE %s")
        params.append(term)
    if has_media and event_id is None and visit_id is None:
        # Default view: hide rows with neither a crop image nor a stored video (crop_status not
        # yet 'done' -- including the 'skipped' rows has_snapshot=false produces, which will never
        # get one) so the grid isn't full of cards with nothing to show. In practice video_path is
        # never set without crop_image_base64 already being set too (claim_video_batch only claims
        # crop_status='done' rows), so this is currently equivalent to crop-image-only, but checks
        # both so it stays correct if that invariant ever changes. Pass has_media=false to see
        # everything. Skipped entirely when event_id/visit_id is given -- searching for one
        # specific known event, or every event linked to one specific visit, should find them
        # regardless of whether they have media yet, same reasoning as event_id bypassing the time
        # window at the API layer.
        clauses.append("(re.crop_image_base64 IS NOT NULL OR re.video_path IS NOT NULL)")
    if event_id is not None:
        clauses.append("re.id = %s")
        params.append(event_id)
    if visit_id is not None:
        # Every raw_event linked to one specific visit -- the web UI's visit lightbox uses this to
        # show all connected det_ids alongside the visit-level alert analysis, not just the
        # deduped AI-analyzed representatives get_sightings_for_visit already returns.
        clauses.append("re.visit_id = %s")
        params.append(visit_id)
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
    visit_id: int | None = None,
) -> list:
    query, params = _build_events_query(
        object_type, camera, start, end, crop_status, ai_status, video_status, has_media, event_id, q, visit_id,
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
    visit_id: int | None = None,
) -> int:
    # Same filters as list_events, no LIMIT/OFFSET -- lets the web UI show "page X of Y" instead
    # of just "there might be more" (e.g. by comparing len(events) to limit).
    query, params = _build_events_query(
        object_type, camera, start, end, crop_status, ai_status, video_status, has_media, event_id, q, visit_id,
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
        # Matches a visit if ANY of its linked raw_events has a sighting whose AI analysis text
        # matches -- same ILIKE substring match as GET /events' own q. An EXISTS subquery against
        # a fresh raw_events/sightings join, not a condition on the `re` row already joined into
        # `linked` below -- a visit can group several distinct events (e.g. a car and a person),
        # and the match might come from either one, not necessarily the one row_number picks as
        # the representative, so this can't be a plain per-row filter without breaking
        # event_count/rn (which need every linked event, matching or not).
        term = f"%{q.strip()}%"
        clauses.append("""
        EXISTS (
            SELECT 1 FROM yard_stats.raw_events re2
            JOIN yard_stats.sightings s2 ON s2.raw_event_id = re2.id
            WHERE re2.visit_id = v.id AND s2.description ILIKE %s
        )
        """)
        params.append(term)

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


def get_sightings(
    camera: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    object_label: str | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list:
    # Replaces the former get_vehicle_sightings/get_person_sightings split -- one universal list,
    # optionally narrowed to a specific Frigate label (object_label) rather than a fixed type.
    # q is a free-text substring match across description -- the old vehicle-only plate_text
    # filter doesn't apply anymore now that plate text (if the prompt asked for it) just lives
    # inside description like everything else; q covers that and anything else equally.
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
    if object_label:
        clauses.append("s.object_label = %s")
        params.append(object_label)
    if q:
        clauses.append("s.description ILIKE %s")
        params.append(f"%{q}%")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    return _execute(
        f"""
        SELECT s.id, s.raw_event_id, re.camera, re.zone, re.start_ts, s.object_label, s.description
        FROM yard_stats.sightings s
        JOIN yard_stats.raw_events re ON re.id = s.raw_event_id
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
    total_sightings = _execute(
        """
        SELECT count(*)::int AS c FROM yard_stats.sightings s
        JOIN yard_stats.raw_events re ON re.id = s.raw_event_id
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
        "total_sightings": total_sightings,
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


def complete_sighting(
    raw_event_id: int,
    object_label: str | None,
    description: str | None,
    embedding: list[float] | None = None,
) -> int:
    # Insert + mark ai_status='done' in one transaction -- replaces the old Insert Vehicle/Person
    # Sighting + Mark Done pair of n8n Postgres nodes, closing the gap where a crash between the
    # two left the row stuck 'processing' until the next reap. One universal function now --
    # object_label is just data on the row (whatever raw_events.objects said), not a branch;
    # there's no vehicle-vs-person split anywhere in this function or the table it writes to.
    conn = get_conn()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO yard_stats.sightings (raw_event_id, object_label, description, embedding)
                VALUES (%s, %s, %s, %s::vector)
                RETURNING id
                """,
                (raw_event_id, object_label, description, _vector_literal(embedding)),
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
    # Cosine-distance ("<=>") ordered search across the one universal sightings table, filtered by
    # time range -- the agent resolves "last week"/"today" into concrete start/end itself (see
    # CLAUDE.md), this only ever ranks by semantic similarity *within* that already-resolved window,
    # same division of labor claim_ai_batch's own time filters already have. embedding IS NOT NULL
    # naturally excludes sightings from before this feature existed (or any run that didn't attach
    # one) -- they're simply not semantically searchable, not an error. object_types now filters by
    # the actual Frigate label (object_label) directly -- e.g. ["car", "dog"] -- rather than the
    # old pseudo-categories ("vehicle"/"person") the two-table split used to require.
    clauses = ["s.embedding IS NOT NULL"]
    params: list = [_vector_literal(embedding)]
    if start:
        clauses.append("re.start_ts >= %s")
        params.append(start)
    if end:
        clauses.append("re.start_ts <= %s")
        params.append(end)
    if object_types:
        clauses.append("s.object_label = ANY(%s)")
        params.append(object_types)
    where = " AND ".join(clauses)
    params.append(limit)
    return _execute(
        f"""
        SELECT s.id AS sighting_id, s.raw_event_id, re.start_ts, re.camera, re.objects,
               s.object_label, s.description, s.embedding <=> %s::vector AS distance
        FROM yard_stats.sightings s
        JOIN yard_stats.raw_events re ON re.id = s.raw_event_id
        WHERE {where}
        ORDER BY distance ASC
        LIMIT %s
        """,
        params,
        fetch=True,
    )


def semantic_search_combined(
    embedding: list[float],
    start: datetime | None = None,
    end: datetime | None = None,
    object_types: list[str] | None = None,
    limit: int = 20,
    source: str | None = None,
    max_distance: float | None = None,
    query_text: str | None = None,
    camera: str | None = None,
) -> list[dict]:
    # Web UI "Search" tab's own combined lookup -- unlike semantic_search_sightings above (the
    # n8n-facing endpoint's underlying function, left untouched so that existing contract never
    # shifts), this one can rank across BOTH sightings (events) and visit_sightings (alerts) at
    # once, since the web UI wants "anything relevant," not one flow specifically. source=None
    # (the default) searches both, unioned and re-ranked together by distance; source="events" or
    # "visits" searches just that one table -- skipping the other branch's JOIN/WHERE entirely
    # rather than fetching then discarding, since a caller that already knows which flow it wants
    # shouldn't pay for the other half of the query.
    #
    # Each row is tagged `kind` ("event"/"visit") and `id` (that row's raw_event_id or visit_id) so
    # the caller can route a click to the right lightbox -- the two id spaces are independent
    # sequences (a raw_event id and a visit id can collide), so `kind` is required to disambiguate,
    # never optional. Also carries has_image/has_video/has_preview_gif/ai_status (the same fields
    # EventSummary/VisitSummary already expose) so the web UI can open a result straight into the
    # existing lightbox with no follow-up fetch -- there's no GET /visits/{id} single-item endpoint
    # to fetch those for a visit-kind result on demand, and adding one just to re-fetch what this
    # query can already compute in the same pass would be a slower, more roundabout path to the
    # same information.
    vector_literal = _vector_literal(embedding)
    branches = []
    params: list = []

    if source in (None, "events"):
        clauses = ["s.embedding IS NOT NULL"]
        branch_params: list = [vector_literal]
        if start:
            clauses.append("re.start_ts >= %s")
            branch_params.append(start)
        if end:
            clauses.append("re.start_ts <= %s")
            branch_params.append(end)
        if object_types:
            clauses.append("s.object_label = ANY(%s)")
            branch_params.append(object_types)
        if camera:
            clauses.append("re.camera = %s")
            branch_params.append(camera)
        branches.append((
            f"""
            SELECT 'event' AS kind, re.id AS id, s.id AS sighting_id, re.start_ts, re.camera,
                   re.objects, s.object_label, s.description, s.embedding <=> %s::vector AS distance,
                   (re.crop_image_base64 IS NOT NULL) AS has_image,
                   (re.video_path IS NOT NULL) AS has_video,
                   false AS has_preview_gif, re.ai_status
            FROM yard_stats.sightings s
            JOIN yard_stats.raw_events re ON re.id = s.raw_event_id
            WHERE {' AND '.join(clauses)}
            """,
            branch_params,
        ))

    if source in (None, "visits"):
        # has_image mirrors _build_visits_query's own (has_thumb_crop OR event_has_image) --
        # falls back to the representative (earliest-linked) raw_event's own crop whenever the
        # visit's own thumb-crop grid isn't ready/enabled, same convention GET /visits/{id}/
        # thumbnail already applies server-side, so a search result never hides a thumbnail that's
        # actually fetchable.
        clauses = ["vs.embedding IS NOT NULL"]
        branch_params = [vector_literal]
        if start:
            clauses.append("v.start_ts >= %s")
            branch_params.append(start)
        if end:
            clauses.append("v.start_ts <= %s")
            branch_params.append(end)
        if object_types:
            clauses.append("vs.object_label = ANY(%s)")
            branch_params.append(object_types)
        if camera:
            clauses.append("v.cameras = %s")
            branch_params.append(camera)
        branches.append((
            f"""
            SELECT 'visit' AS kind, v.id AS id, vs.id AS sighting_id, v.start_ts, v.cameras AS camera,
                   v.objects, vs.object_label, vs.description, vs.embedding <=> %s::vector AS distance,
                   ((v.crop_image_base64 IS NOT NULL) OR COALESCE((
                       SELECT re.crop_image_base64 IS NOT NULL
                       FROM yard_stats.raw_events re
                       WHERE re.visit_id = v.id
                       ORDER BY re.start_ts ASC, re.id ASC
                       LIMIT 1
                   ), false)) AS has_image,
                   (v.video_path IS NOT NULL) AS has_video,
                   (v.preview_gif_base64 IS NOT NULL) AS has_preview_gif, v.alert_ai_status AS ai_status
            FROM yard_stats.visit_sightings vs
            JOIN yard_stats.visits v ON v.id = vs.visit_id
            WHERE {' AND '.join(clauses)}
            """,
            branch_params,
        ))

    for query, branch_params in branches:
        params.extend(branch_params)
    combined = " UNION ALL ".join(q for q, _ in branches)
    # max_distance filters on the computed `distance` column, which isn't addressable in a WHERE
    # clause within the same SELECT it's computed in (no correlated CTE per branch) -- wrapping the
    # union in a subquery is the simplest way to filter post-computation without duplicating the
    # `<=>` expression (and its params) into every branch's own WHERE clause.
    if max_distance is not None:
        # A pure distance cutoff can exclude a sighting whose description literally contains the
        # query word, just because the rest of that sentence is about something else (confirmed in
        # practice: "...an adult wearing a grey t-shirt... with a small dog nearby" for query "dog"
        # landed at distance 0.457, just outside a 0.45 cutoff, despite the literal word being
        # present) -- a general-purpose embedding model weights a sentence's dominant subject more
        # than a short trailing clause, so "the word is literally there" and "distance is below an
        # arbitrary threshold" will never fully agree. This fallback guarantees a literal word match
        # is never hidden by the cutoff, regardless of embedding geometry -- WHOLE-WORD (Postgres's
        # `~*` case-insensitive regex with `\y` word-boundary anchors), not a plain ILIKE substring:
        # confirmed live that a plain `ILIKE '%cat%'` fallback (the original implementation) matched
        # "indi-CAT-ion"/"lo-CAT-ion" for query "cat", returning 24 completely unrelated results
        # (every one of them already past the distance cutoff on its own merits) with nothing
        # actually about a cat anywhere in the dataset. re.escape keeps the caller's free text safe
        # to embed inside the regex pattern (it's still passed as a bound param, never concatenated
        # into the SQL string, so this is about correct regex semantics, not injection).
        if query_text and query_text.strip():
            sql = (
                f"SELECT * FROM ({combined}) AS combined "
                f"WHERE distance <= %s OR description ~* %s ORDER BY distance ASC LIMIT %s"
            )
            params.append(max_distance)
            params.append(r"\y" + re.escape(query_text.strip()) + r"\y")
        else:
            sql = f"SELECT * FROM ({combined}) AS combined WHERE distance <= %s ORDER BY distance ASC LIMIT %s"
            params.append(max_distance)
    else:
        sql = f"{combined} ORDER BY distance ASC LIMIT %s"
    params.append(limit)
    return _execute(
        sql,
        params,
        fetch=True,
    )


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


def reap_stale_alert_ai_processing(stale_minutes: int) -> None:
    _execute(
        """
        UPDATE yard_stats.visits
        SET alert_ai_status = 'retry', alert_ai_status_changed_at = now()
        WHERE alert_ai_status = 'processing'
          AND alert_ai_status_changed_at < now() - (%s * interval '1 minute')
        """,
        (stale_minutes,),
    )


def count_alert_ai_in_progress() -> int:
    rows = _execute(
        "SELECT count(*)::int AS c FROM yard_stats.visits WHERE alert_ai_status = 'processing'",
        fetch=True,
    )
    return rows[0]["c"] if rows else 0


def claim_alert_ai_batch(
    object_types: list[str],
    parallel_limit: int,
    stale_minutes: int,
    max_age_hours: float | None = None,
) -> list:
    # Sixth queue stage (AI_ALERTS_ENABLED), claiming from visits.alert_ai_status instead of
    # raw_events.ai_status -- mirrors claim_ai_batch's reap-stale + count-in-progress + CTE-claim
    # shape (same FOR UPDATE SKIP LOCKED reason). Unlike claim_ai_batch, this only ever claims a
    # visit whose own composite grid is ready (thumb_crop_status='done') -- a visit with
    # VISIT_THUMB_CROP_ENABLED off (or still building) simply has nothing this stage can analyze
    # yet, so it stays alert_ai_status='new' indefinitely, same "nothing to do" treatment as an
    # unmapped object type gets. object_types is matched against the visit's own *representative*
    # event's objects (the single event the grid was actually framed around via its region/box),
    # not visits.objects (a comma-joined list that can span several distinct types per visit,
    # e.g. "car,person" -- see record_visit) -- the grid is inherently single-object-framed, so the
    # representative event's own label is what determines which profiles.yaml prompt applies.
    reap_stale_alert_ai_processing(stale_minutes)
    in_progress = count_alert_ai_in_progress()
    capacity = max(0, parallel_limit - in_progress)
    if capacity == 0:
        return []

    age_clause = ""
    params: list = [object_types]
    if max_age_hours is not None:
        age_clause = "AND v.start_ts >= now() - (%s * interval '1 hour')"
        params.append(max_age_hours)
    params.append(capacity)

    return _execute(
        f"""
        WITH claimable AS (
            SELECT v.id FROM yard_stats.visits v
            JOIN LATERAL (
                SELECT re.objects FROM yard_stats.raw_events re
                WHERE re.visit_id = v.id
                ORDER BY re.start_ts ASC, re.id ASC
                LIMIT 1
            ) rep ON true
            WHERE v.alert_ai_status IN ('new', 'retry')
              AND v.thumb_crop_status = 'done'
              AND rep.objects = ANY(%s)
              {age_clause}
            ORDER BY v.start_ts DESC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE yard_stats.visits
        SET alert_ai_status = 'processing', alert_ai_status_changed_at = now()
        FROM claimable
        JOIN LATERAL (
            SELECT re.objects, re.det_id, re.id AS representative_event_id
            FROM yard_stats.raw_events re
            WHERE re.visit_id = claimable.id
            ORDER BY re.start_ts ASC, re.id ASC
            LIMIT 1
        ) rep ON true
        WHERE yard_stats.visits.id = claimable.id
        RETURNING yard_stats.visits.*, rep.objects, rep.det_id, rep.representative_event_id
        """,
        params,
        fetch=True,
    )


def complete_visit_sighting(
    visit_id: int,
    object_label: str | None,
    description: str | None,
    embedding: list[float] | None = None,
) -> int:
    # Same insert-plus-mark-done-in-one-transaction shape as complete_sighting, just against
    # visit_sightings/visits.alert_ai_status instead of sightings/raw_events.ai_status.
    conn = get_conn()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO yard_stats.visit_sightings (visit_id, object_label, description, embedding)
                VALUES (%s, %s, %s, %s::vector)
                RETURNING id
                """,
                (visit_id, object_label, description, _vector_literal(embedding)),
            )
            sighting_id = cur.fetchone()["id"]
            cur.execute(
                "UPDATE yard_stats.visits SET alert_ai_status = 'done', alert_ai_status_changed_at = now() WHERE id = %s",
                (visit_id,),
            )
        conn.commit()
        return sighting_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def fail_alert_ai_event(visit_id: int, max_attempts: int) -> dict:
    # Same retry-or-fail-with-cap CASE logic as fail_ai_event, against visits.alert_ai_status.
    rows = _execute(
        """
        UPDATE yard_stats.visits
        SET alert_ai_attempt_count = alert_ai_attempt_count + 1,
            alert_ai_status = CASE WHEN alert_ai_attempt_count + 1 >= %s THEN 'failed' ELSE 'retry' END,
            alert_ai_status_changed_at = now()
        WHERE id = %s
        RETURNING alert_ai_status, alert_ai_attempt_count
        """,
        (max_attempts, visit_id),
        fetch=True,
    )
    return rows[0]


def get_visit_alert_sighting(visit_id: int) -> dict | None:
    # The visit's own alert-stage (composite grid) analysis, if AI_ALERTS_ENABLED has produced
    # one -- used by GET /visits/{id}/sightings so the web UI's Visits-tab lightbox can prefer
    # this over the representative event's own per-event sighting (see get_sightings_for_visit),
    # falling back to that when this is null (alert stage off, or not finished yet for this visit).
    rows = _execute(
        """
        SELECT id, visit_id, object_label, description
        FROM yard_stats.visit_sightings WHERE visit_id = %s
        ORDER BY id DESC LIMIT 1
        """,
        (visit_id,), fetch=True,
    )
    return rows[0] if rows else None


def get_report_data(
    start: datetime, end: datetime, source: str = "events", include_preview: str = "gif",
    object_label: str | None = None,
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
    # visit_id is included so report.py can group a visit's sightings into one combined alert
    # entry (source="visits" only -- always NULL under source="events", where there's no grouping
    # concept and every sighting is its own entry). One universal query now -- object_label tells
    # report.py what kind of sighting each row is, there's no separate vehicles/persons split to
    # union or render differently.
    #
    # object_label param (optional) restricts the report to one Frigate object type -- e.g. a
    # "cars only" or "dogs only" report alongside the default "every type" report. Under
    # source="visits" this is a pure filter over which sightings are included in each visit's
    # group, same as any other WHERE clause here -- a visit spanning a car and a person, filtered
    # to "person", still groups by visit_id as normal, just with only the person sighting present.
    params: list = [start, end]
    label_clause = ""
    if object_label:
        label_clause = "AND s.object_label = %s"
        params.append(object_label)
    sightings = _execute(
        f"""
        SELECT re.id AS raw_event_id, re.visit_id, re.camera, re.zone, re.start_ts,
               {crop_image_expr} AS crop_image_base64, {gif_image_expr} AS preview_gif_base64,
               s.object_label, s.description
        FROM yard_stats.sightings s
        JOIN yard_stats.raw_events re ON re.id = s.raw_event_id
        {visit_join}
        WHERE s.created_at >= %s AND s.created_at <= %s
        {visit_clause}
        {label_clause}
        ORDER BY re.start_ts DESC
        """,
        params, fetch=True,
    )
    return {"sightings": sightings}
