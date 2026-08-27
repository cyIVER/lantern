from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_compose_publishes_only_minecraft_ui_and_keeps_viewer_private() -> None:
    compose = yaml.safe_load((ROOT / "stack" / "compose.yml").read_text(encoding="utf-8"))

    minecraft_ui = compose["services"]["minecraft-ui"]
    viewer = compose["services"]["schematic-viewer"]

    assert minecraft_ui["ports"] == ["8093:8093"]
    assert minecraft_ui.get("depends_on") is None
    assert viewer.get("ports") is None
    assert viewer["expose"] == ["4173"]
    assert viewer.get("depends_on") is None
    assert compose["networks"]["schematic-backplane"]["internal"] is True
    assert compose["volumes"]["schematic-viewer-data"]["name"] == (
        "lantern-schematic-viewer-data"
    )
    assert viewer["image"].startswith("${SCHEMATIC_VIEWER_IMAGE:")
    assert "latest" not in viewer["image"]


def test_compose_hardens_both_new_services_and_mounts_file_secrets() -> None:
    compose = yaml.safe_load((ROOT / "stack" / "compose.yml").read_text(encoding="utf-8"))

    for name in ("minecraft-ui", "schematic-viewer"):
        service = compose["services"][name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["restart"] == "unless-stopped"

    assert set(compose["secrets"]) >= {
        "minecraft_admin_password_hash",
        "minecraft_session_secret",
        "schematic_viewer_admin_token",
    }
