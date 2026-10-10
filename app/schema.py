"""Every table that holds a trader's data, in one place, each with a `user_id`.

`ensure_schema` runs when a Database is opened. It is idempotent:
- a missing table is created in its final shape;
- a table from before accounts existed gets `user_id` (existing rows get '' = "not owned yet");
- a table whose primary key must now include the user (so two users can each have an order "ORD-1") is rebuilt.

Rows with user_id = '' belong to nobody until `adopt_legacy_rows` hands them to the first account (or the configured
owner). The stores always filter by user_id, so an unowned row is invisible to everyone until then.
"""

import sqlite3

from app.db import Database

UNOWNED = ""  # user_id of rows saved before accounts existed

_TABLES: dict[str, str] = {
    "audit_events": """
        CREATE TABLE audit_events (
            seq        INTEGER PRIMARY KEY AUTOINCREMENT,
            id         TEXT NOT NULL UNIQUE,
            user_id    TEXT NOT NULL DEFAULT '',
            ts         TEXT NOT NULL,
            kind       TEXT NOT NULL,
            actor      TEXT NOT NULL,
            subject_id TEXT,
            summary    TEXT NOT NULL,
            data       TEXT NOT NULL
        )""",
    # Standing instructions. `status` and `fired_at` are authoritative here; `data` is the rule as created. Firing is a
    # conditional UPDATE (ACTIVE -> FIRED), so a rule can fire exactly once even if the same price arrives twice.
    # `delivered` flips after the trader has been told / the approval card exists.
    "rules": """
        CREATE TABLE rules (
            id             TEXT PRIMARY KEY,
            user_id        TEXT NOT NULL DEFAULT '',
            kind           TEXT NOT NULL,
            status         TEXT NOT NULL,
            instrument_key TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            fired_at       TEXT,
            delivered      INTEGER NOT NULL DEFAULT 0,
            data           TEXT NOT NULL
        )""",
    # Written BEFORE an order is sent. client_order_id is the primary key, so the same order can never be sent twice
    # (by anyone), even if a caller has a bug. status: SENDING | SENT | REJECTED | UNKNOWN | NOT_SENT.
    "executions": """
        CREATE TABLE executions (
            client_order_id TEXT PRIMARY KEY,
            user_id         TEXT NOT NULL DEFAULT '',
            pending_id      TEXT NOT NULL,
            action          TEXT NOT NULL,
            status          TEXT NOT NULL,
            broker_order_id TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            detail          TEXT NOT NULL
        )""",
    "pending_cards": """
        CREATE TABLE pending_cards (
            id         TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL DEFAULT '',
            state      TEXT NOT NULL,
            created_at TEXT NOT NULL,
            data       TEXT NOT NULL
        )""",
    "plans": """
        CREATE TABLE plans (
            id         TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            data       TEXT NOT NULL
        )""",
    "plan_requests": """
        CREATE TABLE plan_requests (
            plan_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT '',
            data    TEXT NOT NULL
        )""",
    "plan_reports": """
        CREATE TABLE plan_reports (
            plan_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT '',
            data    TEXT NOT NULL
        )""",
    # The next group had primary keys that did not include the user; they are rebuilt on upgrade.
    "activity_orders": """
        CREATE TABLE activity_orders (
            user_id          TEXT NOT NULL DEFAULT '',
            order_id         TEXT NOT NULL,
            source           TEXT NOT NULL CHECK(source IN ('app','external')),
            day              TEXT NOT NULL,
            data             TEXT NOT NULL,
            recording_source TEXT NOT NULL DEFAULT 'unknown',
            PRIMARY KEY (user_id, order_id)
        )""",
    "activity_days": """
        CREATE TABLE activity_days (
            user_id TEXT NOT NULL DEFAULT '',
            day     TEXT NOT NULL,
            data    TEXT NOT NULL,
            PRIMARY KEY (user_id, day)
        )""",
    "risk_profile": "CREATE TABLE risk_profile (user_id TEXT NOT NULL DEFAULT '' PRIMARY KEY, data TEXT NOT NULL)",
    "risk_goal": "CREATE TABLE risk_goal (user_id TEXT NOT NULL DEFAULT '' PRIMARY KEY, data TEXT NOT NULL)",
    "risk_cooldown": "CREATE TABLE risk_cooldown (user_id TEXT NOT NULL DEFAULT '' PRIMARY KEY, until_at TEXT NOT NULL)",
    "risk_days": """
        CREATE TABLE risk_days (
            user_id TEXT NOT NULL DEFAULT '',
            day     TEXT NOT NULL,
            demo    INTEGER NOT NULL,
            data    TEXT NOT NULL,
            PRIMARY KEY (user_id, day, demo)
        )""",
    "risk_meta": """
        CREATE TABLE risk_meta (
            user_id TEXT NOT NULL DEFAULT '',
            key     TEXT NOT NULL,
            value   TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        )""",
    "risk_observations_v2": """
        CREATE TABLE risk_observations_v2 (
            user_id   TEXT NOT NULL DEFAULT '',
            source    TEXT NOT NULL,
            day       TEXT NOT NULL,
            bucket    INTEGER NOT NULL,
            at        TEXT NOT NULL,
            risk      REAL,
            portfolio INTEGER NOT NULL,
            pnl       INTEGER NOT NULL,
            PRIMARY KEY (user_id, source, day, bucket)
        )""",
}

_REBUILD = frozenset({"activity_orders", "activity_days", "risk_profile", "risk_goal", "risk_cooldown", "risk_days",
                      "risk_meta", "risk_observations_v2"})

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS audit_kind ON audit_events(kind)",
    "CREATE INDEX IF NOT EXISTS audit_user ON audit_events(user_id, seq)",
    "CREATE INDEX IF NOT EXISTS rules_active ON rules(status, instrument_key)",
    "CREATE INDEX IF NOT EXISTS rules_user ON rules(user_id, status)",
    "CREATE INDEX IF NOT EXISTS executions_user ON executions(user_id, status)",
    "CREATE INDEX IF NOT EXISTS pending_user ON pending_cards(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS plans_user ON plans(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS activity_orders_day ON activity_orders(user_id, day)",
)

USER_DATA_TABLES: tuple[str, ...] = tuple(_TABLES)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _upgrade(conn: sqlite3.Connection, table: str) -> None:
    ddl = _TABLES[table]
    if not _exists(conn, table):
        conn.execute(ddl)
        return
    old = _columns(conn, table)
    if "user_id" in old:
        return
    if table in _REBUILD:
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}__old")
        conn.execute(ddl)
        keep = [c for c in old if c in _columns(conn, table)]
        cols = ", ".join(keep)
        conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {table}__old")  # user_id takes its '' default
        conn.execute(f"DROP TABLE {table}__old")
    else:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")


def ensure_schema(db: Database) -> None:
    conn = db.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in _TABLES:
            _upgrade(conn, table)
        for statement in _INDEXES:
            conn.execute(statement)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def adopt_legacy_rows(db: Database, user_id: str) -> dict[str, int]:
    """Give every unowned row (saved before accounts existed) to `user_id`. Safe to run again: nothing is left to adopt.
    A row that would collide with one the user already has is left unowned rather than overwritten."""
    if not user_id:
        raise ValueError("a real user id is required")
    moved: dict[str, int] = {}
    conn = db.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in USER_DATA_TABLES:
            moved[table] = conn.execute(f"UPDATE OR IGNORE {table} SET user_id = ? WHERE user_id = ''", (user_id,)).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {t: n for t, n in moved.items() if n}
