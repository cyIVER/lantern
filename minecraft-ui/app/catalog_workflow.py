"""Schematic submission, review, and publication workflow.

The workflow owns quarantine and review state.  Publication is deliberately
behind :class:`CatalogPort`, so the viewer API remains an infrastructure detail
and tests never need the network.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol

MAX_SCHEMATIC_BYTES = 250 * 1024 * 1024
SUPPORTED_EXTENSIONS = frozenset({".litematic", ".nbt", ".schem", ".schematic", ".zip"})
UNRESTRICTED_LICENSES = frozenset({"0BSD", "CC0-1.0", "Unlicense"})
_SAFE_REASON = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SAFE_EVENT_FACTS = frozenset({"catalog_id", "failure_codes", "promotion_requested", "reason_code"})


class SubmissionState(StrEnum):
    QUARANTINED = "quarantined"
    ANALYZING = "analyzing"
    ELIGIBLE = "eligible"
    NEEDS_REVIEW = "needs_review"
    PUBLISHING = "publishing"
    CATALOGED = "cataloged"
    REJECTED = "rejected"


_PAYLOAD_STATES = frozenset(
    {
        SubmissionState.QUARANTINED,
        SubmissionState.ANALYZING,
        SubmissionState.ELIGIBLE,
        SubmissionState.NEEDS_REVIEW,
        SubmissionState.PUBLISHING,
    }
)


@dataclass(frozen=True, slots=True)
class SubmitCommand:
    filename: str
    content: bytes
    promotion_requested: bool = False
    rights_attested: bool = False
    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    license_id: str = "CC0-1.0"


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    """Finite durable-quarantine capacity and request-driven retention."""

    max_pending_count: int = 500
    max_pending_bytes: int = 2 * 1024 * 1024 * 1024
    per_source_limit: int = 20
    rate_window: timedelta = timedelta(hours=1)
    retention: timedelta = timedelta(days=30)
    max_concurrent_uploads: int = 4
    admission_ttl: timedelta = timedelta(minutes=10)

    def __post_init__(self) -> None:
        values = (
            self.max_pending_count,
            self.max_pending_bytes,
            self.per_source_limit,
            self.rate_window.total_seconds(),
            self.retention.total_seconds(),
            self.max_concurrent_uploads,
            self.admission_ttl.total_seconds(),
        )
        if any(value <= 0 for value in values):
            raise ValueError("schematic queue policy values must be positive")


@dataclass(frozen=True, slots=True)
class SchematicMetadata:
    filename: str
    title: str
    description: str
    tags: tuple[str, ...]
    license_id: str
    rights_attested: bool


@dataclass(frozen=True, slots=True)
class Recommendation:
    field: str
    value: str | tuple[str, ...]
    applied: bool


@dataclass(frozen=True, slots=True)
class Requirement:
    code: str
    passed: bool
    recommendation: str


@dataclass(frozen=True, slots=True)
class Submission:
    submission_id: str
    sha256: str
    byte_size: int
    metadata: SchematicMetadata
    promotion_requested: bool
    state: SubmissionState
    requirements: tuple[Requirement, ...]
    recommendations: tuple[Recommendation, ...]
    revision: int
    created_at: datetime
    updated_at: datetime
    catalog_id: str | None = None
    rejection_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return bool(self.requirements) and all(item.passed for item in self.requirements)


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    event_id: str
    submission_id: str
    occurred_at: datetime
    actor: str
    action: str
    from_state: SubmissionState | None
    to_state: SubmissionState
    revision: int
    facts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogItem:
    submission_id: str
    sha256: str
    content: bytes
    metadata: SchematicMetadata


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    catalog_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CatalogAnalysis:
    valid: bool
    failure_code: str | None = None


class RevisionConflict(RuntimeError):
    """A caller tried to mutate a stale submission revision."""


class InvalidTransition(RuntimeError):
    """The requested workflow transition is not available from this state."""


class RequirementsNotMet(RuntimeError):
    """Publication was requested before all hard requirements passed."""


class CatalogPublicationError(RuntimeError):
    """The publication target could not accept an otherwise eligible item."""


class CatalogAnalysisError(RuntimeError):
    """The catalog validator was unavailable for a quarantined submission."""


class SubmissionQuotaExceeded(RuntimeError):
    """The bounded quarantine cannot accept another upload right now."""


@dataclass(frozen=True, slots=True)
class UploadAdmission:
    admission_id: str
    reserved_bytes: int


class SubmissionStore(Protocol):
    def create(self, submission: Submission, content: bytes, event: WorkflowEvent) -> None: ...

    def get(self, submission_id: str) -> Submission: ...

    def content(self, submission_id: str) -> bytes: ...

    def find_by_sha256(self, sha256: str) -> tuple[Submission, ...]: ...

    def transition(
        self,
        submission_id: str,
        expected_revision: int,
        updated: Submission,
        event: WorkflowEvent,
    ) -> Submission: ...

    def pending(self, *, limit: int, offset: int) -> tuple[Submission, ...]: ...

    def pending_count(self) -> int: ...

    def pending_usage(self) -> tuple[int, int]: ...

    def pending_before(self, before: datetime) -> tuple[Submission, ...]: ...

    def active_before(self, before: datetime) -> tuple[Submission, ...]: ...

    def discard_content(self, submission_id: str) -> None: ...

    def claim_admission(
        self,
        source_key: str,
        *,
        occurred_at: datetime,
        reserved_bytes: int,
        policy: QueuePolicy,
        admission_id: str,
    ) -> UploadAdmission: ...

    def finalize_admission(
        self, admission_id: str, *, actual_bytes: int, occurred_at: datetime, policy: QueuePolicy
    ) -> None: ...

    def release_admission(self, admission_id: str) -> None: ...

    def events(self, submission_id: str) -> tuple[WorkflowEvent, ...]: ...


class CatalogPort(Protocol):
    def count(self) -> int: ...

    def contains_sha256(self, sha256: str) -> bool: ...

    def find_sha256(self, sha256: str) -> CatalogEntry | None: ...

    def analyze(self, item: CatalogItem) -> CatalogAnalysis: ...

    def publish(self, item: CatalogItem) -> CatalogEntry: ...


class InMemoryCatalog:
    """Deterministic catalog adapter for module tests and local composition."""

    def __init__(self, *, validator: Callable[[CatalogItem], bool] | None = None) -> None:
        self._entries: dict[str, CatalogEntry] = {}
        self._items: dict[str, CatalogItem] = {}
        self._validator = validator or (lambda item: bool(item.content))

    def contains_sha256(self, sha256: str) -> bool:
        return sha256 in self._entries

    def find_sha256(self, sha256: str) -> CatalogEntry | None:
        return self._entries.get(sha256)

    def count(self) -> int:
        return len(self._entries)

    def analyze(self, item: CatalogItem) -> CatalogAnalysis:
        valid = self._validator(item)
        return CatalogAnalysis(valid=valid, failure_code=None if valid else "invalid_schematic")

    def publish(self, item: CatalogItem) -> CatalogEntry:
        if item.sha256 in self._entries:
            raise CatalogPublicationError("catalog already contains this schematic")
        entry = CatalogEntry(catalog_id=f"catalog-{item.sha256[:12]}", sha256=item.sha256)
        self._entries[item.sha256] = entry
        self._items[entry.catalog_id] = item
        return entry

    def item(self, catalog_id: str) -> CatalogItem:
        return self._items[catalog_id]


class InMemorySubmissionStore:
    """In-memory adapter with the same optimistic-revision contract as SQLite."""

    def __init__(self) -> None:
        self._submissions: dict[str, Submission] = {}
        self._content: dict[str, bytes] = {}
        self._events: dict[str, list[WorkflowEvent]] = {}
        self._lock = threading.RLock()
        self._rate_events: dict[str, list[datetime]] = {}
        self._admissions: dict[str, tuple[datetime, int]] = {}

    def create(self, submission: Submission, content: bytes, event: WorkflowEvent) -> None:
        _validate_event(event)
        with self._lock:
            if submission.submission_id in self._submissions:
                raise ValueError("submission id already exists")
            self._submissions[submission.submission_id] = submission
            self._content[submission.submission_id] = bytes(content)
            self._events[submission.submission_id] = [event]

    def get(self, submission_id: str) -> Submission:
        with self._lock:
            try:
                return self._submissions[submission_id]
            except KeyError as exc:
                raise KeyError(f"unknown submission: {submission_id}") from exc

    def content(self, submission_id: str) -> bytes:
        with self._lock:
            try:
                return self._content[submission_id]
            except KeyError as exc:
                raise KeyError(f"unknown submission: {submission_id}") from exc

    def find_by_sha256(self, sha256: str) -> tuple[Submission, ...]:
        with self._lock:
            return tuple(item for item in self._submissions.values() if item.sha256 == sha256)

    def transition(
        self,
        submission_id: str,
        expected_revision: int,
        updated: Submission,
        event: WorkflowEvent,
    ) -> Submission:
        _validate_event(event)
        with self._lock:
            current = self.get(submission_id)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {current.revision}"
                )
            if updated.revision != expected_revision + 1:
                raise ValueError("a transition must increment revision exactly once")
            self._submissions[submission_id] = updated
            self._events[submission_id].append(event)
            return updated

    def pending(self, *, limit: int, offset: int) -> tuple[Submission, ...]:
        _validate_page(limit, offset)
        with self._lock:
            items = sorted(
                (
                    item
                    for item in self._submissions.values()
                    if item.state is SubmissionState.NEEDS_REVIEW
                ),
                key=lambda item: (item.created_at, item.submission_id),
            )
            return tuple(items[offset : offset + limit])

    def pending_count(self) -> int:
        with self._lock:
            return sum(
                item.state is SubmissionState.NEEDS_REVIEW for item in self._submissions.values()
            )

    def pending_usage(self) -> tuple[int, int]:
        with self._lock:
            pending = [item for item in self._submissions.values() if item.state in _PAYLOAD_STATES]
            return len(pending), sum(item.byte_size for item in pending)

    def pending_before(self, before: datetime) -> tuple[Submission, ...]:
        with self._lock:
            return tuple(
                item
                for item in self._submissions.values()
                if item.state is SubmissionState.NEEDS_REVIEW and item.created_at < before
            )

    def active_before(self, before: datetime) -> tuple[Submission, ...]:
        with self._lock:
            return tuple(
                item
                for item in self._submissions.values()
                if item.state in _PAYLOAD_STATES - {SubmissionState.NEEDS_REVIEW}
                and item.updated_at < before
            )

    def discard_content(self, submission_id: str) -> None:
        with self._lock:
            if submission_id not in self._content:
                raise KeyError(f"unknown submission: {submission_id}")
            self._content[submission_id] = b""

    def claim_admission(
        self,
        source_key: str,
        *,
        occurred_at: datetime,
        reserved_bytes: int,
        policy: QueuePolicy,
        admission_id: str,
    ) -> UploadAdmission:
        cutoff = occurred_at - policy.rate_window
        with self._lock:
            self._prune_admissions(occurred_at, policy)
            count, byte_size = self.pending_usage()
            active_bytes = sum(value[1] for value in self._admissions.values())
            if (
                len(self._admissions) >= policy.max_concurrent_uploads
                or count + len(self._admissions) >= policy.max_pending_count
                or byte_size + active_bytes + reserved_bytes > policy.max_pending_bytes
            ):
                raise SubmissionQuotaExceeded("schematic review queue is at capacity")
            recent = [value for value in self._rate_events.get(source_key, []) if value >= cutoff]
            if len(recent) >= policy.per_source_limit:
                self._rate_events[source_key] = recent
                raise SubmissionQuotaExceeded("schematic upload rate limit reached; try later")
            recent.append(occurred_at)
            self._rate_events[source_key] = recent
            self._admissions[admission_id] = (occurred_at, reserved_bytes)
            return UploadAdmission(admission_id, reserved_bytes)

    def finalize_admission(
        self, admission_id: str, *, actual_bytes: int, occurred_at: datetime, policy: QueuePolicy
    ) -> None:
        with self._lock:
            self._prune_admissions(occurred_at, policy)
            if admission_id not in self._admissions:
                raise SubmissionQuotaExceeded("schematic upload admission expired")
            started, _reserved = self._admissions[admission_id]
            self._admissions[admission_id] = (started, actual_bytes)
            count, byte_size = self.pending_usage()
            active_bytes = sum(value[1] for value in self._admissions.values())
            if (
                count + len(self._admissions) > policy.max_pending_count
                or byte_size + active_bytes > policy.max_pending_bytes
            ):
                self._admissions[admission_id] = (started, _reserved)
                raise SubmissionQuotaExceeded("schematic review queue is at capacity")

    def release_admission(self, admission_id: str) -> None:
        with self._lock:
            self._admissions.pop(admission_id, None)

    def _prune_admissions(self, now: datetime, policy: QueuePolicy) -> None:
        cutoff = now - policy.admission_ttl
        self._admissions = {
            key: value for key, value in self._admissions.items() if value[0] >= cutoff
        }

    def events(self, submission_id: str) -> tuple[WorkflowEvent, ...]:
        with self._lock:
            if submission_id not in self._events:
                raise KeyError(f"unknown submission: {submission_id}")
            return tuple(self._events[submission_id])


class SqliteSubmissionStore:
    """Durable SQLite store for quarantine metadata, payloads, and audit events."""

    def __init__(self, database: str) -> None:
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._connection:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schematic_submissions (
                    submission_id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    promotion_requested INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    requirements_json TEXT NOT NULL,
                    recommendations_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    catalog_id TEXT,
                    rejection_reason TEXT,
                    content BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS submissions_sha256
                    ON schematic_submissions (sha256);
                CREATE INDEX IF NOT EXISTS submissions_review_queue
                    ON schematic_submissions (state, created_at, submission_id);
                CREATE TABLE IF NOT EXISTS schematic_workflow_events (
                    event_id TEXT PRIMARY KEY,
                    submission_id TEXT NOT NULL REFERENCES schematic_submissions(submission_id),
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    facts_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schematic_rate_events (
                    source_key TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS schematic_rate_window
                    ON schematic_rate_events (source_key, occurred_at);
                CREATE TABLE IF NOT EXISTS schematic_upload_admissions (
                    admission_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    reserved_bytes INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS schematic_events_no_update
                BEFORE UPDATE ON schematic_workflow_events BEGIN
                    SELECT RAISE(ABORT, 'workflow events are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS schematic_events_no_delete
                BEFORE DELETE ON schematic_workflow_events BEGIN
                    SELECT RAISE(ABORT, 'workflow events are immutable');
                END;
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create(self, submission: Submission, content: bytes, event: WorkflowEvent) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO schematic_submissions VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                _submission_to_row(submission, content),
            )
            self._insert_event(event)

    def get(self, submission_id: str) -> Submission:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM schematic_submissions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown submission: {submission_id}")
        return _submission_from_row(row)

    def content(self, submission_id: str) -> bytes:
        with self._lock:
            row = self._connection.execute(
                "SELECT content FROM schematic_submissions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown submission: {submission_id}")
        return bytes(row["content"])

    def find_by_sha256(self, sha256: str) -> tuple[Submission, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM schematic_submissions WHERE sha256 = ? ORDER BY created_at",
                (sha256,),
            ).fetchall()
        return tuple(_submission_from_row(row) for row in rows)

    def transition(
        self,
        submission_id: str,
        expected_revision: int,
        updated: Submission,
        event: WorkflowEvent,
    ) -> Submission:
        if updated.revision != expected_revision + 1:
            raise ValueError("a transition must increment revision exactly once")
        values = _submission_update_values(updated)
        with self._lock, self._connection:
            result = self._connection.execute(
                """
                UPDATE schematic_submissions
                SET state = ?, requirements_json = ?, recommendations_json = ?,
                    revision = ?, updated_at = ?, catalog_id = ?, rejection_reason = ?
                WHERE submission_id = ? AND revision = ?
                """,
                (*values, submission_id, expected_revision),
            )
            if result.rowcount != 1:
                current = self._connection.execute(
                    "SELECT revision FROM schematic_submissions WHERE submission_id = ?",
                    (submission_id,),
                ).fetchone()
                if current is None:
                    raise KeyError(f"unknown submission: {submission_id}")
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {current['revision']}"
                )
            self._insert_event(event)
        return updated

    def pending(self, *, limit: int, offset: int) -> tuple[Submission, ...]:
        _validate_page(limit, offset)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM schematic_submissions
                WHERE state = ? ORDER BY created_at, submission_id LIMIT ? OFFSET ?
                """,
                (SubmissionState.NEEDS_REVIEW.value, limit, offset),
            ).fetchall()
        return tuple(_submission_from_row(row) for row in rows)

    def pending_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM schematic_submissions WHERE state = ?",
                (SubmissionState.NEEDS_REVIEW.value,),
            ).fetchone()
        return int(row["total"])

    def pending_usage(self) -> tuple[int, int]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS total, COALESCE(SUM(byte_size), 0) AS bytes
                FROM schematic_submissions
                WHERE state IN (?, ?, ?, ?, ?)
                """,
                tuple(state.value for state in _PAYLOAD_STATES),
            ).fetchone()
        return int(row["total"]), int(row["bytes"])

    def pending_before(self, before: datetime) -> tuple[Submission, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM schematic_submissions
                WHERE state = ? AND created_at < ? ORDER BY created_at, submission_id
                """,
                (SubmissionState.NEEDS_REVIEW.value, _format_time(before)),
            ).fetchall()
        return tuple(_submission_from_row(row) for row in rows)

    def active_before(self, before: datetime) -> tuple[Submission, ...]:
        states = _PAYLOAD_STATES - {SubmissionState.NEEDS_REVIEW}
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM schematic_submissions
                WHERE state IN (?, ?, ?, ?) AND updated_at < ?
                ORDER BY updated_at, submission_id
                """,
                (*tuple(state.value for state in states), _format_time(before)),
            ).fetchall()
        return tuple(_submission_from_row(row) for row in rows)

    def discard_content(self, submission_id: str) -> None:
        with self._lock, self._connection:
            result = self._connection.execute(
                "UPDATE schematic_submissions SET content = zeroblob(0) WHERE submission_id = ?",
                (submission_id,),
            )
            if result.rowcount != 1:
                raise KeyError(f"unknown submission: {submission_id}")

    def claim_admission(
        self,
        source_key: str,
        *,
        occurred_at: datetime,
        reserved_bytes: int,
        policy: QueuePolicy,
        admission_id: str,
    ) -> UploadAdmission:
        cutoff = _format_time(occurred_at - policy.rate_window)
        admission_cutoff = _format_time(occurred_at - policy.admission_ttl)
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM schematic_upload_admissions WHERE occurred_at < ?",
                (admission_cutoff,),
            )
            pending_count, pending_bytes = self.pending_usage()
            active = self._connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(reserved_bytes), 0) AS bytes "
                "FROM schematic_upload_admissions"
            ).fetchone()
            if (
                int(active["total"]) >= policy.max_concurrent_uploads
                or pending_count + int(active["total"]) >= policy.max_pending_count
                or pending_bytes + int(active["bytes"]) + reserved_bytes > policy.max_pending_bytes
            ):
                raise SubmissionQuotaExceeded("schematic review queue is at capacity")
            self._connection.execute(
                "DELETE FROM schematic_rate_events WHERE occurred_at < ?", (cutoff,)
            )
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM schematic_rate_events WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            if int(row["total"]) >= policy.per_source_limit:
                raise SubmissionQuotaExceeded("schematic upload rate limit reached; try later")
            self._connection.execute(
                "INSERT INTO schematic_rate_events VALUES (?, ?)",
                (source_key, _format_time(occurred_at)),
            )
            self._connection.execute(
                "INSERT INTO schematic_upload_admissions VALUES (?, ?, ?)",
                (admission_id, _format_time(occurred_at), reserved_bytes),
            )
            return UploadAdmission(admission_id, reserved_bytes)

    def finalize_admission(
        self, admission_id: str, *, actual_bytes: int, occurred_at: datetime, policy: QueuePolicy
    ) -> None:
        cutoff = _format_time(occurred_at - policy.admission_ttl)
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM schematic_upload_admissions WHERE occurred_at < ?", (cutoff,)
            )
            current = self._connection.execute(
                "SELECT reserved_bytes FROM schematic_upload_admissions WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()
            if current is None:
                raise SubmissionQuotaExceeded("schematic upload admission expired")
            pending_count, pending_bytes = self.pending_usage()
            active = self._connection.execute(
                """
                SELECT COUNT(*) AS total, COALESCE(SUM(reserved_bytes), 0) AS bytes
                FROM schematic_upload_admissions WHERE admission_id != ?
                """,
                (admission_id,),
            ).fetchone()
            if (
                pending_count + int(active["total"]) + 1 > policy.max_pending_count
                or pending_bytes + int(active["bytes"]) + actual_bytes > policy.max_pending_bytes
            ):
                raise SubmissionQuotaExceeded("schematic review queue is at capacity")
            self._connection.execute(
                "UPDATE schematic_upload_admissions SET reserved_bytes = ? WHERE admission_id = ?",
                (actual_bytes, admission_id),
            )

    def release_admission(self, admission_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM schematic_upload_admissions WHERE admission_id = ?",
                (admission_id,),
            )

    def events(self, submission_id: str) -> tuple[WorkflowEvent, ...]:
        self.get(submission_id)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM schematic_workflow_events
                WHERE submission_id = ? ORDER BY revision, occurred_at, event_id
                """,
                (submission_id,),
            ).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def _insert_event(self, event: WorkflowEvent) -> None:
        _validate_event(event)
        self._connection.execute(
            "INSERT INTO schematic_workflow_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.submission_id,
                _format_time(event.occurred_at),
                event.actor,
                event.action,
                event.from_state.value if event.from_state else None,
                event.to_state.value,
                event.revision,
                json.dumps(dict(event.facts), sort_keys=True, separators=(",", ":")),
            ),
        )


