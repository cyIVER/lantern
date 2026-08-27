from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
BACKUP_SCRIPT = ROOT / "vm" / "backup-all.sh"
BACKUP_PULL_SCRIPT = ROOT / "vm" / "backup-pull.ps1"
SINGLE_BACKUP_SCRIPT = ROOT / "stack" / "bootstrap" / "backup.sh"
MC_UUID = "2be9425c-1141-4181-b0a0-34f38d84fb7f"
SENTINEL_PASSWORD = "test-rcon-password-must-not-leak"


def _bash_path(path: Path) -> str:
    resolved = path.resolve().as_posix()
    if os.name == "nt":
        return f"/{resolved[0].lower()}{resolved[2:]}"
    return resolved


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _run_backup(
    tmp_path: Path,
    *,
    running: bool = True,
    fail_stage: str = "",
    minecraft_present: bool = True,
    helper_state: str = "executable",
    keep: int = 7,
) -> tuple[subprocess.CompletedProcess[str], list[str], dict[str, object]]:
    stack = tmp_path / "stack"
    bootstrap = stack / "bootstrap"
    destination = tmp_path / "backups"
    fake_bin = tmp_path / "fake-bin"
    state = tmp_path / "state"
    for directory in (bootstrap, destination, fake_bin, state):
        directory.mkdir(parents=True, exist_ok=True)
    (stack / ".env").write_text("DB_ROOT_PASSWORD=disposable\n", encoding="utf-8")
    helper = bootstrap / "mc-rcon.py"
    if helper_state == "executable":
        _write_executable(helper, "#!/usr/bin/env python3\n")
    elif helper_state == "non_executable":
        helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8", newline="\n")
    elif helper_state != "missing":
        raise ValueError(f"unsupported helper state: {helper_state}")

    operations = state / "operations.log"
    encoded_password = "dGVzdC1yY29uLXBhc3N3b3JkLW11c3Qtbm90LWxlYWs="

    _write_executable(
        fake_bin / "docker",
        rf'''#!/usr/bin/env bash
set -u
joined=" $* "
case "$joined" in
  *" compose exec -T panel "*)
    case "${{FAKE_FAIL_STAGE:-}}" in
      credentials) exit 1 ;;
      credential_record) printf 'UNRECOGNIZED:%s:25575\n' '{encoded_password}'; exit 0 ;;
      credential_base64) printf 'LANTERN_RCON_V1:not*base64:25575\n'; exit 0 ;;
      credential_port) printf 'LANTERN_RCON_V1:%s:70000\n' '{encoded_password}'; exit 0 ;;
    esac
    printf 'LANTERN_RCON_V1:{encoded_password}:25575\n'
    exit 0
    ;;
  *" exec -e MYSQL_PWD="*)
    printf '%s\n' 'Dump completed'
    exit 0
    ;;
  *" inspect -f "*" {MC_UUID} "*)
    case "${{FAKE_FAIL_STAGE:-}}" in
      state_error) exit 1 ;;
      state_empty) exit 0 ;;
      state_garbage) printf 'unknown\n'; exit 0 ;;
      start_before_archive)
        count_file="$FAKE_STATE_DIR/inspect-count"
        count=0
        [ ! -f "$count_file" ] || read -r count < "$count_file"
        count=$((count + 1))
        printf '%s\n' "$count" > "$count_file"
        if [ "$count" -eq 1 ]; then printf 'false\n'; else printf 'true\n'; fi
        exit 0
        ;;
      start_during_archive)
        count_file="$FAKE_STATE_DIR/inspect-count"
        count=0
        [ ! -f "$count_file" ] || read -r count < "$count_file"
        count=$((count + 1))
        printf '%s\n' "$count" > "$count_file"
        if [ "$count" -lt 3 ]; then printf 'false\n'; else printf 'true\n'; fi
        exit 0
        ;;
    esac
    printf '%s\n' "${{FAKE_MC_RUNNING:-false}}"
    exit 0
    ;;
  *" volume inspect "*) exit 1 ;;
esac
exit 0
''',
    )
    _write_executable(
        fake_bin / "sudo",
        rf'''#!/usr/bin/env bash
set -u
if [ "$1" = test ] && [ "${{2:-}}" = -d ] && [[ "${{3:-}}" == *"{MC_UUID}" ]]; then
  [ "${{FAKE_MC_PRESENT:-true}}" = true ]
  exit
fi
exec "$@"
''',
    )
    _write_executable(
        fake_bin / "tar",
        r'''#!/usr/bin/env bash
set -u
output=
next=false
for arg in "$@"; do
  if [ "$next" = true ]; then output=$arg; next=false; continue; fi
  [ "$arg" = -czf ] && next=true
done
[ -n "$output" ] || exit 1
case "$output" in
  *minecraft-world.tgz)
    printf 'world-archive\n' >> "$FAKE_OPERATION_LOG"
    [ "${FAKE_FAIL_STAGE:-}" != archive ] || exit 1
    ;;
esac
mkdir -p "$(dirname "$output")"
printf 'test archive\n' > "$output"
''',
    )
    _write_executable(
        fake_bin / "python3",
        r'''#!/usr/bin/env bash
set -uo pipefail
if [[ "${1:-}" == */mc-rcon.py ]]; then
  shift
  printf 'rcon-argv:%s\n' "$*" >> "$FAKE_OPERATION_LOG"
  [ "${1:-}" = --password-stdin ] || exit 90
  shift
  host=${1:-}; port=${2:-}; shift 2 || exit 91
  command="$*"
  password=$(cat)
  [ "$password" = "$FAKE_EXPECTED_RCON_PASSWORD" ] || exit 92
  printf 'rcon:%s:%s:%s\n' "$host" "$port" "$command" >> "$FAKE_OPERATION_LOG"
  case "${FAKE_FAIL_STAGE:-}:$command" in
    save_off:save-off|flush:save-all\ flush|resume:save-on) exit 1 ;;
  esac
  exit 0
fi
exec "$REAL_PYTHON" "$@"
''',
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")

    bash = shutil.which("bash") or "bash"
    if os.name == "nt":
        git_bash = Path(os.environ["ProgramFiles"]) / "Git" / "bin" / "bash.exe"
        if git_bash.exists():
            bash = str(git_bash)
    environment = {
        **os.environ,
        "FAKE_OPERATION_LOG": _bash_path(operations),
        "FAKE_STATE_DIR": _bash_path(state),
        "FAKE_MC_RUNNING": str(running).lower(),
        "FAKE_MC_PRESENT": str(minecraft_present).lower(),
        "FAKE_FAIL_STAGE": fail_stage,
        "FAKE_EXPECTED_RCON_PASSWORD": SENTINEL_PASSWORD,
        "REAL_PYTHON": _bash_path(Path(sys.executable)),
        "LANTERN_STACK": _bash_path(stack),
        "LANTERN_BACKUP_DIR": _bash_path(destination),
        "LANTERN_BACKUP_KEEP": str(keep),
    }
    result = subprocess.run(
        [
            bash,
            "-c",
            'PATH="$1:$PATH"; export PATH; shift; exec "$@"',
            "backup-harness",
            _bash_path(fake_bin),
            _bash_path(BACKUP_SCRIPT),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    operation_lines = (
        operations.read_text(encoding="utf-8").splitlines()
        if operations.exists()
        else []
    )
    status_files = list(destination.glob("*/BACKUP_STATUS.json"))
    status = (
        json.loads(max(status_files).read_text(encoding="utf-8"))
        if status_files
        else {}
    )
    return result, operation_lines, status


def _run_single_backup_state(
    tmp_path: Path, stage: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake_bin = tmp_path / "single-fake-bin"
    destination = tmp_path / "single-backups"
    state = tmp_path / "single-state"
    for directory in (fake_bin, destination, state):
        directory.mkdir(parents=True)
    operations = state / "operations.log"
    _write_executable(
        fake_bin / "docker",
        rf'''#!/usr/bin/env bash
set -u
joined=" $* "
case "$joined" in
  *" compose exec -T panel "*)
    count_file="$FAKE_STATE_DIR/tinker-count"
    count=0
    [ ! -f "$count_file" ] || read -r count < "$count_file"
    count=$((count + 1))
    printf '%s\n' "$count" > "$count_file"
    if [ "$count" -eq 1 ]; then
      printf '{MC_UUID}\n'
    else
      printf 'disposable-rcon 25575\n'
    fi
    exit 0
    ;;
  *" inspect -f "*)
    case "$FAKE_SINGLE_STAGE" in
      state_error) exit 1 ;;
      state_empty) exit 0 ;;
      state_garbage) printf 'unknown\n'; exit 0 ;;
      resume_failure) printf 'true\n'; exit 0 ;;
      start_before_archive)
        count_file="$FAKE_STATE_DIR/inspect-count"
        count=0
        [ ! -f "$count_file" ] || read -r count < "$count_file"
        count=$((count + 1))
        printf '%s\n' "$count" > "$count_file"
        if [ "$count" -eq 1 ]; then printf 'false\n'; else printf 'true\n'; fi
        exit 0
        ;;
      start_during_archive)
        count_file="$FAKE_STATE_DIR/inspect-count"
        count=0
        [ ! -f "$count_file" ] || read -r count < "$count_file"
        count=$((count + 1))
        printf '%s\n' "$count" > "$count_file"
        if [ "$count" -lt 3 ]; then printf 'false\n'; else printf 'true\n'; fi
        exit 0
        ;;
    esac
    ;;
  *" run --rm "*)
    printf 'single-world-archive\n' >> "$FAKE_OPERATION_LOG"
    printf 'archive\n' > "$FAKE_SINGLE_ARCHIVE"
    exit 0
    ;;
esac
exit 0
''',
    )
    _write_executable(
        fake_bin / "python3",
        r'''#!/usr/bin/env bash
set -uo pipefail
if [[ "${1:-}" == */mc-rcon.py ]]; then
  shift
  [ "${1:-}" = --password-stdin ] || exit 90
  shift 3
  command="$*"
  cat >/dev/null
  printf 'single-rcon:%s\n' "$command" >> "$FAKE_OPERATION_LOG"
  [ "$FAKE_SINGLE_STAGE:$command" != resume_failure:save-on ]
  exit
fi
exit 1
''',
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(fake_bin / "zstd", "#!/usr/bin/env bash\nexit 0\n")
    archive = destination / "minecraft-20260827-000000.tar.zst"
    _write_executable(
        fake_bin / "date",
        "#!/usr/bin/env bash\nprintf '20260827-000000\\n'\n",
    )
    bash = shutil.which("bash") or "bash"
    if os.name == "nt":
        git_bash = Path(os.environ["ProgramFiles"]) / "Git" / "bin" / "bash.exe"
        if git_bash.exists():
            bash = str(git_bash)
    result = subprocess.run(
        [
            bash,
            "-c",
            'PATH="$1:$PATH"; export PATH; shift; exec "$@"',
            "single-backup-harness",
            _bash_path(fake_bin),
            _bash_path(SINGLE_BACKUP_SCRIPT),
            "minecraft",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "FAKE_OPERATION_LOG": _bash_path(operations),
            "FAKE_SINGLE_ARCHIVE": _bash_path(archive),
            "FAKE_SINGLE_STAGE": stage,
            "FAKE_STATE_DIR": _bash_path(state),
            "LANTERN_BACKUP_DIR": _bash_path(destination),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    operation_lines = (
        operations.read_text(encoding="utf-8").splitlines()
        if operations.exists()
        else []
    )
    return result, operation_lines


def test_running_minecraft_quiesces_archives_and_resumes_in_order(
    tmp_path: Path,
) -> None:
    result, operations, status = _run_backup(tmp_path)

    assert result.returncode == 0, result.stderr
    expected = [
        "rcon:127.0.0.1:25575:save-off",
        "rcon:127.0.0.1:25575:save-all flush",
        "world-archive",
        "rcon:127.0.0.1:25575:save-on",
    ]
    assert [operations.index(item) for item in expected] == sorted(
        operations.index(item) for item in expected
    )
    assert operations.count(expected[-1]) == 1
    assert status["status"] == "complete"
    assert status["components"]["minecraft_world"] == "quiesced_consistent"


@pytest.mark.parametrize(
    ("stage", "reason", "expect_resume"),
    [
        ("credentials", "minecraft.rcon_credentials_unavailable", False),
        ("save_off", "minecraft.rcon_quiesce_failed", False),
        ("flush", "minecraft.rcon_quiesce_failed", True),
    ],
)
def test_running_minecraft_refuses_archive_when_quiescence_fails(
    tmp_path: Path,
    stage: str,
    reason: str,
    expect_resume: bool,
) -> None:
    result, operations, status = _run_backup(tmp_path, fail_stage=stage)

    assert result.returncode != 0
    assert "world-archive" not in operations
    resumes = operations.count("rcon:127.0.0.1:25575:save-on")
    assert resumes == int(expect_resume)
    assert reason in status["failure_codes"]
    combined = result.stdout + result.stderr + "\n".join(operations) + json.dumps(status)
    assert SENTINEL_PASSWORD not in combined


def test_stopped_minecraft_archives_without_rcon(tmp_path: Path) -> None:
    result, operations, status = _run_backup(tmp_path, running=False)

    assert result.returncode == 0, result.stderr
    assert "world-archive" in operations
    assert not any(item.startswith("rcon:") for item in operations)
    assert status["components"]["minecraft_world"] == "offline_consistent"


def test_resume_failure_makes_backup_incomplete_without_leaking_secret(
    tmp_path: Path,
) -> None:
    result, operations, status = _run_backup(tmp_path, fail_stage="resume")

    assert result.returncode != 0
    assert "world-archive" in operations
    assert operations.count("rcon:127.0.0.1:25575:save-on") == 1
    assert "minecraft.rcon_resume_failed" in status["failure_codes"]
    combined = result.stdout + result.stderr + "\n".join(operations) + json.dumps(status)
    assert SENTINEL_PASSWORD not in combined


def test_incomplete_backup_never_prunes_prior_sets(tmp_path: Path) -> None:
    destination = tmp_path / "backups"
    prior_sets = [destination / "20260101-000000", destination / "20260201-000000"]
    for prior in prior_sets:
        prior.mkdir(parents=True)
        (prior / "BACKUP_STATUS.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "event": "backup.completed",
                    "backup_id": prior.name,
                    "status": "complete",
                    "failure_count": 0,
                    "failure_codes": [],
                    "components": {"minecraft_world": "offline_consistent"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

    result, _operations, status = _run_backup(
        tmp_path, fail_stage="save_off", keep=2
    )

    assert result.returncode != 0
    assert status["status"] == "incomplete"
    assert all(prior.is_dir() for prior in prior_sets)


def test_missing_minecraft_world_makes_set_incomplete(tmp_path: Path) -> None:
    result, operations, status = _run_backup(tmp_path, minecraft_present=False)

    assert result.returncode != 0
    assert "world-archive" not in operations
    assert status["components"]["minecraft_world"] == "missing"
    assert "minecraft.world_missing" in status["failure_codes"]


@pytest.mark.parametrize(
    "helper_state",
    [
        "missing",
        pytest.param(
            "non_executable",
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="Git Bash maps regular NTFS files as executable",
            ),
        ),
    ],
)
def test_running_minecraft_requires_executable_rcon_helper(
    tmp_path: Path, helper_state: str
) -> None:
    result, operations, status = _run_backup(tmp_path, helper_state=helper_state)

    assert result.returncode != 0
    assert "world-archive" not in operations
    assert status["components"]["minecraft_world"] == "credentials_unavailable"
    assert "minecraft.rcon_credentials_unavailable" in status["failure_codes"]


@pytest.mark.parametrize(
    "stage", ["credential_record", "credential_base64", "credential_port"]
)
def test_running_minecraft_rejects_malformed_rcon_credentials(
    tmp_path: Path, stage: str
) -> None:
    result, operations, status = _run_backup(tmp_path, fail_stage=stage)

    assert result.returncode != 0
    assert "world-archive" not in operations
    assert status["components"]["minecraft_world"] == "credentials_unavailable"
    assert "minecraft.rcon_credentials_unavailable" in status["failure_codes"]


@pytest.mark.parametrize("stage", ["state_error", "state_empty", "state_garbage"])
def test_unknown_minecraft_state_refuses_world_archive(
    tmp_path: Path, stage: str
) -> None:
    result, operations, status = _run_backup(tmp_path, fail_stage=stage)

    assert result.returncode != 0
    assert "world-archive" not in operations
    assert status["components"]["minecraft_world"] == "state_unavailable"
    assert "minecraft.state_unavailable" in status["failure_codes"]


def test_offline_server_start_before_archive_is_detected(tmp_path: Path) -> None:
    result, operations, status = _run_backup(
        tmp_path, running=False, fail_stage="start_before_archive"
    )

    assert result.returncode != 0
    assert "world-archive" not in operations
    assert status["components"]["minecraft_world"] == "state_changed"
    assert "minecraft.state_changed" in status["failure_codes"]


def test_offline_server_start_during_archive_discards_archive(tmp_path: Path) -> None:
    result, operations, status = _run_backup(
        tmp_path, running=False, fail_stage="start_during_archive"
    )

    assert result.returncode != 0
    assert "world-archive" in operations
    assert not list(tmp_path.glob("backups/*/minecraft-world.tgz"))
    assert status["components"]["minecraft_world"] == "state_changed"
    assert "minecraft.state_changed" in status["failure_codes"]


def test_success_prunes_only_verified_complete_sets(tmp_path: Path) -> None:
    destination = tmp_path / "backups"
    complete_sets = [destination / "20260101-000000", destination / "20260201-000000"]
    incomplete = destination / "20260301-000000"
    legacy = destination / "20260401-000000"
    for directory in [*complete_sets, incomplete, legacy]:
        directory.mkdir(parents=True)
    for directory in complete_sets:
        (directory / "BACKUP_STATUS.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "event": "backup.completed",
                    "backup_id": directory.name,
                    "status": "complete",
                    "failure_count": 0,
                    "failure_codes": [],
                    "components": {"minecraft_world": "offline_consistent"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
    (incomplete / "BACKUP_STATUS.json").write_text(
        '{"schema":1,"status":"incomplete"}\n', encoding="utf-8"
    )

    result, _operations, status = _run_backup(tmp_path, keep=2)

    assert result.returncode == 0
    assert status["status"] == "complete"
    assert not complete_sets[0].exists()
    assert complete_sets[1].exists()
    assert incomplete.exists()
    assert legacy.exists()


def test_single_game_backup_sets_cleanup_before_disabling_saves() -> None:
    script = (ROOT / "stack" / "bootstrap" / "backup.sh").read_text(
        encoding="utf-8"
    )

    trap_position = script.index("trap finish EXIT")
    save_off_position = script.index('rcon "save-off"')
    disabled_position = script.index("quiesced=1", save_off_position)
    flush_position = script.index('rcon "save-all flush"', disabled_position)
    assert trap_position < save_off_position < disabled_position < flush_position
    assert "refusing a live world archive" in script
    assert "trap - EXIT" in script
    assert 'restore_saves || status=1' in script
    assert 'exit "$status"' in script


def test_single_game_failed_save_on_is_attempted_once_and_returns_nonzero(
    tmp_path: Path,
) -> None:
    result, operations = _run_single_backup_state(tmp_path, "resume_failure")

    assert result.returncode != 0
    assert "single-world-archive" in operations
    assert operations.count("single-rcon:save-on") == 1


@pytest.mark.parametrize("stage", ["state_error", "state_empty", "state_garbage"])
def test_single_game_backup_refuses_unknown_server_state(
    tmp_path: Path, stage: str
) -> None:
    result, operations = _run_single_backup_state(tmp_path, stage)

    assert result.returncode != 0
    assert "single-world-archive" not in operations


def test_single_game_backup_detects_start_before_offline_archive(
    tmp_path: Path,
) -> None:
    result, operations = _run_single_backup_state(tmp_path, "start_before_archive")

    assert result.returncode != 0
    assert "single-world-archive" not in operations


def test_single_game_backup_discards_archive_if_offline_state_changes_during_copy(
    tmp_path: Path,
) -> None:
    result, operations = _run_single_backup_state(tmp_path, "start_during_archive")

    assert result.returncode != 0
    assert "single-world-archive" in operations
    assert not list(tmp_path.glob("single-backups/minecraft-*.tar.zst"))


def test_windows_pull_preserves_backup_exit_status_and_fails_after_copy() -> None:
    script = BACKUP_PULL_SCRIPT.read_text(encoding="utf-8")

    assert "set -o pipefail" in script
    assert "$backupFailed" in script
    assert "if ($backupFailed)" in script
    assert "if (-not $backupFailed)" in script
