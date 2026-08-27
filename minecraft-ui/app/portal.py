"""Deep Minecraft portal module exposed through one workspace and one intent seam."""

from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import asdict
from typing import Any

import anyio
from fastapi import HTTPException, Request, Response

from .admin_session import LoginRateLimiter
from .audit_log import AuditLog
from .browser_trust import BrowserTrustPolicy
from .catalog_workflow import CatalogWorkflow, SubmissionQuotaExceeded, SubmitCommand
from .identity import COOKIE_NAME, AdminPrincipal, IdentityError, NamedAdminIdentity
from .minecraft_control import MinecraftControl, MinecraftControlError
from .pelican_operations import PelicanOperationsError

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_POWER_ACTIONS = frozenset({"start", "stop", "restart"})


class NamedViewerAccess:
    """Authorize private viewer mutations from named portal sessions."""

    def __init__(
        self,
        identity: NamedAdminIdentity,
        viewer_token: str,
        *,
        browser_trust: BrowserTrustPolicy | None = None,
    ) -> None:
        if len(viewer_token.encode("utf-8")) < 32:
            raise ValueError("viewer token must contain at least 32 bytes")
        self._identity = identity
        self._viewer_token = viewer_token
        self._browser_trust = browser_trust or BrowserTrustPolicy()

    @property
    def enabled(self) -> bool:
        return True

    def principal(self, request: Request) -> AdminPrincipal | None:
        try:
            return self._identity.resolve(request.cookies.get(COOKIE_NAME, ""))
        except IdentityError:
            return None

    def is_admin(self, request: Request) -> bool:
        principal = self.principal(request)
        return principal is not None and principal.role == "admin"

    def require_same_origin(self, request: Request) -> None:
        self._browser_trust.require_same_origin(request)

    def viewer_credential(self, request: Request, path: str) -> str | None:
        del path
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            self.require_same_origin(request)
        return self._viewer_token if self.is_admin(request) else None

    def revoke(self, token: str) -> None:
        """Invalidate a named session through the server-side registry."""
        self._identity.revoke(token)


