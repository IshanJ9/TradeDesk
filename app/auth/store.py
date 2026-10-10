"""users and sessions tables. A session row holds only the SHA-256 of the cookie token."""

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.db import Database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                     TEXT PRIMARY KEY,
    email                  TEXT NOT NULL UNIQUE,
    password_hash          TEXT NOT NULL,
    display_name           TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL,
    last_login_at          TEXT,
    disabled               INTEGER NOT NULL DEFAULT 0,
    accepted_no_advice_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    csrf_token   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
"""


class EmailTaken(Exception):
    pass


@dataclass(frozen=True)
class UserRow:
    id: str
    email: str
    password_hash: str
    display_name: str
    created_at: datetime
    last_login_at: datetime | None
    disabled: bool
    accepted_no_advice_at: datetime


@dataclass(frozen=True)
class SessionRow:
    token_hash: str
    user_id: str
    csrf_token: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


def _user(r) -> UserRow:
    return UserRow(r["id"], r["email"], r["password_hash"], r["display_name"], datetime.fromisoformat(r["created_at"]),
                   datetime.fromisoformat(r["last_login_at"]) if r["last_login_at"] else None, bool(r["disabled"]),
                   datetime.fromisoformat(r["accepted_no_advice_at"]))


def _session(r) -> SessionRow:
    return SessionRow(r["token_hash"], r["user_id"], r["csrf_token"], datetime.fromisoformat(r["created_at"]),
                      datetime.fromisoformat(r["last_seen_at"]), datetime.fromisoformat(r["expires_at"]))


class AuthStore:
    def __init__(self, db: Database):
        self._db = db
        db.conn.executescript(_SCHEMA)

    # ---- users ------------------------------------------------------------------- #

    def create_user(self, email: str, password_hash: str, display_name: str, now: datetime) -> UserRow:
        uid = uuid.uuid4().hex
        try:
            self._db.execute(
                "INSERT INTO users (id, email, password_hash, display_name, created_at, accepted_no_advice_at)"
                " VALUES (?,?,?,?,?,?)", (uid, email, password_hash, display_name, now.isoformat(), now.isoformat()))
        except sqlite3.IntegrityError as exc:
            raise EmailTaken(email) from exc
        return self.user(uid)  # type: ignore[return-value]

    def user(self, user_id: str) -> UserRow | None:
        rows = self._db.query("SELECT * FROM users WHERE id = ?", (user_id,))
        return _user(rows[0]) if rows else None

    def user_by_email(self, email: str) -> UserRow | None:
        rows = self._db.query("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
        return _user(rows[0]) if rows else None

    def all_users(self) -> list[UserRow]:
        return [_user(r) for r in self._db.query("SELECT * FROM users ORDER BY created_at, rowid")]

    def count_users(self) -> int:
        return self._db.query("SELECT COUNT(*) AS n FROM users")[0]["n"]

    def first_user(self) -> UserRow | None:
        rows = self._db.query("SELECT * FROM users ORDER BY created_at, rowid LIMIT 1")
        return _user(rows[0]) if rows else None

    def set_password(self, user_id: str, password_hash: str) -> None:
        self._db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))

    def touch_login(self, user_id: str, now: datetime) -> None:
        self._db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now.isoformat(), user_id))

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        self._db.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user_id))
        if disabled:
            self.delete_user_sessions(user_id)

    # ---- sessions ---------------------------------------------------------------- #

    def create_session(self, token_hash: str, user_id: str, csrf_token: str, now: datetime, expires_at: datetime) -> None:
        self._db.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen_at, expires_at) VALUES (?,?,?,?,?,?)",
            (token_hash, user_id, csrf_token, now.isoformat(), now.isoformat(), expires_at.isoformat()))

    def session(self, token_hash: str) -> SessionRow | None:
        rows = self._db.query("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,))
        return _session(rows[0]) if rows else None

    def touch_session(self, token_hash: str, now: datetime) -> None:
        self._db.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now.isoformat(), token_hash))

    def delete_session(self, token_hash: str) -> None:
        self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_user_sessions(self, user_id: str) -> None:
        self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def delete_expired(self, now: datetime, idle_cutoff: datetime) -> None:
        self._db.execute("DELETE FROM sessions WHERE expires_at <= ? OR last_seen_at <= ?",
                         (now.isoformat(), idle_cutoff.isoformat()))

    def count_sessions(self, user_id: str | None = None) -> int:
        if user_id is None:
            return self._db.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"]
        return self._db.query("SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", (user_id,))[0]["n"]
