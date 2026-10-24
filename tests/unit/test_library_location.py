"""Library choices never move data and use isolated user settings."""
import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from litforge.cli import app
from litforge.config import resolve_paths
from litforge.desktop.workspace import save_settings, user_config_dir


@pytest.fixture
def location(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("LITFORGE_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path, user_config_dir(sys.platform, os.environ, tmp_path)


def test_library_set_show_and_preserve_settings(location):
    home, config = location
    save_settings(config, {"port": 1234})
    result = CliRunner().invoke(app, ["library", "set", "~/My Library"])
    assert result.exit_code == 0, result.output
    workspace = home / "My Library"
    assert (workspace / "data").is_dir()
    settings = json.loads((config / "desktop.json").read_text())
    assert settings == {"port": 1234, "workspace": str(workspace)}
    result = CliRunner().invoke(app, ["library", "show", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"workspace": str(workspace), "data": str(workspace / "data")}


def test_library_set_rejects_file_without_changing_settings(location):
    home, config = location
    save_settings(config, {"workspace": str(home)})
    blocked = home / "file"
    blocked.write_text("keep")
    result = CliRunner().invoke(app, ["library", "set", str(blocked)])
    assert result.exit_code != 0
    assert json.loads((config / "desktop.json").read_text())["workspace"] == str(home)


@pytest.mark.parametrize("branch", ["explicit", "env", "checkout", "saved", "fallback"])
def test_library_paths_precedence(location, monkeypatch, branch):
    home, config = location
    saved = home / "saved"
    if branch != "fallback":
        (saved / "data").mkdir(parents=True)
        save_settings(config, {"workspace": str(saved)})
    (home / "data").mkdir()
    expected = saved / "data" if branch == "saved" else home / "data"
    if branch in ("explicit", "env", "checkout"):
        (home / ".git").mkdir()
        (home / "pyproject.toml").write_text('[project]\nname = "litforge"\n')
    if branch in ("explicit", "env"):
        monkeypatch.setenv("LITFORGE_HOME", str(home / "env"))
        expected = home / "env"
    explicit = home / "explicit" if branch == "explicit" else None
    assert resolve_paths(explicit).root == (explicit or expected)


@pytest.mark.parametrize("override", [False, True])
def test_library_shortcut_uses_saved_workspace(location, monkeypatch, override):
    home, config = location
    monkeypatch.setattr(sys, "platform", "linux")
    # use the Linux config path even when this test runs on macOS
    config = user_config_dir("linux", os.environ, home)
    saved = home / "saved"
    save_settings(config, {"workspace": str(saved)})
    args = ["shortcut", "--mode", "desktop"]
    if override:
        args += ["--workspace", str(home / "override")]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    text = (home / ".local/share/applications/litforge.desktop").read_text()
    assert str(home / "override" if override else saved) in text


def test_library_paths_ignore_missing_saved_data(location):
    home, config = location
    save_settings(config, {"workspace": str(home / "missing")})
    assert resolve_paths().root == home / "data"
    assert not (home / "missing").exists()


def test_library_paths_ignore_other_checkout(location):
    home, config = location
    (home / ".git").mkdir()
    (home / "data").mkdir()
    (home / "pyproject.toml").write_text('[project]\nname = "other"\n')
    saved = home / "saved"
    (saved / "data").mkdir(parents=True)
    save_settings(config, {"workspace": str(saved)})
    assert resolve_paths().root == saved / "data"


def test_library_set_write_failure_keeps_old_choice(location, monkeypatch):
    home, config = location
    save_settings(config, {"workspace": str(home / "old")})

    def denied(*args, **kwargs):
        raise PermissionError("write denied")

    monkeypatch.setattr("litforge.desktop.workspace.tempfile.NamedTemporaryFile", denied)
    result = CliRunner().invoke(app, ["library", "set", str(home / "unwritable")])
    assert result.exit_code == 1
    assert "cannot write" in result.output
    assert json.loads((config / "desktop.json").read_text())["workspace"] == str(home / "old")
