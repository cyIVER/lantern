from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.catalog_workflow import (
    CatalogAnalysisError,
    CatalogPublicationError,
    CatalogWorkflow,
    InMemoryCatalog,
    InMemorySubmissionStore,
    QueuePolicy,
    RequirementsNotMet,
    RevisionConflict,
    SqliteSubmissionStore,
    SubmissionQuotaExceeded,
    SubmissionState,
    SubmitCommand,
)


class DeterministicRuntime:
    def __init__(self) -> None:
        self._counter = 0
        self._now = datetime(2026, 8, 27, 17, 0, tzinfo=UTC)

    def identifier(self) -> str:
        self._counter += 1
        return f"id-{self._counter:03d}"

    def clock(self) -> datetime:
        self._now += timedelta(microseconds=1)
        return self._now


def workflow(store=None, catalog=None):
    runtime = DeterministicRuntime()
    return CatalogWorkflow(
        store or InMemorySubmissionStore(),
        catalog or InMemoryCatalog(),
        clock=runtime.clock,
        id_factory=runtime.identifier,
    )


def valid_command(**changes) -> SubmitCommand:
    values = {
        "filename": "compact_iron_farm.schem",
        "content": b"safe schematic fixture",
        "rights_attested": True,
    }
    values.update(changes)
    return SubmitCommand(**values)


def test_requested_promotion_autofills_and_catalogs() -> None:
    catalog = InMemoryCatalog()
    service = workflow(catalog=catalog)

    result = service.submit(valid_command(promotion_requested=True), actor="lan-user-7")

    assert result.state is SubmissionState.CATALOGED
    assert result.metadata.title == "Compact Iron Farm"
    assert result.metadata.tags == ("minecraft", "schem")
    assert result.catalog_id == f"catalog-{result.sha256[:12]}"
    assert catalog.item(result.catalog_id).content == b"safe schematic fixture"
    assert service.content(result.submission_id) == b""
    assert [event.to_state for event in service.events(result.submission_id)] == [
        SubmissionState.QUARANTINED,
        SubmissionState.ANALYZING,
        SubmissionState.ELIGIBLE,
        SubmissionState.PUBLISHING,
        SubmissionState.CATALOGED,
    ]


def test_valid_unrequested_submission_waits_for_admin_review() -> None:
    service = workflow()
    queued = service.submit(valid_command(), actor="lan-user-7")

    assert queued.state is SubmissionState.NEEDS_REVIEW
    assert queued.eligible
    assert service.pending_count() == 1
    assert service.pending() == (queued,)

    published = service.publish(
        queued.submission_id, actor="admin-alpha", expected_revision=queued.revision
    )

    assert published.state is SubmissionState.CATALOGED
    assert service.pending_count() == 0


def test_hard_failures_are_queued_and_cannot_be_published() -> None:
    service = workflow()
    queued = service.submit(
        valid_command(filename="readme.txt", rights_attested=False, license_id="restricted"),
        actor="lan-user-7",
    )

    failures = {item.code for item in queued.requirements if not item.passed}
    assert failures == {
        "rights_attestation",
        "supported_format",
        "unrestricted_license",
    }
    assert queued.state is SubmissionState.NEEDS_REVIEW
    with pytest.raises(RequirementsNotMet, match="supported_format"):
        service.publish(
            queued.submission_id, actor="admin-alpha", expected_revision=queued.revision
        )


def test_catalog_analysis_is_a_hard_requirement() -> None:
    catalog = InMemoryCatalog(validator=lambda item: False)
    service = workflow(catalog=catalog)

    queued = service.submit(valid_command(promotion_requested=True), actor="lan-user-7")

    validation = next(item for item in queued.requirements if item.code == "catalog_validation")
    assert validation.passed is False
    assert queued.state is SubmissionState.NEEDS_REVIEW


def test_duplicate_sha256_is_a_hard_requirement() -> None:
    service = workflow()
    first = service.submit(valid_command(), actor="lan-user-7")
    duplicate = service.submit(
        valid_command(filename="renamed.schem", promotion_requested=True), actor="lan-user-8"
    )

    assert first.sha256 == duplicate.sha256
    assert duplicate.state is SubmissionState.NEEDS_REVIEW
    assert (
        next(item for item in duplicate.requirements if item.code == "unique_sha256").passed
        is False
    )


def test_reject_requires_current_revision_and_records_safe_reason() -> None:
    service = workflow()
    queued = service.submit(valid_command(), actor="lan-user-7")

    with pytest.raises(RevisionConflict):
        service.reject(
            queued.submission_id,
            actor="admin-alpha",
            expected_revision=queued.revision - 1,
            reason_code="duplicate_upload",
        )

    rejected = service.reject(
        queued.submission_id,
        actor="admin-alpha",
        expected_revision=queued.revision,
        reason_code="duplicate_upload",
    )
    assert rejected.state is SubmissionState.REJECTED
    assert rejected.rejection_reason == "duplicate_upload"
    assert service.content(rejected.submission_id) == b""
    assert service.events(queued.submission_id)[-1].facts == (("reason_code", "duplicate_upload"),)


