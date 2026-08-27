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
    assert set(minecraft_ui["networks"]) == {"minecraft-edge", "schematic-backplane"}
    assert "default" not in minecraft_ui["networks"]
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
        assert service["group_add"] == ["${LANTERN_SECRET_GID:-1000}"]

    assert set(compose["secrets"]) >= {
        "minecraft_admin_password_hash",
        "minecraft_session_secret",
        "schematic_viewer_admin_token",
    }


def test_ci_uses_the_read_only_cross_repository_viewer_key() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["minecraft-integration"]["steps"]
    checkout = next(
        step for step in steps if step.get("name") == "check out the reviewed viewer contract"
    )

    assert checkout["with"]["repository"] == "ScotsGamez/create-schematic-viewer"
    assert checkout["with"]["ssh-key"] == "${{ secrets.SCHEMATIC_VIEWER_DEPLOY_KEY }}"
    assert checkout["with"]["persist-credentials"] is False

    secret_step = next(
        step for step in steps if step.get("name") == "create disposable file secrets"
    )
    assert 'chmod 640 stack/secrets/*' in secret_step["run"]
    assert 'LANTERN_SECRET_GID=$secret_gid' in secret_step["run"]


def test_ci_integration_assertions_report_the_failed_boundary() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["minecraft-integration"]["steps"]
    verify = next(
        step
        for step in steps
        if step.get("name") == "verify health, proxy, and administrator boundary"
    )["run"]

    assert "assert_status()" in verify
    assert "for _ in $(seq 1 45)" in verify
    assert 'if [ "$viewer_health" = healthy ]' in verify
    for boundary in (
        "schematics redirect",
        "administrator login",
        "cross-origin mutation",
        "viewer container health",
        "viewer host bindings",
    ):
        assert boundary in verify
