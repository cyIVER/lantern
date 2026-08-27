"""Configuration loaded from environment and Docker secret files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _optional_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


@dataclass(frozen=True, slots=True)
class Settings:
    viewer_url: str = "http://schematic-viewer:4173"
    admin_password_hash_file: Path | None = None
    session_secret_file: Path | None = None
    viewer_admin_token_file: Path | None = None
    session_ttl_seconds: int = 8 * 60 * 60
    secure_cookie: bool = False
    allow_insecure_admin: bool = False

    @classmethod
    def from_environment(cls) -> Settings:
        """Load application settings from environment variables."""
        return cls(
            viewer_url=os.environ.get(
                "SCHEMATIC_VIEWER_URL", "http://schematic-viewer:4173"
            ).rstrip("/"),
            admin_password_hash_file=_optional_path("MINECRAFT_ADMIN_PASSWORD_HASH_FILE"),
            session_secret_file=_optional_path("MINECRAFT_SESSION_SECRET_FILE"),
            viewer_admin_token_file=_optional_path("SCHEMATIC_VIEWER_ADMIN_TOKEN_FILE"),
            session_ttl_seconds=int(os.environ.get("MINECRAFT_SESSION_TTL_SECONDS", "28800")),
            secure_cookie=os.environ.get("MINECRAFT_SECURE_COOKIE", "false").lower()
            in {"1", "true", "yes"},
            allow_insecure_admin=os.environ.get(
                "MINECRAFT_ALLOW_INSECURE_ADMIN", "false"
            ).lower()
            in {"1", "true", "yes"},
        )
