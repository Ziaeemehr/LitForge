"""Compile Writing Studio documents with Tectonic or a system XeLaTeX, inside an OS sandbox where possible."""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from litforge.config import Paths
from litforge.jobs import check_cancelled
from litforge.parsing.mineru_parser import _run_command
from litforge.writing.documents import TEXT_SUFFIXES, Document, list_files

TECTONIC_VERSION = "0.17.0"
_RELEASE = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40" + TECTONIC_VERSION
# (asset, sha256, bytes) from the GitHub release; the download is refused when the digest differs.
TECTONIC_ASSETS: dict[tuple[str, str], tuple[str, str, int]] = {
    ("darwin", "arm64"): ("tectonic-0.17.0-aarch64-apple-darwin.tar.gz",
                          "a3f1cac7c5678f01661a92212f58480ae3b0634115d880dbc59e2953ded45667", 21704674),
    ("darwin", "x86_64"): ("tectonic-0.17.0-x86_64-apple-darwin.tar.gz",
                           "7c90ef5b6ddb1eb1937e4337add5237b79338e4b9676459fa91187d24d6cdf80", 21790177),
    ("linux", "x86_64"): ("tectonic-0.17.0-x86_64-unknown-linux-gnu.tar.gz",
                          "1a715688baf591e650c8aeb160ae934e181685eecbb38b317de30b269ac5d606", 22749118),
    ("linux", "aarch64"): ("tectonic-0.17.0-aarch64-unknown-linux-musl.tar.gz",
                           "b10954a95404f3ab2328d2fa59a5ebab8e657f893fab096f98be8db7c0c979b8", 9926834),
    ("windows", "amd64"): ("tectonic-0.17.0-x86_64-pc-windows-msvc.zip",
                           "f61ce51f0b0ade1015b7de7ef368541c5424e9756ecbd0d7af97d6d48030845f", 21060223),
}
COMPILE_TIMEOUT_S = 120
FIRST_COMPILE_TIMEOUT_S = 600  # the first Tectonic run downloads the LaTeX packages it needs
_EXE = ".exe" if os.name == "nt" else ""

# Commands that read a file named in their argument; an absolute or `..` path there is refused before compiling.
_FILE_COMMANDS = re.compile(
    r"\\(input|include|InputIfFileExists|IfFileExists|openin|includegraphics|includepdf|lstinputlisting|"
    r"verbatiminput|VerbatimInput|bibliography|addbibresource|import|subimport|includeonly|usepackage|"
    r"RequirePackage|documentclass|LoadClass|fontspec|setmainfont|settextfont|setlatintextfont|newfontfamily|"
    r"tikzexternalize|pgfplotstableread|DTLloaddb|csvreader|includesvg)\*?\s*(?:\[[^\]]*\]\s*)*\{([^}]*)\}")
_BARE_INPUT = re.compile(r"\\input\s+([^\s{}%\\]+)|\\openin\s*\\?\w+\s*=?\s*([^\s{}%\\]+)")
_PATH_OPTION = re.compile(r"Path\s*=\s*([^,\]]+)")


class CompileError(RuntimeError):
    pass


@dataclass
class LogItem:
    message: str
    file: str | None = None
    line: int | None = None


@dataclass
class CompileResult:
    ok: bool
    engine: str
    sandbox: str
    elapsed_ms: float
    pdf: str | None
    errors: list[LogItem] = field(default_factory=list)
    warnings: list[LogItem] = field(default_factory=list)
    log: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- engines ----------------------------------------------------------------------------------------------


def tools_dir(paths: Paths) -> Path:
    return paths.root / "tools"


def tectonic_cache(paths: Paths) -> Path:
    return tools_dir(paths) / "tectonic-cache"


def tectonic_command(paths: Paths) -> str | None:
    own = tools_dir(paths) / "tectonic" / f"tectonic{_EXE}"
    if own.is_file() and os.access(own, os.X_OK):
        return str(own)
    return shutil.which("tectonic")


def xelatex_command() -> str | None:
    latexmk = shutil.which("latexmk")
    return latexmk if latexmk and shutil.which("xelatex") else None