class ConfirmationTokens:
    """Issue short-lived challenges bound to the peer and exact power action."""

    def __init__(self, secret: bytes, *, ttl_seconds: int = 120) -> None:
        if len(secret) < 32:
            raise ValueError("confirmation secret must contain at least 32 bytes")
        self._secret = bytes(secret)
        self._ttl = ttl_seconds

    def issue(self, *, action: str, peer: str, correlation_id: str) -> str:
        payload = json.dumps(
            {
                "action": action,
                "correlation_id": correlation_id,
                "exp": int(time.time()) + self._ttl,
                "jti": secrets.token_hex(16),
                "peer": peer,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return f"{_encode(payload)}.{_encode(signature)}"

    def verify(self, token: str, *, action: str, peer: str, correlation_id: str) -> bool:
        try:
            payload_text, signature_text = token.split(".", 1)
            payload = _decode(payload_text)
            signature = _decode(signature_text)
            expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
            claims = json.loads(payload)
            return (
                hmac.compare_digest(signature, expected)
                and claims
                == {
                    "action": action,
                    "correlation_id": correlation_id,
                    "peer": peer,
                    "exp": claims.get("exp"),
                    "jti": claims.get("jti"),
                }
                and type(claims["exp"]) is int
                and isinstance(claims["jti"], str)
                and len(claims["jti"]) == 32
                and claims["exp"] >= time.time()
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False


class MinecraftPortal:
    """Compose identity, operations, catalog attention, and audit behind one interface."""

    def __init__(
        self,
        *,
        identity: NamedAdminIdentity | None,
        control: MinecraftControl,
        catalog: CatalogWorkflow,
        audit: AuditLog,
        confirmations: ConfirmationTokens,
        operations: Any | None = None,
        login_limiter: LoginRateLimiter | None = None,
        browser_trust: BrowserTrustPolicy | None = None,
    ) -> None:
        self.identity = identity
        self.control = control
        self.catalog = catalog
        self.audit = audit
        self.confirmations = confirmations
        self.operations = operations
        self.login_limiter = login_limiter or LoginRateLimiter()
        # Power and offline maintenance share one process-wide critical section.
        # The production container runs one worker, so a guest cannot start or
        # restart Minecraft between the offline proof and backup/restore.
        self._maintenance_lock = asyncio.Lock()
        self.browser_trust = browser_trust or BrowserTrustPolicy()

    def require_trusted_host(self, request: Request) -> None:
        """Enforce the configured Host allowlist before routing portal traffic."""
        self.browser_trust.require_host(request)

    def require_same_origin(self, request: Request) -> None:
        """Authorize an unsafe browser request before its body is buffered."""
        self.browser_trust.require_same_origin(request)

    def require_mod_upload(self, request: Request) -> AdminPrincipal:
        """Authorize a mod upload before reading its potentially large body."""
        self.require_same_origin(request)
        return self._require_admin(request)

    async def workspace(self, request: Request) -> dict[str, Any]:
        principal = self._principal(request)
        try:
            minecraft = await self.control.inspect()
        except MinecraftControlError as exc:
            minecraft = None
            minecraft_error = str(exc)
        else:
            minecraft_error = None

        pending_count = await anyio.to_thread.run_sync(self.catalog.pending_count)
        try:
            catalog_count = await anyio.to_thread.run_sync(self.catalog.catalog_count)
        except RuntimeError:
            catalog_count = 0
        admin: dict[str, Any] | None = None
        if principal and principal.role == "admin":
            pending = await anyio.to_thread.run_sync(self.catalog.pending)
            recent_audit = await anyio.to_thread.run_sync(self.audit.recent)
            admin = {
                "attention": [_submission_view(item) for item in pending],
                "files": {},
                "mods": {},
                "restores": [],
                "jobs": [],
                "audit": [
                    {
                        "actor": record.actor,
                        "action": record.action,
                        "target": record.target,
                        "outcome": record.outcome,
                        "at": record.occurred_at.isoformat(),
                    }
                    for record in recent_audit
                ],
            }
            if self.operations is not None:
                try:
                    files, mods, backups = await asyncio.gather(
                        self.operations.list_files(),
                        self.operations.list_mods(),
                        self.operations.list_backups(),
                    )
                    admin.update(
                        {
                            "files": {"entries": [asdict(item) for item in files]},
                            "mods": {"entries": [asdict(item) for item in mods]},
                            "restores": [asdict(item) for item in backups],
                        }
                    )
                except PelicanOperationsError as exc:
                    admin["attention"].append(
                        {"title": "Minecraft administration unavailable", "code": exc.code}
                    )

        return {
            "revision": str(int(time.time())),
            "session": {
                "enabled": self.identity is not None,
                "authenticated": principal is not None,
                "actor": principal.username if principal else None,
                "role": principal.role if principal else "guest",
            },
            "minecraft": {
                "state": minecraft.state if minecraft else "unknown",
                "available": minecraft.available if minecraft else False,
                "players": minecraft.players if minecraft else None,
                "allowed": sorted(_POWER_ACTIONS),
                **({"detail": minecraft_error} if minecraft_error else {}),
            },
            "schematics": {
                "pendingCount": pending_count,
                "catalogCount": catalog_count,
            },
            "admin": admin,
        }

    async def execute(self, request: Request, response: Response, intent: dict[str, Any]) -> dict:
        self.require_same_origin(request)
        intent_type = intent.get("type")
        if intent_type == "session.login":
            return await self._login(request, response, intent)
        if intent_type == "session.logout":
            if self.identity is not None:
                try:
                    await anyio.to_thread.run_sync(
                        self.identity.revoke, request.cookies.get(COOKIE_NAME, "")
                    )
                except IdentityError:
                    pass
            response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
            return {"outcome": "done", "notice": "Signed out"}
        if intent_type == "minecraft.power":
            return await self._power(request, intent)
        if intent_type == "schematic.review":
            return await self._review(request, intent)
        if isinstance(intent_type, str) and intent_type.startswith(
            ("file.", "mods.", "backup.", "restore.")
        ):
            return await self._admin_operation(request, intent)
        raise HTTPException(422, {"code": "invalid_intent", "message": "unknown portal intent"})

    async def submit_schematic(
        self,
        request: Request,
        *,
        filename: str,
        content: bytes,
        promote: bool,
        metadata: dict[str, Any],
        admission_id: str | None = None,
    ) -> dict[str, Any]:
        self.require_same_origin(request)
        principal = self._principal(request)
        peer = request.client.host if request.client else "unknown"
        actor = principal.username if principal else f"lan-{_safe_peer(peer)}"
        raw_tags = metadata.get("tags", ())
        if not isinstance(raw_tags, (list, tuple)) or not all(
            isinstance(tag, str) for tag in raw_tags
        ):
            raise HTTPException(422, "schematic tags must be a list of strings")
        command = SubmitCommand(
            filename=filename,
            content=content,
            promotion_requested=promote,
            rights_attested=True,
            title=str(metadata.get("title", "")),
            description=str(metadata.get("description", "")),
            tags=tuple(raw_tags),
            license_id="CC0-1.0",
        )
        try:
            submission = await anyio.to_thread.run_sync(
                lambda: self.catalog.submit(
                    command,
                    actor=actor,
                    source_key=f"peer-{_safe_peer(peer)}",
                    admission_id=admission_id,
                )
            )
        except SubmissionQuotaExceeded as exc:
            status = 429 if "rate limit" in str(exc) else 507
            raise HTTPException(
                status, {"code": "submission_capacity", "message": str(exc)}
            ) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(422, {"code": "submission_invalid", "message": str(exc)}) from exc
        return {
            "outcome": "done",
            "notice": (
                "Schematic published to the catalog"
                if submission.state.value == "cataloged"
                else "Schematic submitted for administrator review"
            ),
            "submission": _submission_view(submission),
        }

    async def admit_schematic_upload(self, request: Request, *, maximum_body_bytes: int) -> str:
        """Claim rate, concurrency, and worst-case payload capacity before buffering."""
        self.require_same_origin(request)
        declared = request.headers.get("content-length")
        if declared is None:
            reserved_bytes = maximum_body_bytes
        else:
            try:
                reserved_bytes = int(declared)
            except ValueError as exc:
                raise HTTPException(400, "invalid content length") from exc
            if reserved_bytes < 0:
                raise HTTPException(400, "invalid content length")
            if reserved_bytes > maximum_body_bytes:
                raise HTTPException(413, "schematic submission exceeds its limit")
        peer = request.client.host if request.client else "unknown"
        try:
            admission = await anyio.to_thread.run_sync(
                lambda: self.catalog.begin_upload(
                    source_key=f"peer-{_safe_peer(peer)}", reserved_bytes=reserved_bytes
                )
            )
        except SubmissionQuotaExceeded as exc:
            status = 429 if "rate limit" in str(exc) else 507
            raise HTTPException(
                status, {"code": "submission_capacity", "message": str(exc)}
            ) from exc
        return admission.admission_id

    async def cancel_schematic_upload(self, admission_id: str) -> None:
        await anyio.to_thread.run_sync(self.catalog.cancel_upload, admission_id)

    async def stage_mod(
        self,
        request: Request,
        *,
        filename: str,
        content: bytes,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.require_same_origin(request)
        principal = self._require_admin(request)
        if self.operations is None:
            raise HTTPException(503, "Minecraft administration is not configured")
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise HTTPException(422, "a valid idempotency key is required")
        await self._claim(principal.username, "mods.stage", "minecraft", idempotency_key)
        try:
            mod = await self.operations.stage_mod(filename, content)
        except PelicanOperationsError as exc:
            await self._audit_admin(
                principal.username,
                "mods.stage",
                "minecraft",
                idempotency_key,
                "failed",
                exc.code,
            )
            raise HTTPException(422, {"code": exc.code, "message": str(exc)}) from exc
        await self._audit_admin(principal.username, "mods.stage", mod.name, idempotency_key, "done")
        return {
            "outcome": "done",
            "mod": asdict(mod),
            "notice": "Mod staged disabled; review it before enabling",
        }

    async def download_submission(self, request: Request, submission_id: str) -> tuple[str, bytes]:
        """Return one quarantined payload only to a named administrator."""
        self._require_admin(request)
        try:
            submission = await anyio.to_thread.run_sync(self.catalog.get, submission_id)
            content = await anyio.to_thread.run_sync(self.catalog.content, submission_id)
        except KeyError as exc:
            raise HTTPException(404, "submission not found") from exc
        if len(content) != submission.byte_size:
            raise HTTPException(410, "submission payload has expired")
        return submission.metadata.filename, content

    async def _login(self, request: Request, response: Response, intent: dict[str, Any]) -> dict:
        if self.identity is None:
            raise HTTPException(503, "administrator login is disabled")
        username = intent.get("username")
        password = intent.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise HTTPException(422, "username and password are required")
        peer = request.client.host if request.client else "unknown"
        retry_after = self.login_limiter.retry_after(peer)
        if retry_after is not None:
            raise HTTPException(
                429,
                "too many failed login attempts",
                headers={"Retry-After": str(retry_after)},
            )
        try:
            authenticated = await anyio.to_thread.run_sync(
                self.identity.authenticate, username, password
            )
        except IdentityError as exc:
            self.login_limiter.failed(peer)
            raise HTTPException(401, str(exc)) from exc
        self.login_limiter.succeeded(peer)
        response.set_cookie(**authenticated.cookie.response_kwargs())
        return {
            "outcome": "done",
            "notice": f"Signed in as {authenticated.principal.username}",
        }

    async def _power(self, request: Request, intent: dict[str, Any]) -> dict:
        action = intent.get("action")
        key = intent.get("idempotencyKey")
        if (
            action not in _POWER_ACTIONS
            or not isinstance(key, str)
            or not _IDEMPOTENCY_KEY.fullmatch(key)
        ):
            raise HTTPException(422, "invalid Minecraft power intent")
        peer = request.client.host if request.client else "unknown"
        confirmation = intent.get("confirmation")
        confirmed = isinstance(confirmation, str) and self.confirmations.verify(
            confirmation, action=action, peer=peer, correlation_id=key
        )
        if confirmation and not confirmed:
            raise HTTPException(
                409,
                {
                    "code": "confirmation_invalid",
                    "message": "confirmation expired; refresh and try again",
                },
            )
        actor = self._principal(request)
        actor_name = actor.username if actor else f"lan-{_safe_peer(peer)}"
        claim_action = f"minecraft.{action}.{'execute' if confirmed else 'probe'}"
        await self._claim(actor_name, claim_action, "minecraft", key)
        try:
            async with self._maintenance_lock:
                result = await self.control.power(action, confirmed=confirmed)
        except MinecraftControlError as exc:
            error_code = exc.code
            await anyio.to_thread.run_sync(
                lambda: self.audit.append(
                    actor=actor_name,
                    action=f"minecraft.{action}",
                    target="minecraft",
                    outcome="failed",
                    correlation_id=key,
                    details={"code": error_code},
                )
            )
            status = 504 if error_code == "dependency_timeout" else 503
            raise HTTPException(status, {"code": error_code, "message": str(exc)}) from exc
        if result.outcome == "confirmation_required":
            token = self.confirmations.issue(action=action, peer=peer, correlation_id=key)
            return {
                "outcome": "confirmation_required",
                "challenge": {
                    "token": token,
                    "message": result.confirmation_message,
                    "effects": list(result.effects),
                },
            }
        await anyio.to_thread.run_sync(
            lambda: self.audit.append(
                actor=actor_name,
                action=f"minecraft.{action}",
                target="minecraft",
                outcome="done",
                correlation_id=key,
                details={"state": result.state.state},
            )
        )
        return {"outcome": "done", "notice": result.notice}

    async def _review(self, request: Request, intent: dict[str, Any]) -> dict:
        principal = self._require_admin(request)
        submission_id = intent.get("submissionId")
        decision = intent.get("decision")
        revision = intent.get("expectedRevision")
        key = intent.get("idempotencyKey")
        if (
            not isinstance(submission_id, str)
            or decision not in {"publish", "reject"}
            or type(revision) is not int
            or not isinstance(key, str)
            or not _IDEMPOTENCY_KEY.fullmatch(key)
        ):
            raise HTTPException(422, "invalid schematic review intent")
        action = f"schematic.{decision}"
        await self._claim(principal.username, action, submission_id, key)
        try:
            if decision == "publish":
                submission = await anyio.to_thread.run_sync(
                    lambda: self.catalog.publish(
                        submission_id,
                        actor=principal.username,
                        expected_revision=revision,
                    )
                )
            else:
                reason = intent.get("reasonCode", "admin_rejected")
                if not isinstance(reason, str):
                    raise ValueError("invalid rejection reason")
                submission = await anyio.to_thread.run_sync(
                    lambda: self.catalog.reject(
                        submission_id,
                        actor=principal.username,
                        expected_revision=revision,
                        reason_code=reason,
                    )
                )
        except (KeyError, RuntimeError, ValueError) as exc:
            await self._audit_admin(
                principal.username, action, submission_id, key, "failed", "review_conflict"
            )
            raise HTTPException(409, {"code": "review_conflict", "message": str(exc)}) from exc
        await self._audit_admin(principal.username, action, submission_id, key, "done")
        return {
            "outcome": "done",
            "notice": f"Schematic {submission.state.value}",
            "submission": _submission_view(submission),
        }

    async def _admin_operation(self, request: Request, intent: dict[str, Any]) -> dict:
        principal = self._require_admin(request)
        if self.operations is None:
            raise HTTPException(503, "Minecraft administration is not configured")
        intent_type = intent["type"]
        key = intent.get("idempotencyKey")
        if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
            raise HTTPException(422, "a valid idempotencyKey is required")
        try:
            if intent_type == "file.list":
                directory = intent.get("directory", "")
                if not isinstance(directory, str):
                    raise ValueError("directory must be text")
                files = await self.operations.list_files(directory)
                return {
                    "outcome": "done",
                    "files": {
                        "directory": directory,
                        "entries": [asdict(item) for item in files],
                    },
                }
            if intent_type == "file.read":
                document = await self.operations.read_text(_required_text(intent, "path"))
                return {"outcome": "done", "document": asdict(document)}
            if intent_type == "file.save":
                path = _required_text(intent, "path")
                await self._claim(principal.username, intent_type, path, key)
                document = await self.operations.write_text(
                    path,
                    _required_text(intent, "content", allow_empty=True),
                    expected_revision=_required_text(intent, "expectedRevision"),
                )
                await self._audit_admin(principal.username, intent_type, document.path, key, "done")
                return {"outcome": "done", "document": asdict(document)}
            if intent_type in {"mods.enable", "mods.disable"}:
                filename = _required_text(intent, "filename")
                await self._claim(principal.username, intent_type, filename, key)
                operation = (
                    self.operations.enable_mod
                    if intent_type == "mods.enable"
                    else self.operations.disable_mod
                )
                mod = await operation(
                    filename,
                    expected_revision=_required_text(intent, "expectedRevision"),
                )
                await self._audit_admin(principal.username, intent_type, mod.name, key, "done")
                return {"outcome": "done", "mod": asdict(mod), "notice": "Restart required"}
            if intent_type == "mods.delete":
                filename = _required_text(intent, "filename")
                token = intent.get("confirmation")
                action = f"mods.delete:{filename}"
                if not isinstance(token, str) or not self.confirmations.verify(
                    token,
                    action=action,
                    peer=principal.session_id,
                    correlation_id=key,
                ):
                    return self._confirmation(
                        action=action,
                        peer=principal.session_id,
                        correlation_id=key,
                        message=f"Delete {filename} from the server?",
                        effects=["The mod file will be removed", "A restart may be required"],
                    )
                await self._claim(principal.username, intent_type, filename, key)
                await self.operations.delete_mod(
                    filename,
                    expected_revision=_required_text(intent, "expectedRevision"),
                    confirmed=True,
                )
                await self._audit_admin(principal.username, intent_type, filename, key, "done")
                return {"outcome": "done", "notice": "Mod deleted; restart required"}
            if intent_type == "backup.create":
                token = intent.get("confirmation")
                action = "backup.create"
                if not isinstance(token, str) or not self.confirmations.verify(
                    token,
                    action=action,
                    peer=principal.session_id,
                    correlation_id=key,
                ):
                    return self._confirmation(
                        action=action,
                        peer=principal.session_id,
                        correlation_id=key,
                        message="Stop Minecraft and create a verified backup?",
                        effects=[
                            "Minecraft will be stopped and proven offline first",
                            "The backup will be marked restore-eligible after checksum verification",
                            "Minecraft will remain stopped after the backup",
                        ],
                    )
                await self._claim(principal.username, intent_type, "minecraft", key)
                async with self._maintenance_lock:
                    backup = await self.operations.create_backup(
                        str(intent.get("name") or "Minecraft UI safety backup")
                    )
                await self._audit_admin(
                    principal.username, intent_type, backup.backup_id, key, "done"
                )
                return {"outcome": "done", "backup": asdict(backup)}
            if intent_type in {"restore.prepare", "restore.execute"}:
                backup_id = _required_text(intent, "backupId")
                action = f"restore:{backup_id}"
                token = intent.get("confirmation")
                if not isinstance(token, str) or not self.confirmations.verify(
                    token,
                    action=action,
                    peer=principal.session_id,
                    correlation_id=key,
                ):
                    return self._confirmation(
                        action=action,
                        peer=principal.session_id,
                        correlation_id=key,
                        message="Stop Minecraft and restore this verified backup?",
                        effects=[
                            "Minecraft will be stopped and proven offline first",
                            "Minecraft files will be replaced",
                            "A verified safety backup will be created first",
                            "Minecraft will remain stopped after the restore",
                        ],
                    )
                await self._claim(principal.username, "restore.execute", backup_id, key)
                async with self._maintenance_lock:
                    receipt = await self.operations.restore_backup(backup_id, confirmed=True)
                await self._audit_admin(
                    principal.username, "restore.execute", backup_id, key, "done"
                )
                return {"outcome": "done", "restore": asdict(receipt)}
        except (PelicanOperationsError, ValueError) as exc:
            code = exc.code if isinstance(exc, PelicanOperationsError) else "invalid_intent"
            await self._audit_admin(
                principal.username, intent_type, "minecraft", key, "failed", code
            )
            status = 409 if code in {"revision_conflict", "confirmation_required"} else 422
            raise HTTPException(status, {"code": code, "message": str(exc)}) from exc
        raise HTTPException(422, "unknown administrator operation")

    async def _audit_admin(
        self,
        actor: str,
        action: str,
        target: str,
        key: str,
        outcome: str,
        code: str | None = None,
    ) -> None:
        await anyio.to_thread.run_sync(
            lambda: self.audit.append(
                actor=actor,
                action=action,
                target=target,
                outcome=outcome,
                correlation_id=key,
                details={"code": code} if code else None,
            )
        )

    async def _claim(self, actor: str, action: str, target: str, key: str) -> None:
        claimed = await anyio.to_thread.run_sync(
            lambda: self.audit.claim(
                actor=actor,
                action=action,
                target=target,
                correlation_id=key,
            )
        )
        if not claimed:
            raise HTTPException(
                409,
                {
                    "code": "idempotency_replayed",
                    "message": "This operation was already requested; refresh before trying again",
                },
            )

    def _confirmation(
        self,
        *,
        action: str,
        peer: str,
        correlation_id: str,
        message: str,
        effects: list[str],
    ) -> dict:
        return {
            "outcome": "confirmation_required",
            "challenge": {
                "token": self.confirmations.issue(
                    action=action, peer=peer, correlation_id=correlation_id
                ),
                "message": message,
                "effects": effects,
            },
        }

    def _require_admin(self, request: Request) -> AdminPrincipal:
        principal = self._principal(request)
        if principal is None:
            raise HTTPException(401, "administrator sign-in required")
        if principal.role != "admin":
            raise HTTPException(403, "administrator permission required")
        return principal

    def _principal(self, request: Request) -> AdminPrincipal | None:
        if self.identity is None:
            return None
        try:
            return self.identity.resolve(request.cookies.get(COOKIE_NAME, ""))
        except IdentityError:
            return None


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _safe_peer(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return digest


def _required_text(intent: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = intent.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{key} is required")
    return value


def _submission_view(submission) -> dict[str, Any]:
    return {
        "id": submission.submission_id,
        "sha256": submission.sha256,
        "byteSize": submission.byte_size,
        "title": submission.metadata.title,
        "metadata": asdict(submission.metadata),
        "promotionRequested": submission.promotion_requested,
        "state": submission.state.value,
        "revision": submission.revision,
        "eligible": submission.eligible,
        "failedRequirements": [item.code for item in submission.requirements if not item.passed],
        "createdAt": submission.created_at.isoformat(),
        "updatedAt": submission.updated_at.isoformat(),
        "requirements": [asdict(item) for item in submission.requirements],
        "recommendations": [asdict(item) for item in submission.recommendations],
        "downloadUrl": f"/api/admin/submissions/{submission.submission_id}/download",
    }
