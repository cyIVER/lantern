import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from app.identity import (
    AdminUser,
    CookieMode,
    IdentityConfigurationError,
    IdentityError,
    IdentityErrorCode,
    InMemoryIdentityDirectory,
    JsonFileIdentityDirectory,
    NamedAdminIdentity,
    SqliteSessionRevocations,
)


class MutableClock:
    def __init__(self, now: int = 2_000_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


def _users() -> list[AdminUser]:
    hasher = PasswordHasher()
    return [
        AdminUser(
            username="iveri",
            password_hash=hasher.hash("iveri test password"),
            role="admin",
            upstream_alias="iveri",
            credential_version=1,
        ),
        AdminUser(
            username="scotlandf",
            password_hash=hasher.hash("scotlandf test password"),
            role="admin",
            upstream_alias="scotland",
            credential_version=4,
        ),
    ]


def _identity(
    directory: InMemoryIdentityDirectory | JsonFileIdentityDirectory,
    clock: MutableClock,
    *,
    cookie_mode: CookieMode = CookieMode.TRUSTED_LAN,
) -> NamedAdminIdentity:
    return NamedAdminIdentity(
        directory,
        session_secret=b"session-test-secret-not-a-deployment-secret",
        ttl_seconds=8 * 60 * 60,
        cookie_mode=cookie_mode,
        clock=clock,
        session_id_factory=lambda: "0123456789abcdef0123456789abcdef",
    )


def _write_directory(path: Path, users: list[AdminUser]) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "users": [
                    {
                        "username": user.username,
                        "password_hash": user.password_hash,
                        "role": user.role,
                        "upstream_alias": user.upstream_alias,
                        "credential_version": user.credential_version,
                    }
                    for user in users
                ],
            }
        ),
        encoding="utf-8",
    )


def test_in_memory_adapter_authenticates_named_admin_and_resolves_all_claims() -> None:
    clock = MutableClock()
    identity = _identity(InMemoryIdentityDirectory(_users()), clock)

    issued = identity.authenticate(" ScotlandF ", "scotlandf test password")
    resolved = identity.resolve(issued.cookie.value)

    assert resolved == issued.principal
    assert resolved.username == "scotlandf"
    assert resolved.role == "admin"
    assert resolved.upstream_alias == "scotland"
    assert resolved.credential_version == 4
    assert resolved.session_id == "0123456789abcdef0123456789abcdef"
    assert resolved.issued_at == 2_000_000_000
    assert resolved.expires_at == 2_000_028_800

    payload_text, _signature_text = issued.cookie.value.split(".")
    payload = json.loads(base64.urlsafe_b64decode(payload_text + "=" * (-len(payload_text) % 4)))
    assert payload == {
        "sub": "scotlandf",
        "role": "admin",
        "sid": "0123456789abcdef0123456789abcdef",
        "iat": 2_000_000_000,
        "exp": 2_000_028_800,
        "cv": 4,
    }


def test_cookie_transport_is_secure_or_explicit_trusted_lan() -> None:
    clock = MutableClock()
    users = _users()
    secure = _identity(
        InMemoryIdentityDirectory(users), clock, cookie_mode=CookieMode.SECURE
    ).authenticate("iveri", "iveri test password")
    trusted_lan = _identity(
        InMemoryIdentityDirectory(users), clock, cookie_mode=CookieMode.TRUSTED_LAN
    ).authenticate("iveri", "iveri test password")

    assert secure.cookie.response_kwargs() == {
        "key": "lantern_minecraft_admin",
        "value": secure.cookie.value,
        "max_age": 28800,
        "secure": True,
        "httponly": True,
        "samesite": "strict",
        "path": "/",
    }
    assert trusted_lan.cookie.secure is False
    assert trusted_lan.cookie.httponly is True
    assert trusted_lan.cookie.samesite == "strict"


def test_invalid_credentials_have_one_stable_non_enumerating_error() -> None:
    clock = MutableClock()
    identity = _identity(InMemoryIdentityDirectory(_users()), clock)

    for username, password in [
        ("iveri", "wrong"),
        ("unknown", "wrong"),
        ("NOT A USERNAME", "wrong"),
    ]:
        with pytest.raises(IdentityError) as caught:
            identity.authenticate(username, password)
        assert caught.value.code is IdentityErrorCode.INVALID_CREDENTIALS
        assert str(caught.value) == "invalid administrator credentials"


def test_credential_version_change_revokes_an_existing_session() -> None:
    users = _users()
    directory = InMemoryIdentityDirectory(users)
    identity = _identity(directory, MutableClock())
    issued = identity.authenticate("iveri", "iveri test password")

    directory.replace([replace(users[0], credential_version=2), users[1]])

    with pytest.raises(IdentityError) as caught:
        identity.resolve(issued.cookie.value)
    assert caught.value.code is IdentityErrorCode.REVOKED_SESSION
    assert str(caught.value) == "administrator session revoked"


def test_logout_revocation_invalidates_an_otherwise_valid_session() -> None:
    identity = _identity(InMemoryIdentityDirectory(_users()), MutableClock())
    issued = identity.authenticate("iveri", "iveri test password")

    identity.revoke(issued.cookie.value)

    with pytest.raises(IdentityError) as caught:
        identity.resolve(issued.cookie.value)
    assert caught.value.code is IdentityErrorCode.REVOKED_SESSION


