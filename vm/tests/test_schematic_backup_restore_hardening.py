from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from typing import Mapping


ROOT = Path(__file__).parents[2]
BACKUP_SCRIPT = ROOT / "vm" / "backup-all.sh"
RESTORE_SCRIPT = ROOT / "vm" / "restore-schematic-library.sh"
VIEWER_IMAGE = (
    "ghcr.io/scotsgamez/create-schematic-viewer:v1.0.0@"
    "sha256:d8dcef565e7da6c7536b591cc9cbe0471637364ffc22ae40590cd2c0910484a3"
)


def _write_archive(path: Path) -> None:
    payload = path.parent / "payload"
    payload.mkdir()
    (payload / "backup-manifest.json").write_text(
        '{"format":"create-schematic-viewer-library-backup","version":1}\n',
        encoding="utf-8",
    )
    with tarfile.open(path, "w:gz") as archive:
        archive.add(payload / "backup-manifest.json", arcname="backup-manifest.json")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(f"{path.name}.sha256").write_text(
        f"{digest}  {path.name}\n", encoding="ascii", newline="\n"
    )


def _bash_path(path: Path) -> str:
    resolved = path.resolve().as_posix()
    if os.name == "nt":
        return f"/{resolved[0].lower()}{resolved[2:]}"
    return resolved


