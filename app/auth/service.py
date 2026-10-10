"""Register, log in, log out, change password. All the rules about accounts live here; the routes only translate."""

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.auth import passwords
from app.auth.ratelimit import LoginLimiter
from app.auth.store import AuthStore, EmailTaken, SessionRow, UserRow
from app.auth.tokens import hash_token, new_token
from app.identity import Actor, default_display_name

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL = 254
MAX_NAME = 80
TOUCH_EVERY = timedelta(minutes=1)  # last_seen is written at most this often, not on every request

BAD_LOGIN = "Incorrect email or password."  # the same words whether or not the email exists


class AuthError(Exception):
    def __init__(self, status: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


@dataclass(frozen=True)
class Issued:
    """A new session: the raw token goes into the cookie once and is never stored."""
    token: str
    csrf_token: str
    expires_at: datetime
    actor: Actor


@dataclass(frozen=True)
class Authenticated:
    actor: Actor
    session: SessionRow


def normalise_email(email: str) -> str:
    return email.strip().lower()


def actor_of(user: UserRow) -> Actor:
    return Actor(id=user.id, email=user.email, display_name=user.display_name or default_display_name(user.email))


class AuthService:
    def __init__(self, store: AuthStore, clock: Callable[[], datetime], limiter: LoginLimiter | None = None, *,
                 idle_timeout: timedelta = timedelta(hours=12), absolute_timeout: timedelta = timedelta(days=7),
                 on_user_created: Callable[[UserRow, bool], None] | None = None):
        self._store, self._clock = store, clock
        self.limiter = limiter or LoginLimiter(clock)
        self.idle_timeout, self.absolute_timeout = idle_timeout, absolute_timeout
        self._on_user_created = on_user_created  # (user, is_first_user): adopts legacy data, starts the user's desk

    @property
    def store(self) -> AuthStore:
        return self._store

    # ---- accounts ------------------------------------------------------------------ #

    async def register(self, email: str, password: str, display_name: str, accepts_no_advice: bool) -> Issued:
        email = normalise_email(email)
        if not accepts_no_advice:
            raise AuthError(422, "Please confirm you understand TradeDesk gives no investment advice.")
        if len(email) > MAX_EMAIL or not _EMAIL.match(email):
            raise AuthError(422, "Enter a valid email address.")
        problem = passwords.password_problem(password, email)
        if problem:
            raise AuthError(422, problem)
        name = display_name.strip()[:MAX_NAME]
        hashed = await asyncio.to_thread(passwords.hash_password, password)  # CPU-heavy: off the event loop
        first = self._store.count_users() == 0
        try:
            user = self._store.create_user(email, hashed, name, self._clock())
        except EmailTaken:
            raise AuthError(409, "That email already has an account. Log in instead.") from None
        if self._on_user_created is not None:
            self._on_user_created(user, first)
        return self._issue(user)

    async def login(self, email: str, password: str, ip: str) -> Issued:
        email = normalise_email(email)
        wait = self.limiter.retry_after(email, ip)
        if wait:
            raise AuthError(429, "Too many attempts. Try again in a few minutes.", retry_after=wait)
        user = self._store.user_by_email(email) if len(email) <= MAX_EMAIL else None
        if user is None:
            await asyncio.to_thread(passwords.dummy_verify, password)  # similar time whether or not the email exists
            ok = False
        else:
            ok = await asyncio.to_thread(passwords.verify_password, user.password_hash, password)
        if not ok or user is None or user.disabled:
            self.limiter.failed(email, ip)
            raise AuthError(401, BAD_LOGIN)
        self.limiter.succeeded(email, ip)
        if passwords.needs_rehash(user.password_hash):
            self._store.set_password(user.id, await asyncio.to_thread(passwords.hash_password, password))
        self._store.touch_login(user.id, self._clock())
        return self._issue(user)

    def logout(self, token: str | None) -> None:
        if token:
            self._store.delete_session(hash_token(token))

    async def change_password(self, auth: Authenticated, current: str, new: str) -> Issued:
        user = self._store.user(auth.actor.id)
        if user is None or not await asyncio.to_thread(passwords.verify_password, user.password_hash, current):
            raise AuthError(403, "The current password is not right.")
        problem = passwords.password_problem(new, user.email)
        if problem:
            raise AuthError(422, problem)
        self._store.set_password(user.id, await asyncio.to_thread(passwords.hash_password, new))
        self._store.delete_user_sessions(user.id)  # every device is signed out, including this one
        return self._issue(user)  # ... and this one gets a fresh session so the person is not thrown out mid-task

    # ---- sessions ------------------------------------------------------------------- #

    def _issue(self, user: UserRow) -> Issued:
        now = self._clock()
        token, csrf = new_token(), new_token()
        expires = now + self.absolute_timeout
        self._store.create_session(hash_token(token), user.id, csrf, now, expires)
        return Issued(token=token, csrf_token=csrf, expires_at=expires, actor=actor_of(user))

    def authenticate(self, token: str | None) -> Authenticated | None:
        """The session behind a cookie token, or None if there is none, it expired, or the user is gone/disabled."""
        if not token:
            return None
        th = hash_token(token)
        row = self._store.session(th)
        if row is None:
            return None
        now = self._clock()
        if now >= row.expires_at or now - row.last_seen_at >= self.idle_timeout:
            self._store.delete_session(th)
            return None
        user = self._store.user(row.user_id)
        if user is None or user.disabled:
            self._store.delete_session(th)
            return None
        if now - row.last_seen_at >= TOUCH_EVERY:
            self._store.touch_session(th, now)
        return Authenticated(actor_of(user), row)
