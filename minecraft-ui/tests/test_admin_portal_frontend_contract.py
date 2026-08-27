from pathlib import Path


STATIC = Path(__file__).parents[1] / "static"


def _asset(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_frontend_uses_workspace_and_typed_intent_gateway_only() -> None:
    script = _asset("app.js")

    assert 'fetch("/api/workspace"' in script
    assert 'fetch("/api/intents"' in script
    assert 'type: "session.login"' in script
    assert 'type: "session.logout"' in script
    assert 'type: "minecraft.power"' in script
    assert "idempotencyKey" in script
    assert "confirmation_required" in script
    assert 'fetch("/api/session"' not in script
    assert 'fetch("/api/session/login"' not in script


def test_portal_exposes_guest_power_controls_and_named_login() -> None:
    markup = _asset("index.html")

    assert 'id="server-controls"' in markup
    for action in ("start", "stop", "restart"):
        assert f'data-power-action="{action}"' in markup
    assert 'id="admin-username"' in markup
    assert 'autocomplete="username"' in markup
    assert 'id="admin-password"' in markup
    assert 'autocomplete="current-password"' in markup


def test_admin_tab_has_required_operational_surfaces_and_pending_badge() -> None:
    markup = _asset("index.html")

    assert 'id="tab-admin"' in markup
    assert 'id="admin-pending-badge"' in markup
    assert 'id="review-queue"' in markup
    assert 'id="admin-files"' in markup
    assert 'id="admin-mods"' in markup
    assert 'id="admin-restores"' in markup
    assert 'id="admin-jobs"' in markup
    assert 'id="admin-audit"' in markup
    assert 'id="confirm-dialog"' in markup


def test_schematics_remain_embedded_but_pelican_is_a_separate_link() -> None:
    markup = _asset("index.html")

    assert 'id="schematic-frame"' in markup
    assert 'src="/schematics/"' in markup
    assert 'id="pelican-link"' in markup
    assert 'target="_blank"' in markup
    assert "Pelican" not in markup.split("<iframe", 1)[1].split("</iframe>", 1)[0]


def test_schematic_contribution_uses_raw_upload_contract_and_rights_notice() -> None:
    markup = _asset("index.html")
    script = _asset("app.js")

    assert 'id="schematic-upload-form"' in markup
    assert "CC0" in markup
    assert 'fetch("/api/submissions"' not in script  # Routed through the binary client.
    assert 'sendBinary("/api/submissions"' in script
    assert '"x-schematic-filename"' in script
    assert '"x-schematic-promote"' in script
    assert '"x-schematic-metadata": encodeURIComponent(JSON.stringify(metadata))' in script


def test_review_and_file_editor_preserve_revision_contracts() -> None:
    script = _asset("app.js")

    assert 'type: "schematic.review"' in script
    assert "expectedRevision: item.revision" in script
    assert "idempotencyKey: makeIdempotencyKey()" in script
    assert "publish.disabled = item.eligible !== true" in script
    assert 'type: "file.read"' in script
    assert 'type: "file.save"' in script
    assert "expectedRevision: activeDocument.revision" in script
    assert "result.document.content" in script


def test_file_browser_lists_directories_and_only_reads_files() -> None:
    script = _asset("app.js")

    assert 'type: "file.list"' in script
    assert 'directoryEntry = entry.kind === "directory"' in script
    assert 'directoryEntry ? "Open" : "Edit"' in script
    assert 'actionButton("Back"' in script
    assert 'actionButton("Root"' in script
    assert "parentDirectory(currentFileDirectory)" in script


def test_mod_backup_and_restore_operations_use_governed_interfaces() -> None:
    script = _asset("app.js")

    assert 'sendBinary("/api/admin/mods"' in script
    assert '"idempotency-key": makeIdempotencyKey()' in script
    assert "type: `mods.${action}`" in script
    assert 'type: "backup.create"' in script
    assert 'type: "restore.prepare"' in script
    assert 'intent.type === "restore.prepare" ? {type: "restore.execute"}' in script
    assert 'backup.state === "ready"' in script
    assert "/^[a-fA-F0-9]{64}$/.test(backup.checksum_sha256)" in script


def test_confirmed_operation_uses_the_bound_response_before_reporting_success() -> None:
    script = _asset("app.js")

    assert "const result = await sendIntent(intent);" in script
    assert "const completed = await applyIntentResult(result, intent);" in script
    assert 'pendingConfirmationStatus.textContent = result.notice || "Completed."' in script