def test_audit_events_never_contain_content_or_freeform_metadata() -> None:
    secret_marker = "marker-that-must-not-enter-audit"
    service = workflow()
    result = service.submit(
        valid_command(
            content=secret_marker.encode(),
            description=secret_marker,
            title=secret_marker,
        ),
        actor="lan-user-7",
    )

    serialized_events = repr(service.events(result.submission_id))
    assert secret_marker not in serialized_events
    assert result.sha256 not in serialized_events


def test_catalog_failure_returns_submission_to_review() -> None:
    class FailingCatalog(InMemoryCatalog):
        def publish(self, item):
            raise OSError("viewer unavailable")

    service = workflow(catalog=FailingCatalog())
    with pytest.raises(CatalogPublicationError, match="catalog publication failed"):
        service.submit(valid_command(promotion_requested=True), actor="lan-user-7")

    [queued] = service.pending()
    assert queued.state is SubmissionState.NEEDS_REVIEW
    assert service.events(queued.submission_id)[-1].facts == (
        ("failure_codes", "catalog_unavailable"),
    )


def test_duplicate_lookup_outage_durably_queues_upload_for_review() -> None:
    class LookupOutageCatalog(InMemoryCatalog):
        def contains_sha256(self, sha256):
            raise CatalogAnalysisError("viewer unavailable")

    service = workflow(catalog=LookupOutageCatalog())
    queued = service.submit(valid_command(promotion_requested=True), actor="lan-user-7")

    assert queued.state is SubmissionState.NEEDS_REVIEW
    assert service.content(queued.submission_id) == b"safe schematic fixture"
    requirement = next(
        item for item in queued.requirements if item.code == "catalog_duplicate_lookup"
    )
    assert requirement.passed is False


def test_ambiguous_publication_failure_reconciles_exact_remote_digest() -> None:
    class PublishedThenDisconnected(InMemoryCatalog):
        def publish(self, item):
            super().publish(item)
            raise OSError("connection lost after response")

    service = workflow(catalog=PublishedThenDisconnected())
    result = service.submit(valid_command(promotion_requested=True), actor="lan-user-7")

    assert result.state is SubmissionState.CATALOGED
    assert result.catalog_id == f"catalog-{result.sha256[:12]}"
    assert service.events(result.submission_id)[-1].action == "publication_reconciled"


def test_queue_enforces_rate_count_bytes_and_retention_without_losing_history() -> None:
    now = datetime(2026, 8, 27, 17, 0, tzinfo=UTC)
    store = InMemorySubmissionStore()
    policy = QueuePolicy(
        max_pending_count=1,
        max_pending_bytes=30,
        per_source_limit=1,
        rate_window=timedelta(hours=1),
        retention=timedelta(minutes=30),
    )
    service = CatalogWorkflow(store, InMemoryCatalog(), clock=lambda: now, queue_policy=policy)
    first = service.submit(valid_command(), actor="u1", source_key="peer-1")

    with pytest.raises(SubmissionQuotaExceeded, match="capacity"):
        service.submit(valid_command(content=b"different"), actor="u2", source_key="peer-2")

    now += timedelta(minutes=31)
    second = service.submit(valid_command(content=b"different"), actor="u2", source_key="peer-2")
    assert second.state is SubmissionState.NEEDS_REVIEW
    expired = service.get(first.submission_id)
    assert expired.state is SubmissionState.REJECTED
    assert expired.rejection_reason == "retention_expired"
    assert service.content(first.submission_id) == b""
    assert service.events(first.submission_id)[-1].action == "retention_expired"

    service.reject(
        second.submission_id,
        actor="admin",
        expected_revision=second.revision,
        reason_code="review_complete",
    )

    with pytest.raises(SubmissionQuotaExceeded, match="rate limit"):
        service.submit(valid_command(content=b"third"), actor="u2", source_key="peer-2")


def test_sqlite_adapter_round_trips_and_enforces_immutable_events(tmp_path) -> None:
    store = SqliteSubmissionStore(str(tmp_path / "catalog.db"))
    service = workflow(store=store)
    queued = service.submit(valid_command(), actor="lan-user-7")

    restored = service.get(queued.submission_id)
    assert restored == queued
    assert service.pending_count() == 1
    assert len(service.events(queued.submission_id)) == 4

    # Contract proof against accidental future store mutations.
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), store._connection:
        store._connection.execute(
            "DELETE FROM schematic_workflow_events WHERE submission_id = ?",
            (queued.submission_id,),
        )
    store.close()


