"""Login throttling per email and per IP, with a short lock-out.

Failures are counted in a sliding window. At the limit the key is locked for a while, and during the lock even the
right password is refused, so a guesser cannot tell when they hit the right one. The email key is the typed
address whether or not an account exists, so the lock reveals nothing about which accounts exist.

State is in memory: a restart clears it. That is a deliberate simplification (see README, Security).
"""

from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta


class LoginLimiter:
    def __init__(self, clock: Callable[[], datetime], *, max_per_email: int = 5, max_per_ip: int = 20,
                 window: timedelta = timedelta(minutes=15), lock: timedelta = timedelta(minutes=5)):
        self._clock = clock
        self._limits = {"email": max_per_email, "ip": max_per_ip}
        self._window, self._lock = window, lock
        self._failures: dict[tuple[str, str], deque[datetime]] = {}
        self._locked_until: dict[tuple[str, str], datetime] = {}

    @staticmethod
    def _keys(email: str, ip: str) -> list[tuple[str, str]]:
        return [("email", email.strip().lower()), ("ip", ip)]

    def retry_after(self, email: str, ip: str) -> int:
        """Seconds until the caller may try again, or 0 if they may try now."""
        now = self._clock()
        waits = [(self._locked_until[k] - now).total_seconds() for k in self._keys(email, ip) if self._locked_until.get(k, now) > now]
        return int(max(waits)) + 1 if waits else 0

    def failed(self, email: str, ip: str) -> None:
        now = self._clock()
        for key in self._keys(email, ip):
            q = self._failures.setdefault(key, deque())
            q.append(now)
            while q and now - q[0] > self._window:
                q.popleft()
            if len(q) >= self._limits[key[0]]:
                self._locked_until[key] = now + self._lock
                q.clear()

    def succeeded(self, email: str, ip: str) -> None:
        """A good login clears this email's failures (not the IP's: one address can try many emails)."""
        key = ("email", email.strip().lower())
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)
