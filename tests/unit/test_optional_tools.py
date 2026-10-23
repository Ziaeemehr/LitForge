from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner


@pytest.fixture(autouse=True)
def _pix2text_not_installed(monkeypatch: Any) -> None:
    """These tests describe a machine without Pix2Text, whatever the developer's own environment holds."""
    import importlib.metadata

    real = importlib.metadata.version

    def version(name: str) -> str:
        if name == "pix2text":
            raise importlib.metadata.PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr(importlib.metadata, "version", version)


def test_pix2text_install_is_limited_to_editable_source_root_and_uv(tmp_path: Path, monkeypatch: Any) -> None:
    from litforge import tools

    root = tmp_path / "source"
    (root / "src" / "litforge").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "litforge"\n')
    module = root / "src" / "litforge" / "__init__.py"
    module.touch()
    monkeypatch.setattr(tools, "_package_file", lambda: module)
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(tools.sys, "executable", str(tmp_path / "bin" / "python"))
    assert tools.pix2text_install_status()["offerable"] is False
    fake_uv = tmp_path / "bin" / "uv"
    fake_uv.parent.mkdir()
    fake_uv.write_text("#!/bin/sh\nexit 0\n")
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(tools.shutil, "which", lambda name: str(fake_uv) if name == "uv" else None)
    assert tools.pix2text_install_status()["offerable"] is True
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".venv").symlink_to(outside, target_is_directory=True)
    status = tools.pix2text_install_status()
    assert status["offerable"] is False
    assert "outside the install root" in status["reason"]


def test_uv_next_to_python_is_accepted_when_not_on_path(tmp_path: Path, monkeypatch: Any) -> None:
    from litforge import tools

    python = tmp_path / "bin" / "python"
    uv = python.with_name("uv")
    python.parent.mkdir()
    uv.write_text("#!/bin/sh\n")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(tools.sys, "executable", str(python))
    assert tools._uv_path() == str(uv)


def test_pix2text_install_uses_argv_and_install_root(tmp_path: Path, monkeypatch: Any) -> None:
    from litforge import tools

    root = tmp_path / "source"
    (root / "src" / "litforge").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "litforge"\n')
    module = root / "src" / "litforge" / "__init__.py"
    module.touch()
    uv = tmp_path / "uv"
    record = tmp_path / "argv"
    cwd = tmp_path / "working-directory"
    target_env = tmp_path / "uv-environment"
    uv.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" > '{record}'\nprintf '%s\\n' \"$PWD\" > '{cwd}'\n"
        f"printf '%s\\n' \"$UV_PROJECT_ENVIRONMENT|$UV_CACHE_DIR\" > '{target_env}'\n"
        "mkdir -p .venv\necho installing\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(tools, "_package_file", lambda: module)
    monkeypatch.setattr(tools.shutil, "which", lambda name: str(uv) if name == "uv" else None)
    monkeypatch.setattr(tools, "_has_nvidia_gpu", lambda path: True)
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "outside"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "outside-cache"))
    messages: list[str] = []
    tools.install_pix2text(messages.append, timeout_s=4)
    assert record.read_text().strip() == "sync --extra formulas-gpu --inexact"
    assert cwd.read_text().strip() == str(root)
    assert target_env.read_text().strip() == f"{root}/.venv|{root}/.litforge-uv-cache"
    assert messages
    assert (root / ".venv").is_dir()
    assert all((root / name).exists() for name in ("pyproject.toml", ".venv"))


