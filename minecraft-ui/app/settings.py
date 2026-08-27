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
    admin_users_file: Path | None = None
    session_secret_file: Path | None = None
    viewer_admin_token_file: Path | None = None
    session_ttl_seconds: int = 8 * 60 * 60
    secure_cookie: bool = False
    allow_insecure_admin: bool = False
    lantern_control_url: str = "http://ui:8090"
    portal_data_dir: Path = Path("/data")
    pelican_url: str = "http://panel"
    pelican_virtual_host: str = "192.168.0.115"
    pelican_api_key_file: Path | None = None
    pelican_server_name: str = "LANtern Minecraft"
    pelican_upload_origins: frozenset[str] = frozenset({"http://192.168.0.115:8080"})
    # None preserves dynamic dependency-injected test factories. Environment
    # composition always supplies an explicit non-empty production allowlist.
    trusted_browser_origins: frozenset[str] | None = None
    schematic_queue_max_count: int = 500
    schematic_queue_max_bytes: int = 2 * 1024 * 1024 * 1024
    schematic_uploads_per_ip: int = 20
    schematic_rate_window_seconds: int = 3600
    schematic_retention_seconds: int = 30 * 24 * 60 * 60
    schematic_max_concurrent_uploads: int = 4
    schematic_admission_ttl_seconds: int = 600

    @classmethod
    def from_environment(cls) -> Settings:
        """Load application settings from environment variables."""
        return cls(
            viewer_url=os.environ.get(
                "SCHEMATIC_VIEWER_URL", "http://schematic-viewer:4173"
            ).rstrip("/"),
            admin_password_hash_file=_optional_path("MINECRAFT_ADMIN_PASSWORD_HASH_FILE"),
            admin_users_file=_optional_path("MINECRAFT_ADMIN_USERS_FILE"),
            session_secret_file=_optional_path("MINECRAFT_SESSION_SECRET_FILE"),
            viewer_admin_token_file=_optional_path("SCHEMATIC_VIEWER_ADMIN_TOKEN_FILE"),
            session_ttl_seconds=int(os.environ.get("MINECRAFT_SESSION_TTL_SECONDS", "28800")),
            secure_cookie=os.environ.get("MINECRAFT_SECURE_COOKIE", "false").lower()
            in {"1", "true", "yes"},
            allow_insecure_admin=os.environ.get("MINECRAFT_ALLOW_INSECURE_ADMIN", "false").lower()
            in {"1", "true", "yes"},
            lantern_control_url=os.environ.get("LANTERN_CONTROL_URL", "http://ui:8090").rstrip("/"),
            portal_data_dir=Path(os.environ.get("MINECRAFT_PORTAL_DATA_DIR", "/data")),
            pelican_url=os.environ.get("PELICAN_URL", "http://panel").rstrip("/"),
            pelican_virtual_host=os.environ.get("PELICAN_VIRTUAL_HOST", "192.168.0.115").strip(),
            pelican_api_key_file=_optional_path("PELICAN_API_KEY_FILE"),
            pelican_server_name=os.environ.get(
                "PELICAN_MINECRAFT_SERVER_NAME", "LANtern Minecraft"
            ).strip(),
            pelican_upload_origins=frozenset(
                origin.strip()
                for origin in os.environ.get(
                    "PELICAN_UPLOAD_ORIGINS", "http://192.168.0.115:8080"
                ).split(",")
                if origin.strip()
            ),
            trusted_browser_origins=frozenset(
                origin.strip()
                for origin in os.environ.get(
                    "MINECRAFT_TRUSTED_BROWSER_ORIGINS",
                    "http://192.168.0.115:8093,http://lantern:8093,http://127.0.0.1:8093",
                ).split(",")
                if origin.strip()
            ),
            schematic_queue_max_count=int(os.environ.get("SCHEMATIC_QUEUE_MAX_COUNT", "500")),
            schematic_queue_max_bytes=int(
                os.environ.get("SCHEMATIC_QUEUE_MAX_BYTES", str(2 * 1024 * 1024 * 1024))
            ),
            schematic_uploads_per_ip=int(os.environ.get("SCHEMATIC_UPLOADS_PER_IP", "20")),
            schematic_rate_window_seconds=int(
                os.environ.get("SCHEMATIC_RATE_WINDOW_SECONDS", "3600")
            ),
            schematic_retention_seconds=int(
                os.environ.get("SCHEMATIC_RETENTION_SECONDS", str(30 * 24 * 60 * 60))
            ),
            schematic_max_concurrent_uploads=int(
                os.environ.get("SCHEMATIC_MAX_CONCURRENT_UPLOADS", "4")
            ),
            schematic_admission_ttl_seconds=int(
                os.environ.get("SCHEMATIC_ADMISSION_TTL_SECONDS", "600")
            ),
        )
