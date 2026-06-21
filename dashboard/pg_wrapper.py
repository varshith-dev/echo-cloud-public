import psycopg2
import psycopg2.extras
from psycopg2 import pool
import re
import threading
import time
import logging

logger = logging.getLogger(__name__)

_pool = None
_pool_lock = threading.Lock()
_dsn_cached = None

# Per-request connection tracking — stores all active PgConnection objects
# for the current thread so teardown can reclaim any that weren't closed
_thread_conns = threading.local()

# Pool settings
MIN_CONN = 2
MAX_CONN = 40   # raised from 20 — handles burst traffic


def _get_pool(dsn=None):
    global _pool, _dsn_cached
    with _pool_lock:
        if _pool is None:
            if dsn is None:
                dsn = _dsn_cached
            _dsn_cached = dsn
            _pool = psycopg2.pool.ThreadedConnectionPool(MIN_CONN, MAX_CONN, dsn)
            logger.info(f"[pg_wrapper] Pool created (min={MIN_CONN}, max={MAX_CONN})")
        return _pool


def _acquire(retries=5, wait=0.3):
    """Get a raw psycopg2 connection, retrying if the pool is momentarily full."""
    p = _get_pool()
    for attempt in range(retries):
        try:
            return p.getconn()
        except psycopg2.pool.PoolError:
            if attempt < retries - 1:
                logger.warning(f"[pg_wrapper] Pool exhausted — retry {attempt+1}/{retries} in {wait:.2f}s")
                time.sleep(wait)
                wait *= 1.5
            else:
                logger.error("[pg_wrapper] Pool still exhausted — resetting pool")
                _reset_pool()
                return _get_pool().getconn()


def _reset_pool():
    """Emergency: destroy and recreate the pool."""
    global _pool
    with _pool_lock:
        try:
            if _pool:
                _pool.closeall()
        except Exception:
            pass
        _pool = psycopg2.pool.ThreadedConnectionPool(MIN_CONN, MAX_CONN, _dsn_cached)
        logger.info("[pg_wrapper] Pool reset successfully")


def close_thread_connections():
    """
    Called by Flask teardown_request to reclaim any connections that a route
    handler opened but did not explicitly close (e.g. due to an exception).
    """
    conns = getattr(_thread_conns, 'active', None)
    if conns:
        leaked = [c for c in conns if not c._closed]
        if leaked:
            logger.warning(f"[pg_wrapper] Teardown reclaiming {len(leaked)} leaked connection(s)")
            for c in leaked:
                c.close()
        _thread_conns.active = []


def _register(conn):
    """Track this connection on the current thread."""
    if not hasattr(_thread_conns, 'active') or _thread_conns.active is None:
        _thread_conns.active = []
    _thread_conns.active.append(conn)


class PgConnection:
    def __init__(self, raw_conn):
        self.conn = raw_conn
        self.row_factory = None
        self._closed = False
        _register(self)

    def cursor(self):
        if self.row_factory:
            return PgCursor(self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor))
        return PgCursor(self.conn.cursor())

    def commit(self):
        self.conn.commit()

    def rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    def close(self):
        """Return connection to pool — safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        p = _get_pool()
        try:
            # Roll back any open transaction before returning
            try:
                if self.conn.status == psycopg2.extensions.STATUS_IN_TRANSACTION:
                    self.conn.rollback()
            except Exception:
                pass
            p.putconn(self.conn)
        except Exception as e:
            logger.error(f"[pg_wrapper] Error returning connection to pool: {e}")
            try:
                p.putconn(self.conn, close=True)
            except Exception:
                pass

    # Context manager support — guarantees connection is always returned
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        self.close()
        return False

    def __del__(self):
        """Last-resort safety net if GC collects before teardown."""
        if not self._closed:
            self.close()


class PgCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    @property
    def lastrowid(self):
        try:
            cur = self.cursor.connection.cursor()
            cur.execute("SELECT LASTVAL()")
            val = cur.fetchone()[0]
            cur.close()
            return val
        except Exception:
            return None

    def execute(self, query, params=None):
        query = self._convert_query(query)
        if params:
            self.cursor.execute(query, params)
        else:
            self.cursor.execute(query)

    def fetchall(self):
        return self.cursor.fetchall()

    def fetchone(self):
        return self.cursor.fetchone()

    def close(self):
        try:
            self.cursor.close()
        except Exception:
            pass

    def _convert_query(self, query):
        parts = re.split(r"('[^']*'|\"[^\"]*\")", query)
        for i in range(0, len(parts), 2):
            parts[i] = parts[i].replace('?', '%s')
            parts[i] = parts[i].replace('INSERT OR IGNORE', 'INSERT')
            parts[i] = parts[i].replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY')
            parts[i] = parts[i].replace('DATETIME', 'TIMESTAMP')
        q = ''.join(parts)
        if re.search(r'(?i)\bINSERT\s+OR\s+REPLACE\s+INTO\s+system_config\b', q):
            q = re.sub(r'(?i)\bINSERT\s+OR\s+REPLACE\s+INTO\s+system_config\b', 'INSERT INTO system_config', q)
            q += ' ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value'
        elif re.search(r'(?i)\bINSERT\s+OR\s+REPLACE\s+INTO\s+support_poll_votes\b', q):
            q = re.sub(r'(?i)\bINSERT\s+OR\s+REPLACE\s+INTO\s+support_poll_votes\b', 'INSERT INTO support_poll_votes', q)
            q += ' ON CONFLICT (poll_id, voter_id) DO UPDATE SET selected_option = EXCLUDED.selected_option'
        return q


class RowProxy:
    pass


def connect(dsn):
    """Drop-in replacement for sqlite3.connect()."""
    _get_pool(dsn)
    raw = _acquire()
    return PgConnection(raw_conn=raw)


Row = RowProxy
IntegrityError = psycopg2.IntegrityError
