"""Persistence. One module owns the schema. SQLite by default (tests, local); Postgres when DATABASE_URL is set (Render free)."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import get_settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,            -- 'a1:lead:<id>' or 'a2:run:<id>'
  agent TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL DEFAULT 1,
  error TEXT,
  created_at TEXT NOT NULL
);

-- ============ Assignment 1 ============
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,                 -- instagram | whatsapp | email
  sender_id TEXT NOT NULL,               -- provider-scoped id (IGSID / wa_id / email address)
  sender_name TEXT,
  thread_ref TEXT,                       -- e.g. Gmail threadId / last RFC Message-ID
  classification TEXT NOT NULL DEFAULT 'new',     -- new | needs_more_info | qualified | not_qualified
  state TEXT NOT NULL DEFAULT 'collecting',       -- collecting | offering_slots | awaiting_confirmation | booked | closed
  assigned_rep TEXT,
  automation_enabled INTEGER NOT NULL DEFAULT 1,
  fields_json TEXT NOT NULL DEFAULT '{}',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  rule_trace_json TEXT NOT NULL DEFAULT '[]',
  offered_slots_json TEXT NOT NULL DEFAULT '[]',
  pending_slot TEXT,
  last_inbound_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(channel, sender_id)
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL REFERENCES leads(id),
  direction TEXT NOT NULL,               -- in | out
  author TEXT NOT NULL,                  -- lead | bot | human
  text TEXT NOT NULL,
  provider_msg_id TEXT,
  delivery TEXT,                         -- sent | failed | skipped | n/a
  delivery_error TEXT,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_msg_provider ON messages(lead_id, provider_msg_id) WHERE provider_msg_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS seen_events (       -- webhook / poll dedup across leads
  channel TEXT NOT NULL, provider_msg_id TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(channel, provider_msg_id)
);

CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER,
  level TEXT NOT NULL DEFAULT 'info',    -- info | warn | error
  kind TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL REFERENCES leads(id),
  rep_id TEXT NOT NULL,
  start_iso TEXT NOT NULL,
  end_iso TEXT NOT NULL,
  event_id TEXT NOT NULL UNIQUE,         -- deterministic: hash(lead, rep, start)
  status TEXT NOT NULL,                  -- booking | confirmed | uncertain | failed | slot_taken
  html_link TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_health (
  channel TEXT PRIMARY KEY,
  status TEXT NOT NULL,                  -- unconfigured | ok | error | expired
  detail TEXT,
  last_inbound_at TEXT,
  last_outbound_at TEXT,
  updated_at TEXT NOT NULL
);

-- ============ Assignment 2 ============
CREATE TABLE IF NOT EXISTS a2_sessions (id TEXT PRIMARY KEY, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS a2_runs (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES a2_sessions(id),
  transcript TEXT NOT NULL,
  meeting_date TEXT NOT NULL,
  rules_json TEXT NOT NULL,
  context_version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,                  -- pending | running | done | needs_human | failed
  fault_json TEXT NOT NULL DEFAULT '{}',
  final_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS a2_fact_overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES a2_runs(id),
  context_version INTEGER NOT NULL,      -- version this override created
  fact_id TEXT NOT NULL,
  patch_json TEXT NOT NULL,              -- {"text":..., "owner":..., "deadline_text":...} (any subset)
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS a2_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES a2_runs(id),
  kind TEXT NOT NULL,                    -- info | reuse | rerun | fault | warn | error
  text TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS a2_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES a2_runs(id),
  agent TEXT NOT NULL,                   -- intake | planner | review
  round INTEGER NOT NULL DEFAULT 0,
  context_version INTEGER NOT NULL,
  input_hash TEXT NOT NULL,
  status TEXT NOT NULL,                  -- running | done | failed | superseded
  input_json TEXT NOT NULL,
  output_json TEXT,
  error TEXT,
  simulated_fault INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 1,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_a2_steps_run ON a2_steps(run_id, id);

CREATE TABLE IF NOT EXISTS a2_handoffs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES a2_runs(id),
  seq INTEGER NOT NULL,
  from_agent TEXT NOT NULL,
  to_agent TEXT NOT NULL,
  kind TEXT NOT NULL,                    -- handoff | feedback | escalation
  context_version INTEGER NOT NULL,
  valid INTEGER NOT NULL DEFAULT 1,
  validation_error TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialised: set[str] = set()

# Tables whose primary key is a generated integer `id` (needed to emulate sqlite's cursor.lastrowid on Postgres).
_ID_TABLES = {"llm_calls", "leads", "messages", "actions", "bookings", "a2_fact_overrides", "a2_events", "a2_steps", "a2_handoffs"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_pg() -> bool:
    return bool(get_settings().database_url)


# ----------------------------------------------------------------------------- SQLite
def _connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ----------------------------------------------------------------------------- Postgres
def pg_schema() -> str:
    """Derive the Postgres DDL from the single SQLite-flavoured SCHEMA so the two cannot drift."""
    out = []
    for line in SCHEMA.splitlines():
        if line.strip().upper().startswith("PRAGMA"):
            continue
        out.append(line)
    ddl = "\n".join(out)
    ddl = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    ddl = re.sub(r"\bREAL\b", "DOUBLE PRECISION", ddl)
    return ddl


def pg_translate(sql: str) -> tuple[str, bool]:
    """sqlite dialect -> psycopg. Returns (sql, wants_returning_id)."""
    if re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO", sql, re.I):
        sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, count=1, flags=re.I)
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    returning = False
    m = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
    if m and m.group(1).lower() in _ID_TABLES:
        sql += " RETURNING id"
        returning = True
    return sql.replace("%", "%%").replace("?", "%s"), returning


class _PgCursor:
    def __init__(self, cur: Any, returning: bool) -> None:
        self._cur = cur
        self.rowcount = cur.rowcount
        self.lastrowid = 0
        if returning:
            row = cur.fetchone()
            self.lastrowid = row["id"] if row else 0

    def fetchone(self) -> Any:
        return self._cur.fetchone() if self._cur.description else None

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall() if self._cur.description else []


class PgConn:
    """Minimal sqlite3.Connection look-alike over one psycopg connection (autocommit; explicit BEGIN/COMMIT)."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._open()

    def _open(self) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self.conn = psycopg.connect(self.url, autocommit=True, row_factory=dict_row, connect_timeout=15)
        self.in_tx = False

    def execute(self, sql: str, params: tuple | list = ()) -> _PgCursor:
        import psycopg

        s = sql.strip().upper()
        if s in ("BEGIN", "BEGIN IMMEDIATE"):
            self.conn.execute("BEGIN")
            self.conn.execute("SELECT pg_advisory_xact_lock(7331)")  # same writer serialisation as sqlite's BEGIN IMMEDIATE
            self.in_tx = True
            return _PgCursor(self.conn.cursor(), False)
        if s in ("COMMIT", "ROLLBACK"):
            self.in_tx = False
            return _PgCursor(self.conn.execute(s), False)
        text, returning = pg_translate(sql)
        for attempt in (0, 1):
            try:
                cur = self.conn.execute(text, tuple(params))
                return _PgCursor(cur, returning)
            except psycopg.OperationalError:
                # Managed Postgres drops idle connections. Reconnect once, but never silently inside a transaction.
                if attempt == 1 or self.in_tx or not self.conn.closed:
                    raise
                self._open()
        raise AssertionError("unreachable")


