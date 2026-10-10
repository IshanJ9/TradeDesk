"""Each user's linked 021 account: the login, encrypted at rest (app/vault.py), and the code that opens their session.

The table holds, per user: a keyed fingerprint of the 021 client id (so one 021 account cannot be linked twice, which
would make two sessions revoke each other), its last four characters for display, and the sealed username+password.
Nothing here is ever returned by an API, logged or put in an audit event."""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from app.broker.adapter_errors import AuthFailed
from app.broker.base import BrokerAdapter
from app.broker.disconnected import DisconnectedBroker
from app.db import Database
from app.vault import Vault, VaultCorrupt, VaultUnavailable

log = logging.getLogger("tradedesk.links")

START_TIMEOUT = 25.0  # seconds to log in to 021 and load its instrument list

_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_links (
    user_id    TEXT PRIMARY KEY,
    ucc_hash   TEXT NOT NULL UNIQUE,
    ucc_last4  TEXT NOT NULL,
    nonce      BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class UccTaken(Exception):
    pass


@dataclass(frozen=True)
class LinkRow:
    user_id: str
    ucc_last4: str
    status: str  # connected | needs_reconnect


def normalise_ucc(ucc: str) -> str:
    return ucc.strip().upper()


class BrokerLinks:
    def __init__(self, db: Database, vault: Vault):
        self._db, self._vault = db, vault
        db.conn.executescript(_SCHEMA)

    def get(self, user_id: str) -> LinkRow | None:
        rows = self._db.query("SELECT user_id, ucc_last4, status FROM broker_links WHERE user_id = ?", (user_id,))
        return LinkRow(rows[0]["user_id"], rows[0]["ucc_last4"], rows[0]["status"]) if rows else None

    def taken_by_other(self, ucc: str, user_id: str) -> bool:
        h = self._vault.fingerprint(normalise_ucc(ucc))
        return bool(self._db.query("SELECT 1 FROM broker_links WHERE ucc_hash = ? AND user_id <> ?", (h, user_id)))

    def save(self, user_id: str, ucc: str, password: str, now: datetime) -> None:
        ucc = normalise_ucc(ucc)
        nonce, blob = self._vault.seal(user_id, {"username": ucc, "password": password})
        try:
            self._db.execute(
                "INSERT INTO broker_links (user_id, ucc_hash, ucc_last4, nonce, ciphertext, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,'connected',?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET ucc_hash=excluded.ucc_hash, ucc_last4=excluded.ucc_last4,"
                " nonce=excluded.nonce, ciphertext=excluded.ciphertext, status='connected', updated_at=excluded.updated_at",
                (user_id, self._vault.fingerprint(ucc), ucc[-4:], nonce, blob, now.isoformat(), now.isoformat()))
        except sqlite3.IntegrityError as exc:
            raise UccTaken() from exc

    def credentials(self, user_id: str) -> tuple[str, str] | None:
        """(username, password) for this user, or None if they have no link. Raises VaultUnavailable / VaultCorrupt."""
        rows = self._db.query("SELECT nonce, ciphertext FROM broker_links WHERE user_id = ?", (user_id,))
        if not rows:
            return None
        data, _ = self._vault.open(user_id, rows[0]["nonce"], rows[0]["ciphertext"])
        return data["username"], data["password"]

    def set_status(self, user_id: str, status: str, now: datetime) -> None:
        self._db.execute("UPDATE broker_links SET status = ?, updated_at = ? WHERE user_id = ?", (status, now.isoformat(), user_id))

    def delete(self, user_id: str) -> None:
        self._db.execute("DELETE FROM broker_links WHERE user_id = ?", (user_id,))

    def rewrap(self, now: datetime) -> int:
        """After the key changed: re-seal records that only the previous key opens, under the current one."""
        if not self._vault.available:
            return 0
        moved = 0
        for row in self._db.query("SELECT user_id, nonce, ciphertext FROM broker_links"):
            try:
                data, old = self._vault.open(row["user_id"], row["nonce"], row["ciphertext"])
            except VaultCorrupt:
                log.warning("a linked 021 login can no longer be decrypted (wrong or missing key); that user must link again")
                self.set_status(row["user_id"], "needs_reconnect", now)
                continue
            if old:
                self.save(row["user_id"], data["username"], data["password"], now)
                moved += 1
        return moved


BrokerFactory = Callable[[str, str], BrokerAdapter]  # (username, password) -> an unstarted 021 adapter


class LinkedBrokers:
    """Opens the 021 session of a user who has linked an account. Never raises for a session that cannot be opened:
    it returns a DisconnectedBroker and records `needs_reconnect`, so the user sees a Reconnect state, not an error."""

    def __init__(self, links: BrokerLinks, make: BrokerFactory, clock: Callable[[], datetime]):
        self._links, self._make, self._clock = links, make, clock
        self.failure: dict[str, str] = {}  # user id -> why the last attempt failed: "refused" | "unreachable"

    async def open(self, user) -> tuple[BrokerAdapter, bool] | None:
        row = self._links.get(user.id)
        if row is None:
            return None
        try:
            creds = self._links.credentials(user.id)
        except (VaultUnavailable, VaultCorrupt):
            self._links.set_status(user.id, "needs_reconnect", self._clock())
            return DisconnectedBroker(), True
        assert creds is not None
        adapter = self._make(*creds)
        try:
            await asyncio.wait_for(adapter.start(), START_TIMEOUT)
        except Exception as exc:  # refused login, no network, slow: either way this desk starts in the Reconnect state
            self.failure[user.id] = "refused" if isinstance(exc, AuthFailed) else "unreachable"
            log.warning("could not open a user's 021 session at startup: %s", type(exc).__name__)
            try:
                await adapter.close()
            except Exception:
                pass
            self._links.set_status(user.id, "needs_reconnect", self._clock())
            return DisconnectedBroker(), True
        self.failure.pop(user.id, None)
        self._links.set_status(user.id, "connected", self._clock())
        return adapter, True
