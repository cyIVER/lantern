import asyncio
import json

from argon2 import PasswordHasher

from app.portal import NamedViewerAccess
from app.runtime import build_runtime
from app.settings import Settings


def test_runtime_composes_named_trusted_lan_identity_and_persistent_state(tmp_path) -> None:
    hasher = PasswordHasher()
    users = tmp_path / "minecraft-admins.json"
    users.write_text(
        json.dumps(
            {
                "version": 1,
                "users": [
                    {
                        "username": "iveri",
                        "password_hash": hasher.hash("first unique password"),
                        "role": "admin",
                        "upstream_alias": "iveri",
                        "credential_version": 1,
                    },
                    {
                        "username": "scotlandf",
                        "password_hash": hasher.hash("second unique password"),
                        "role": "admin",
                        "upstream_alias": "scotland",
                        "credential_version": 1,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    session = tmp_path / "session-secret"
    session.write_bytes(b"s" * 48)
    viewer = tmp_path / "viewer-token"
    viewer.write_text("v" * 48, encoding="utf-8")
    pelican = tmp_path / "pelican-token"
    pelican.write_text("p" * 48, encoding="utf-8")
    data = tmp_path / "data"

    runtime = build_runtime(
        Settings(
            admin_users_file=users,
            session_secret_file=session,
            viewer_admin_token_file=viewer,
            pelican_api_key_file=pelican,
            pelican_virtual_host="pelican.lan",
            allow_insecure_admin=True,
            portal_data_dir=data,
            trusted_browser_origins=frozenset({"http://testserver:80"}),
        )
    )
    try:
        assert isinstance(runtime.viewer_access, NamedViewerAccess)
        assert runtime.portal.identity is not None
        assert runtime.operations is not None
        assert runtime.operations._virtual_host == "pelican.lan"
        assert (data / "portal.sqlite3").is_file()
    finally:
        asyncio.run(runtime.aclose())
