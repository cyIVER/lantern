from pathlib import Path
from typing import Any, Iterator

import yaml

ROOT = Path(__file__).parents[2]
VIEWER_IMAGE = (
    "ghcr.io/scotsgamez/create-schematic-viewer:v1.0.1@sha256:"
    "d5501af9de95f9b89484ae4e4dbea098b0cdd3e86af3b19e50976855b533444c"
)
APPROVED_ACTION_PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "astral-sh/setup-uv": "20cfd1bf945f4377ade1205e4dbc17946fc9a30d",
}


class ComposeLoader(yaml.SafeLoader):
    """Safe YAML loader that understands Compose sequence replacement tags."""


ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))


def _action_references(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "uses":
                yield child
            else:
                yield from _action_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _action_references(child)


def test_compose_publishes_only_minecraft_ui_and_keeps_viewer_private() -> None:
    compose = yaml.safe_load((ROOT / "stack" / "compose.yml").read_text(encoding="utf-8"))

    minecraft_ui = compose["services"]["minecraft-ui"]
    viewer = compose["services"]["schematic-viewer"]

    assert minecraft_ui["ports"] == ["${LANTERN_MINECRAFT_UI_BIND_IP:-192.168.0.115}:8093:8093"]
    assert minecraft_ui.get("depends_on") is None
    assert viewer.get("ports") is None
    assert viewer["expose"] == ["4173"]
    assert viewer.get("depends_on") is None
    assert set(minecraft_ui["networks"]) == {
        "minecraft-edge",
        "schematic-backplane",
        "minecraft-admin-backplane",
    }
    assert "default" not in minecraft_ui["networks"]
    assert compose["networks"]["schematic-backplane"]["internal"] is True
    assert compose["networks"]["minecraft-admin-backplane"]["internal"] is True
    assert "minecraft-admin-backplane" in compose["services"]["ui"]["networks"]
    assert "minecraft-admin-backplane" in compose["services"]["panel"]["networks"]
    assert compose["volumes"]["schematic-viewer-data"]["name"] == ("lantern-schematic-viewer-data")
    assert viewer["image"] == VIEWER_IMAGE
    assert minecraft_ui["volumes"] == ["minecraft-ui-data:/data"]
    assert compose["volumes"]["minecraft-ui-data"]["name"] == ("lantern-minecraft-ui-data")
    assert minecraft_ui["environment"]["MINECRAFT_TRUSTED_BROWSER_ORIGINS"] == (
        "http://${LANTERN_MINECRAFT_UI_BIND_IP:-192.168.0.115}:8093,"
        "http://lantern:8093,http://127.0.0.1:8093"
    )
    assert minecraft_ui["environment"]["PELICAN_UPLOAD_ORIGINS"] == (
        "http://${LANTERN_MINECRAFT_UI_BIND_IP:-192.168.0.115}:8080"
    )
    assert minecraft_ui["environment"]["PELICAN_URL"] == "http://panel"
    assert minecraft_ui["environment"]["PELICAN_VIRTUAL_HOST"] == (
        "${LANTERN_MINECRAFT_UI_BIND_IP:-192.168.0.115}"
    )


def test_landing_probe_uses_the_minecraft_ui_lan_bind_address() -> None:
    compose = yaml.safe_load((ROOT / "stack" / "compose.yml").read_text(encoding="utf-8"))
    bind_address = "${LANTERN_MINECRAFT_UI_BIND_IP:-192.168.0.115}"

    assert compose["services"]["minecraft-ui"]["ports"] == [f"{bind_address}:8093:8093"]
    assert compose["services"]["ui"]["environment"]["UI_PROBE_HOST"] == bind_address


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
        "minecraft_admin_users",
        "minecraft_session_secret",
        "schematic_viewer_admin_token",
        "pelican_client_api_key",
    }


