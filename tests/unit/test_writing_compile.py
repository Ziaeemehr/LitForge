from __future__ import annotations

import io
import json
import os
import re
import sys
import tarfile
import time
from pathlib import Path

import pytest

from litforge.config import Paths
from litforge.projects import create_project
from litforge.storage.db import open_db
from litforge.writing import compile as wc
from litforge.writing import documents as wd

pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake engines are POSIX shell scripts")

FAKE = """#!{python}
import json, os, sys, subprocess, time
from pathlib import Path
Path({record!r}).write_text(json.dumps({{"argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ)}}))
mode = Path({mode!r}).read_text() if Path({mode!r}).exists() else "ok"
out = Path(".build")
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path({record!r}).with_suffix(".child").write_text(str(child.pid))
    time.sleep(60)
ok_log = "(./main.tex\\nLaTeX Warning: Citation `x' on page 1 undefined on input line 7.\\n)"
log = {log!r} if mode == "error" else ok_log
(out / "main.log").write_text(log)
if mode != "error":
    (out / "main.pdf").write_bytes(b"%PDF-1.5 fake")
sys.exit(1 if mode == "error" else 0)
"""

ERROR_LOG = """(./main.tex
! Undefined control sequence.
l.12 \\foo
          {bar}
)"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    paths = Paths(tmp_path / "data", exports_root=tmp_path / "exports")
    paths.ensure()
    conn = open_db(paths.db)
    create_project(conn, paths, "demo")
    doc = wd.create_document(conn, paths, "demo", "d")
    record, mode = tmp_path / "record.json", tmp_path / "mode"
    for target in (paths.root / "tools" / "tectonic" / "tectonic", tmp_path / "bin" / "latexmk"):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(FAKE.format(python=sys.executable, record=str(record), mode=str(mode), log=ERROR_LOG))
        target.chmod(0o755)
    (tmp_path / "bin" / "xelatex").write_text("#!/bin/sh\n")
    (tmp_path / "bin" / "xelatex").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(wc, "sandbox_kind", lambda: "scan")
    yield conn, paths, doc, record, mode
    conn.close()


def test_tectonic_flags_and_env(env) -> None:
    conn, paths, doc, record, _ = env
    result = wc.compile_document(doc, paths, "tectonic")
    assert result.ok and result.pdf == ".build/main.pdf"
    seen = json.loads(record.read_text())
    assert "--untrusted" in seen["argv"] and seen["argv"][-1] == "main.tex"
    assert "--synctex" in seen["argv"]  # main.synctex.gz for jumping between the text and the PDF
    assert Path(seen["cwd"]).resolve() == doc.folder.resolve()
    assert seen["env"]["TECTONIC_CACHE_DIR"] == str(paths.root / "tools" / "tectonic-cache")
    assert [w.line for w in result.warnings] == [7]


def test_xelatex_flags_and_paranoid_env(env) -> None:
    conn, paths, doc, record, _ = env
    assert wc.compile_document(doc, paths, "xelatex").ok
    seen = json.loads(record.read_text())
    assert "-no-shell-escape" in seen["argv"] and "-xelatex" in seen["argv"]
    assert "-synctex=1" in seen["argv"]
    assert seen["env"]["openout_any"] == "p" and seen["env"]["openin_any"] == "p"
    assert seen["env"]["shell_escape"] == "f"


def test_error_log_reported_with_line(env) -> None:
    conn, paths, doc, record, mode = env
    mode.write_text("error")
    result = wc.compile_document(doc, paths, "tectonic")
    assert not result.ok
    assert [(e.message, e.file, e.line) for e in result.errors] == [("Undefined control sequence.", "main.tex", 12)]


def test_timeout_kills_grandchild(env) -> None:
    conn, paths, doc, record, mode = env
    mode.write_text("hang")
    started = time.monotonic()
    result = wc.compile_document(doc, paths, "tectonic", timeout_s=2)
    assert not result.ok and "longer than" in result.errors[0].message
    assert time.monotonic() - started < 15
    child = int(record.with_suffix(".child").read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("grandchild still running")


@pytest.mark.parametrize("line", [r"\input{/etc/passwd}", r"\include{../secret}", r"\input /etc/hosts",
                                  r"\includegraphics[width=3cm]{~/photo.png}", r"\bibliography{../../refs}",
                                  r"\settextfont[Path=/Users/x/fonts/]{Vazirmatn}", r"\openin\r=/etc/hosts"])
def test_scan_refuses_outside_paths(env, line: str) -> None:
    conn, paths, doc, record, _ = env
    (doc.folder / "main.tex").write_text("\\documentclass{article}\n" + line + "\n")
    result = wc.compile_document(doc, paths, "tectonic")
    assert not result.ok and result.errors[0].line == 2 and not record.exists()


def test_scan_ignores_comments_and_inside_paths(env) -> None:
    conn, paths, doc, record, _ = env
    (doc.folder / "main.tex").write_text("% \\input{/etc/passwd}\n\\input{sections/intro}\n"
                                         "\\includegraphics{figures/a.png}\n\\settextfont[Path=fonts/]{V}\n")
    assert wc.scan_paths(doc) == []


def test_missing_engine_message(env, monkeypatch) -> None:
    conn, paths, doc, record, _ = env
    monkeypatch.setattr(wc, "tectonic_command", lambda _p: None)
    with pytest.raises(wc.CompileError, match="Optional tools"):
        wc.compile_document(doc, paths, "tectonic")


def test_macos_profile_reads_only_allowed_paths(tmp_path: Path) -> None:
    folder = tmp_path / "doc"
    profile = wc.macos_profile(folder, [tmp_path / "engine"], [folder / ".build"], tmp_path / "scratch", network=False)
    read = next(line for line in profile.splitlines() if "file-read-data" in line)
    assert read.startswith("(deny file-read-data (require-not (require-any")  # an allowlist, not a denylist
    allowed = set(re.findall(r'\(subpath "([^"]+)"\)', read))
    assert {str(folder), str(tmp_path / "engine"), str(tmp_path / "scratch"), "/System", "/usr"} <= allowed
    assert not {"/Users", "/private/tmp", "/private/var/folders", "/Volumes", "/private/etc"} & allowed
    assert not any(p == str(Path.home()) or str(Path.home()).startswith(p + "/") for p in allowed)
    assert "(deny network*)" in profile and '(literal "/dev/null")' in profile
    assert str(folder / ".build") in next(line for line in profile.splitlines() if "file-write" in line)


def test_macos_profile_legacy_denylist_on_request(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LITFORGE_MAC_SANDBOX", "denylist")
    profile = wc.macos_profile(tmp_path / "doc", [], [tmp_path / "doc" / ".build"], tmp_path / "scratch")
    assert '(deny file-read* (subpath "/Users")' in profile


def test_bwrap_hides_home(tmp_path: Path) -> None:
    argv = wc.bwrap_argv(tmp_path / "doc", [], [tmp_path / "doc" / ".build"])
    assert ["--tmpfs", str(Path.home())] in [argv[i:i + 2] for i in range(len(argv))]
    assert argv[-1] == "--" and "--unshare-pid" in argv
    assert ["--ro-bind", "/", "/"] not in [argv[i:i + 3] for i in range(len(argv))]  # no whole-disk mount


def test_bwrap_reads_only_the_document_and_system_files(tmp_path: Path) -> None:
    import shutil
    import subprocess

    if not shutil.which("bwrap"):
        pytest.skip("bwrap is not installed")
    folder = tmp_path / "doc"
    (folder / ".build").mkdir(parents=True)
    (folder / "main.tex").write_text("inside")
    (tmp_path / "secret.txt").write_text("outside")
    argv = wc.bwrap_argv(folder, [], [folder / ".build"], network=False)
    probe = subprocess.run([*argv, "/bin/sh", "-c", "true"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("bwrap cannot create a sandbox here: " + probe.stderr.decode(errors="replace")[:200])

    def run(script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*argv, "/bin/sh", "-c", script], capture_output=True, text=True)

    assert run("cat main.tex").stdout == "inside"
    for outside in [tmp_path / "secret.txt", Path("/etc/passwd"), Path("/etc/shadow"), Path("/root"), Path("/var/log")]:
        assert run(f"cat {outside} || ls {outside}").returncode != 0, outside
    assert run("echo x > .build/out && cat .build/out").stdout == "x\n"
    assert run("echo x > main.tex").returncode != 0  # the source is read-only


def _tarball(payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("tectonic")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def test_install_verifies_sha(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    paths = Paths(tmp_path, exports_root=tmp_path / "x")
    blob = _tarball(b"#!/bin/sh\necho Tectonic 0.17.0\n")
    monkeypatch.setattr(wc, "_platform_key", lambda: ("darwin", "arm64"))
    monkeypatch.setitem(wc.TECTONIC_ASSETS, ("darwin", "arm64"),
                        ("t.tar.gz", hashlib.sha256(blob).hexdigest(), len(blob)))
    status = wc.install_tectonic(paths, opener=lambda url: io.BytesIO(blob))
    assert status["tectonic"]["installed"] and status["tectonic"]["version"] == "0.17.0"
    monkeypatch.setitem(wc.TECTONIC_ASSETS, ("darwin", "arm64"), ("t.tar.gz", "0" * 64, len(blob)))
    (paths.root / "tools" / "tectonic" / "tectonic").unlink()
    with pytest.raises(wc.CompileError, match="SHA-256"):
        wc.install_tectonic(paths, opener=lambda url: io.BytesIO(blob))
    assert not (paths.root / "tools" / "tectonic" / "tectonic").exists()


REAL_LOGS = [
    ("(./main.tex\n! Missing } inserted.\n<inserted text> \n                }\nl.20 \\end{document}\n)",
     [("Missing } inserted.", "main.tex", 20)]),
    ("(./main.tex (./sections/intro.tex\n! LaTeX Error: File `missing.sty' not found.\n\nType X to quit\n"
     "l.3 \\usepackage{missing}\n", [("LaTeX Error: File `missing.sty' not found.", "sections/intro.tex", 3)]),
    ("./main.tex:9: Undefined control sequence.\n", [("Undefined control sequence.", "main.tex", 9)]),
    ("(./main.tex\n! Package fontspec Error: The font \"Vazirmatn\" cannot be found.\n\nl.6 }\n",
     [('Package fontspec Error: The font "Vazirmatn" cannot be found.', "main.tex", 6)]),
]


@pytest.mark.parametrize(("log", "expected"), REAL_LOGS)
def test_parse_log_errors(log: str, expected) -> None:
    errors, _ = wc.parse_log(log)
    assert [(e.message, e.file, e.line) for e in errors] == expected


def test_parse_log_warnings() -> None:
    _, warnings = wc.parse_log(
        "(./main.tex\nLaTeX Warning: Reference `fig:a' on page 2 undefined on input line 31.\n"
        "Package natbib Warning: Citation `smith2020' on page 1 undefined on input line\n    14.\n"
        "LaTeX Font Warning: Font shape `TU/Vazirmatn(0)/m/it' undefined\n)")
    assert [w.line for w in warnings] == [31, 14, None]
    assert "smith2020" in warnings[1].message


def test_latexmkrc_refused_and_norc(env) -> None:
    conn, paths, doc, record, _ = env
    (doc.folder / "latexmkrc").write_text('system("touch /tmp/x");')
    result = wc.compile_document(doc, paths, "xelatex")
    assert not result.ok and "latexmk" in result.errors[0].message and not record.exists()
    (doc.folder / "latexmkrc").unlink()
    wc.compile_document(doc, paths, "xelatex")
    assert "-norc" in json.loads(record.read_text())["argv"]


def test_parse_log_returns_to_main_after_a_closed_subfile() -> None:
    log = ("(./main.tex (/usr/share/texmf/article.cls (size10.clo)) (./sections/a.tex\n"
           "LaTeX Warning: Reference `x' on page 1 undefined on input line 3.\n)\n"
           "LaTeX Warning: Citation `y' on page 1 undefined on input line 14.\n"
           "! Undefined control sequence.\nl.12 \\foo\n)")
    errors, warnings = wc.parse_log(log)
    assert [(w.file, w.line) for w in warnings] == [("sections/a.tex", 3), ("main.tex", 14)]
    assert [(e.file, e.line) for e in errors] == [("main.tex", 12)]
