"""Explicit schedule file installation and optional OS notifications."""

from __future__ import annotations

import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path

from litforge.analysis.runs import input_hash
from litforge.analysis.snapshots import Signal

Runner = Callable[[list[str]], object]


def _seconds(every: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([smhdw])", every)
    if not match:
        raise ValueError("interval must be a positive integer followed by s, m, h, d or w")
    seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
    if seconds > 2**31 - 1:
        raise ValueError("interval is too large")
    return seconds


def _validate_argument(value: str, label: str) -> None:
    if any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError(f"{label} must not contain control characters")


def _name(project: str) -> str:
    _validate_argument(project, "project name")
    if not project.strip():
        raise ValueError("invalid project name")
    return "org.litforge.watch-" + input_hash(project)[:16]


def _systemd_arg(value: str) -> str:
    # ExecStart is not a shell, but systemd expands percent specifiers and dollar variables.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def install_watch(project: str, every: str = "7d", *, data_dir: Path, config_path: Path | None = None,
                  platform: str | None = None, executable: str | None = None) -> str:
    seconds, name = _seconds(every), _name(project)
    platform = platform or sys.platform
    executable = executable or shutil.which("litforge") or str(Path(sys.executable).with_name("litforge"))
    _validate_argument(executable, "executable path")
    _validate_argument(str(data_dir), "data directory")
    if config_path is not None:
        _validate_argument(str(config_path), "config path")
    command = [executable, "--data-dir", str(data_dir.resolve())]
    if config_path:
        command += ["--config", str(config_path.resolve())]
    for argument in command:
        _validate_argument(argument, "resolved command path")
    command += ["refresh", "-p", project]
    home = Path.home()
    _validate_argument(str(home), "home directory")
    if platform == "darwin":
        target = home / "Library" / "LaunchAgents" / f"{name}.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plistlib.dumps({"Label": name, "ProgramArguments": command, "StartInterval": seconds,
                                          "WorkingDirectory": str(data_dir.resolve())}))
        return f"Wrote {target}\nActivate explicitly: launchctl load {shlex.quote(str(target))}"
    if platform.startswith("linux"):
        folder = home / ".config" / "systemd" / "user"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.service").write_text(
            "[Unit]\nDescription=LitForge living review refresh\n[Service]\nType=oneshot\nExecStart=" +
            " ".join(_systemd_arg(c) for c in command) + "\n", encoding="utf-8")
        target = folder / f"{name}.timer"
        target.write_text(f"[Unit]\nDescription=LitForge living review schedule\n[Timer]\nOnBootSec={seconds}s\n"
                          f"OnUnitActiveSec={seconds}s\nUnit={name}.service\n[Install]\nWantedBy=timers.target\n",
                          encoding="utf-8")
        return f"Wrote {target}\nActivate explicitly: systemctl --user daemon-reload && " \
               f"systemctl --user enable --now {name}.timer"
    if platform == "win32":
        if seconds % 86400 == 0 and seconds // 86400 <= 365:
            frequency, multiple = "DAILY", seconds // 86400
        elif seconds % 3600 == 0 and seconds // 3600 <= 23:
            frequency, multiple = "HOURLY", seconds // 3600
        elif seconds % 60 == 0 and seconds // 60 <= 1439:
            frequency, multiple = "MINUTE", seconds // 60
        else:
            raise ValueError("Windows requires an interval expressible as whole minutes, hours or days (up to 365d)")
        return subprocess.list2cmdline(["schtasks", "/Create", "/TN", name, "/SC", frequency, "/MO",
                                       str(multiple), "/TR", subprocess.list2cmdline(command)])
    raise ValueError("schedule platform is not supported")


def remove_watch(project: str, *, platform: str | None = None) -> str:
    name, platform = _name(project), platform or sys.platform
    home = Path.home()
    _validate_argument(str(home), "home directory")
    if platform == "darwin":
        target = home / "Library" / "LaunchAgents" / f"{name}.plist"
        target.unlink(missing_ok=True)
        return f"Removed {target}\nIf loaded, unload explicitly: launchctl remove {name}"
    if platform.startswith("linux"):
        folder = home / ".config" / "systemd" / "user"
        for suffix in ("timer", "service"):
            (folder / f"{name}.{suffix}").unlink(missing_ok=True)
        return f"Removed schedule files\nIf enabled, disable explicitly: systemctl --user disable --now {name}.timer"
    if platform == "win32":
        return subprocess.list2cmdline(["schtasks", "/Delete", "/TN", name, "/F"])
    raise ValueError("schedule platform is not supported")


def _run(args: list[str]) -> object:
    return subprocess.run(args, check=True, capture_output=True, text=True)


def notify_signals(signals: list[Signal], *, platform: str | None = None, runner: Runner = _run) -> bool:
    if not signals:
        return False
    message = "\n".join(s.message for s in signals)
    platform = platform or sys.platform
    if platform == "darwin":
        # Text is passed as an argv value, never concatenated into AppleScript source.
        runner(["osascript", "-e", 'on run argv\ndisplay notification (item 1 of argv) with title "LitForge"\nend run',
                message])
        return True
    if platform.startswith("linux"):
        runner(["notify-send", "--", "LitForge", message])
        return True
    return False
