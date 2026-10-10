"""One SQLite database shared by every store. The tables live in app/schema.py; each row belongs to a user."""

import sqlite3


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
        from app.schema import ensure_schema  # imported here: schema.py imports Database for its type

        ensure_schema(self)

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self.conn.close()
