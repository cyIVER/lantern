from __future__ import annotations

import hashlib
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
RESULT_MARKER = "LANTERN_BACKUP_RESULT_V1:"


def _write_fake(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8", newline="\n")


def _status(*, stamp: str = STAMP, state: str = "complete") -> dict[str, object]:
    return {
        "schema": 1,
        "event": "backup.completed",
        "backup_id": stamp,
        "status": state,
        "failure_count": 0 if state == "complete" else 1,
        "failure_codes": []
        if state == "complete"
        else ["minecraft.rcon_quiesce_failed"],
        "components": {"minecraft_world": "offline_consistent"},
    }


def _status_without(field: str) -> dict[str, object]:
    status = _status()
    status.pop(field)
    return status


def _checksum_manifest(directory: Path) -> str:
    return "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(directory.iterdir())
        if path.is_file() and path.name != "SHA256SUMS"
    )


def _fake_tools(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_vbox = tmp_path / "fake-vbox.ps1"
    fake_ssh = tmp_path / "fake-ssh.ps1"
    fake_scp = tmp_path / "fake-scp.ps1"
    _write_fake(fake_vbox, "Write-Output 'VMState=\"running\"'\n")
    _write_fake(
        fake_ssh,
        r"""param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$command = $Arguments[-1]
if ($command -eq 'true') { exit 0 }
if ($command -like '*backup-all.sh*') {
    Write-Output 'backup evidence produced'
    if ($env:FAKE_RESULT_MARKERS) {
        $env:FAKE_RESULT_MARKERS.Split('|') | Write-Output
    }
    exit [int]$env:FAKE_BACKUP_EXIT_CODE
}
if ($command -like 'ls -1d *') {
    Write-Output $env:FAKE_REMOTE_LATEST
    exit 0
}
if ($command -like 'ls -1 * | wc -l') {
    Write-Output ("{0}" -f (Get-ChildItem -LiteralPath $env:FAKE_REMOTE_DIR -File).Count)
    exit 0
}
exit 90
""",
    )
    _write_fake(
        fake_scp,
        r"""param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
$target = $Arguments[-1]
if ($env:FAKE_REPARSE_MODE -eq 'root') {
    New-Item -ItemType Junction -Path $target -Target $env:FAKE_REMOTE_DIR |
        Out-Null
    exit 0
}
New-Item -ItemType Directory -Force -Path $target | Out-Null
Get-ChildItem -LiteralPath $env:FAKE_REMOTE_DIR -File |
    Copy-Item -Destination $target
if ($env:FAKE_REPARSE_MODE -eq 'child') {
    New-Item -ItemType Directory -Path $env:FAKE_REPARSE_TARGET | Out-Null
    New-Item -ItemType Junction `
        -Path (Join-Path $target 'linked-child') `
        -Target $env:FAKE_REPARSE_TARGET | Out-Null
}
exit 0
""",
    )
    return fake_vbox, fake_ssh, fake_scp


def _run_pull(
    tmp_path: Path,
    status: dict[str, object] | str | None,
    *,
    remote_exit: int = 0,
    keep: int = 14,
    local_statuses: dict[str, dict[str, object] | str | None] | None = None,
    corrupt_prior_checksums: set[str] | None = None,
    checksum_variant: str = "valid",
    result_path: str = f"/var/backups/lantern/{STAMP}/",
    result_markers: list[str] | None = None,
    fallback_latest: str = f"/var/backups/lantern/{STAMP}/",
    publish_race: bool = False,
    post_publish_corruption: bool = False,
    block_publish_rollback: bool = False,
    reparse_mode: str = "none",
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

    checksum = _checksum_manifest(remote)
    checksum_lines = checksum.splitlines(keepends=True)
    if checksum_variant == "missing":
        pass
    elif checksum_variant == "malformed-hash":
        (remote / "SHA256SUMS").write_text(
            "z" * 64 + checksum_lines[0][64:], encoding="utf-8"
        )
    elif checksum_variant == "absolute-path":
        (remote / "SHA256SUMS").write_text(
            checksum_lines[0][:66] + "C:\\backup.txt\n", encoding="utf-8"
        )
    elif checksum_variant == "traversal-path":
        (remote / "SHA256SUMS").write_text(
            checksum_lines[0][:66] + "../MANIFEST.txt\n", encoding="utf-8"
        )
    elif checksum_variant == "subdirectory-path":
        (remote / "SHA256SUMS").write_text(
            checksum_lines[0][:66] + "nested/MANIFEST.txt\n", encoding="utf-8"
        )
    elif checksum_variant == "duplicate":
        (remote / "SHA256SUMS").write_text(
            checksum + checksum_lines[0], encoding="utf-8"
        )
    elif checksum_variant == "missing-local-file":
        (remote / "SHA256SUMS").write_text(
            checksum + "0" * 64 + "  missing.txt\n", encoding="utf-8"
        )
    elif checksum_variant == "extra-local-file":
        (remote / "SHA256SUMS").write_text(checksum, encoding="utf-8")
        (remote / "unlisted.txt").write_text("unlisted\n", encoding="utf-8")
    elif checksum_variant == "hash-mismatch":
        (remote / "SHA256SUMS").write_text(
            "0" * 64 + checksum_lines[0][64:] + "".join(checksum_lines[1:]),
            encoding="utf-8",
        )
    elif checksum_variant == "uppercase-hash":
        (remote / "SHA256SUMS").write_text(
            checksum_lines[0][:64].upper()
            + checksum_lines[0][64:]
            + "".join(checksum_lines[1:]),
            encoding="utf-8",
        )
    elif checksum_variant == "binary-marker":
        (remote / "SHA256SUMS").write_text(
            checksum_lines[0][:65]
            + "*"
            + checksum_lines[0][66:]
            + "".join(checksum_lines[1:]),
            encoding="utf-8",
        )
    elif checksum_variant == "case-insensitive-duplicate":
        (remote / "SHA256SUMS").write_text(
            checksum + checksum_lines[0][:66] + checksum_lines[0][66:].lower(),
            encoding="utf-8",
        )
    elif checksum_variant == "reserved-name":
        (remote / "SHA256SUMS").write_text(
            checksum + "0" * 64 + "  CON.txt\n", encoding="utf-8"
        )
    elif checksum_variant == "missing-required-manifest-entry":
        (remote / "SHA256SUMS").write_text(
            "".join(
                line for line in checksum_lines if not line.endswith("MANIFEST.txt\n")
            ),
            encoding="utf-8",
        )
    elif checksum_variant == "missing-required-status-entry":
        (remote / "SHA256SUMS").write_text(
            "".join(
                line
                for line in checksum_lines
                if not line.endswith("BACKUP_STATUS.json\n")
            ),
            encoding="utf-8",
        )
    elif checksum_variant == "overlong-name":
        (remote / "SHA256SUMS").write_text(
            checksum + "0" * 64 + "  " + "a" * 256 + "\n", encoding="utf-8"
        )
    elif checksum_variant == "too-many-lines":
        (remote / "SHA256SUMS").write_text(checksum_lines[0] * 4097, encoding="utf-8")
    elif checksum_variant == "valid":
        (remote / "SHA256SUMS").write_text(checksum, encoding="utf-8")
    else:
        raise ValueError(f"unknown checksum variant: {checksum_variant}")

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
        (prior / "SHA256SUMS").write_text(_checksum_manifest(prior), encoding="utf-8")
        if name in (corrupt_prior_checksums or set()):
            checksum_path = prior / "SHA256SUMS"
            content = checksum_path.read_text(encoding="utf-8")
            checksum_path.write_text("0" * 64 + content[64:], encoding="utf-8")
    fake_vbox, fake_ssh, fake_scp = _fake_tools(tmp_path)
    environment = {
        **os.environ,
        "FAKE_BACKUP_EXIT_CODE": str(remote_exit),
        "FAKE_BACKUP_STAMP": STAMP,
        "FAKE_RESULT_MARKERS": "|".join(
            result_markers
            if result_markers is not None
            else [f"{RESULT_MARKER}{result_path}"]
        ),
        "FAKE_REMOTE_LATEST": fallback_latest,
        "FAKE_REMOTE_DIR": str(remote),
        "FAKE_REPARSE_MODE": reparse_mode,
        "FAKE_REPARSE_TARGET": str(tmp_path / "reparse-child-target"),
    }
    command = [
        POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
    ]
    if publish_race or post_publish_corruption:
        wrapper = tmp_path / "run-backup-pull-with-publish-race.ps1"
        _write_fake(
            wrapper,
            r"""if ($env:FAKE_BREAKPOINT_MODE -eq 'collision') {
    $pattern = '(Move-Item -LiteralPath \$staging|\[IO\.Directory\]::Move\(\$staging, \$target\))'
    $action = {
        [IO.Directory]::CreateDirectory($env:FAKE_RACE_TARGET) | Out-Null
        [IO.File]::WriteAllText(
            (Join-Path $env:FAKE_RACE_TARGET 'collision-marker.txt'),
            'existing target'
        )
    }
} else {
    $pattern = '^\s*\$publishedIntegrityValid = Test-BackupTransferIntegrity -Path \$target\s*$'
    $action = {
        [IO.File]::AppendAllText(
            (Join-Path $env:FAKE_RACE_TARGET 'MANIFEST.txt'),
            'post-publish corruption'
        )
        if ($env:FAKE_BLOCK_ROLLBACK -eq '1') {
            [IO.Directory]::CreateDirectory($staging) | Out-Null
        }
    }
}
$publishLine = @(
    Select-String -LiteralPath $env:FAKE_BACKUP_PULL_SCRIPT -Pattern $pattern
)
if ($publishLine.Count -ne 1) { exit 97 }
Set-PSBreakpoint `
    -Script $env:FAKE_BACKUP_PULL_SCRIPT `
    -Line $publishLine[0].LineNumber `
    -Action $action | Out-Null

. $env:FAKE_BACKUP_PULL_SCRIPT `
    -Dest $env:FAKE_DESTINATION `
    -Key $env:FAKE_KEY `
    -Keep ([int]$env:FAKE_KEEP) `
    -VBoxManageExecutable $env:FAKE_VBOX `
    -SshExecutable $env:FAKE_SSH `
    -ScpExecutable $env:FAKE_SCP
if (-not $?) { exit 1 }
""",
        )
        environment.update(
            {
                "FAKE_BACKUP_PULL_SCRIPT": str(BACKUP_PULL),
                "FAKE_DESTINATION": str(destination),
                "FAKE_KEY": str(key),
                "FAKE_KEEP": str(keep),
                "FAKE_VBOX": str(fake_vbox),
                "FAKE_SSH": str(fake_ssh),
                "FAKE_SCP": str(fake_scp),
                "FAKE_RACE_TARGET": str(destination / STAMP),
                "FAKE_BREAKPOINT_MODE": (
                    "collision" if publish_race else "post-publish-corruption"
                ),
                "FAKE_BLOCK_ROLLBACK": "1" if block_publish_rollback else "0",
            }
        )
        command.extend(["-File", str(wrapper)])
    else:
        command.extend(
            [
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
            ]
        )
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, destination


@pytest.mark.parametrize(
    "checksum_variant",
    [
        "missing",
        "malformed-hash",
        "absolute-path",
        "traversal-path",
        "subdirectory-path",
        "duplicate",
        "missing-local-file",
        "extra-local-file",
        "hash-mismatch",
        "uppercase-hash",
        "binary-marker",
        "case-insensitive-duplicate",
        "reserved-name",
        "missing-required-manifest-entry",
        "missing-required-status-entry",
        "overlong-name",
        "too-many-lines",
    ],
)
def test_invalid_transfer_manifest_is_rejected_retained_and_skips_pruning(
    tmp_path: Path, checksum_variant: str
) -> None:
    prior = "20260101-000000"
    result, destination = _run_pull(
        tmp_path,
        _status(),
        keep=1,
        local_statuses={prior: _status(stamp=prior)},
        checksum_variant=checksum_variant,
    )

    assert result.returncode != 0
    copied_sets = list(destination.glob(f".{STAMP}.transfer-*"))
    assert len(copied_sets) == 1
    assert (destination / prior).is_dir()
    assert "transfer verification failed" in result.stdout
    assert "skipping retention pruning" in result.stdout


def test_valid_producer_sha256sum_manifest_is_accepted(tmp_path: Path) -> None:
    result, destination = _run_pull(tmp_path, _status())

    assert result.returncode == 0, result.stdout + result.stderr
    assert (destination / STAMP / "SHA256SUMS").is_file()
    assert RESULT_MARKER not in result.stdout


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows junction and reparse-point semantics require NTFS",
)
@pytest.mark.parametrize("reparse_mode", ["root", "child"])
def test_reparse_points_are_rejected_before_publication(
    tmp_path: Path, reparse_mode: str
) -> None:
    result, destination = _run_pull(tmp_path, _status(), reparse_mode=reparse_mode)

    assert result.returncode != 0
    assert not (destination / STAMP).exists()
    assert len(list(destination.glob(f".{STAMP}.transfer-*"))) == 1
    assert "transfer verification failed" in result.stdout


@pytest.mark.parametrize(
    "result_path",
    [
        f"/var/backups/lantern/../{STAMP}/",
        f"/var/backups/lantern/{STAMP}",
        f"/tmp/{STAMP}/",
        f"/var/backups/lantern/{STAMP}/extra/",
    ],
)
def test_untrusted_result_path_marker_is_rejected(
    tmp_path: Path, result_path: str
) -> None:
    result, destination = _run_pull(tmp_path, _status(), result_path=result_path)

    assert result.returncode != 0
    assert not destination.exists()
    assert "invalid backup result path marker" in result.stdout


def test_missing_result_marker_never_falls_back_to_older_set(tmp_path: Path) -> None:
    prior = "20260101-000000"
    result, destination = _run_pull(
        tmp_path,
        _status(),
        remote_exit=1,
        local_statuses={prior: _status(stamp=prior)},
        result_markers=[],
        fallback_latest=f"/var/backups/lantern/{prior}/",
    )

    assert result.returncode != 0
    assert (destination / prior).is_dir()
    assert not list(destination.glob(".*.transfer-*"))
    assert "result path marker is missing or ambiguous" in result.stdout


def test_duplicate_result_markers_are_rejected(tmp_path: Path) -> None:
    marker = f"{RESULT_MARKER}/var/backups/lantern/{STAMP}/"
    result, destination = _run_pull(
        tmp_path, _status(), result_markers=[marker, marker]
    )

    assert result.returncode != 0
    assert not destination.exists()
    assert "result path marker is missing or ambiguous" in result.stdout


def test_result_marker_must_be_the_final_remote_output_line(tmp_path: Path) -> None:
    marker = f"{RESULT_MARKER}/var/backups/lantern/{STAMP}/"
    result, destination = _run_pull(
        tmp_path, _status(), result_markers=[marker, "unexpected trailing output"]
    )

    assert result.returncode != 0
    assert not destination.exists()
    assert "result path marker is missing or ambiguous" in result.stdout


def test_success_rejects_hidden_staging_result_path(tmp_path: Path) -> None:
    result, destination = _run_pull(
        tmp_path,
        _status(),
        result_path=f"/var/backups/lantern/.{STAMP}.staging.aB3dE9/",
    )

    assert result.returncode != 0
    assert not destination.exists()
    assert "invalid backup result path marker" in result.stdout


def test_publish_failure_pulls_current_hidden_diagnostic_not_older_set(
    tmp_path: Path,
) -> None:
    prior = "20260101-000000"
    result, destination = _run_pull(
        tmp_path,
        _status(state="incomplete"),
        remote_exit=1,
        checksum_variant="missing",
        result_path=f"/var/backups/lantern/.{STAMP}.staging.aB3dE9/",
        fallback_latest=f"/var/backups/lantern/{prior}/",
        local_statuses={prior: _status(stamp=prior)},
    )

    staging = list(destination.glob(f".{STAMP}.transfer-*"))
    assert result.returncode != 0
    assert len(staging) == 1
    assert (staging[0] / "MANIFEST.txt").read_text(encoding="utf-8") == "evidence\n"
    assert (destination / prior).is_dir()
    assert not list(destination.glob(f".{prior}.transfer-*"))


def test_hidden_diagnostic_status_must_match_marker_stamp(tmp_path: Path) -> None:
    marker_stamp = "20260827-020304"
    result, destination = _run_pull(
        tmp_path,
        _status(state="incomplete"),
        remote_exit=1,
        result_path=f"/var/backups/lantern/.{marker_stamp}.staging.aB3dE9/",
    )

    assert result.returncode != 0
    assert len(list(destination.glob(f".{marker_stamp}.transfer-*"))) == 1
    assert "backup status contract is not restore-eligible" in result.stdout


@pytest.mark.parametrize(
    "result_path",
    [
        f"/var/backups/lantern/.{STAMP}.staging.short/",
        f"/var/backups/lantern/.{STAMP}.staging.aB3dE9/extra/",
        f"/var/backups/lantern/{STAMP}.staging.aB3dE9/",
    ],
)
def test_failure_rejects_malformed_hidden_result_path(
    tmp_path: Path, result_path: str
) -> None:
    result, destination = _run_pull(
        tmp_path, _status(state="incomplete"), remote_exit=1, result_path=result_path
    )

    assert result.returncode != 0
    assert not destination.exists()
    assert "invalid backup result path marker" in result.stdout


def test_existing_final_set_is_not_overwritten(tmp_path: Path) -> None:
    existing = _status(stamp=STAMP)
    result, destination = _run_pull(
        tmp_path, _status(), local_statuses={STAMP: existing}
    )

    assert result.returncode != 0
    assert (
        json.loads(
            (destination / STAMP / "BACKUP_STATUS.json").read_text(encoding="utf-8")
        )
        == existing
    )
    assert len(list(destination.glob(f".{STAMP}.transfer-*"))) == 1
    assert "refusing to overwrite" in result.stdout


def test_target_created_during_publish_is_not_accepted(tmp_path: Path) -> None:
    result, destination = _run_pull(tmp_path, _status(), publish_race=True)

    target = destination / STAMP
    assert result.returncode != 0, result.stdout + result.stderr
    assert (target / "collision-marker.txt").read_text(encoding="utf-8") == (
        "existing target"
    )
    assert not (target / "MANIFEST.txt").exists()
    assert len(list(target.glob(f".{STAMP}.transfer-*"))) == 0
    assert len(list(destination.glob(f".{STAMP}.transfer-*"))) == 1
    assert "could not be published atomically" in result.stdout


def test_published_set_is_revalidated_and_returned_to_staging(tmp_path: Path) -> None:
    result, destination = _run_pull(tmp_path, _status(), post_publish_corruption=True)

    assert result.returncode != 0
    assert not (destination / STAMP).exists()
    staging = list(destination.glob(f".{STAMP}.transfer-*"))
    assert len(staging) == 1
    assert "post-publish corruption" in (staging[0] / "MANIFEST.txt").read_text(
        encoding="utf-8"
    )
    assert "failed validation after publication" in result.stdout


def test_failed_publish_rollback_remains_nonzero_and_unprunable(
    tmp_path: Path,
) -> None:
    prior = "20260101-000000"
    result, destination = _run_pull(
        tmp_path,
        _status(),
        keep=0,
        local_statuses={prior: _status(stamp=prior)},
        post_publish_corruption=True,
        block_publish_rollback=True,
    )

    assert result.returncode != 0
    assert (destination / STAMP).is_dir()
    assert (destination / prior).is_dir()
    assert len(list(destination.glob(f".{STAMP}.transfer-*"))) == 1
    assert "could not be returned to diagnostic staging" in result.stdout
    assert "skipping retention pruning" in result.stdout


def test_remote_failure_is_propagated_after_evidence_is_copied(tmp_path: Path) -> None:
    # Keep the copied evidence independently valid so the only reason for the
    # nonzero result is the remote backup process itself.
    result, destination = _run_pull(tmp_path, _status(), remote_exit=1)

    staging = list(destination.glob(f".{STAMP}.transfer-*"))
    assert not (destination / STAMP).exists()
    copied = staging[0]
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

    staging = list(destination.glob(f".{STAMP}.transfer-*"))
    assert not (destination / STAMP).exists()
    copied = staging[0]
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
    assert not (destination / STAMP).exists()
    assert len(list(destination.glob(f".{STAMP}.transfer-*"))) == 1
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
    corrupt_checksum = "20260115-000000"
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
        corrupt_checksum: _status(stamp=corrupt_checksum),
    }

    result, destination = _run_pull(
        tmp_path,
        _status(),
        keep=2,
        local_statuses=local_statuses,
        corrupt_prior_checksums={corrupt_checksum},
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
    assert (destination / corrupt_checksum).is_dir()
