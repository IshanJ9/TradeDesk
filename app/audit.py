"""Append-only audit log: every request, intent, approval, broker call and response."""

import json
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.api_models import AuditEventMessage
from app.db import Database
from app.events import EventHub
from app.schemas import AuditEvent, AuditKind


class AuditLog:
    def __init__(self, db: Database, clock: Callable[[], datetime], hub: EventHub | None = None):
        self._db = db
        self._clock = clock
        self._hub = hub

    def record(
        self,
        kind: AuditKind,
        actor: str,
        summary: str,
        *,
        subject_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=uuid.uuid4().hex,
            ts=self._clock(),
            kind=kind,
            actor=actor,
            subject_id=subject_id,
            summary=summary,
            data=json.loads(json.dumps(data or {}, default=str)),  # force JSON-safe values
        )
        self._db.execute(
            "INSERT INTO audit_events (id, ts, kind, actor, subject_id, summary, data) VALUES (?,?,?,?,?,?,?)",
            (
                event.id,
                event.ts.isoformat(),
                event.kind.value,
                event.actor,
                event.subject_id,
                event.summary,
                json.dumps(event.data),
            ),
        )
        if self._hub is not None:
            self._hub.publish(AuditEventMessage, event=event)
        return event

    def list(self, limit: int = 200, kind: AuditKind | None = None) -> list[AuditEvent]:
        """Newest first."""
        sql, params = "SELECT * FROM audit_events", ()
        if kind is not None:
            sql, params = sql + " WHERE kind = ?", (kind.value,)
        rows = self._db.query(sql + " ORDER BY seq DESC LIMIT ?", params + (limit,))
        return [self._row(r) for r in rows]

    def export_jsonl(self) -> str:
        """Oldest first, one JSON object per line."""
        rows = self._db.query("SELECT * FROM audit_events ORDER BY seq ASC")
        return "".join(self._row(r).model_dump_json() + "\n" for r in rows)

    @staticmethod
    def _row(row) -> AuditEvent:
        return AuditEvent(
            id=row["id"],
            ts=datetime.fromisoformat(row["ts"]),
            kind=AuditKind(row["kind"]),
            actor=row["actor"],
            subject_id=row["subject_id"],
            summary=row["summary"],
            data=json.loads(row["data"]),
        )