def _platform_key() -> tuple[str, str]:
    system = sys.platform
    name = "darwin" if system == "darwin" else "windows" if system.startswith("win") else "linux"
    machine = platform.machine().lower()
    machine = {"aarch64": "aarch64" if name == "linux" else "arm64", "arm64": "arm64" if name == "darwin"
               else "aarch64", "x86_64": "amd64" if name == "windows" else "x86_64",
               "amd64": "amd64" if name == "windows" else "x86_64"}.get(machine, machine)
    return name, machine


def engine_status(paths: Paths) -> dict[str, Any]:
    command = tectonic_command(paths)
    asset = TECTONIC_ASSETS.get(_platform_key())
    version = None
    if command:
        try:
            out = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=10, check=False)
            version = out.stdout.strip().removeprefix("Tectonic ").strip() or None
        except (OSError, subprocess.SubprocessError):
            version = None
    cache = tectonic_cache(paths)
    return {
        "tectonic": {"installed": command is not None, "path": command, "version": version,
                     "offerable": command is None and asset is not None, "release": TECTONIC_VERSION,
                     "size": f"~{round(asset[2] / 1e6)} MB + ~50 MB of LaTeX packages on first use" if asset
                     else None, "cache_ready": cache.is_dir() and any(cache.iterdir())},
        "xelatex": {"installed": xelatex_command() is not None, "path": xelatex_command()},
        "sandbox": sandbox_kind(),
    }


