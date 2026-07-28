"""Regression test for a real production bug: record_visit/complete_sighting/
complete_visit_summary each temporarily flip the single shared global connection's autocommit
off to wrap two statements in one transaction. With multiple worker threads (mqtt_ingest,
ai_worker, visit_summary_worker) sharing that one connection and no locking, two threads doing
this at once raced -- confirmed live in production:

    psycopg2.ProgrammingError: set_session cannot be used inside a transaction

(thread A's autocommit=False left the connection mid-transaction; thread B's own attempt to set
conn.autocommit = False required an idle connection and raised). Fixed with a module-level
threading.Lock (db._conn_lock) serializing every use of the shared connection, in get_conn/
_execute/record_visit/complete_sighting/complete_visit_summary.

Pure unit tests -- a fake connection object stands in for psycopg2, no real Postgres needed.
"""
import os
import threading
import time

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import db  # noqa: E402


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params=None):
        # Deliberately racy: check-sleep-set-sleep-clear, with no lock of its own. If two threads
        # are ever inside a _FakeConn's critical section concurrently, this reliably catches it --
        # the sleep between check and clear gives a real overlap a wide window to land in.
        if self._conn.busy:
            self._conn.violations += 1
        self._conn.busy = True
        time.sleep(0.005)
        self._conn.busy = False
        self._conn.executed += 1

    def fetchone(self):
        return {"id": self._conn.executed}


class _FakeConn:
    def __init__(self):
        self.autocommit = True
        self.busy = False
        self.violations = 0
        self.executed = 0

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def _patch_conn(monkeypatch):
    fake = _FakeConn()
    monkeypatch.setattr(db, "get_conn", lambda: fake)
    return fake


def test_complete_sighting_serializes_against_concurrent_complete_sighting(monkeypatch):
    fake = _patch_conn(monkeypatch)
    errors = []

    def worker(event_id):
        try:
            db.complete_sighting(event_id, "car", "a description")
        except Exception as exc:  # pragma: no cover - only hit on a real regression
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert fake.violations == 0


def test_record_visit_and_complete_sighting_serialize_against_each_other(monkeypatch):
    fake = _patch_conn(monkeypatch)
    monkeypatch.setattr(db, "_get_representative_object_label_for_det_ids", lambda det_ids: "car")
    errors = []

    def run_record_visit():
        review = {
            "zone": "z", "objects": "car", "start_time": 0, "end_time": 1,
            "camera": "outside", "det_ids": ["a", "b"], "thumb_time": None,
        }
        try:
            for _ in range(3):
                db.record_visit(review, profile=None)
        except Exception as exc:  # pragma: no cover - only hit on a real regression
            errors.append(exc)

    def run_complete_sighting():
        try:
            for i in range(3):
                db.complete_sighting(i, "car", "a description")
        except Exception as exc:  # pragma: no cover - only hit on a real regression
            errors.append(exc)

    threads = [threading.Thread(target=run_record_visit), threading.Thread(target=run_complete_sighting)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert fake.violations == 0


def test_execute_does_not_interleave_with_complete_sighting(monkeypatch):
    fake = _patch_conn(monkeypatch)
    errors = []

    def run_complete_sighting():
        try:
            for i in range(5):
                db.complete_sighting(i, "car", "a description")
        except Exception as exc:  # pragma: no cover - only hit on a real regression
            errors.append(exc)

    def run_plain_execute():
        try:
            for _ in range(5):
                db._execute("SELECT 1")
        except Exception as exc:  # pragma: no cover - only hit on a real regression
            errors.append(exc)

    threads = [threading.Thread(target=run_complete_sighting), threading.Thread(target=run_plain_execute)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert fake.violations == 0