def _run_restore(
    archive: Path,
    backup_root: Path,
    *,
    fake_bin: Path | None = None,
    extra_environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    archive_argument = str(archive)
    script_argument = str(RESTORE_SCRIPT)
    backup_root_argument = str(backup_root)
    bash = shutil.which("bash") or "bash"
    if os.name == "nt":
        git_bash = Path(os.environ["ProgramFiles"]) / "Git" / "bin" / "bash.exe"
        if git_bash.exists():
            bash = str(git_bash)
            archive_argument = _bash_path(archive)
            script_argument = _bash_path(RESTORE_SCRIPT)
            backup_root_argument = _bash_path(backup_root)
    environment["LANTERN_BACKUP_DIR"] = backup_root_argument
    environment["LANTERN_STACK"] = backup_root_argument
    if extra_environment:
        environment.update(extra_environment)
    command = [bash, script_argument, archive_argument, "--confirm-replace"]
    if fake_bin is not None:
        command = [
            bash,
            "-c",
            'PATH="$1:$PATH"; export PATH; shift; exec "$@"',
            "restore-harness",
            _bash_path(fake_bin),
            script_argument,
            archive_argument,
            "--confirm-replace",
        ]
    return subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _fake_runtime(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    fake_bin = tmp_path / "fake-bin"
    state = tmp_path / "fake-state"
    fake_bin.mkdir()
    state.mkdir()
    log = state / "operations.log"

    _write_executable(
        fake_bin / "docker",
        r'''#!/usr/bin/env bash
set -u
printf 'docker %s\n' "$*" >> "$FAKE_DOCKER_LOG"

expect_user=false
user_value=
safety_host=
backup_host=
for arg in "$@"; do
  if [ "$expect_user" = true ]; then
    user_value=$arg
    expect_user=false
    continue
  fi
  [ "$arg" = "--user" ] && expect_user=true
  case "$arg" in
    *:/safety) safety_host=${arg%:/safety} ;;
    *:/backup) backup_host=${arg%:/backup} ;;
  esac
done

joined=" $* "
case "$joined" in
  *" volume inspect "*) exit 0 ;;
  *" volume create "*) printf '%s\n' "${@: -1}"; exit 0 ;;
  *" volume rm "*) exit 0 ;;
  *" compose port minecraft-ui 8093 "*) printf '192.168.0.115:8093\n'; exit 0 ;;
  *" compose ps -q schematic-viewer "*) printf 'viewer-container\n'; exit 0 ;;
  *" inspect -f "*) printf 'true\n'; exit 0 ;;
  *" compose stop schematic-viewer "*)
    printf 'stop\n' >> "$FAKE_OPERATION_LOG"
    [ "${FAKE_STOP_FAIL:-0}" != 1 ]
    exit
    ;;
  *" compose start schematic-viewer "*)
    printf 'start\n' >> "$FAKE_OPERATION_LOG"
    exit 0
    ;;
  *" --entrypoint sh "*) printf '1000:1000\n'; exit 0 ;;
  *" chown 1000:1000 /stage "*)
    [ "$user_value" = "0:0" ] || exit 94
    case "$joined" in *" --cap-add CHOWN "*) ;; *) exit 95 ;; esac
    printf 'staging-init\n' >> "$FAKE_OPERATION_LOG"
    exit 0
    ;;
  *"mkdir /stage/backup"*)
    [ "$user_value" = "1000:1000" ] || exit 96
    case "$joined" in *"chown"*) exit 97 ;; esac
    printf 'candidate-extract\n' >> "$FAKE_OPERATION_LOG"
    exit 0
    ;;
  *" backup /safety/backup "*)
    [ "$user_value" = "1000:1000" ] || exit 98
    mkdir -p "$safety_host/backup"
    printf '%s\n' '{"format":"create-schematic-viewer-library-backup","version":1}' > "$safety_host/backup/backup-manifest.json"
    printf 'original\n' > "$safety_host/backup/library.json"
    printf 'safety-backup\n' >> "$FAKE_OPERATION_LOG"
    exit 0
    ;;
  *" backup /backup/library "*)
    [ "$user_value" = "1000:1000" ] || exit 99
    mkdir -p "$backup_host/library"
    printf '%s\n' '{"format":"create-schematic-viewer-library-backup","version":1}' > "$backup_host/library/backup-manifest.json"
    printf 'backup-stage-write\n' >> "$FAKE_OPERATION_LOG"
    exit 0
    ;;
  *" restore /safety/backup "*)
    mkdir -p "$safety_host/restored"
    printf 'original\n' > "$safety_host/restored/library.json"
    printf 'safety-validate\n' >> "$FAKE_OPERATION_LOG"
    exit 0
    ;;
  *" restore /stage/backup "*)
    printf 'candidate-validate\n' >> "$FAKE_OPERATION_LOG"
    exit 0
    ;;
  *" find /live "*)
    [ "$user_value" = "1000:1000" ] || exit 91
    printf 'clear\n' >> "$FAKE_OPERATION_LOG"
    exit 0
    ;;
  *"cp -a /stage/restored/. /live/"*)
    [ "$user_value" = "1000:1000" ] || exit 92
    printf 'install-copy\n' >> "$FAKE_OPERATION_LOG"
    [ "${FAKE_INSTALL_COPY_FAIL:-0}" != 1 ]
    exit
    ;;
  *"cp -a /safety/. /live/"*)
    [ "$user_value" = "1000:1000" ] || exit 93
    printf 'rollback-copy\n' >> "$FAKE_OPERATION_LOG"
    [ "${FAKE_ROLLBACK_COPY_FAIL:-0}" != 1 ]
    exit
    ;;
esac
exit 0
''',
    )
    _write_executable(
        fake_bin / "curl",
        r'''#!/usr/bin/env bash
set -u
count_file="$FAKE_STATE_DIR/curl-count"
count=0
[ ! -f "$count_file" ] || read -r count < "$count_file"
count=$((count + 1))
printf '%s\n' "$count" > "$count_file"
printf 'readiness-%s\n' "$count" >> "$FAKE_OPERATION_LOG"
fail_calls=${FAKE_CURL_FAIL_CALLS:-0}
if [ "$fail_calls" = all ] || [ "$count" -le "$fail_calls" ]; then
  exit 22
fi
exit 0
''',
    )
    _write_executable(
        fake_bin / "sleep",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        fake_bin / "chown",
        r'''#!/usr/bin/env bash
set -u
[ "$1" = "1000:1000" ] || exit 89
printf 'safety-stage-owner\n' >> "$FAKE_OPERATION_LOG"
exit 0
''',
    )
    _write_executable(
        fake_bin / "sudo",
        r'''#!/usr/bin/env bash
set -u
case "$1" in
  chown)
    case "$*" in
      *"1000:1000"*".schematic-viewer-backup."*)
        printf 'backup-stage-owner\n' >> "$FAKE_OPERATION_LOG"
        ;;
    esac
    exit 0
    ;;
  mkdir|tar|test|rm) exec "$@" ;;
  *) exec "$@" ;;
esac
''',
    )
    environment = {
        "FAKE_DOCKER_LOG": _bash_path(log),
        "FAKE_OPERATION_LOG": _bash_path(log),
        "FAKE_STATE_DIR": _bash_path(state),
    }
    return fake_bin, log, environment


def _run_state_machine(
    tmp_path: Path, **injections: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    approved = tmp_path / "approved"
    approved.mkdir()
    archive = approved / "schematic-viewer-data.tgz"
    _write_archive(archive)
    fake_bin, log, environment = _fake_runtime(tmp_path)
    environment.update(injections)
    result = _run_restore(
        archive,
        approved,
        fake_bin=fake_bin,
        extra_environment=environment,
    )
    assert log.exists(), f"fake runtime was not invoked: {result.stderr}"
    operations = log.read_text(encoding="utf-8").splitlines()
    return result, operations


def _positions(operations: list[str], *names: str) -> list[int]:
    return [operations.index(name) for name in names]


def _run_backup_state_machine(
    tmp_path: Path,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    stack = tmp_path / "stack"
    destination = tmp_path / "backups"
    stack.mkdir()
    destination.mkdir()
    (stack / ".env").write_text("DB_ROOT_PASSWORD=disposable\n", encoding="utf-8")
    fake_bin, log, environment = _fake_runtime(tmp_path)
    environment.update(
        {
            "LANTERN_STACK": _bash_path(stack),
            "LANTERN_BACKUP_DIR": _bash_path(destination),
            "LANTERN_BACKUP_KEEP": "7",
        }
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
            "backup-harness",
            _bash_path(fake_bin),
            _bash_path(BACKUP_SCRIPT),
        ],
        cwd=ROOT,
        env={**os.environ, **environment},
        text=True,
        capture_output=True,
        check=False,
    )
    assert log.exists(), f"fake runtime was not invoked: {result.stderr}"
    return result, log.read_text(encoding="utf-8").splitlines()


def test_restore_rejects_archive_outside_approved_backup_root(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    archive = outside / "schematic-viewer-data.tgz"
    _write_archive(archive)

    result = _run_restore(archive, approved)

    assert result.returncode != 0
    assert "approved backup root" in result.stderr


def test_scripts_use_typed_viewer_backup_and_sha256_companion() -> None:
    backup = BACKUP_SCRIPT.read_text(encoding="utf-8")
    restore = RESTORE_SCRIPT.read_text(encoding="utf-8")

    assert VIEWER_IMAGE in backup
    assert VIEWER_IMAGE in restore
    assert "node tools/library_data.js backup" in backup
    assert "node tools/library_data.js backup" in restore
    assert "node tools/library_data.js restore" in restore
    assert "sha256sum" in backup
    assert "sha256sum" in restore
    assert ".sha256" in backup
    assert ".sha256" in restore


def test_restore_state_machine_validates_then_installs_as_viewer_user(tmp_path: Path) -> None:
    result, operations = _run_state_machine(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _positions(
        operations,
        "staging-init",
        "candidate-extract",
        "candidate-validate",
        "stop",
        "safety-stage-owner",
        "safety-backup",
        "safety-validate",
        "clear",
        "install-copy",
        "start",
        "readiness-1",
    ) == sorted(
        _positions(
            operations,
            "staging-init",
            "candidate-extract",
            "candidate-validate",
            "stop",
            "safety-stage-owner",
            "safety-backup",
            "safety-validate",
            "clear",
            "install-copy",
            "start",
            "readiness-1",
        )
    )


def test_install_copy_failure_clears_then_rolls_back_before_restart(tmp_path: Path) -> None:
    result, operations = _run_state_machine(tmp_path, FAKE_INSTALL_COPY_FAIL="1")

    assert result.returncode != 0
    install = operations.index("install-copy")
    rollback_clear = operations.index("clear", operations.index("clear") + 1)
    rollback_copy = operations.index("rollback-copy")
    restart = operations.index("start")
    assert install < rollback_clear < rollback_copy < restart


def test_rollback_copy_failure_never_restarts_viewer(tmp_path: Path) -> None:
    result, operations = _run_state_machine(
        tmp_path,
        FAKE_INSTALL_COPY_FAIL="1",
        FAKE_ROLLBACK_COPY_FAIL="1",
    )

    assert result.returncode != 0
    assert operations.count("rollback-copy") >= 1
    assert "start" not in operations


def test_candidate_readiness_failure_rolls_back_before_serving_original(
    tmp_path: Path,
) -> None:
    result, operations = _run_state_machine(tmp_path, FAKE_CURL_FAIL_CALLS="30")

    assert result.returncode != 0
    first_start = operations.index("start")
    post_failure_stop = operations.index("stop", operations.index("stop") + 1)
    rollback_clear = operations.index("clear", operations.index("clear") + 1)
    rollback_copy = operations.index("rollback-copy")
    second_start = operations.index("start", first_start + 1)
    assert first_start < post_failure_stop < rollback_clear < rollback_copy < second_start
    assert "readiness-31" in operations


def test_exit_recovery_stops_and_suppresses_unready_restart(tmp_path: Path) -> None:
    result, operations = _run_state_machine(
        tmp_path,
        FAKE_STOP_FAIL="1",
        FAKE_CURL_FAIL_CALLS="all",
    )

    assert result.returncode != 0
    assert operations.count("start") == 1
    assert operations.count("stop") >= 2
    assert "readiness-30" in operations


def test_readiness_uses_the_actual_compose_published_address() -> None:
    restore = RESTORE_SCRIPT.read_text(encoding="utf-8")

    assert "docker compose port minecraft-ui 8093" in restore
    assert '"$READINESS_URL"' in restore
    assert "http://127.0.0.1:8093/readyz" not in restore


def test_restore_rejects_corrupt_archive_before_any_docker_operation(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    archive = approved / "schematic-viewer-data.tgz"
    _write_archive(archive)
    archive.write_bytes(archive.read_bytes() + b"corrupt")

    result = _run_restore(archive, approved)

    assert result.returncode != 0
    assert "failed SHA-256 verification" in result.stderr


def test_backup_cleans_all_temporary_paths_and_recovers_ambiguous_stop() -> None:
    backup = BACKUP_SCRIPT.read_text(encoding="utf-8")

    for variable in (
        "SCHEMATIC_BACKUP_TMP",
        "SCHEMATIC_ARCHIVE_TMP",
        "SCHEMATIC_CHECKSUM_TMP",
    ):
        assert f'[ -n "${variable}" ]' in backup
    stop_failure = backup.index(
        "schematic-viewer could not be stopped; refusing a live volume copy"
    )
    preceding_branch = backup[max(0, stop_failure - 500) : stop_failure]
    assert "SCHEMATIC_VIEWER_STATE_KNOWN=false" in preceding_branch
    assert "SCHEMATIC_VIEWER_STOPPED" not in backup


def test_backup_uses_released_viewer_identity_and_writable_staging(
    tmp_path: Path,
) -> None:
    _result, operations = _run_backup_state_machine(tmp_path)

    stage_owner = operations.index("backup-stage-owner")
    stage_write = operations.index("backup-stage-write")
    assert stage_owner < stage_write