def install_tectonic(paths: Paths, progress: Callable[[str], None] = lambda _: None,
                     opener: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """Download the pinned Tectonic release for this platform, verify its SHA-256 and install it."""
    asset = TECTONIC_ASSETS.get(_platform_key())
    if asset is None:
        raise CompileError(f"no Tectonic build for {'/'.join(_platform_key())}; install a TeX distribution")
    name, digest, size = asset
    progress(f"Downloading {name} ({size / 1e6:.1f} MB)…")
    open_url = opener or (lambda url: urllib.request.urlopen(url, timeout=60))  # noqa: S310 - fixed https URL
    hasher = hashlib.sha256()
    data = io.BytesIO()
    with open_url(f"{_RELEASE}/{name}") as response:
        while chunk := response.read(1 << 20):
            check_cancelled()
            data.write(chunk)
            hasher.update(chunk)
            if data.tell() > size * 2:
                raise CompileError("the download is larger than expected")
    if hasher.hexdigest() != digest:
        raise CompileError("the downloaded Tectonic does not match its published SHA-256; nothing was installed")
    target_dir = tools_dir(paths) / "tectonic"
    target_dir.mkdir(parents=True, exist_ok=True)
    data.seek(0)
    binary = _extract_binary(name, data)
    tmp = target_dir / f".tectonic{_EXE}.tmp"
    tmp.write_bytes(binary)
    tmp.chmod(0o755)
    os.replace(tmp, target_dir / f"tectonic{_EXE}")
    progress("Tectonic installed.")
    return engine_status(paths)


def _extract_binary(name: str, data: io.BytesIO) -> bytes:
    wanted = f"tectonic{_EXE}"
    if name.endswith(".zip"):
        with zipfile.ZipFile(data) as archive:
            member = next((m for m in archive.namelist() if Path(m).name == wanted), None)
            if member is None:
                raise CompileError("the archive has no tectonic binary")
            return archive.read(member)
    with tarfile.open(fileobj=data, mode="r:gz") as archive:
        for info in archive.getmembers():
            if info.isfile() and Path(info.name).name == wanted:
                stream = archive.extractfile(info)
                if stream is not None:
                    return stream.read()
    raise CompileError("the archive has no tectonic binary")


# ---- sandbox ----------------------------------------------------------------------------------------------


def sandbox_kind() -> str:
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
        return "macos"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap"
    return "scan"


def _sb_quote(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


# macOS: reads of file contents are denied everywhere except the system (/usr covers MacTeX's /usr/local/texlive),
# the document, the engine, its cache and fonts. Metadata (stat) stays readable: dyld and path lookups need it.
MAC_SYSTEM = ["/System", "/usr", "/bin", "/sbin", "/Library/Fonts", "/Library/TeX", "/Library/Apple",
              "/opt/homebrew", "/opt/local", "/private/var/db/dyld", "/private/var/db/timezone",
              "/private/etc/ssl", "/private/etc/hosts", "/private/etc/resolv.conf", "/private/etc/localtime",
              "/private/etc/services", "/private/etc/protocols", "/private/etc/texmf", "/dev"]


def _write_rule(writable: list[Path], scratch: Path) -> str:
    return ("(deny file-write* (require-not (require-any " +
            " ".join(f"(subpath {_sb_quote(p)})" for p in [*writable, scratch]) +
            ' (literal "/dev/null") (literal "/dev/tty") (literal "/dev/dtracehelper"))))')


def macos_profile(folder: Path, readable: list[Path], writable: list[Path], scratch: Path,
                  network: bool = True) -> str:
    """Allow reading file contents only from the system, TeX, the document, the engine, its cache and fonts; allow
    writes only to the build folder, the cache and a private temp folder. The network is left on only for Tectonic,
    which downloads LaTeX packages on first use. LITFORGE_MAC_SANDBOX=denylist restores the older, looser profile
    in case a TeX installation needs a path missing here."""
    fonts = Path.home().resolve() / "Library" / "Fonts"
    if os.environ.get("LITFORGE_MAC_SANDBOX") == "denylist":
        denied = [Path("/Users"), Path("/private/tmp"), Path("/private/var/folders"), Path("/Volumes"),
                  Path("/private/etc"), Path("/private/var/root")]
        allowed = [folder, *readable, *writable, scratch, fonts,
                   *map(Path, ["/private/etc/ssl", "/private/etc/hosts", "/private/etc/resolv.conf",
                               "/private/etc/localtime"])]
        lines = ["(version 1)", "(allow default)",
                 "(deny file-read* " + " ".join(f"(subpath {_sb_quote(p)})" for p in denied) + ")",
                 "(allow file-read* " + " ".join(f"(subpath {_sb_quote(p)})" for p in allowed) + ")"]
    else:
        allowed = [*map(Path, MAC_SYSTEM), folder, *readable, *writable, scratch, fonts]
        lines = ["(version 1)", "(allow default)",
                 "(deny file-read-data (require-not (require-any " +
                 " ".join(f"(subpath {_sb_quote(p)})" for p in allowed) + ' (literal "/"))))']
    lines.append(_write_rule(writable, scratch))
    if not network:
        lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


# Linux: start from an empty root and mount only what an engine needs, read-only. Home folders, /tmp, /var, /mnt,
# /media, /srv, /root and most of /etc are absent, so a document cannot read them. `-try` skips missing paths.
BWRAP_SYSTEM = ["/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32"]
BWRAP_ETC = ["/etc/ssl", "/etc/ca-certificates", "/etc/pki", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
             "/etc/gai.conf", "/etc/localtime", "/etc/fonts", "/etc/texmf", "/etc/alternatives", "/etc/ld.so.cache",
             "/etc/ld.so.conf", "/etc/ld.so.conf.d"]
BWRAP_VAR = ["/var/lib/texmf", "/var/cache/fontconfig"]


def bwrap_argv(folder: Path, readable: list[Path], writable: list[Path], network: bool = True) -> list[str]:
    argv = ["bwrap"]
    for mount in [*BWRAP_SYSTEM, *BWRAP_ETC, *BWRAP_VAR]:
        argv += ["--ro-bind-try", mount, mount]
    argv += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", str(Path.home()),
             "--die-with-parent", "--unshare-pid"]
    if not network:
        argv.append("--unshare-net")
    for path in readable:
        argv += ["--ro-bind", str(path), str(path)]
    argv += ["--ro-bind", str(folder), str(folder)]
    for path in writable:
        argv += ["--bind", str(path), str(path)]
    return argv + ["--chdir", str(folder), "--"]


def scan_paths(doc: Document) -> list[LogItem]:
    """Refuse commands that name a file outside the document folder (absolute paths, `..`, `~`)."""
    problems = []
    for item in list_files(doc):
        if Path(item["name"]).name.lower() in {"latexmkrc", "_latexmkrc"} or item["name"].endswith(".rc"):
            problems.append(LogItem("latexmk configuration files run code and are not allowed", item["name"], None))
            continue
        if item["kind"] != "text" or Path(item["name"]).suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = (doc.folder / item["name"]).read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            code = re.sub(r"(?<!\\)%.*", "", line)
            args = [a for m in _FILE_COMMANDS.finditer(code) for a in m.group(2).split(",")]
            args += [a or b for a, b in _BARE_INPUT.findall(code)] + _PATH_OPTION.findall(code)
            for arg in args:
                arg = arg.strip()
                if _outside(arg):
                    problems.append(LogItem(f"file outside the document folder is not allowed: {arg}",
                                            item["name"], number))
    return problems


def _outside(arg: str) -> bool:
    arg = arg.strip().strip('"')
    return bool(arg) and (arg.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:", arg) is not None
                          or ".." in re.split(r"[/\\]", arg) or "|" in arg[:1])


# ---- compile ----------------------------------------------------------------------------------------------


def _command(doc: Document, paths: Paths, engine: str, main: str) -> tuple[list[str], dict[str, str], list[Path]]:
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SystemRoot",
                                                         "USERPROFILE", "TEMP", "TMP", "SSL_CERT_FILE"}}
    if engine == "tectonic":
        command = tectonic_command(paths)
        if command is None:
            raise CompileError("Tectonic is not installed; install it in Settings → Optional tools")
        cache = tectonic_cache(paths)
        cache.mkdir(parents=True, exist_ok=True)
        env["TECTONIC_CACHE_DIR"] = str(cache)
        argv = [command, "--untrusted", "--keep-logs", "--synctex", "--chatter", "minimal", "-o", ".build", main]
        return argv, env, [Path(command).resolve().parent, cache.resolve()]
    if engine == "xelatex":
        command = xelatex_command()
        if command is None:
            raise CompileError("latexmk and xelatex were not found; install TeX Live or MacTeX")
        env.update(openout_any="p", openin_any="p", shell_escape="f")
        # -norc: a latexmkrc in the folder is Perl code and must not run.
        argv = [command, "-norc", "-xelatex", "-synctex=1", "-no-shell-escape", "-interaction=nonstopmode",
                "-halt-on-error", "-file-line-error", "-outdir=.build", main]
        return argv, env, []
    raise CompileError(f"unknown engine {engine}")


