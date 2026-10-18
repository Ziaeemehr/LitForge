"""Isolated, local MinerU command adapter and docvortex.middle 2.0 mapping."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, BinaryIO

import pymupdf

from litforge.config import Config, ParsingConfig, Paths
from litforge.jobs import check_cancelled, track_process
from litforge.llm.llamacpp_server import LlamaCppServer
from litforge.parsing.base import Asset, PageInfo, Paragraph, ParsedDocument, Section, section_kind
from litforge.parsing.formulas import tidy_latex

PACKAGE = "mineru[core]==4.0.12"
INSTALL_NOTICE = ("Install MinerU 4.0.12 in an isolated environment: torch/dependencies plus about 2 GB of models "
                  "(several GB total disk/download). Models download on first use. PDFs are parsed locally.")
OUTPUT_TAIL_BYTES = 64 * 1024


@contextmanager
def _windows_job(process: subprocess.Popen[bytes]) -> Iterator[Callable[[], None]]:
    """Retain a handle to descendants even when their group leader exits on CTRL_BREAK."""
    import ctypes

    kernel: Any = ctypes.WinDLL('kernel32', use_last_error=True)  # type: ignore[attr-defined]
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError('Cannot create MinerU process job')
    try:
        if not kernel.AssignProcessToJobObject(job, int(process._handle)):  # type: ignore[attr-defined]
            raise OSError('Cannot assign MinerU to process job')
        def kill_tree() -> None:
            if not kernel.TerminateJobObject(job, 1):
                raise OSError('Cannot terminate MinerU process job')
        yield kill_tree
    finally:
        kernel.CloseHandle(job)


def _signal_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == 'nt':
        with suppress(ProcessLookupError, OSError):
            process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
    else:
        with suppress(ProcessLookupError, PermissionError):  # macOS: EPERM once the group is gone
            os.killpg(process.pid, signal.SIGTERM)


def _kill_tree(process: subprocess.Popen[bytes], kill_tree: Callable[[], None] | None = None) -> None:
    """Kill descendants even if their group leader has exited."""
    if os.name == 'nt':
        if kill_tree is not None:
            kill_tree()
        else:  # Job setup itself failed; terminate the newly spawned process tree.
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    else:
        with suppress(ProcessLookupError, PermissionError):  # macOS: EPERM once the group is gone
            os.killpg(process.pid, signal.SIGKILL)


def _read_tail(stream: BinaryIO) -> str:
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - OUTPUT_TAIL_BYTES))
    return stream.read(OUTPUT_TAIL_BYTES).decode('utf-8', errors='replace')


def _run_command(argv: list[str], *, env: dict[str, str], timeout: float,
                 out_dir: Path | None = None, capture_stdout: bool = False,
                 merge_output: bool = False) -> subprocess.CompletedProcess[str]:
    """Spool diagnostics to disk; Python retains only their final 64 KiB."""
    check_cancelled()
    with ExitStack() as stack:
        stderr = stack.enter_context(tempfile.TemporaryFile(dir=out_dir))
        stdout = stack.enter_context(tempfile.TemporaryFile(dir=out_dir)) if capture_stdout else None
        options: dict[str, Any] = {'start_new_session': True} if os.name != 'nt' else {
            'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
        process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stderr if merge_output else stdout or subprocess.DEVNULL,
                                   stderr=stderr, **options)
        kill_tree = None
        terminate_lock = threading.Lock()
        signalled = False
        killed = threading.Event()

        def terminate(force: bool) -> None:
            nonlocal signalled
            with terminate_lock:
                if killed.is_set():
                    return
                if force:
                    _kill_tree(process, kill_tree)
                    killed.set()
                elif not signalled:
                    _signal_tree(process)
                    signalled = True

        try:
            if os.name == 'nt':
                kill_tree = stack.enter_context(_windows_job(process))
            with track_process(process.pid, terminate):
                process.wait(timeout=timeout)
        except BaseException as exc:
            terminate(False)
            # Shutdown can escalate all trees against its shared deadline and wake this cleanup.
            killed.wait(1)
            terminate(True)
            process.wait()
            if isinstance(exc, subprocess.TimeoutExpired):
                exc.stderr = _read_tail(stderr).encode('utf-8')
            raise
        return subprocess.CompletedProcess(argv, process.returncode,
                                           _read_tail(stdout) if stdout else '', _read_tail(stderr))


def find_command(paths: Paths, config: ParsingConfig) -> str | None:
    candidates = [config.mineru.command, str(paths.root / 'tools' / 'mineru' / 'bin' / 'mineru-kit'), 'mineru-kit']
    for candidate in candidates:
        if candidate and (found := shutil.which(str(Path(candidate).expanduser()))):
            return found
    return None


def child_env(paths: Paths) -> dict[str, str]:
    env = dict(os.environ, MINERU_HOME=str(paths.root / 'models' / 'mineru'), CI='1')
    env.pop('MINERU_API_KEY', None)
    env.pop('MINERU_VL_API_KEY', None)
    return env


@lru_cache(maxsize=32)
def command_version(command: str) -> str:
    result = _run_command([command, 'version'], env=dict(os.environ), timeout=30, capture_stdout=True)
    result.check_returncode()
    version = re.search(r'\d+\.\d+\.\d+(?:[\w.+-]*)', result.stdout)
    if not version:
        raise RuntimeError('MinerU did not report an installed version')
    return version.group()


def mineru_status(paths: Paths, config: ParsingConfig) -> dict[str, Any]:
    command = find_command(paths, config)
    version, error = None, None
    if command:
        try:
            version = command_version(command)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            error = str(exc)
    model_dir = paths.root / 'models' / 'mineru'
    size = sum(p.stat().st_size for p in model_dir.rglob('*') if p.is_file()) if model_dir.exists() else 0
    return dict(installed=version is not None, command=command, version=version, error=error,
                models_bytes=size, notice=INSTALL_NOTICE)


def install_mineru(paths: Paths, progress: Callable[[str], None] = lambda _: None, *,
                   timeout_s: float = 45 * 60) -> None:
    """Only called after CLI/UI confirmation; never modifies LitForge's environment."""
    target = paths.root / 'tools' / 'mineru'
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation establishes ownership. Never modify or remove an existing installation.
    target.mkdir()
    try:
        deadline = time.monotonic() + timeout_s
        uv = shutil.which('uv')
        python = target / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        commands = ([[uv, 'venv', str(target)], [uv, 'pip', 'install', '--python', str(python), PACKAGE]] if uv else
                    [[sys.executable, '-m', 'venv', str(target)], [str(python), '-m', 'pip', 'install', PACKAGE]])
        for command in commands:
            progress('Running ' + ' '.join(command))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_s)
            result = _run_command(command, env=child_env(paths), timeout=remaining,
                                  out_dir=target.parent, merge_output=True)
            progress(result.stderr)
            if result.returncode:
                raise RuntimeError('MinerU installation failed: ' + result.stderr)
    except BaseException as exc:
        shutil.rmtree(target)
        if isinstance(exc, subprocess.TimeoutExpired):
            tail = (exc.stderr or b'').decode('utf-8', errors='replace')
            raise RuntimeError(f'MinerU installation timed out after {timeout_s}s: {tail}') from exc
        raise
    command_version.cache_clear()
    progress('MinerU installed. Models download locally on first parse (about 2 GB).')


