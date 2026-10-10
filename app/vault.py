"""Encryption for the 021 login a user links to their TradeDesk account.

A password we must be able to use again cannot be hashed, so it is encrypted: AES-256-GCM with a fresh random nonce
per record, and the user's id bound in as associated data, so a ciphertext copied onto another user's row will not
decrypt. The key is TRADEDESK_SECRET_KEY in .env (32 random bytes, base64) and is never stored, logged or returned.
Two sub-keys are derived from it (one to encrypt, one to fingerprint the 021 client id), so one key is never used for
two purposes. TRADEDESK_SECRET_KEY_PREVIOUS lets the key be changed: records written under it are read, then
re-sealed under the new key at startup (`BrokerLinks.rewrap`).

Generate a key:  python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
"""

import base64
import binascii
import hashlib
import hmac
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class VaultUnavailable(Exception):
    """No usable key is configured, so nothing can be sealed or opened."""


class VaultCorrupt(Exception):
    """A record did not decrypt (wrong key, wrong user, or damaged). Never says which."""


def _parse(key: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(key.strip() + "=" * (-len(key.strip()) % 4))
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 32:
        raise VaultUnavailable("TRADEDESK_SECRET_KEY must be 32 random bytes, base64 encoded")
    return raw


def _sub(master: bytes, purpose: bytes) -> bytes:
    return hmac.new(master, b"tradedesk:" + purpose, hashlib.sha256).digest()


class Vault:
    def __init__(self, key: str = "", previous: str = ""):
        self._keys: list[bytes] = []
        self.problem: str | None = None
        for k in (key, previous):
            if not k.strip():
                continue
            try:
                self._keys.append(_parse(k))
            except VaultUnavailable as exc:
                self.problem = str(exc)
                self._keys = []
                break
        if not key.strip() and not self.problem:
            self.problem = "TRADEDESK_SECRET_KEY is not set"

    @property
    def available(self) -> bool:
        return bool(self._keys) and self.problem is None

    def _require(self) -> None:
        if not self.available:
            raise VaultUnavailable(self.problem or "no key")

    def seal(self, user_id: str, data: dict) -> tuple[bytes, bytes]:
        """(nonce, ciphertext), under the current key."""
        self._require()
        nonce = os.urandom(12)
        aead = AESGCM(_sub(self._keys[0], b"aesgcm"))
        return nonce, aead.encrypt(nonce, json.dumps(data).encode(), user_id.encode())

    def open(self, user_id: str, nonce: bytes, ciphertext: bytes) -> tuple[dict, bool]:
        """(data, was_under_the_previous_key). Raises VaultCorrupt if no key opens it for this user."""
        self._require()
        for i, master in enumerate(self._keys):
            try:
                plain = AESGCM(_sub(master, b"aesgcm")).decrypt(nonce, ciphertext, user_id.encode())
            except InvalidTag:
                continue
            return json.loads(plain), i > 0
        raise VaultCorrupt()

    def fingerprint(self, ucc: str, *, previous: bool = False) -> str:
        """Stable, keyed fingerprint of a 021 client id: lets us refuse a second link of the same account without
        keeping the id in the clear in an index."""
        self._require()
        master = self._keys[1] if previous and len(self._keys) > 1 else self._keys[0]
        return hmac.new(_sub(master, b"ucc"), ucc.strip().upper().encode(), hashlib.sha256).hexdigest()
