"""Check what the one-line installer left behind (CI, after .github/workflows/install.yml ran it).

Standard library only: it runs with the runner's Python, outside the installed environment.
The library is LITFORGE_LIBRARY when set, else "Test Library" in the home folder (what the workflow passes).
Exits 1 listing every failed check.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def expected_dirs(home: Path) -> tuple[Path, Path]:
    """(program folder, desktop.json folder) of a fresh install, per platform."""
    if sys.platform == "win32":
        return (Path(os.environ["LOCALAPPDATA"]) / "LitForge" / "app", Path(os.environ["APPDATA"]) / "LitForge")
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support" / "LitForge"
        return support / "app", support
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return data / "litforge" / "app", config / "litforge"


def main() -> int:
    home = Path.home()
    library = Path(os.environ.get("LITFORGE_LIBRARY") or home / "Test Library").expanduser().resolve()
    program, config = expected_dirs(home)
    problems: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("ok    " if ok else "FAIL  ") + what)
        if not ok:
            problems.append(what)

    check((program / "pyproject.toml").is_file(), f"program installed in {program}")
    exe = program / ".venv" / ("Scripts/litforge.exe" if sys.platform == "win32" else "bin/litforge")
    check(exe.is_file(), f"command installed at {exe}")
    check((library / "data").is_dir(), f"library data folder {library / 'data'}")
    try:
        settings = json.loads((config / "desktop.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    saved = Path(str(settings.get("workspace", ""))).resolve() if settings.get("workspace") else None
    check(saved == library, f"desktop.json workspace is {library} (found {saved})")
    check(bool(settings.get("engine")), f"window engine recorded (found {settings.get('engine')!r})")

    if sys.platform == "darwin":
        launcher = home / "Applications" / "LitForge.app" / "Contents" / "MacOS" / "LitForge"
        check(launcher.is_file() and str(library) in launcher.read_text(), f"{launcher} opens the library")
    elif sys.platform == "win32":
        desktop = home / "OneDrive" / "Desktop"
        desktop = desktop if desktop.is_dir() else home / "Desktop"
        icons = [p for p in (desktop / "LitForge.lnk", desktop / "LitForge.bat") if p.is_file()]
        check(bool(icons), f"LitForge icon on {desktop}")
        bat = desktop / "LitForge.bat"
        if bat.is_file():
            check(str(library) in bat.read_text(encoding="utf-8", errors="replace"), f"{bat} opens the library")
    else:
        entry = home / ".local" / "share" / "applications" / "litforge.desktop"
        check(entry.is_file() and str(library) in entry.read_text(), f"{entry} opens the library")

    if exe.is_file():  # the CLI, run from an unrelated folder, must find the chosen library too
        run = subprocess.run([str(exe), "library", "show", "--json"], cwd=home, capture_output=True, text=True,
                             timeout=300)
        try:
            shown = Path(json.loads(run.stdout.strip().splitlines()[-1])["data"]).resolve()
        except (ValueError, KeyError, IndexError):
            shown = None
            print(run.stdout, run.stderr, sep="\n")
        check(shown == library / "data", f"`litforge library show` finds {library / 'data'} (found {shown})")

    print(f"{len(problems)} problem(s)" if problems else "installation looks right")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