def compile_document(doc: Document, paths: Paths, engine: str | None = None, *, main: str = "main.tex",
                     timeout_s: float | None = None) -> CompileResult:
    engine = engine or doc.engine
    started = time.monotonic()
    folder = doc.folder.resolve()
    if not (folder / main).is_file():
        raise CompileError(f"{main} not found")
    kind = sandbox_kind()
    problems = scan_paths(doc)
    if problems:
        return CompileResult(False, engine, kind, 0.0, None, errors=problems)
    build = folder / ".build"
    if build.is_symlink():
        raise CompileError("the build folder must not be a symbolic link")
    build.mkdir(exist_ok=True)
    argv, env, readable = _command(doc, paths, engine, main)
    writable = [build, *[p for p in readable if p.name == "tectonic-cache"]]
    if timeout_s is None:
        cache = tectonic_cache(paths)
        first = engine == "tectonic" and not (cache.is_dir() and any(cache.iterdir()))
        timeout_s = FIRST_COMPILE_TIMEOUT_S if first else COMPILE_TIMEOUT_S
    if engine == "xelatex":
        writable = [build]
    with tempfile.TemporaryDirectory(prefix="litforge-compile-") as scratch:
        env["TMPDIR"] = scratch  # a private temp folder; the shared one stays unreadable
        if kind == "macos":
            profile = Path(scratch) / "profile.sb"
            profile.write_text(macos_profile(folder, readable, writable, Path(scratch).resolve(),
                                             network=engine == "tectonic"), encoding="utf-8")
            argv = ["/usr/bin/sandbox-exec", "-f", str(profile), *argv]
        elif kind == "bwrap":
            argv = bwrap_argv(folder, readable, writable, network=engine == "tectonic") + argv
        try:
            completed = _run_command(argv, env=env, timeout=timeout_s, merge_output=True, cwd=folder)
        except subprocess.TimeoutExpired as exc:
            tail = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else ""
            return CompileResult(False, engine, kind, (time.monotonic() - started) * 1000, None,
                                 errors=[LogItem(f"compilation took longer than {timeout_s:.0f} s and was stopped")],
                                 log=tail)
    stem = Path(main).stem
    log_file = build / f"{stem}.log"
    log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
    errors, warnings = parse_log(log)
    if completed.returncode and not errors:
        errors.extend(LogItem(line.strip()) for line in completed.stderr.splitlines()
                      if line.strip().startswith(("error:", "caused by:")))
        if not errors:
            errors.append(LogItem(f"the engine stopped with exit code {completed.returncode}"))
    pdf = build / f"{stem}.pdf"
    ok = completed.returncode == 0 and pdf.is_file()
    return CompileResult(ok, engine, kind, (time.monotonic() - started) * 1000,
                         f".build/{stem}.pdf" if pdf.is_file() else None, errors, warnings,
                         (log or completed.stderr)[-64_000:])


