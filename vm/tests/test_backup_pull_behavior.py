from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
BACKUP_PULL = ROOT / "vm" / "backup-pull.ps1"
POWERSHELL = (
    shutil.which("powershell") or shutil.which("pwsh")
    if os.name == "nt"
    else shutil.which("pwsh")
)
STAMP = "20260827-010203"


def _write_fake(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8", newline="\n")


def _status(*, stamp: str = STAMP, state: str = "complete") -> dict[str, object]:
    return {
        "schema": 1,
        "event": "backup.completed",
        "backup_id": stamp,
        "status": state,
        "failure_count": 0 if state == "complete" else 1,
        "failure_codes": [] if state == "complete" else ["minecraft.rcon_quiesce_failed"],
        "components": {"minecraft_world": "offline_consistent"},
    }


def _status_without(field: str) -> dict[str, object]:
    status = _status()
    status.pop(field)
    return status


def _fake_tools(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_vbox = tmp_path / "fake-vbox.ps1"
    fake_ssh = tmp_path / "fake-ssh.ps1"
    fake_scp = tmp_path / "fake-scp.ps1"
    _write_fake(fake_vbox, 'Write-Output \'VMState="running"\'\n')
    _write_fake(
        fake_ssh,
        r'''param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$command = $Arguments[-1]
if ($command -eq 'true') { exit 0 }
if ($command -like '*backup-all.sh*') {
    Write-Output 'backup evidence produced'
    exit [int]$env:FAKE_BACKUP_EXIT_CODE
}
if ($command -like 'ls -1d *') {
    Write-Output "/var/backups/lantern/$($env:FAKE_BACKUP_STAMP)/"
    exit 0
}
if ($command -like 'ls -1 * | wc -l') {
    Write-Output ("{0}" -f (Get-ChildItem -LiteralPath $env:FAKE_REMOTE_DIR -File).Count)
    exit 0
}
exit 90
''',
    )
    _write_fake(
        fake_scp,
        r'''param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$target = $Arguments[-1]
New-Item -ItemType Directory -Force -Path $target | Out-Null
Get-ChildItem -LiteralPath $env:FAKE_REMOTE_DIR -File |
    Copy-Item -Destination $target
exit 0
''',
    )
    return fake_vbox, fake_ssh, fake_scp


def _run_pull(
    tmp_path: Path,
    status: dict[str, object] | str | None,
    *,
    remote_exit: int = 0,
    keep: int = 14,
    local_statuses: dict[str, dict[str, object] | str | None] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    if POWERSHELL is None:
        pytest.skip("PowerShell is required for executable backup-pull coverage")
    remote = tmp_path / "remote" / STAMP
    remote.mkdir(parents=True)
    (remote / "MANIFEST.txt").write_text("evidence\n", encoding="utf-8")
    if isinstance(status, dict):
        (remote / "BACKUP_STATUS.json").write_text(
            json.dumps(status) + "\n", encoding="utf-8"
        )
    elif isinstance(status, str):
        (remote / "BACKUP_STATUS.json").write_text(status, encoding="utf-8")

    key = tmp_path / "lantern_vm"
    key.write_text("disposable test key\n", encoding="utf-8")
    destination = tmp_path / "local"
    for name, prior_status in (local_statuses or {}).items():
        prior = destination / name
        prior.mkdir(parents=True)
        (prior / "MANIFEST.txt").write_text("prior evidence\n", encoding="utf-8")
        if isinstance(prior_status, dict):
            (prior / "BACKUP_STATUS.json").write_text(
                json.dumps(prior_status) + "\n", encoding="utf-8"
            )
        elif isinstance(prior_status, str):
            (prior / "BACKUP_STATUS.json").write_text(prior_status, encoding="utf-8")
    fake_vbox, fake_ssh, fake_scp = _fake_tools(tmp_path)
    environment = {
        **os.environ,
        "FAKE_BACKUP_EXIT_CODE": str(remote_exit),
        "FAKE_BACKUP_STAMP": STAMP,
        "FAKE_REMOTE_DIR": str(remote),
    }
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BACKUP_PULL),
            "-Dest",
            str(destination),
            "-Key",
            str(key),
            "-Keep",
            str(keep),
            "-VBoxManageExecutable",
            str(fake_vbox),
            "-SshExecutable",
            str(fake_ssh),
            "-ScpExecutable",
            str(fake_scp),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, destination


def test_remote_failure_is_propagated_after_evidence_is_copied(tmp_path: Path) -> None:
    # Keep the copied evidence independently valid so the only reason for the
    # nonzero result is the remote backup process itself.
    result, destination = _run_pull(tmp_path, _status(), remote_exit=1)

    copied = destination / STAMP
    assert result.returncode != 0
    assert (copied / "MANIFEST.txt").read_text(encoding="utf-8") == "evidence\n"
    assert (copied / "BACKUP_STATUS.json").is_file()
    assert "retained for diagnosis" in result.stdout


@pytest.mark.parametrize(
    "status",
    [
        None,
        "{not-json\n",
        {"schema": 1, "status": "complete"},
        json.dumps([_status()]),
        _status() | {"schema": "1"},
        _status() | {"failure_count": "0"},
        _status() | {"event": 1},
        _status() | {"backup_id": [STAMP]},
        _status() | {"status": True},
        _status() | {"failure_codes": None},
        _status() | {"failure_codes": ""},
        _status() | {"components": []},
        _status() | {"components": {"minecraft_world": 1}},
        _status() | {"components": {"minecraft_world": "OFFLINE_CONSISTENT"}},
        *(
            _status_without(field)
            for field in (
                "schema",
                "event",
                "backup_id",
                "status",
                "failure_count",
                "failure_codes",
                "components",
            )
        ),
        _status() | {"components": {}},
    ],
    ids=[
        "missing",
        "invalid-json",
        "partial",
        "top-level-array",
        "schema-numeric-string",
        "failure-count-numeric-string",
        "event-wrong-type",
        "backup-id-wrong-type",
        "status-wrong-type",
        "failure-codes-null",
        "failure-codes-string",
        "components-wrong-type",
        "minecraft-world-wrong-type",
        "minecraft-world-wrong-case",
        "missing-schema",
        "missing-event",
        "missing-backup-id",
        "missing-status",
        "missing-failure-count",
        "missing-failure-codes",
        "missing-components",
        "missing-minecraft-world",
    ],
)
def test_missing_invalid_or_partial_status_is_rejected_but_retained(
    tmp_path: Path, status: dict[str, object] | str | None
) -> None:
    result, destination = _run_pull(tmp_path, status)

    copied = destination / STAMP
    assert result.returncode != 0
    assert (copied / "MANIFEST.txt").is_file()
    assert copied.is_dir()
    assert "retained for diagnosis" in result.stdout


def test_incomplete_run_skips_retention_pruning(tmp_path: Path) -> None:
    prior_names = ["20260101-000000", "20260201-000000"]
    prior_statuses = {name: _status(stamp=name) for name in prior_names}

    result, destination = _run_pull(
        tmp_path,
        _status(state="incomplete"),
        keep=1,
        local_statuses=prior_statuses,
    )

    assert result.returncode != 0
    assert (destination / STAMP / "BACKUP_STATUS.json").is_file()
    assert all((destination / name).is_dir() for name in prior_names)
    assert "skipping retention pruning" in result.stdout


def test_success_prunes_only_fully_verified_complete_sets(tmp_path: Path) -> None:
    oldest_complete = "20260101-000000"
    newest_complete = "20260201-000000"
    incomplete = "20260301-000000"
    partial = "20260401-000000"
    legacy = "20260501-000000"
    numeric_schema = "20260601-000000"
    top_level_array = "20260701-000000"
    wrong_case_state = "20260801-000000"
    local_statuses: dict[str, dict[str, object] | str | None] = {
        oldest_complete: _status(stamp=oldest_complete),
        newest_complete: _status(stamp=newest_complete),
        incomplete: _status(stamp=incomplete, state="incomplete"),
        partial: {"schema": 1, "status": "complete"},
        legacy: None,
        numeric_schema: _status(stamp=numeric_schema) | {"schema": "1"},
        top_level_array: json.dumps([_status(stamp=top_level_array)]),
        wrong_case_state: _status(stamp=wrong_case_state)
        | {"components": {"minecraft_world": "OFFLINE_CONSISTENT"}},
    }

    result, destination = _run_pull(
        tmp_path,
        _status(),
        keep=2,
        local_statuses=local_statuses,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (destination / oldest_complete).exists()
    assert (destination / newest_complete).is_dir()
    assert (destination / STAMP).is_dir()
    assert (destination / incomplete).is_dir()
    assert (destination / partial).is_dir()
    assert (destination / legacy).is_dir()
    assert (destination / numeric_schema).is_dir()
    assert (destination / top_level_array).is_dir()
    assert (destination / wrong_case_state).is_dir()
