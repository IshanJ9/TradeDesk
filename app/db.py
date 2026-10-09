"""SQLite storage shared by the audit log, the execution write-ahead log and (step 6) rules."""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT NOT NULL UNIQUE,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    actor      TEXT NOT NULL,
    subject_id TEXT,
    summary    TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_kind ON audit_events(kind);

-- Standing instructions. `status` and `fired_at` are authoritative here; `data` is the rule as
-- created. Firing is a conditional UPDATE (ACTIVE -> FIRED), so a rule can fire exactly once even
-- if the same price arrives twice or two workers race. `delivered` flips after the trader has
-- been told / the approval card exists; on restart, fired-but-undelivered rules are re-delivered.
CREATE TABLE IF NOT EXISTS rules (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    status         TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    fired_at       TEXT,
    delivered      INTEGER NOT NULL DEFAULT 0,
    data           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rules_active ON rules(status, instrument_key);

-- Written BEFORE an order is sent. client_order_id is the primary key, so the same order
-- can never be sent twice, even if a caller has a bug. status:
--   SENDING   about to call / calling the broker
--   SENT      broker accepted (or reconciled as accepted)
--   REJECTED  broker refused
--   UNKNOWN   timed out; outcome not known; never retried, only reconciled
--   NOT_SENT  reconciled: the broker never saw it
CREATE TABLE IF NOT EXISTS executions (
    client_order_id TEXT PRIMARY KEY,
    pending_id      TEXT NOT NULL,
    action          TEXT NOT NULL,
    status          TEXT NOT NULL,
    broker_order_id TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    detail          TEXT NOT NULL
);
"""


def _path(url: str) -> str:
    if url in (":memory:", "sqlite:///:memory:"):
        return ":memory:"
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///") :]
    raise ValueError(f"unsupported DATABASE_URL {url!r}; use sqlite:///path or sqlite:///:memory:")


class Database:
    def __init__(self, url: str = "sqlite:///:memory:"):
        self.path = _path(url)
        # One connection, used from the event loop only; autocommit so every write is durable.
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self.conn.close()
