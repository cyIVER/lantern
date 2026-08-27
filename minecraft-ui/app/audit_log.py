"""Append-only operational audit records for the Minecraft portal."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

_SAFE_TOKEN = re.compile(r"^[a-zA-Z0-9_.:/-]{1,160}$")
_SAFE_TARGET = re.compile(r"^[a-zA-Z0-9 _+.,@()'\[\]/:-]{1,240}$")
_ALLOWED_DETAIL_KEYS = frozenset(
    {"action", "code", "new_sha256", "old_sha256", "state", "submission_id"}
)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    event_id: str
    occurred_at: datetime
    actor: str
    action: str
    target: str
    outcome: str
    correlation_id: str
    details: tuple[tuple[str, str], ...] = ()


class AuditLog(Protocol):
    def claim(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        correlation_id: str,
    ) -> bool: ...

    def append(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        outcome: str,
        correlation_id: str,
        details: Mapping[str, str] | None = None,
    ) -> AuditRecord: ...

    def recent(self, *, limit: int = 100) -> tuple[AuditRecord, ...]: ...


class InMemoryAuditLog:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._records: list[AuditRecord] = []
        self._lock = threading.RLock()

    def append(self, **values) -> AuditRecord:
        record = _build_record(event_id=self._id_factory(), occurred_at=self._clock(), **values)
        with self._lock:
            self._records.append(record)
        return record

    def claim(self, *, actor: str, action: str, target: str, correlation_id: str) -> bool:
        record = _build_record(
            event_id=self._id_factory(),
            occurred_at=self._clock(),
            actor=actor,
            action=action,
            target=target,
            outcome="requested",
            correlation_id=correlation_id,
        )
        with self._lock:
            if any(
                item.actor == actor
                and item.action == action
                and item.correlation_id == correlation_id
                and item.outcome == "requested"
                for item in self._records
            ):
                return False
            self._records.append(record)
        return True

    def recent(self, *, limit: int = 100) -> tuple[AuditRecord, ...]:
        _validate_limit(limit)
        with self._lock:
            return tuple(reversed(self._records[-limit:]))


class SqliteAuditLog:
    """Durable adapter whose records cannot be updated or deleted through SQLite."""

    def __init__(
        self,
        database: str,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS minecraft_audit_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS minecraft_audit_recent
                    ON minecraft_audit_events (occurred_at DESC, event_id DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS minecraft_audit_idempotency
                    ON minecraft_audit_events (actor, action, correlation_id)
                    WHERE outcome = 'requested';
                CREATE TRIGGER IF NOT EXISTS minecraft_audit_no_update
                    BEFORE UPDATE ON minecraft_audit_events BEGIN
                        SELECT RAISE(ABORT, 'audit records are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS minecraft_audit_no_delete
                    BEFORE DELETE ON minecraft_audit_events BEGIN
                        SELECT RAISE(ABORT, 'audit records are immutable');
                    END;
                """
            )

    def claim(self, *, actor: str, action: str, target: str, correlation_id: str) -> bool:
        record = _build_record(
            event_id=self._id_factory(),
            occurred_at=self._clock(),
            actor=actor,
            action=action,
            target=target,
            outcome="requested",
            correlation_id=correlation_id,
        )
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    "INSERT INTO minecraft_audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.event_id,
                        record.occurred_at.isoformat(),
                        record.actor,
                        record.action,
                        record.target,
                        record.outcome,
                        record.correlation_id,
                        "{}",
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def append(self, **values) -> AuditRecord:
        record = _build_record(event_id=self._id_factory(), occurred_at=self._clock(), **values)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO minecraft_audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.event_id,
                    record.occurred_at.isoformat(),
                    record.actor,
                    record.action,
                    record.target,
                    record.outcome,
                    record.correlation_id,
                    json.dumps(dict(record.details), sort_keys=True, separators=(",", ":")),
                ),
            )
        return record

    def recent(self, *, limit: int = 100) -> tuple[AuditRecord, ...]:
        _validate_limit(limit)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM minecraft_audit_events
                ORDER BY occurred_at DESC, event_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(
            AuditRecord(
                event_id=row["event_id"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]).astimezone(UTC),
                actor=row["actor"],
                action=row["action"],
                target=row["target"],
                outcome=row["outcome"],
                correlation_id=row["correlation_id"],
                details=tuple(sorted(json.loads(row["details_json"]).items())),
            )
            for row in rows
        )


def _build_record(
    *,
    event_id: str,
    occurred_at: datetime,
    actor: str,
    action: str,
    target: str,
    outcome: str,
    correlation_id: str,
    details: Mapping[str, str] | None = None,
) -> AuditRecord:
    if occurred_at.tzinfo is None:
        raise ValueError("audit clock must be timezone-aware")
    for label, value in {
        "event_id": event_id,
        "actor": actor,
        "action": action,
        "outcome": outcome,
        "correlation_id": correlation_id,
    }.items():
        if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
            raise ValueError(f"invalid audit {label}")
    if not isinstance(target, str) or not _SAFE_TARGET.fullmatch(target):
        raise ValueError("invalid audit target")
    safe_details = tuple(sorted((details or {}).items()))
    for key, value in safe_details:
        if key not in _ALLOWED_DETAIL_KEYS or not isinstance(value, str) or len(value) > 256:
            raise ValueError("audit details are not safe")
    return AuditRecord(
        event_id=event_id,
        occurred_at=occurred_at.astimezone(UTC),
        actor=actor,
        action=action,
        target=target,
        outcome=outcome,
        correlation_id=correlation_id,
        details=safe_details,
    )


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("audit limit must be between 1 and 500")
