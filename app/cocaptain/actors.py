"""Who the people in a Co-Captain pairing are: the app's real accounts (app/auth), nothing else.

`Actor` is the one identity type (app/identity.py). The directory looks accounts up by email (to invite someone) or by
id (to show a name); it creates no users, sessions or passwords.
"""

from typing import Protocol

from app.auth.service import actor_of
from app.auth.store import AuthStore
from app.identity import Actor

__all__ = ["Actor", "ActorDirectory", "AccountDirectory"]


class ActorDirectory(Protocol):
    def by_email(self, email: str) -> Actor | None: ...
    def by_id(self, actor_id: str) -> Actor | None: ...


class AccountDirectory:
    """The signed-up, enabled accounts."""

    def __init__(self, store: AuthStore):
        self._store = store

    def by_id(self, actor_id: str) -> Actor | None:
        user = self._store.user(actor_id)
        return actor_of(user) if user is not None and not user.disabled else None

    def by_email(self, email: str) -> Actor | None:
        user = self._store.user_by_email(email.strip().lower())
        return actor_of(user) if user is not None and not user.disabled else None