class CatalogWorkflow:
    """Application service coordinating analysis, review, and publication."""

    def __init__(
        self,
        store: SubmissionStore,
        catalog: CatalogPort,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        queue_policy: QueuePolicy | None = None,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._queue_policy = queue_policy or QueuePolicy()
        self._lock = threading.RLock()

    def submit(
        self,
        command: SubmitCommand,
        *,
        actor: str = "lan-user",
        source_key: str | None = None,
        admission_id: str | None = None,
    ) -> Submission:
        _validate_actor(actor)
        source = source_key or actor
        _validate_actor(source)
        content = bytes(command.content)
        submission_id = self._id_factory()
        digest = hashlib.sha256(content).hexdigest()
        metadata, recommendations = _autofill(command)
        if admission_id is None:
            admission_id = self.begin_upload(
                source_key=source, reserved_bytes=len(content)
            ).admission_id
        now = self._now()
        try:
            with self._lock:
                self.finalize_upload(admission_id, actual_bytes=len(content), occurred_at=now)
                return self._submit_admitted(
                    command,
                    actor=actor,
                    content=content,
                    now=now,
                    submission_id=submission_id,
                    metadata=metadata,
                    recommendations=recommendations,
                    digest=digest,
                )
        finally:
            # Explicit portal admissions and direct service admissions both end
            # here. Release is idempotent so request error cleanup may repeat it.
            self._store.release_admission(admission_id)

    def begin_upload(self, *, source_key: str, reserved_bytes: int) -> UploadAdmission:
        _validate_actor(source_key)
        if reserved_bytes < 0 or reserved_bytes > MAX_SCHEMATIC_BYTES:
            raise SubmissionQuotaExceeded("schematic upload size is outside policy")
        now = self._now()
        with self._lock:
            self._purge_expired_locked(now)
            return self._store.claim_admission(
                source_key,
                occurred_at=now,
                reserved_bytes=reserved_bytes,
                policy=self._queue_policy,
                admission_id=self._id_factory(),
            )

    def cancel_upload(self, admission_id: str) -> None:
        self._store.release_admission(admission_id)

    def finalize_upload(
        self,
        admission_id: str,
        *,
        actual_bytes: int,
        occurred_at: datetime | None = None,
    ) -> None:
        if actual_bytes < 0 or actual_bytes > MAX_SCHEMATIC_BYTES:
            raise SubmissionQuotaExceeded("schematic upload size is outside policy")
        self._store.finalize_admission(
            admission_id,
            actual_bytes=actual_bytes,
            occurred_at=occurred_at or self._now(),
            policy=self._queue_policy,
        )

    def _submit_admitted(
        self,
        command: SubmitCommand,
        *,
        actor: str,
        content: bytes,
        now: datetime,
        submission_id: str,
        metadata: SchematicMetadata,
        recommendations: tuple[Recommendation, ...],
        digest: str,
    ) -> Submission:
        duplicate = bool(self._store.find_by_sha256(digest))
        duplicate_lookup_available = True
        try:
            duplicate = duplicate or self._catalog.contains_sha256(digest)
        except CatalogAnalysisError:
            duplicate_lookup_available = False
        submission = Submission(
            submission_id=submission_id,
            sha256=digest,
            byte_size=len(content),
            metadata=metadata,
            promotion_requested=command.promotion_requested,
            state=SubmissionState.QUARANTINED,
            requirements=(),
            recommendations=recommendations,
            revision=0,
            created_at=now,
            updated_at=now,
        )
        self._store.create(
            submission,
            content,
            self._event(
                submission,
                actor=actor,
                action="submitted",
                from_state=None,
                facts={"promotion_requested": str(command.promotion_requested).lower()},
            ),
        )
        submission = self._move(
            submission, SubmissionState.ANALYZING, actor=actor, action="analysis_started"
        )
        catalog_item = self._catalog_item(submission)
        try:
            analysis = self._catalog.analyze(catalog_item)
        except CatalogAnalysisError:
            analysis = CatalogAnalysis(valid=False, failure_code="analyzer_unavailable")
        requirements = _requirements(
            submission,
            duplicate=duplicate,
            duplicate_lookup_available=duplicate_lookup_available,
            catalog_valid=analysis.valid,
            catalog_failure=analysis.failure_code,
        )
        if all(item.passed for item in requirements):
            submission = self._move(
                submission,
                SubmissionState.ELIGIBLE,
                actor=actor,
                action="analysis_passed",
                requirements=requirements,
            )
            if command.promotion_requested:
                return self._publish(submission, actor=actor, action="auto_publish")
            return self._move(
                submission,
                SubmissionState.NEEDS_REVIEW,
                actor=actor,
                action="queued_for_review",
            )
        return self._move(
            submission,
            SubmissionState.NEEDS_REVIEW,
            actor=actor,
            action="analysis_requires_review",
            requirements=requirements,
            facts={"failure_codes": _failed_codes(requirements)},
        )

    def get(self, submission_id: str) -> Submission:
        return self._store.get(submission_id)

    def content(self, submission_id: str) -> bytes:
        return self._store.content(submission_id)

    def pending(self, *, limit: int = 100, offset: int = 0) -> tuple[Submission, ...]:
        self.purge_expired()
        return self._store.pending(limit=limit, offset=offset)

    def pending_count(self) -> int:
        self.purge_expired()
        return self._store.pending_count()

    def purge_expired(self) -> int:
        with self._lock:
            return self._purge_expired_locked(self._now())

    def _purge_expired_locked(self, now: datetime) -> int:
        recovered = 0
        for submission in self._store.active_before(now - self._queue_policy.admission_ttl):
            if submission.state is SubmissionState.PUBLISHING:
                try:
                    entry = self._catalog.find_sha256(submission.sha256)
                except CatalogAnalysisError:
                    entry = None
                if entry is not None:
                    cataloged = self._move(
                        submission,
                        SubmissionState.CATALOGED,
                        actor="workflow-recovery",
                        action="publication_reconciled",
                        catalog_id=entry.catalog_id,
                        facts={"catalog_id": entry.catalog_id},
                    )
                    self._store.discard_content(cataloged.submission_id)
                    recovered += 1
                    continue
            self._move(
                submission,
                SubmissionState.NEEDS_REVIEW,
                actor="workflow-recovery",
                action="interrupted_recovered",
                facts={"failure_codes": "workflow_interrupted"},
            )
            recovered += 1
        expired = self._store.pending_before(now - self._queue_policy.retention)
        for submission in expired:
            rejected = self._move(
                submission,
                SubmissionState.REJECTED,
                actor="retention-policy",
                action="retention_expired",
                rejection_reason="retention_expired",
                facts={"reason_code": "retention_expired"},
            )
            self._store.discard_content(rejected.submission_id)
        return recovered + len(expired)

    def catalog_count(self) -> int:
        return self._catalog.count()

    def events(self, submission_id: str) -> tuple[WorkflowEvent, ...]:
        return self._store.events(submission_id)

    def publish(self, submission_id: str, *, actor: str, expected_revision: int) -> Submission:
        _validate_actor(actor)
        with self._lock:
            self._purge_expired_locked(self._now())
            submission = self._store.get(submission_id)
            _expect_revision(submission, expected_revision)
            if submission.state is not SubmissionState.NEEDS_REVIEW:
                raise InvalidTransition(f"cannot publish from {submission.state.value}")
            if not submission.eligible:
                raise RequirementsNotMet(_failed_codes(submission.requirements))
            return self._publish(submission, actor=actor, action="admin_publish")

    def reject(
        self,
        submission_id: str,
        *,
        actor: str,
        expected_revision: int,
        reason_code: str,
    ) -> Submission:
        _validate_actor(actor)
        if not _SAFE_REASON.fullmatch(reason_code):
            raise ValueError("reason_code must be a short machine-readable identifier")
        with self._lock:
            self._purge_expired_locked(self._now())
            submission = self._store.get(submission_id)
            _expect_revision(submission, expected_revision)
            if submission.state is not SubmissionState.NEEDS_REVIEW:
                raise InvalidTransition(f"cannot reject from {submission.state.value}")
            rejected = self._move(
                submission,
                SubmissionState.REJECTED,
                actor=actor,
                action="admin_rejected",
                rejection_reason=reason_code,
                facts={"reason_code": reason_code},
            )
            self._store.discard_content(rejected.submission_id)
            return rejected

    def _publish(self, submission: Submission, *, actor: str, action: str) -> Submission:
        publishing = self._move(
            submission, SubmissionState.PUBLISHING, actor=actor, action=f"{action}_started"
        )
        item = CatalogItem(
            submission_id=publishing.submission_id,
            sha256=publishing.sha256,
            content=self._store.content(publishing.submission_id),
            metadata=publishing.metadata,
        )
        try:
            entry = self._catalog.publish(item)
        except Exception as exc:
            try:
                reconciled = self._catalog.find_sha256(item.sha256)
            except CatalogAnalysisError:
                reconciled = None
            if reconciled is not None:
                cataloged = self._move(
                    publishing,
                    SubmissionState.CATALOGED,
                    actor="catalog-adapter",
                    action="publication_reconciled",
                    catalog_id=reconciled.catalog_id,
                    facts={"catalog_id": reconciled.catalog_id},
                )
                self._store.discard_content(cataloged.submission_id)
                return cataloged
            self._move(
                publishing,
                SubmissionState.NEEDS_REVIEW,
                actor="catalog-adapter",
                action="publication_failed",
                facts={"failure_codes": "catalog_unavailable"},
            )
            if isinstance(exc, CatalogPublicationError):
                raise
            raise CatalogPublicationError("catalog publication failed") from exc
        cataloged = self._move(
            publishing,
            SubmissionState.CATALOGED,
            actor=actor,
            action=f"{action}_completed",
            catalog_id=entry.catalog_id,
            facts={"catalog_id": entry.catalog_id},
        )
        self._store.discard_content(cataloged.submission_id)
        return cataloged

    def _catalog_item(self, submission: Submission) -> CatalogItem:
        return CatalogItem(
            submission_id=submission.submission_id,
            sha256=submission.sha256,
            content=self._store.content(submission.submission_id),
            metadata=submission.metadata,
        )

    def _move(
        self,
        submission: Submission,
        state: SubmissionState,
        *,
        actor: str,
        action: str,
        requirements: tuple[Requirement, ...] | None = None,
        catalog_id: str | None = None,
        rejection_reason: str | None = None,
        facts: Mapping[str, str] | None = None,
    ) -> Submission:
        updated = replace(
            submission,
            state=state,
            requirements=requirements if requirements is not None else submission.requirements,
            revision=submission.revision + 1,
            updated_at=self._now(),
            catalog_id=catalog_id if catalog_id is not None else submission.catalog_id,
            rejection_reason=(
                rejection_reason if rejection_reason is not None else submission.rejection_reason
            ),
        )
        event = self._event(
            updated,
            actor=actor,
            action=action,
            from_state=submission.state,
            facts=facts,
        )
        return self._store.transition(submission.submission_id, submission.revision, updated, event)

    def _event(
        self,
        submission: Submission,
        *,
        actor: str,
        action: str,
        from_state: SubmissionState | None,
        facts: Mapping[str, str] | None = None,
    ) -> WorkflowEvent:
        event = WorkflowEvent(
            event_id=self._id_factory(),
            submission_id=submission.submission_id,
            occurred_at=self._now(),
            actor=actor,
            action=action,
            from_state=from_state,
            to_state=submission.state,
            revision=submission.revision,
            facts=tuple(sorted((facts or {}).items())),
        )
        _validate_event(event)
        return event

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("workflow clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _autofill(command: SubmitCommand) -> tuple[SchematicMetadata, tuple[Recommendation, ...]]:
    filename = PurePosixPath(command.filename.replace("\\", "/")).name.strip()
    extension = PurePosixPath(filename).suffix.lower()
    recommended_title = _title_from_filename(filename)
    title = command.title.strip() or recommended_title
    supplied_tags = tuple(sorted({tag.strip().lower() for tag in command.tags if tag.strip()}))
    recommended_tags = tuple(sorted({"minecraft", extension.removeprefix(".")} - {""}))
    tags = supplied_tags or recommended_tags
    metadata = SchematicMetadata(
        filename=filename,
        title=title,
        description=command.description.strip(),
        tags=tags,
        license_id=command.license_id.strip() or "CC0-1.0",
        rights_attested=command.rights_attested,
    )
    return metadata, (
        Recommendation("title", recommended_title, not bool(command.title.strip())),
        Recommendation("tags", recommended_tags, not bool(supplied_tags)),
        Recommendation("license_id", "CC0-1.0", not bool(command.license_id.strip())),
    )


def _title_from_filename(filename: str) -> str:
    stem = PurePosixPath(filename).stem
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem)
    words = re.sub(r"[_-]+", " ", separated).split()
    return " ".join(word[:1].upper() + word[1:] for word in words) or "Untitled Schematic"


def _requirements(
    submission: Submission,
    *,
    duplicate: bool,
    duplicate_lookup_available: bool,
    catalog_valid: bool,
    catalog_failure: str | None,
) -> tuple[Requirement, ...]:
    metadata = submission.metadata
    extension = PurePosixPath(metadata.filename).suffix.lower()
    safe_filename = bool(metadata.filename) and metadata.filename not in {".", ".."}
    return (
        Requirement("nonempty_content", submission.byte_size > 0, "Upload a non-empty file."),
        Requirement(
            "size_limit",
            submission.byte_size <= MAX_SCHEMATIC_BYTES,
            "Keep the schematic at or below 250 MiB.",
        ),
        Requirement(
            "catalog_duplicate_lookup",
            duplicate_lookup_available,
            "Verify this digest against the catalog when the viewer is available.",
        ),
        Requirement(
            "supported_format",
            safe_filename and extension in SUPPORTED_EXTENSIONS,
            "Use .schem, .schematic, .litematic, .nbt, or a supported .zip.",
        ),
        Requirement(
            "catalog_validation",
            catalog_valid,
            (
                f"Resolve catalog validation failure: {catalog_failure}."
                if catalog_failure
                else "Submit a schematic the viewer can validate."
            ),
        ),
        Requirement(
            "title",
            bool(metadata.title) and len(metadata.title) <= 120,
            "Use a title between 1 and 120 characters.",
        ),
        Requirement(
            "rights_attestation",
            metadata.rights_attested,
            "Confirm the upload may be distributed without restrictions.",
        ),
        Requirement(
            "unrestricted_license",
            metadata.license_id in UNRESTRICTED_LICENSES,
            "Choose CC0-1.0, 0BSD, or Unlicense.",
        ),
        Requirement("unique_sha256", not duplicate, "Review the existing identical upload."),
    )


def _failed_codes(requirements: Sequence[Requirement]) -> str:
    return ",".join(item.code for item in requirements if not item.passed)


def _expect_revision(submission: Submission, expected: int) -> None:
    if submission.revision != expected:
        raise RevisionConflict(f"expected revision {expected}, found {submission.revision}")


def _validate_actor(actor: str) -> None:
    if not actor.strip() or len(actor) > 128 or any(character.isspace() for character in actor):
        raise ValueError("actor must be a compact non-empty subject identifier")


def _validate_event(event: WorkflowEvent) -> None:
    if not event.action or len(event.action) > 64:
        raise ValueError("event action is invalid")
    keys = [key for key, _ in event.facts]
    if len(keys) != len(set(keys)):
        raise ValueError("event fact keys must be unique")
    for key, value in event.facts:
        if key not in _SAFE_EVENT_FACTS:
            raise ValueError(f"event fact is not audit-safe: {key}")
        if len(value) > 256:
            raise ValueError("event fact is too long")


def _validate_page(limit: int, offset: int) -> None:
    if limit < 1 or limit > 500 or offset < 0:
        raise ValueError("limit must be 1..500 and offset must be non-negative")


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _metadata_json(metadata: SchematicMetadata) -> str:
    return json.dumps(
        {
            "description": metadata.description,
            "filename": metadata.filename,
            "license_id": metadata.license_id,
            "rights_attested": metadata.rights_attested,
            "tags": list(metadata.tags),
            "title": metadata.title,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _requirements_json(requirements: Sequence[Requirement]) -> str:
    return json.dumps(
        [
            {"code": item.code, "passed": item.passed, "recommendation": item.recommendation}
            for item in requirements
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _recommendations_json(recommendations: Sequence[Recommendation]) -> str:
    return json.dumps(
        [
            {"applied": item.applied, "field": item.field, "value": item.value}
            for item in recommendations
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _submission_to_row(submission: Submission, content: bytes) -> tuple[object, ...]:
    return (
        submission.submission_id,
        submission.sha256,
        submission.byte_size,
        _metadata_json(submission.metadata),
        int(submission.promotion_requested),
        submission.state.value,
        _requirements_json(submission.requirements),
        _recommendations_json(submission.recommendations),
        submission.revision,
        _format_time(submission.created_at),
        _format_time(submission.updated_at),
        submission.catalog_id,
        submission.rejection_reason,
        sqlite3.Binary(content),
    )


def _submission_update_values(submission: Submission) -> tuple[object, ...]:
    return (
        submission.state.value,
        _requirements_json(submission.requirements),
        _recommendations_json(submission.recommendations),
        submission.revision,
        _format_time(submission.updated_at),
        submission.catalog_id,
        submission.rejection_reason,
    )


def _submission_from_row(row: sqlite3.Row) -> Submission:
    metadata = json.loads(row["metadata_json"])
    requirements = json.loads(row["requirements_json"])
    recommendations = json.loads(row["recommendations_json"])
    return Submission(
        submission_id=row["submission_id"],
        sha256=row["sha256"],
        byte_size=row["byte_size"],
        metadata=SchematicMetadata(
            filename=metadata["filename"],
            title=metadata["title"],
            description=metadata["description"],
            tags=tuple(metadata["tags"]),
            license_id=metadata["license_id"],
            rights_attested=bool(metadata["rights_attested"]),
        ),
        promotion_requested=bool(row["promotion_requested"]),
        state=SubmissionState(row["state"]),
        requirements=tuple(Requirement(**item) for item in requirements),
        recommendations=tuple(
            Recommendation(
                field=item["field"],
                value=tuple(item["value"]) if isinstance(item["value"], list) else item["value"],
                applied=item["applied"],
            )
            for item in recommendations
        ),
        revision=row["revision"],
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
        catalog_id=row["catalog_id"],
        rejection_reason=row["rejection_reason"],
    )


def _event_from_row(row: sqlite3.Row) -> WorkflowEvent:
    facts = json.loads(row["facts_json"])
    return WorkflowEvent(
        event_id=row["event_id"],
        submission_id=row["submission_id"],
        occurred_at=_parse_time(row["occurred_at"]),
        actor=row["actor"],
        action=row["action"],
        from_state=SubmissionState(row["from_state"]) if row["from_state"] else None,
        to_state=SubmissionState(row["to_state"]),
        revision=row["revision"],
        facts=tuple(sorted(facts.items())),
    )
