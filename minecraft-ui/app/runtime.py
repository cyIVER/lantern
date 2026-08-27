"""Production composition for the Minecraft portal's adapters."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from .admin_session import AdminSessionAccess
from .audit_log import SqliteAuditLog
from .browser_trust import BrowserTrustPolicy
from .catalog_workflow import CatalogWorkflow, QueuePolicy, SqliteSubmissionStore
from .identity import (
    CookieMode,
    JsonFileIdentityDirectory,
    NamedAdminIdentity,
    SqliteSessionRevocations,
)
from .minecraft_control import LanternControlHttpAdapter
from .pelican_operations import PelicanOperationsHttpAdapter, SqliteBackupConsistencyRegistry
from .portal import ConfirmationTokens, MinecraftPortal, NamedViewerAccess
from .settings import Settings
from .viewer_catalog import ViewerCatalogHttpAdapter


@dataclass(slots=True)
class PortalRuntime:
    portal: MinecraftPortal
    viewer_access: AdminSessionAccess | NamedViewerAccess
    control: LanternControlHttpAdapter
    catalog_adapter: ViewerCatalogHttpAdapter
    operations: PelicanOperationsHttpAdapter | None
    backup_registry: SqliteBackupConsistencyRegistry | None

    async def aclose(self) -> None:
        await self.control.aclose()
        self.catalog_adapter.close()
        if self.operations is not None:
            await self.operations.aclose()
        if self.backup_registry is not None:
            self.backup_registry.close()


def build_runtime(settings: Settings) -> PortalRuntime:
    """Validate configuration and compose the production portal adapters."""
    session_secret = _optional_secret(settings.session_secret_file)
    viewer_token = _optional_text(settings.viewer_admin_token_file)
    if settings.admin_users_file is not None and not settings.trusted_browser_origins:
        raise ValueError("named administration requires trusted browser origins")
    # Legacy/unconfigured factories retain request-derived behavior so test
    # adapters and disabled-admin health checks remain injectable.
    browser_trust = BrowserTrustPolicy(
        settings.trusted_browser_origins if settings.admin_users_file is not None else None
    )

    # Persistent state is mandatory in a configured deployment. Unconfigured
    # developer/test factories remain in memory and do not write to /data.
    configured = settings.admin_users_file is not None or settings.pelican_api_key_file is not None
    if configured:
        settings.portal_data_dir.mkdir(parents=True, exist_ok=True)
        database = str(settings.portal_data_dir / "portal.sqlite3")
    else:
        database = ":memory:"

    identity: NamedAdminIdentity | None = None
    viewer_access: AdminSessionAccess | NamedViewerAccess
    if settings.admin_users_file is not None:
        if session_secret is None or viewer_token is None:
            raise ValueError("named administration requires session and viewer secrets")
        if not (settings.secure_cookie or settings.allow_insecure_admin):
            raise ValueError("named administration requires an approved browser transport")
        identity = NamedAdminIdentity(
            JsonFileIdentityDirectory(settings.admin_users_file),
            session_secret=session_secret,
            ttl_seconds=settings.session_ttl_seconds,
            cookie_mode=(CookieMode.SECURE if settings.secure_cookie else CookieMode.TRUSTED_LAN),
            revocations=SqliteSessionRevocations(database),
        )
        viewer_access = NamedViewerAccess(identity, viewer_token, browser_trust=browser_trust)
    else:
        viewer_access = AdminSessionAccess.from_settings(settings)

    catalog_adapter = ViewerCatalogHttpAdapter(
        settings.viewer_url,
        admin_token_file=settings.viewer_admin_token_file,
    )
    catalog = CatalogWorkflow(
        SqliteSubmissionStore(database),
        catalog_adapter,
        queue_policy=QueuePolicy(
            max_pending_count=settings.schematic_queue_max_count,
            max_pending_bytes=settings.schematic_queue_max_bytes,
            per_source_limit=settings.schematic_uploads_per_ip,
            rate_window=timedelta(seconds=settings.schematic_rate_window_seconds),
            retention=timedelta(seconds=settings.schematic_retention_seconds),
            max_concurrent_uploads=settings.schematic_max_concurrent_uploads,
            admission_ttl=timedelta(seconds=settings.schematic_admission_ttl_seconds),
        ),
    )
    audit = SqliteAuditLog(database)
    control = LanternControlHttpAdapter(settings.lantern_control_url)
    operations = None
    backup_registry = None
    if settings.pelican_api_key_file is not None:
        backup_registry = SqliteBackupConsistencyRegistry(database)
        operations = PelicanOperationsHttpAdapter(
            settings.pelican_url,
            token_file=settings.pelican_api_key_file,
            server_name=settings.pelican_server_name,
            virtual_host=settings.pelican_virtual_host,
            allowed_upload_origins=settings.pelican_upload_origins,
            consistency_registry=backup_registry,
        )
    confirmation_secret = session_secret or secrets.token_bytes(32)
    portal = MinecraftPortal(
        identity=identity,
        control=control,
        catalog=catalog,
        audit=audit,
        confirmations=ConfirmationTokens(confirmation_secret),
        operations=operations,
        browser_trust=browser_trust,
    )
    return PortalRuntime(
        portal=portal,
        viewer_access=viewer_access,
        control=control,
        catalog_adapter=catalog_adapter,
        operations=operations,
        backup_registry=backup_registry,
    )


def _optional_secret(path) -> bytes | None:
    return path.read_bytes().strip() if path is not None else None


def _optional_text(path) -> str | None:
    return path.read_text(encoding="utf-8").strip() if path is not None else None