def _math(value: str) -> str:
    # Compact grouping whitespace too: MinerU emits e.g. 2^{ 1 0 }.
    value = re.sub(r'(?<=\d)\s+(?=\d)', '', value)
    value = re.sub(r'\{\s*(\d+)\s*\}', r'{\1}', value)
    return tidy_latex(value)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(f"${_math(str(span.get('content', '')))}$" if span.get('type') == 'equation_inline'
                       else _text(span.get('content', '')) for span in content)
    return ''


class _Table(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.cells: list[list[tuple[str, int, int]]] = []
        self.cell: list[str] | None = None
        self.colspan = self.rowspan = 1
        self.feed(html)
        grid: dict[tuple[int, int], str] = {}
        for r, row in enumerate(self.cells):
            col = 0
            for text, colspan, rowspan in row:
                while any((r, c) in grid for c in range(col, col + colspan)):
                    col += 1
                for rr in range(r, min(len(self.cells), r + (rowspan or len(self.cells)))):
                    for cc in range(col, col + colspan):
                        grid[rr, cc] = text
                col += colspan
        width = max((c + 1 for _, c in grid), default=0)
        self.rows = [[grid.get((r, c), '') for c in range(width)] for r in range(len(self.cells))]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'tr':
            self.cells.append([])
        elif tag in ('td', 'th'):
            self.cell = []
            attributes = dict(attrs)
            self.colspan = self._span(attributes.get('colspan'), 1)
            self.rowspan = self._span(attributes.get('rowspan'), 0)
        elif tag == 'br' and self.cell is not None:
            self.cell.append(' ')

    @staticmethod
    def _span(value: str | None, minimum: int) -> int:
        try:
            return max(minimum, min(1000, int(value or '1')))
        except ValueError:
            return 1

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ('td', 'th') and self.cell is not None:
            if not self.cells:
                self.cells.append([])
            self.cells[-1].append((' '.join(''.join(self.cell).split()), self.colspan, self.rowspan))
            self.cell = None


def map_middle_json(data: dict[str, Any], version: str) -> ParsedDocument:
    if data.get('schema') != 'docvortex.middle' or str(data.get('schema_version')) != '2.0':
        raise ValueError('Unsupported MinerU middle_json schema (expected docvortex.middle 2.0)')
    dimensions = {p['page_idx']: (float(p['width_pt']), float(p['height_pt']))
                  for p in data['extensions']['docvortex_layout']['pages']}
    sections = [Section(heading=None, kind='front')]
    pages, assets, furniture = [], [], []
    for page in data['pages']:
        idx = page['page_idx']
        width, height = dimensions[idx]
        page_no = idx + 1
        info = PageInfo(index=page_no, label=None, width=width, height=height, char_count=0, image_count=0)
        pages.append(info)
        preceding_figure: Asset | None = None
        for block in page['blocks']:
            kind, content = block['type'], block.get('content', '')
            raw_box = block.get('bbox')
            bbox = (raw_box[0] * width, raw_box[1] * height, raw_box[2] * width,
                    raw_box[3] * height) if raw_box else None
            text = _text(content).strip()
            figure = preceding_figure
            preceding_figure = None
            if kind in ('header', 'footer', 'page_number'):
                if text:
                    furniture.append(text)
                continue
            if kind == 'paragraph_title':
                sections.append(Section(heading=text, kind=section_kind(text), level=max(1, block.get('level', 1))))
            elif kind in ('table', 'chart', 'image'):
                children = content if isinstance(content, list) else []
                caption = '\n'.join(_text(c.get('content', '')).strip() for c in children
                                    if c['type'].endswith('_caption')).strip() or None
                asset = Asset(kind='table' if kind == 'table' else 'figure', label=None,
                              caption=caption, page=page_no, bbox=bbox)
                text = caption or ''
                if kind == 'table':
                    html = next((str(c['content']) for c in children if c['type'] == 'table_body'), '')
                    rows = _Table(html).rows
                    plain = '\n'.join(' | '.join(row) for row in rows)
                    markdown_rows = ['| ' + ' | '.join(cell.replace('|', r'\|') for cell in row) + ' |'
                                     for row in rows]
                    if rows:
                        markdown_rows.insert(1, '| ' + ' | '.join('---' for _ in rows[0]) + ' |')
                    asset.data = {'html': html, 'markdown': '\n'.join(markdown_rows)}
                    text = '\n\n'.join(t for t in (caption, plain) if t)
                else:
                    info.image_count += 1
                    preceding_figure = asset
                assets.append(asset)
                if text:
                    sections[-1].paragraphs.append(Paragraph(text=text, kind='caption', page=page_no, bbox=bbox))
            elif kind in ('text', 'equation') and text:
                is_caption = kind == 'text' and figure is not None and re.match(r'^(?:Figure|Fig\.)\s+\d+\b',
                                                                               text, re.IGNORECASE)
                if is_caption and figure is not None:
                    figure.caption = '\n'.join(t for t in (figure.caption, text) if t)
                sections[-1].paragraphs.append(Paragraph(
                    text=f'$${_math(text)}$$' if kind == 'equation' else text,
                    kind='equation' if kind == 'equation' else 'caption' if is_caption else 'text',
                    page=page_no, bbox=bbox))
    for page_info in pages:
        page_info.char_count = sum(len(p.text) for s in sections for p in s.paragraphs if p.page == page_info.index)
    doc = ParsedDocument(parser='mineru', parser_version=version, page_count=len(pages), pages=pages,
                         sections=[s for s in sections if s.heading or s.paragraphs],
                         assets=assets, furniture=furniture)
    if not doc.full_text().strip():
        raise ValueError('MinerU returned no body text')
    return doc


class MinerUParser:
    name = 'mineru'

    def __init__(self, paths: Paths, config: Config):
        self.paths, self.config = paths, config
        self.command = find_command(paths, config.parsing)
        self.reasons: list[str] = []

    @property
    def version(self) -> str:
        try:
            return command_version(self.command) if self.command else 'unavailable'
        except (OSError, subprocess.SubprocessError, RuntimeError):
            return 'unavailable'

    def parse(self, pdf_path: Path, out_dir: Path, config: ParsingConfig) -> ParsedDocument:
        if not self.command:
            raise RuntimeError('MinerU not installed; run `litforge parse install-mineru`')
        cfg = self.config.models.servers.get('llamacpp')
        if cfg and cfg.managed:
            server = LlamaCppServer(self.paths.root)
            if server.is_idle(cfg):
                server.stop(cfg, only_if_idle=True)
        with pymupdf.open(pdf_path) as pdf:
            timeout = max(600, len(pdf) * config.mineru.seconds_per_page_timeout)
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / 'middle.json'
        argv = [self.command, 'parse', str(pdf_path), '-o', str(output), '-f', 'middle_json']
        env = child_env(self.paths)
        self.reasons = []
        for attempt in range(2):
            output.unlink(missing_ok=True)
            try:
                result = _run_command(argv, env=env, timeout=timeout, out_dir=out_dir)
            except subprocess.TimeoutExpired as exc:
                raw_tail = exc.stderr or b''
                tail = raw_tail.decode(errors='replace') if isinstance(raw_tail, bytes) else raw_tail
                raise RuntimeError(f'MinerU timed out after {timeout}s: {tail[-4000:]}') from exc
            if result.returncode == 0:
                return map_middle_json(json.loads(output.read_text(encoding='utf-8')), self.version)
            if attempt == 0 and any(s in result.stderr.lower() for s in
                                    ('outofmemory', 'out of memory', 'insufficient memory')):
                env['MINERU_DEVICE_MODE'] = 'cpu'
                self.reasons.append('mineru: retried on CPU after GPU out of memory')
                continue
            raise RuntimeError(f'MinerU exited {result.returncode}: {result.stderr[-4000:]}')
        raise RuntimeError('MinerU failed')