def _fake_source(tmp_path: Path, monkeypatch: Any, script: str) -> Any:
    from litforge import tools

    root = tmp_path / "source"
    (root / "src" / "litforge").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "litforge"\n')
    module = root / "src" / "litforge" / "__init__.py"
    module.touch()
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n" + script)
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(tools, "_package_file", lambda: module)
    monkeypatch.setattr(tools.shutil, "which", lambda name: str(uv) if name == "uv" else None)
    monkeypatch.setattr(tools, "_has_nvidia_gpu", lambda path: False)
    return tools


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_interrupted_pix2text_install_stops_uv(tmp_path: Path, monkeypatch: Any) -> None:
    pid_file = tmp_path / "pid"
    tools = _fake_source(tmp_path, monkeypatch, f"echo $$ > '{pid_file}'\necho started\nexec sleep 30\n")

    def interrupt(message: str) -> None:
        if message == "started":
            raise KeyboardInterrupt  # Ctrl-C while uv is running

    with pytest.raises(KeyboardInterrupt):
        tools.install_pix2text(interrupt, timeout_s=20)
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_pix2text_progress_bounds_a_huge_line_without_newline(tmp_path: Path, monkeypatch: Any) -> None:
    tools = _fake_source(tmp_path, monkeypatch,
                         "head -c 3000000 /dev/zero | tr '\\0' x\nprintf '\\ndone\\n'\nmkdir -p .venv\n")
    messages: list[str] = []
    tools.install_pix2text(messages.append, timeout_s=20)
    assert "done" in messages
    assert max(map(len, messages)) <= tools._MAX_PROGRESS_LINE


def test_tools_cli_installs_pix2text_with_confirmation_and_mineru_alias(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import litforge.cli as cli
    from litforge.cli import app

    calls: list[str] = []
    monkeypatch.setattr(cli, "pix2text_install_status", lambda: {"offerable": True, "extra": "formulas",
                                                                  "command": "uv sync --extra formulas --inexact",
                                                                  "size": "~2 GB"})
    monkeypatch.setattr(cli, "install_pix2text", lambda progress, timeout_s: (
        calls.append("pix2text") or {"installed": True, "version": "1.0"}))
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "tools", "install", "pix2text"], input="y\n")
    assert result.exit_code == 0, result.output
    assert calls == ["pix2text"]
    confirmed = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "tools", "install", "pix2text", "--yes"])
    assert confirmed.exit_code == 0, confirmed.output
    assert calls == ["pix2text", "pix2text"]

    mineru: list[bool] = []
    monkeypatch.setattr("litforge.parsing.mineru_parser.install_mineru", lambda *args, **kwargs: mineru.append(True))
    alias = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "tools", "install", "mineru", "--yes"])
    assert alias.exit_code == 0, alias.output
    assert mineru == [True]


def test_tools_cli_prints_status_for_all_tools(tmp_path: Path, monkeypatch: Any) -> None:
    import litforge.ui.server as server_module
    from litforge.cli import app

    class FakeApp:
        def __init__(self, *args: Any) -> None:
            pass

        def optional_tools_status(self) -> dict[str, Any]:
            return {
                "mineru": {"installed": False},
                "pix2text": {"installed": False, "available": False, "offerable": False,
                             "command": "uv sync --extra formulas --inexact", "size": "~2 GB"},
                "vision": {"model": "vision-profile", "notice": "local"},
            }

    monkeypatch.setattr(server_module, "App", FakeApp)
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "tools"])
    assert result.exit_code == 0, result.output
    assert "MinerU" in result.output
    assert "Pix2Text" in result.output
    assert "Image model" in result.output and "Nemotron" in result.output
    assert "Not available on this install" in result.output
    assert "uv sync --extra formulas --inexact" in result.output


def test_installer_skips_prompts_without_tty_and_prints_hint(tmp_path: Path) -> None:
    result, log = _run_installer(tmp_path, {})
    assert result.returncode == 0, result.stderr
    assert "Install MinerU" not in result.stdout
    assert "Optional tools" in result.stdout
    # Tectonic installs without a prompt
    assert log == ["sync --extra embed --extra documents --extra desktop --inexact", "run litforge desktop-setup",
                   "run litforge shortcut", "run litforge tools install tectonic --yes"]


def test_installer_extras_and_failed_optional_install_are_nonfatal(tmp_path: Path) -> None:
    result, log = _run_installer(tmp_path, {"LITFORGE_EXTRAS": "mineru,pix2text", "FAIL_OPTIONAL": "pix2text"})
    assert result.returncode == 0, result.stderr
    assert log == ["sync --extra embed --extra documents --extra desktop --inexact", "run litforge desktop-setup",
                   "run litforge shortcut", "run litforge tools install tectonic --yes",
                   "run litforge tools install mineru --yes", "run litforge tools install pix2text --yes"]
    assert "warning" in result.stderr.lower()
    assert "litforge tools install pix2text" in result.stderr