def test_logout_revocation_persists_across_identity_instances(tmp_path) -> None:
    clock = MutableClock()
    directory = InMemoryIdentityDirectory(_users())
    database = str(tmp_path / "portal.sqlite3")
    first = NamedAdminIdentity(
        directory,
        session_secret=b"session-test-secret-not-a-deployment-secret",
        ttl_seconds=8 * 60 * 60,
        cookie_mode=CookieMode.TRUSTED_LAN,
        clock=clock,
        session_id_factory=lambda: "0123456789abcdef0123456789abcdef",
        revocations=SqliteSessionRevocations(database),
    )
    issued = first.authenticate("iveri", "iveri test password")
    first.revoke(issued.cookie.value)
    restarted = NamedAdminIdentity(
        directory,
        session_secret=b"session-test-secret-not-a-deployment-secret",
        ttl_seconds=8 * 60 * 60,
        cookie_mode=CookieMode.TRUSTED_LAN,
        clock=clock,
        revocations=SqliteSessionRevocations(database),
    )

    with pytest.raises(IdentityError) as caught:
        restarted.resolve(issued.cookie.value)
    assert caught.value.code is IdentityErrorCode.REVOKED_SESSION


def test_expiry_and_tampering_have_distinct_stable_errors() -> None:
    clock = MutableClock()
    identity = _identity(InMemoryIdentityDirectory(_users()), clock)
    issued = identity.authenticate("iveri", "iveri test password")

    clock.now += 8 * 60 * 60
    with pytest.raises(IdentityError) as expired:
        identity.resolve(issued.cookie.value)
    assert expired.value.code is IdentityErrorCode.EXPIRED_SESSION

    payload, signature = issued.cookie.value.split(".")
    tampered = f"{payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    with pytest.raises(IdentityError) as invalid:
        identity.resolve(tampered)
    assert invalid.value.code is IdentityErrorCode.INVALID_SESSION


def test_file_adapter_maps_portal_identity_to_current_upstream_alias(tmp_path: Path) -> None:
    users = _users()
    path = tmp_path / "minecraft-admin-users.json"
    _write_directory(path, users)
    identity = _identity(JsonFileIdentityDirectory(path), MutableClock())

    issued = identity.authenticate("scotlandf", "scotlandf test password")

    assert issued.principal.username == "scotlandf"
    assert issued.principal.upstream_alias == "scotland"


def test_file_adapter_reloads_secret_mount_for_revocation_without_restart(
    tmp_path: Path,
) -> None:
    users = _users()
    path = tmp_path / "minecraft-admin-users.json"
    _write_directory(path, users)
    identity = _identity(JsonFileIdentityDirectory(path), MutableClock())
    issued = identity.authenticate("iveri", "iveri test password")

    _write_directory(path, [replace(users[0], credential_version=2), users[1]])

    with pytest.raises(IdentityError) as caught:
        identity.resolve(issued.cookie.value)
    assert caught.value.code is IdentityErrorCode.REVOKED_SESSION


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update(version=2),
        lambda doc: doc["users"].append(dict(doc["users"][0])),
        lambda doc: doc["users"][0].update(username="Iveri"),
        lambda doc: doc["users"][0].update(password_hash="not-an-argon2-hash"),
        lambda doc: doc["users"][0].update(credential_version=0),
        lambda doc: doc["users"][0].update(unexpected=True),
    ],
)
def test_file_adapter_fails_closed_on_invalid_or_unsupported_documents(
    tmp_path: Path, mutate
) -> None:
    users = _users()
    path = tmp_path / "minecraft-admin-users.json"
    _write_directory(path, users)
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(IdentityConfigurationError) as caught:
        JsonFileIdentityDirectory(path)

    assert caught.value.code in {
        "identity_directory_invalid",
        "identity_directory_version_unsupported",
    }


def test_directory_requires_distinct_per_user_argon2_hashes() -> None:
    users = _users()
    duplicate_hash = replace(users[1], password_hash=users[0].password_hash)

    with pytest.raises(IdentityConfigurationError) as caught:
        InMemoryIdentityDirectory([users[0], duplicate_hash])

    assert caught.value.code == "identity_directory_invalid"


def test_constructor_rejects_undersized_secret_and_implicit_cookie_mode() -> None:
    directory = InMemoryIdentityDirectory(_users())

    with pytest.raises(IdentityConfigurationError) as short_secret:
        NamedAdminIdentity(
            directory,
            session_secret=b"too-short",
            ttl_seconds=300,
            cookie_mode=CookieMode.SECURE,
        )
    assert short_secret.value.code == "session_secret_invalid"

    with pytest.raises(IdentityConfigurationError) as invalid_mode:
        NamedAdminIdentity(
            directory,
            session_secret=b"s" * 32,
            ttl_seconds=300,
            cookie_mode="trusted-lan",  # type: ignore[arg-type]
        )
    assert invalid_mode.value.code == "cookie_mode_invalid"
