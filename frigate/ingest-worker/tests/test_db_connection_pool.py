"""Concurrency tests for db.py's connection pool.

Supersedes tests/test_db_connection_lock.py. That file guarded a real production bug -- a single
shared connection whose autocommit flag three functions (record_visit / complete_sighting /
complete_visit_summary) temporarily flipped, letting concurrent threads raise
"set_session cannot be used inside a transaction" or silently fold statements into each other's
transactions. It was fixed first with a global lock, which was correct but serialized every
database call in the process.

The pool fixes it structurally instead: no connection is ever shared between threads in the first
place, so there is no cross-thread transaction state to corrupt. These tests assert exactly that
(no two concurrent callers ever hold the same connection), plus the properties the pool has to keep
for that guarantee to be worth anything: connections come back clean, autocommit never leaks
between unrelated callers, and a burst larger than the pool waits rather than failing.

Pure unit tests -- a fake pool stands in for psycopg2, no real Postgres needed. The integration
suite covers the real thing.
"""
import os
import threading
import time

os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("FRIGATE_API_BASE", "http://frigate.test:5000")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params=None):
        # Records which connection served this statement, and asserts the connection isn't already
        # mid-statement for a different thread (which is what a shared connection would allow).
        self._conn.note_use()
        time.sleep(0.002)

    def fetchone(self):
        return {"id": 1}

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, conn_id):
        self.conn_id = conn_id
        self.autocommit = True
        self.in_use_by = None
        self.overlaps = 0
        self.commits = 0
        self.rollbacks = 0
        self.autocommit_history = []

    def note_use(self):
        me = threading.current_thread().name
        if self.in_use_by is not None and self.in_use_by != me:
            self.overlaps += 1
        self.in_use_by = me
        time.sleep(0.002)
        self.in_use_by = None

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FakePool:
    """Mimics psycopg2's ThreadedConnectionPool, including raising when exhausted."""

    def __init__(self, maxconn):
        self.maxconn = maxconn
        self._free = [_FakeConn(i) for i in range(maxconn)]
        self._lock = threading.Lock()
        self.all_conns = list(self._free)
        self.checked_out = set()
        self.max_concurrent = 0

    def getconn(self):
        import psycopg2.pool
        with self._lock:
            if not self._free:
                raise psycopg2.pool.PoolError("connection pool exhausted")
            conn = self._free.pop()
            self.checked_out.add(conn.conn_id)
            self.max_concurrent = max(self.max_concurrent, len(self.checked_out))
            return conn

    def putconn(self, conn):
        with self._lock:
            self.checked_out.discard(conn.conn_id)
            self._free.append(conn)


@pytest.fixture
def fake_pool(monkeypatch):
    pool = _FakePool(maxconn=5)
    monkeypatch.setattr(db, "_get_pool", lambda: pool)
    return pool


