"""PostgreSQL access layer (psycopg 3).

* every statement the application sends is echoed to the terminal (termlog) with its
  bound parameters expanded and its duration - and PostgreSQL itself logs the same
  statements on the server side (docker logs -f logistics-db)
* each connection carries an `application_name` (web-api, state-updater, ...) so the
  server log and pg_stat_activity show WHICH component sent a statement
* write transactions set two transaction-local settings, app.actor and app.trace_id;
  the audit trigger inside the database copies them into infra.audit_log
* SQL in this code base is written with `?` placeholders; they are translated to psycopg's
  `%s` here (only when parameters are passed)
"""
from __future__ import annotations

import queue
import re
import threading
import time
from contextlib import contextmanager

import psycopg
from psycopg import errors as pgerr
from psycopg.adapt import Loader

from . import config, termlog

Error = psycopg.Error
IntegrityError = pgerr.IntegrityError           # SQLSTATE class 23: unique / FK / check / not-null (+ our rule triggers)
ForeignKeyViolation = pgerr.ForeignKeyViolation
UniqueViolation = pgerr.UniqueViolation
OperationalError = psycopg.OperationalError


# ---------------------------------------------------------------------------
#  row + type adaptation
# ---------------------------------------------------------------------------
class Row(dict):
    """A dict that also supports row[0] (like sqlite3.Row)."""
    __slots__ = ()

    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return super().__getitem__(k)


def row_factory(cursor):
    cols = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(zip(cols, values))


class TimestamptzLoader(Loader):
    """timestamptz -> 'YYYY-MM-DDTHH:MM:SSZ' (the session time zone is UTC)."""

    def load(self, data):
        s = bytes(data).decode()
        if s[0] in "-i" or len(s) < 19:
            return s
        return s[:10] + "T" + s[11:19] + "Z"


class NumericLoader(Loader):
    def load(self, data):
        return float(bytes(data).decode())


_NAMED = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


def translate(sql: str, params):
    """`?` / `:name` placeholders -> psycopg's `%s` / `%(name)s`."""
    if params is None:
        return sql
    if isinstance(params, dict):
        return _NAMED.sub(r"%(\1)s", sql.replace("%", "%%"))
    return sql.replace("%", "%%").replace("?", "%s")


def _literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (dict, list)):
        import json
        v = json.dumps(v, separators=(",", ":"))
    return "'" + str(v).replace("'", "''") + "'"


def expand(sql: str, params) -> str:
    """Runnable-looking SQL for the terminal log."""
    try:
        if params is None or params == () or params == []:
            return " ".join(sql.split())
        if isinstance(params, dict):
            out = _NAMED.sub(lambda m: _literal(params.get(m.group(1))), sql)
        else:
            it = iter(params)
            out = re.sub(r"\?", lambda m: _literal(next(it)), sql)
        return " ".join(out.split())
    except Exception:
        return " ".join(sql.split())


# ---------------------------------------------------------------------------
#  connection
# ---------------------------------------------------------------------------
class Connection(psycopg.Connection):
    actor = "app"
    trace_id = None
    logging = True

    def execute(self, query, params=None, *, prepare=None, binary=False):      # type: ignore[override]
        sql = translate(query, params)
        t0 = time.perf_counter()
        try:
            cur = super().execute(sql, params, prepare=prepare, binary=binary)
        except Exception as e:
            if self.logging:
                termlog.sql(self.actor, expand(query, params), (time.perf_counter() - t0) * 1000)
                if not isinstance(e, ForeignKeyViolation):        # an FK miss is the normal "dependency not ready" retry
                    termlog.warn(f"{type(e).__name__}: {str(e).splitlines()[0]}", self.actor)
            raise
        if self.logging:
            termlog.sql(self.actor, expand(query, params), (time.perf_counter() - t0) * 1000, cur.rowcount)
        return cur


def dsn(user: str | None = None, password: str | None = None, dbname: str | None = None) -> dict:
    return dict(host=config.PG_HOST, port=config.PG_PORT, dbname=dbname or config.PG_DB,
                user=user or config.PG_APP_USER, password=password or config.PG_APP_PASSWORD)


def connect(actor: str = "app", *, user: str | None = None, password: str | None = None,
            dbname: str | None = None, log: bool = True) -> Connection:
    conn = Connection.connect(**dsn(user, password, dbname), autocommit=True, row_factory=row_factory,
                              application_name=actor, prepare_threshold=None, connect_timeout=10)
    conn.adapters.register_loader("timestamptz", TimestamptzLoader)
    conn.adapters.register_loader("numeric", NumericLoader)
    conn.actor, conn.logging = actor, log
    conn.execute("SET TIME ZONE 'UTC'")
    return conn


@contextmanager
def write_tx(conn: Connection, actor: str, trace_id: str | None = None):
    """One atomic write transaction attributed to `actor` / `trace_id` in the audit log."""
    conn.actor, conn.trace_id = actor, trace_id
    conn.execute("BEGIN")
    try:
        conn.execute("SELECT set_config('app.actor', ?, true), set_config('app.trace_id', ?, true)",
                     (actor, trace_id or ""))
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


class Pool:
    """A tiny thread-safe connection pool for the web server threads."""

    def __init__(self, actor: str = "web-api", size: int = 8):
        self.actor, self.size = actor, size
        self._q: queue.LifoQueue = queue.LifoQueue()
        self._made = 0
        self._lock = threading.Lock()

    @contextmanager
    def connection(self):
        conn = None
        try:
            conn = self._q.get_nowait()
            if conn.closed:
                conn = None
        except queue.Empty:
            pass
        if conn is None:
            with self._lock:
                self._made += 1
            conn = connect(self.actor)
        try:
            yield conn
        finally:
            try:
                if conn.closed or conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                    conn.close()                              # never return a connection in the middle of a transaction
                elif self._q.qsize() < self.size:
                    self._q.put(conn)
                else:
                    conn.close()
            except Exception:
                pass


POOL = Pool()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
