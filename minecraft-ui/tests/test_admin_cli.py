import json

import pytest

from app.admin_cli import write_directory
from app.identity import JsonFileIdentityDirectory


def test_admin_cli_writes_only_distinct_hashes_and_identity_mapping(tmp_path) -> None:
    target = tmp_path / "minecraft-admins.json"
    write_directory(
        target,
        [("iveri", "iveri"), ("scotlandf", "scotland")],
        ["first private password", "second private password"],
    )

    document = json.loads(target.read_text(encoding="utf-8"))
    assert [user["username"] for user in document["users"]] == ["iveri", "scotlandf"]
    assert [user["upstream_alias"] for user in document["users"]] == [
        "iveri",
        "scotland",
    ]
    assert len({user["password_hash"] for user in document["users"]}) == 2
    assert "private password" not in target.read_text(encoding="utf-8")
    assert JsonFileIdentityDirectory(target).find("scotlandf").upstream_alias == "scotland"


def test_admin_cli_rejects_short_passwords_before_writing(tmp_path) -> None:
    target = tmp_path / "admins.json"

    with pytest.raises(ValueError, match="16-1024"):
        write_directory(target, [("iveri", "iveri")], ["short password"])

    assert not target.exists()