def get_conn() -> Any:
    st = get_settings()
    key = st.database_url or st.db_path
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}
    conn = cache.get(key)
    if conn is None:
        conn = cache[key] = PgConn(st.database_url) if st.database_url else _connect(st.db_path)
    with _init_lock:
        if key not in _initialised:
            if st.database_url:
                conn.execute("SELECT pg_advisory_lock(7332)")  # several workers/boots may race to create tables
                try:
                    conn.conn.execute(pg_schema())
                finally:
                    conn.execute("SELECT pg_advisory_unlock(7332)")
            else:
                conn.executescript(SCHEMA)
            _initialised.add(key)
    return conn


@contextmanager
def tx() -> Iterator[Any]:
    """Serialised write transaction (sqlite BEGIN IMMEDIATE / Postgres advisory xact lock)."""
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def q(sql: str, params: tuple | list = ()) -> list[Any]:
    return get_conn().execute(sql, params).fetchall()


def q1(sql: str, params: tuple | list = ()) -> Any:
    return get_conn().execute(sql, params).fetchone()


def run(sql: str, params: tuple | list = ()) -> int:
    cur = get_conn().execute(sql, params)
    return cur.lastrowid or 0


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def jload(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    return json.loads(s)


def kv_get(key: str) -> str | None:
    row = q1("SELECT value FROM kv WHERE key=?", (key,))
    return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    run(
        "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now_iso()),
    )


def kv_del(key: str) -> None:
    run("DELETE FROM kv WHERE key=?", (key,))
