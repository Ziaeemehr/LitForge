"""Stage the platform payload of a native LitForge installer (Task 7 of the desktop plan).

    python scripts/build_desktop.py --target macos-arm64 --output dist/desktop

stages OUTPUT/TARGET/ with:
  app/             the read-only source-project layout (pyproject.toml, uv.lock, src/, configs/, ...) and manifest.json
  bin/uv[.exe]     the uv that provisions the runtime and later adds optional tools
  provision.py     creates the writable per-user runtime from app/ (see packaging/desktop/provision.py)

and then builds the installer next to it: LitForge-VERSION-TARGET.dmg (macOS, a .app with its launcher),
LitForge-VERSION-linux-x64.deb, or LitForge-VERSION-windows-x64-setup.exe (Inno Setup 6), each with a .sha256 file.
--stage-only skips that step. No .venv is copied: the runtime is built on the user's machine (packaging/desktop/).
Run it on the matching OS and architecture. Installers are unsigned (macOS: ad-hoc signature only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "desktop"
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
    """Tracked and not-ignored files only: no caches, local data or .venv slips into an installer."""
    argv = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", directory]
    out = subprocess.run(argv, cwd=ROOT, capture_output=True, check=True)
    return [Path(p) for p in out.stdout.decode().split("\0") if p and (ROOT / p).is_file()]  # not deleted


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
    for name in ("provision.py", "THIRD_PARTY.md"):
        shutil.copy2(PACKAGING / name, out / name)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    uv_version = subprocess.run([str(uv), "--version"], capture_output=True, text=True, check=True).stdout.split()[1]
    files = {p.relative_to(out).as_posix(): sha256(p) for p in sorted(out.rglob("*")) if p.is_file()}
    manifest = {"app_version": project["version"], "target": target, "python": project["requires-python"],
                "uv": uv_version, "renderer": RENDERER[target], "sha256": files}
    (app / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return out


def run(*argv: str | Path) -> None:
    subprocess.run([str(a) for a in argv], check=True)


def launcher(version: str, res_line: str) -> str:
    text = (PACKAGING / "launch.sh").read_text(encoding="utf-8")
    return text.replace("@VERSION@", version).replace("@RES@", res_line)


def write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def build_macos(payload: Path, version: str, name: str) -> Path:
    sys.path.insert(0, str(ROOT / "src"))
    from litforge.shortcut import _make_icns  # the same icon as the source installer's .app

    work = payload.parent / f"{payload.name}-dmg"
    shutil.rmtree(work, ignore_errors=True)
    app = work / "LitForge.app" / "Contents"
    shutil.copytree(payload, app / "Resources", symlinks=True)
    write_exec(app / "MacOS" / "LitForge", launcher(version, 'RES="$(cd "$(dirname "$0")/../Resources" && pwd)"'))
    info = {"CFBundleName": "LitForge", "CFBundleDisplayName": "LitForge", "CFBundleExecutable": "LitForge",
            "CFBundleIdentifier": "org.litforge.app", "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": version, "CFBundleVersion": version, "NSHighResolutionCapable": True}
    if _make_icns(ROOT / "src" / "litforge" / "ui" / "static" / "icon.svg", app / "Resources" / "LitForge.icns"):
        info["CFBundleIconFile"] = "LitForge"
    (app / "Info.plist").write_bytes(plistlib.dumps(info))
    run("codesign", "--force", "--deep", "--sign", "-", work / "LitForge.app")  # ad hoc: arm64 needs a signature
    (work / "Applications").symlink_to("/Applications")
    dmg = payload.parent / f"{name}.dmg"
    run("hdiutil", "create", "-volname", "LitForge", "-srcfolder", work, "-ov", "-format", "UDZO", dmg)
    shutil.rmtree(work)
    return dmg


def build_deb(payload: Path, version: str, name: str) -> Path:
    root = payload.parent / f"{payload.name}-deb"
    shutil.rmtree(root, ignore_errors=True)
    opt = root / "opt" / "litforge"
    shutil.copytree(payload, opt, symlinks=True)
    write_exec(opt / "litforge-desktop", launcher(version, "RES=/opt/litforge"))
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "litforge-desktop").symlink_to("/opt/litforge/litforge-desktop")
    icon = root / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "litforge.svg"
    icon.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "src" / "litforge" / "ui" / "static" / "icon.svg", icon)
    entry = root / "usr" / "share" / "applications" / "litforge.desktop"
    entry.parent.mkdir(parents=True)
    entry.write_text("[Desktop Entry]\nType=Application\nName=LitForge\nComment=Scientific literature discovery, "
                     "analysis and writing\nExec=/usr/bin/litforge-desktop\nIcon=litforge\nTerminal=false\n"
                     "Categories=Education;Science;Office;\nStartupWMClass=LitForge\n", encoding="utf-8")
    size_kb = sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink()) // 1024
    (root / "DEBIAN").mkdir()
    # The window uses the system Python's GTK bindings and WebKitGTK (Ubuntu 22.04: 4.0 or 4.1; 24.04: 4.1).
    (root / "DEBIAN" / "control").write_text(
        f"Package: litforge\nVersion: {version}\nArchitecture: amd64\nSection: science\nPriority: optional\n"
        "Maintainer: LitForge <https://github.com/Ziaeemehr/LitForge>\n"
        "Depends: python3, python3-gi, gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0, ca-certificates\n"
        f"Installed-Size: {size_kb}\nHomepage: https://github.com/Ziaeemehr/LitForge\n"
        "Description: Local-first scientific literature discovery, screening and analysis\n"
        " The first start downloads Python 3.12 and LitForge's packages (about 100 MB) into\n"
        " ~/.local/share/litforge/runtime. Your library is kept elsewhere and never removed.\n", encoding="utf-8")
    deb = payload.parent / f"{name}.deb"
    run("dpkg-deb", "--root-owner-group", "--build", root, deb)
    shutil.rmtree(root)
    return deb


def build_windows(payload: Path, version: str, name: str) -> Path:
    iscc = shutil.which("iscc") or shutil.which("ISCC") or str(
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe")
    run(iscc, f"/DVersion={version}", f"/DPayload={payload}", f"/DOutput={payload.parent}", PACKAGING / "litforge.iss")
    return payload.parent / f"{name}-setup.exe"


def build_installer(payload: Path, target: str) -> Path:
    version = json.loads((payload / "app" / "manifest.json").read_text(encoding="utf-8"))["app_version"]
    name = f"LitForge-{version}-{target}"
    builders = {"windows": build_windows, "macos": build_macos, "linux": build_deb}
    builder = builders[target.split("-")[0]]
    artifact = builder(payload, version, name)
    artifact.with_name(artifact.name + ".sha256").write_text(f"{sha256(artifact)}  {artifact.name}\n")
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage a native LitForge installer payload")
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uv", type=Path, help="uv binary to bundle (default: the one on PATH)")
    parser.add_argument("--stage-only", action="store_true", help="stage the payload without building the installer")
    args = parser.parse_args(argv)
    if args.target != host_target():
        parser.error(f"build {args.target} on that OS and architecture; this machine is {host_target()}")
    uv = args.uv or (Path(found) if (found := shutil.which("uv")) else None)
    if uv is None or not uv.is_file():
        parser.error("uv not found; pass --uv")
    payload = stage(args.target, args.output.resolve(), uv.resolve())
    print(payload if args.stage_only else build_installer(payload, args.target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
