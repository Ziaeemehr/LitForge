from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from litforge.analysis.snapshots import Signal
from litforge.analysis.watch import install_watch, notify_signals, remove_watch


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_watch_install_explicit_only_and_quoted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert list(home.iterdir()) == []
    result = install_watch("Review $(touch forbidden) spaced", "7d", data_dir=tmp_path / "data spaced",
                           platform=platform, executable="/env spaced/bin/litforge")
    if platform == "darwin":
        path = next(home.rglob("*.plist"))
        doc = plistlib.loads(path.read_bytes())
        assert doc["StartInterval"] == 604800 and doc["ProgramArguments"][-1] == "Review $(touch forbidden) spaced"
        assert doc["ProgramArguments"][0] == "/env spaced/bin/litforge"
    elif platform == "linux":
        timer = next(home.rglob("*.timer"))
        assert "OnUnitActiveSec=604800s" in timer.read_text()
        service = next(home.rglob("*.service"))
        assert '"Review $$(touch forbidden) spaced"' in service.read_text()
    else:
        assert "schtasks" in result and "604800" not in result  # schedule uses minutes
        assert not list(home.rglob("*.plist")) and not list(home.rglob("*.timer"))
    assert not (tmp_path / "forbidden").exists()
    remove_watch("Review $(touch forbidden) spaced", platform=platform)
    assert not list(home.rglob("*.plist")) and not list(home.rglob("*.timer"))


def test_invalid_schedule_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for interval in ["0d", "-1h", "7x", "1d; touch x", "0.5d"]:
        with pytest.raises(ValueError):
            install_watch("demo", interval, data_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_notifier_only_significant_and_injected() -> None:
    calls: list[list[str]] = []
    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)
    assert not notify_signals([], platform="darwin", runner=runner)
    assert not calls
    signal = Signal(kind="answer_evidence", message='quote " and $(never)')
    assert notify_signals([signal], platform="darwin", runner=runner)
    assert calls[0][:2] == ["osascript", "-e"]
    assert '$(never)' not in calls[0][2]  # text is argv, not interpolated AppleScript
    assert notify_signals([signal], platform="linux", runner=runner)
    assert calls[1][0] == "notify-send"


def test_windows_weekly_interval_is_representable(tmp_path: Path) -> None:
    command = install_watch("demo", "7d", data_dir=tmp_path, platform="win32", executable="litforge")
    assert "/SC DAILY /MO 7" in command


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("argument", ["project", "data_dir", "config_path", "executable", "home"])
@pytest.mark.parametrize("control", ["\n", "\r", "\t", "\x00", "\x7f", "\x85"])
def test_watch_rejects_control_characters_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, argument: str, control: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    project, data_dir, config_path, executable = "demo", tmp_path / "data", tmp_path / "config", "litforge"
    bad = "bad" + control + "path"
    if argument == "project":
        project = bad
    elif argument == "data_dir":
        data_dir = tmp_path / bad
    elif argument == "config_path":
        config_path = tmp_path / bad
    elif argument == "executable":
        executable = bad
    else:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / bad))
    with pytest.raises(ValueError, match="control characters"):
        install_watch(project, data_dir=data_dir, config_path=config_path, executable=executable, platform=platform)
    assert list(tmp_path.iterdir()) == []


def test_launchd_xml_escapes_paths_and_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    install_watch("Review <a>&b", data_dir=tmp_path / "data & <x>", config_path=tmp_path / "<config>&",
                  executable="/env & <x>/litforge", platform="darwin")
    doc = plistlib.loads(next(tmp_path.rglob("*.plist")).read_bytes())
    assert doc["ProgramArguments"] == ["/env & <x>/litforge", "--data-dir", str(tmp_path / "data & <x>"),
                                       "--config", str(tmp_path / "<config>&"), "refresh", "-p", "Review <a>&b"]


@pytest.mark.parametrize("control", ["\n", "\x7f", "\x85"])
def test_watch_remove_rejects_control_characters(control: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        remove_watch("review" + control, platform="linux")
