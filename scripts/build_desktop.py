"""Stage the platform payload of a native LitForge installer (Task 7 of the desktop plan).

    python scripts/build_desktop.py --target macos-arm64 --output dist/desktop

writes OUTPUT/TARGET/ with:
  app/             the read-only source-project layout (pyproject.toml, uv.lock, src/, configs/, ...) and manifest.json
  bin/uv[.exe]     the uv that provisions the runtime and later adds optional tools
  provision.py     creates the writable per-user runtime from app/ (see packaging/desktop/provision.py)

No .venv is copied: the runtime is built on the user's machine. Run it on the matching OS and architecture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {"windows-x64": ("win32", "x86_64"), "macos-arm64": ("darwin", "arm64"),
           "macos-x64": ("darwin", "x86_64"), "linux-x64": ("linux", "x86_64")}
APP_FILES = ["pyproject.toml", "uv.lock", "README.md", "LICENSE"]
APP_DIRS = ["src", "configs"]
RENDERER = {"windows-x64": "Microsoft Edge WebView2 (Windows 11; updated Windows 10)",
            "macos-arm64": "WKWebView (system WebKit)", "macos-x64": "WKWebView (system WebKit)",
            "linux-x64": "WebKitGTK 4.1 (gir1.2-webkit2-4.1), else Qt WebEngine"}


def host_target() -> str | None:
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
    os_name = "linux" if sys.platform.startswith("linux") else sys.platform
    return next((t for t, (o, m) in TARGETS.items() if o == os_name and m == machine), None)


def tracked_files(directory: str) -> list[Path]:
    """Only what git tracks: no caches, local data or .venv slips into an installer."""
    out = subprocess.run(["git", "ls-files", "-z", "--", directory], cwd=ROOT, capture_output=True, check=True)
    return [Path(p) for p in out.stdout.decode().split("\0") if p]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stage(target: str, output: Path, uv: Path) -> Path:
    out = output / target
    if out.exists():
        shutil.rmtree(out)
    app = out / "app"
    for rel in [Path(f) for f in APP_FILES] + [f for d in APP_DIRS for f in tracked_files(d)]:
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, app / rel)
    (out / "bin").mkdir(parents=True)
    shutil.copy2(uv, out / "bin" / uv.name)
    shutil.copy2(ROOT / "packaging" / "desktop" / "provision.py", out / "provision.py")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    uv_version = subprocess.run([str(uv), "--version"], capture_output=True, text=True, check=True).stdout.split()[1]
    files = {p.relative_to(out).as_posix(): sha256(p) for p in sorted(out.rglob("*")) if p.is_file()}
    manifest = {"app_version": project["version"], "target": target, "python": project["requires-python"],
                "uv": uv_version, "renderer": RENDERER[target], "sha256": files}
    (app / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage a native LitForge installer payload")
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uv", type=Path, help="uv binary to bundle (default: the one on PATH)")
    args = parser.parse_args(argv)
    if args.target != host_target():
        parser.error(f"build {args.target} on that OS and architecture; this machine is {host_target()}")
    uv = args.uv or (Path(found) if (found := shutil.which("uv")) else None)
    if uv is None or not uv.is_file():
        parser.error("uv not found; pass --uv")
    print(stage(args.target, args.output.resolve(), uv.resolve()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