def test_compose_admin_transport_configuration_is_fail_closed() -> None:
    compose = yaml.safe_load((ROOT / "stack" / "compose.yml").read_text(encoding="utf-8"))
    ci_override = yaml.load(
        (ROOT / "stack" / "compose.ci.yml").read_text(encoding="utf-8"),
        Loader=ComposeLoader,
    )
    env_example = (ROOT / "stack" / ".env.example").read_text(encoding="utf-8")

    assert (
        compose["services"]["minecraft-ui"]["environment"]["MINECRAFT_ALLOW_INSECURE_ADMIN"]
        == "${MINECRAFT_ALLOW_INSECURE_ADMIN:-true}"
    )
    assert (
        ci_override["services"]["minecraft-ui"]["environment"]["MINECRAFT_ALLOW_INSECURE_ADMIN"]
        == "true"
    )
    assert (
        compose["services"]["minecraft-ui"]["environment"]["MINECRAFT_SECURE_COOKIE"]
        == "${MINECRAFT_SECURE_COOKIE:-false}"
    )
    assert "MINECRAFT_SECURE_COOKIE=false" in env_example.splitlines()
    assert "MINECRAFT_ALLOW_INSECURE_ADMIN=true" in env_example.splitlines()
    assert ci_override["services"]["minecraft-ui"]["ports"] == ["127.0.0.1:8093:8093"]
    assert (
        ci_override["services"]["minecraft-ui"]["environment"]["MINECRAFT_TRUSTED_BROWSER_ORIGINS"]
        == "http://127.0.0.1:8093"
    )


def test_ci_runs_minecraft_and_vm_recovery_tests() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["python"]["steps"]
    test_step = next(
        step for step in steps if step.get("name") == "Minecraft UI and VM recovery tests"
    )

    assert test_step["working-directory"] == "minecraft-ui"
    assert test_step["run"] == ".venv/bin/python -m pytest tests ../vm/tests -q"


def test_ci_pulls_the_released_viewer_without_repository_credentials() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    )
    integration = workflow["jobs"]["minecraft-integration"]
    steps = integration["steps"]
    serialized_workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
        encoding="utf-8"
    )

    assert "LANTERN_MINECRAFT_UI_BIND_IP" not in integration["env"]
    assert "SCHEMATIC_VIEWER_DEPLOY_KEY" not in serialized_workflow
    assert "repository: ScotsGamez/create-schematic-viewer" not in serialized_workflow
    assert not any(step.get("name") == "check out the reviewed viewer contract" for step in steps)

    build_step = next(step for step in steps if step.get("name") == "build the Minecraft UI image")
    assert "schematic-viewer" not in build_step["run"]

    start_step = next(
        step for step in steps if step.get("name") == "start only the Minecraft UI and viewer"
    )
    assert "pull schematic-viewer" in start_step["run"]
    compose_prefix = "docker compose --file stack/compose.yml --file stack/compose.ci.yml"
    compose_commands = [
        line.strip()
        for step in steps
        for line in step.get("run", "").splitlines()
        if line.strip().startswith("docker compose")
    ]
    assert compose_commands
    assert all(command.startswith(compose_prefix) for command in compose_commands)

    compose_job = workflow["jobs"]["compose"]
    render_step = next(
        step
        for step in compose_job["steps"]
        if step.get("name") == "validate rendered Compose model"
    )
    assert "--file stack/compose.ci.yml" in render_step["run"]
    assert '$ports[0].host_ip == "127.0.0.1"' in render_step["run"]
    assert '$ui.environment.MINECRAFT_ALLOW_INSECURE_ADMIN == "true"' in render_step["run"]

    secret_step = next(
        step for step in steps if step.get("name") == "create disposable file secrets"
    )
    assert "chmod 640 stack/secrets/*" in secret_step["run"]
    assert "LANTERN_SECRET_GID=$secret_gid" in secret_step["run"]


def test_all_workflow_actions_use_the_approved_commit_pins() -> None:
    found_actions: set[str] = set()
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for reference in _action_references(workflow):
            action, separator, revision = reference.rpartition("@")
            assert separator, f"{path.name}: action is not pinned: {reference}"
            assert action in APPROVED_ACTION_PINS, f"{path.name}: unapproved action: {action}"
            assert revision == APPROVED_ACTION_PINS[action], (
                f"{path.name}: {action} must use the approved full commit pin"
            )
            found_actions.add(action)

    assert found_actions == set(APPROVED_ACTION_PINS)


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