# ---- log parsing ------------------------------------------------------------------------------------------

_FILE_LINE_ERROR = re.compile(r"^(\.?/?[^\s:()]+\.(?:tex|sty|cls|bib|bbl)):(\d+): (.*)$")
_LINE_NO = re.compile(r"^l\.(\d+)")
_PAREN = re.compile(r"\((\.?/?[^\s()]+\.(?:tex|bbl|sty|cls|cfg|def|fd|clo|aux|ltx))|\(|\)")
_WARNING = re.compile(r"^(LaTeX|Package [\w-]+|Class [\w-]+|LaTeX Font) Warning: (.*)")
_INPUT_LINE = re.compile(r"on input line (\d+)")


def parse_log(log: str) -> tuple[list[LogItem], list[LogItem]]:
    """Errors (`! ...` with their `l.<n>` line) and warnings (undefined citations/references, packages)."""
    errors: list[LogItem] = []
    warnings: list[LogItem] = []
    lines = log.splitlines()
    stack: list[str | None] = []  # TeX prints "(file" on open and ")" on close; other parentheses push None

    def current_file() -> str:
        return next((f for f in reversed(stack) if f), "main.tex")

    i = 0
    while i < len(lines):
        line = lines[i]
        current = current_file()
        if not line.startswith(("!", "l.")) and not _WARNING.match(line):
            for paren in _PAREN.finditer(line):
                if paren.group(0) == ")":
                    if stack:
                        stack.pop()
                else:
                    name = paren.group(1)
                    stack.append(name.removeprefix("./") if name and name.endswith((".tex", ".bbl")) else
                                 ("" if name else None))
            current = current_file()
        if match := _FILE_LINE_ERROR.match(line):
            errors.append(LogItem(match.group(3).strip(), match.group(1).removeprefix("./"), int(match.group(2))))
            i += 1
            continue
        if line.startswith("! "):
            message = line[2:].strip()
            number = None
            for follow in lines[i + 1:i + 30]:
                if found := _LINE_NO.match(follow):
                    number = int(found.group(1))
                    break
            if not any(e.message == message and e.line == number for e in errors):
                errors.append(LogItem(message, current, number))
        elif match := _WARNING.match(line):
            text = match.group(2)
            j = i + 1
            while j < len(lines) and lines[j].startswith(" " * 4) and len(text) < 400:
                text += " " + lines[j].strip()
                j += 1
            found = _INPUT_LINE.search(text)
            warnings.append(LogItem(f"{match.group(1)}: {text.strip()}", current if found else None,
                                    int(found.group(1)) if found else None))
        i += 1
    return errors, warnings