def test_sqlite_adapter_rejects_stale_admin_write(tmp_path) -> None:
    store = SqliteSubmissionStore(str(tmp_path / "catalog.db"))
    service = workflow(store=store)
    queued = service.submit(valid_command(), actor="lan-user-7")
    rejected = service.reject(
        queued.submission_id,
        actor="admin-alpha",
        expected_revision=queued.revision,
        reason_code="not_useful",
    )

    assert rejected.state is SubmissionState.REJECTED
    with pytest.raises(RevisionConflict):
        service.reject(
            queued.submission_id,
            actor="admin-beta",
            expected_revision=queued.revision,
            reason_code="duplicate_upload",
        )
    store.close()


def test_sqlite_rate_limit_survives_service_restart(tmp_path) -> None:
    database = str(tmp_path / "catalog.db")
    now = datetime(2026, 8, 27, 17, 0, tzinfo=UTC)
    policy = QueuePolicy(per_source_limit=1)
    first_store = SqliteSubmissionStore(database)
    CatalogWorkflow(first_store, InMemoryCatalog(), clock=lambda: now, queue_policy=policy).submit(
        valid_command(), actor="lan-user-7", source_key="peer-stable-hash"
    )
    first_store.close()

    second_store = SqliteSubmissionStore(database)
    restarted = CatalogWorkflow(
        second_store, InMemoryCatalog(), clock=lambda: now, queue_policy=policy
    )
    with pytest.raises(SubmissionQuotaExceeded, match="rate limit"):
        restarted.submit(
            valid_command(content=b"different"),
            actor="lan-user-7",
            source_key="peer-stable-hash",
        )
    second_store.close()


def test_pre_body_admission_claims_rate_and_global_concurrency() -> None:
    policy = QueuePolicy(per_source_limit=1, max_concurrent_uploads=1)
    service = CatalogWorkflow(InMemorySubmissionStore(), InMemoryCatalog(), queue_policy=policy)
    held = service.begin_upload(source_key="peer-one", reserved_bytes=10)

    with pytest.raises(SubmissionQuotaExceeded, match="capacity"):
        service.begin_upload(source_key="peer-two", reserved_bytes=10)
    service.cancel_upload(held.admission_id)
    admitted = service.begin_upload(source_key="peer-two", reserved_bytes=10)
    service.cancel_upload(admitted.admission_id)
    with pytest.raises(SubmissionQuotaExceeded, match="rate limit"):
        service.begin_upload(source_key="peer-two", reserved_bytes=10)


def test_actual_payload_resize_is_atomic_across_active_admissions() -> None:
    policy = QueuePolicy(
        max_pending_count=3,
        max_pending_bytes=10,
        max_concurrent_uploads=2,
    )
    service = CatalogWorkflow(InMemorySubmissionStore(), InMemoryCatalog(), queue_policy=policy)
    first = service.begin_upload(source_key="peer-one", reserved_bytes=2)
    second = service.begin_upload(source_key="peer-two", reserved_bytes=2)
    now = datetime.now(UTC)
    service.finalize_upload(first.admission_id, actual_bytes=8, occurred_at=now)
    with pytest.raises(SubmissionQuotaExceeded, match="capacity"):
        service.finalize_upload(second.admission_id, actual_bytes=8, occurred_at=now)
    service.cancel_upload(first.admission_id)
    service.finalize_upload(second.admission_id, actual_bytes=8, occurred_at=now)
    service.cancel_upload(second.admission_id)


def test_interrupted_analysis_is_recovered_into_durable_review_queue() -> None:
    class CrashingCatalog(InMemoryCatalog):
        def analyze(self, item):
            raise OSError("worker terminated")

    now = datetime(2026, 8, 27, 17, 0, tzinfo=UTC)
    store = InMemorySubmissionStore()
    policy = QueuePolicy(admission_ttl=timedelta(minutes=10))
    service = CatalogWorkflow(store, CrashingCatalog(), clock=lambda: now, queue_policy=policy)
    with pytest.raises(OSError, match="worker terminated"):
        service.submit(valid_command(), source_key="peer-one")

    [interrupted] = store.active_before(now + timedelta(minutes=1))
    assert interrupted.state is SubmissionState.ANALYZING
    now += timedelta(minutes=11)
    assert service.pending_count() == 1
    recovered = service.get(interrupted.submission_id)
    assert recovered.state is SubmissionState.NEEDS_REVIEW
    assert service.content(recovered.submission_id) == b"safe schematic fixture"
    assert service.events(recovered.submission_id)[-1].action == "interrupted_recovered"
