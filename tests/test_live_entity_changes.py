from __future__ import annotations

import pytest

from tests import live_entity_changes
from tests.live_entity_changes import live_config, validate_owner
from zenmoney_mcp.hardened_database import HardenedDatabase


def _set_config(monkeypatch, path, owner="1"):
    monkeypatch.setenv("ZENMONEY_TEST_TOKEN_FILE", str(path))
    monkeypatch.setenv("ZENMONEY_TEST_USER_ID", owner)


def test_live_config_requires_external_file_and_numeric_owner(
    tmp_path, monkeypatch
):
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    token.chmod(0o600)
    _set_config(monkeypatch, token, "not-a-number")

    with pytest.raises(ValueError, match="ZENMONEY_TEST_USER_ID"):
        live_config()


@pytest.mark.parametrize("case", ["relative", "missing", "broad", "empty"])
def test_live_config_rejects_unsafe_token_files(tmp_path, monkeypatch, case):
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    token.chmod(0o600)
    if case == "relative":
        path = "relative-token"
    elif case == "missing":
        path = tmp_path / "missing"
    elif case == "broad":
        token.chmod(0o640)
        path = token
    else:
        token.write_text("", encoding="utf-8")
        path = token
    _set_config(monkeypatch, path)

    with pytest.raises(ValueError, match="ZENMONEY_TEST_TOKEN_FILE"):
        live_config()


def test_live_config_rejects_repository_file(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    token.chmod(0o600)
    _set_config(monkeypatch, token)

    with pytest.raises(ValueError, match="outside the repository"):
        live_config(repo_root=tmp_path)


def test_live_config_accepts_owner_only_external_file(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("secret\n", encoding="utf-8")
    token.chmod(0o600)
    _set_config(monkeypatch, token, "42")

    config = live_config(repo_root=tmp_path / "repository")

    assert config.owner_user_id == 42
    assert config.token == "secret"
    assert config.token_path == token.resolve()


def test_owner_validation_requires_the_one_configured_user():
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.upsert_users(
        [
            {"id": 1, "currency": 1, "changed": 1},
            {"id": 2, "currency": 1, "changed": 1},
        ]
    )

    with pytest.raises(ValueError, match="exactly one user"):
        validate_owner(db, 1)

    db.connect().execute("DELETE FROM users WHERE id=2")
    with pytest.raises(ValueError, match="does not match"):
        validate_owner(db, 2)
    assert validate_owner(db, 1) == 1


def test_main_does_not_misclassify_runtime_validation_errors(monkeypatch, capsys):
    def fail(coroutine):
        coroutine.close()
        raise ValueError("operation validation failed")

    monkeypatch.setattr(live_entity_changes.asyncio, "run", fail)

    assert live_entity_changes.main() == 1
    assert "live_gate_failed" in capsys.readouterr().out
