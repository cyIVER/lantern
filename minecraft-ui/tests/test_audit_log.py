import sqlite3
from datetime import UTC, datetime

import pytest

from app.audit_log import InMemoryAuditLog, SqliteAuditLog


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def test_audit_log_records_named_and_anonymous_actions_without_content() -> None:
    audit = InMemoryAuditLog(clock=lambda: NOW, id_factory=lambda: "event-1")
    audit.append(
        actor="iveri",
        action="file.save",
        target="config/server.toml",
        outcome="done",
        correlation_id="request-1",
        details={"old_sha256": "a" * 64, "new_sha256": "b" * 64},
    )

    record = audit.recent()[0]
    assert record.actor == "iveri"
    assert dict(record.details) == {"new_sha256": "b" * 64, "old_sha256": "a" * 64}


def test_audit_rejects_unapproved_detail_fields() -> None:
    audit = InMemoryAuditLog(clock=lambda: NOW)
    with pytest.raises(ValueError, match="not safe"):
        audit.append(
            actor="iveri",
            action="file.save",
            target="server.properties",
            outcome="done",
            correlation_id="request-1",
            details={"content": "secret-like file contents"},
        )


def test_sqlite_audit_is_durable_ordered_and_immutable(tmp_path) -> None:
    database = tmp_path / "portal.sqlite3"
    ids = iter(("event-1", "event-2"))
    audit = SqliteAuditLog(str(database), clock=lambda: NOW, id_factory=lambda: next(ids))
    for action in ("minecraft.start", "minecraft.stop"):
        audit.append(
            actor="lan-user",
            action=action,
            target="minecraft",
            outcome="done",
            correlation_id=action,
        )

    reopened = SqliteAuditLog(str(database))
    assert [item.action for item in reopened.recent()] == [
        "minecraft.stop",
        "minecraft.start",
    ]

    connection = sqlite3.connect(database)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("DELETE FROM minecraft_audit_events")


@pytest.mark.parametrize("factory", [InMemoryAuditLog, SqliteAuditLog])
def test_idempotency_claim_is_atomic_and_durable(factory, tmp_path) -> None:
    audit = (
        factory(str(tmp_path / "claims.sqlite3"), clock=lambda: NOW)
        if factory is SqliteAuditLog
        else factory(clock=lambda: NOW)
    )

    claim = {
        "actor": "iveri",
        "action": "restore.execute",
        "target": "backup-1",
        "correlation_id": "request-12345678",
    }
    assert audit.claim(**claim) is True
    assert audit.claim(**claim) is False
    audit.append(**claim, outcome="done")
    assert {record.outcome for record in audit.recent()} == {"done", "requested"}

    if factory is SqliteAuditLog:
        reopened = SqliteAuditLog(str(tmp_path / "claims.sqlite3"))
        assert reopened.claim(**claim) is False