def _run_threads(targets):
    errors = []

    def wrapped(fn):
        def inner():
            try:
                fn()
            except Exception as exc:  # pragma: no cover - only on a real regression
                errors.append(exc)
        return inner

    threads = [threading.Thread(target=wrapped(fn), name=f"t{i}") for i, fn in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_transactions_never_share_a_connection(fake_pool):
    # The exact scenario that broke in production: record_visit and complete_sighting running at
    # the same time, each wanting its own multi-statement transaction.
    def do_sighting():
        for i in range(5):
            db.complete_sighting(i, "car", "a description")

    def do_visit():
        review = {
            "zone": "z", "objects": "car", "start_time": 0, "end_time": 1,
            "camera": "outside", "det_ids": ["a"], "thumb_time": None,
        }
        for _ in range(5):
            db.record_visit(review, profile=None)

    def do_summary():
        for i in range(5):
            db.complete_visit_summary(i, "a summary")

    errors = _run_threads([do_sighting, do_visit, do_summary, do_sighting])
    assert errors == []
    assert all(c.overlaps == 0 for c in fake_pool.all_conns)


def test_plain_queries_and_transactions_do_not_interleave_on_one_connection(fake_pool):
    def do_transactions():
        for i in range(5):
            db.complete_sighting(i, "car", "a description")

    def do_plain_queries():
        for _ in range(10):
            db._execute("SELECT 1")

    errors = _run_threads([do_transactions, do_plain_queries, do_plain_queries])
    assert errors == []
    assert all(c.overlaps == 0 for c in fake_pool.all_conns)


def test_every_connection_is_returned_to_the_pool(fake_pool):
    for i in range(20):
        db._execute("SELECT 1")
        db.complete_sighting(i, "car", "d")
    assert fake_pool.checked_out == set()


def test_connection_is_returned_even_when_the_query_raises(fake_pool):
    def boom(*_args, **_kwargs):
        raise RuntimeError("query blew up")

    original = _FakeCursor.execute
    try:
        _FakeCursor.execute = boom
        with pytest.raises(RuntimeError):
            db._execute("SELECT 1")
    finally:
        _FakeCursor.execute = original
    # A leaked connection here would slowly starve the pool -- the exact failure mode that makes
    # pool bugs show up hours later as "everything hangs" rather than immediately.
    assert fake_pool.checked_out == set()


def test_failed_transaction_rolls_back_and_returns_the_connection(fake_pool):
    original = _FakeCursor.execute
    calls = {"n": 0}

    def fail_on_second(self, query, params=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("mid-transaction failure")
        return original(self, query, params)

    try:
        _FakeCursor.execute = fail_on_second
        with pytest.raises(RuntimeError):
            db.complete_sighting(1, "car", "d")
    finally:
        _FakeCursor.execute = original
    assert fake_pool.checked_out == set()
    assert sum(c.rollbacks for c in fake_pool.all_conns) == 1


def test_autocommit_does_not_leak_from_a_transaction_to_the_next_caller(monkeypatch):
    # A single-connection pool forces every call onto the SAME connection, so a leaked
    # autocommit=False from the transaction would be directly visible to the next plain caller --
    # which would silently run it inside a transaction that nothing ever commits.
    single = _FakePool(maxconn=1)
    monkeypatch.setattr(db, "_get_pool", lambda: single)
    conn = single.all_conns[0]

    db.complete_sighting(1, "car", "d")
    assert conn.autocommit is False  # the transaction set it, and nothing resets it on release

    db._execute("SELECT 1")
    # _checkout_connection sets autocommit explicitly on every checkout, so the plain query runs
    # with it back on rather than inheriting the transaction's setting.
    assert conn.autocommit is True

    db.complete_sighting(2, "car", "d")
    assert conn.autocommit is False  # and back off again for the next transaction


def test_burst_larger_than_the_pool_waits_instead_of_failing(fake_pool, monkeypatch):
    # 5-connection pool, 12 concurrent callers -- mirrors a web-UI grid page requesting far more
    # thumbnails at once than the pool has connections. psycopg2 would raise PoolError immediately;
    # _checkout_connection waits instead.
    monkeypatch.setattr(config, "POSTGRES_POOL_WAIT_SECONDS", 10)
    errors = _run_threads([lambda: db._execute("SELECT 1") for _ in range(12)])
    assert errors == []
    assert fake_pool.max_concurrent <= fake_pool.maxconn


def test_pool_exhaustion_eventually_raises_rather_than_hanging_forever(monkeypatch):
    import psycopg2.pool

    class _AlwaysExhausted:
        def getconn(self):
            raise psycopg2.pool.PoolError("connection pool exhausted")

        def putconn(self, conn):  # pragma: no cover - never reached
            pass

    monkeypatch.setattr(db, "_get_pool", lambda: _AlwaysExhausted())
    monkeypatch.setattr(config, "POSTGRES_POOL_WAIT_SECONDS", 0.2)
    started = time.monotonic()
    with pytest.raises(psycopg2.pool.PoolError):
        db._execute("SELECT 1")
    assert time.monotonic() - started >= 0.2
