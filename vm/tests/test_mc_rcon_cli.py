from __future__ import annotations

import importlib.util
import sys
from io import StringIO
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
RCON_SCRIPT = ROOT / "stack" / "bootstrap" / "mc-rcon.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("lantern_mc_rcon", RCON_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_password_stdin_mode_keeps_secret_out_of_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    captured: dict[str, object] = {}

    def fake_execute(host: str, port: int, password: str, command: str) -> str:
        captured.update(
            host=host,
            port=port,
            password=password,
            command=command,
        )
        return "ok"

    monkeypatch.setattr(module, "execute", fake_execute)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mc-rcon.py", "--password-stdin", "127.0.0.1", "25575", "save-all", "flush"],
    )
    monkeypatch.setattr(sys, "stdin", StringIO("sentinel-secret"))

    assert module.main() == 0
    assert "sentinel-secret" not in sys.argv
    assert captured == {
        "host": "127.0.0.1",
        "port": 25575,
        "password": "sentinel-secret",
        "command": "save-all flush",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["mc-rcon.py", "--password-stdin", "127.0.0.1", "invalid", "list"],
        ["mc-rcon.py", "--password-stdin", "127.0.0.1", "70000", "list"],
    ],
)
def test_password_stdin_mode_rejects_invalid_ports(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    module = _load_module()
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdin", StringIO("sentinel-secret"))

    assert module.main() == 2


def test_legacy_positional_mode_remains_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    captured: list[tuple[str, int, str, str]] = []
    monkeypatch.setattr(
        module,
        "execute",
        lambda host, port, password, command: captured.append(
            (host, port, password, command)
        )
        or "ok",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["mc-rcon.py", "127.0.0.1", "25575", "legacy-secret", "list"],
    )

    assert module.main() == 0
    assert captured == [("127.0.0.1", 25575, "legacy-secret", "list")]
