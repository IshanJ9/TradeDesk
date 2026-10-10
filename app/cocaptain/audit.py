"""Pairing and review events are shared code, but each one is about one trader: it is written to THAT trader's audit log
(app/audit.py is per user), with the real person who acted in `data.actor_id`."""

from collections.abc import Callable
from datetime import datetime

from app.audit import AuditLog
from app.db import Database
from app.events import EventHub


class OwnerAudit:
    def __init__(self, db: Database, clock: Callable[[], datetime], events: EventHub):
        self._db, self._clock, self._events = db, clock, events

    def record(self, kind, actor, summary, *, subject_id=None, data=None):
        owner = (data or {}).get("owner_id")
        if not owner:  # nothing says whose it is: never write it to someone else's log
            return None
        log = AuditLog(self._db, self._clock, self._events.for_user(owner), user_id=owner)
        return log.record(kind, actor, summary, subject_id=subject_id, data=data)