def test_installer_continues_with_a_browser_icon_when_the_window_cannot_be_set_up(tmp_path: Path) -> None:
    result, log = _run_installer(tmp_path, {"FAIL_SETUP": "1"})
    assert result.returncode == 0, result.stderr
    assert log[1:3] == ["run litforge desktop-setup", "run litforge shortcut"]
    assert "uv run litforge desktop-setup" in result.stderr  # how to retry


def test_installer_prompts_read_from_configured_tty(tmp_path: Path) -> None:
    tty = tmp_path / "tty"
    tty.write_text("y\nn\n")
    result, log = _run_installer(tmp_path, {"LITFORGE_TTY": str(tty)})
    assert result.returncode == 0, result.stderr
    asked = tty.read_text()  # questions go to the terminal, not to stdout
    assert "Install MinerU for accurate PDF reading (~4 GB)? [y/N]" in asked
    assert "Install Pix2Text for better math in exports (~2 GB)? [y/N]" in asked
    assert log[-1] == "run litforge tools install mineru --yes"
    assert len(log) == 5  # sync, window setup, shortcut, Tectonic, MinerU


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is not installed")
def test_powershell_installer_optional_tools_are_nonfatal(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "litforge"\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "commands"
    if os.name == "nt":
        git = bindir / "git.cmd"
        uv = bindir / "uv.cmd"
        git.write_text("@echo off\nexit /b 0\n")
        uv.write_text(
            "@echo off\n"
            "echo %*>>\"%LITFORGE_TEST_LOG%\"\n"
            "if \"%*\"==\"run litforge tools install pix2text --yes\" exit /b 1\n"
            "exit /b 0\n"
        )
    else:
        git = bindir / "git"
        uv = bindir / "uv"
        git.write_text("#!/bin/sh\nexit 0\n")
        uv.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> '{record}'\n"
            "case \"$*\" in *'install pix2text'*) exit 1;; esac\n"
            "exit 0\n"
        )
        git.chmod(git.stat().st_mode | stat.S_IXUSR)
        uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
               LITFORGE_DIR=str(repo), LITFORGE_EXTRAS="mineru,pix2text", LITFORGE_TEST_LOG=str(record))
    script = Path(__file__).resolve().parents[2] / "install.ps1"
    result = subprocess.run(["pwsh", "-NoProfile", "-File", str(script)], cwd=repo, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    commands = record.read_text().splitlines()
    assert commands[-2:] == ["run litforge tools install mineru --yes",
                             "run litforge tools install pix2text --yes"]
    assert "Optional pix2text installation failed" in result.stderr
    assert "Optional tools" in result.stdout


def _run_installer(tmp_path: Path, overrides: dict[str, str]) -> tuple[Any, list[str]]:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "litforge"\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "commands"
    git = bindir / "git"
    git.write_text("#!/bin/sh\nexit 0\n")
    uv = bindir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{record}'\n"
        "case \"$*\" in *desktop-setup*) [ \"${FAIL_SETUP:-}\" != 1 ] || exit 1;; esac\n"
        "case \"$*\" in *\"tools install \"*)\n"
        "  case \"$*\" in *\"install mineru\"*) tool=mineru;; *) tool=pix2text;; esac\n"
        "  [ \"${FAIL_OPTIONAL:-}\" != \"$tool\" ] || exit 1;;\n"
        "esac\nexit 0\n"
    )
    for executable in (git, uv):
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", LITFORGE_DIR=str(repo),
               LITFORGE_TEST_LOG=str(record), **overrides)
    script = Path(__file__).resolve().parents[2] / "install.sh"
    result = subprocess.run(["sh", str(script)], cwd=repo, env=env, capture_output=True, text=True, timeout=20)
    return result, record.read_text().splitlines() if record.exists() else []
