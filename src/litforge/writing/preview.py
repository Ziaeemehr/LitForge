"""Isolated, cached math previews and document figure reads."""

from __future__ import annotations

import hashlib
import re
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from litforge.config import Paths
from litforge.writing import compile as wc
from litforge.writing.documents import MAX_FIGURE_BYTES, Document, DocumentError, resolve_inside

_LOCK = threading.Lock()
_PACKAGES = {'amsmath', 'amssymb', 'amsfonts', 'mathtools', 'bm', 'physics', 'siunitx',
             'mathrsfs', 'esint', 'upgreek'}
_REFUSED = re.compile(
    r'\\(?:input|include|openin|openout|write|read|immediate|catcode|csname|usepackage|documentclass|special|'
    r'directlua|includegraphics|jobname|loop|def|gdef|edef|xdef)(?![a-zA-Z])|\\(?:begin|end)\s*\{document\}')
_DEFINITION = re.compile(r'\\(?:newcommand|renewcommand|providecommand|DeclareMathOperator)\*?(?![a-zA-Z])')
_PACKAGE = re.compile(r'\\usepackage(?:\[[^\]\n]*\])?\{([a-zA-Z, ]+)\}\s*(?:%.*)?$')
_SECOND = re.compile(r'(?<!\\)(?:\\\\)*(?:\$|\\[()[\]])|\\(?:begin|end)\s*\{(?:equation|align|gather|multline|'
                     r'eqnarray|displaymath|math|document)\*?\}')
_ENV = re.compile(r'\\begin\{(equation\*?|align\*?|gather\*?|multline\*?|eqnarray\*?|displaymath\*?|math\*?)\}'
                  r'(.*?)\\end\{\1\}', re.S)


def source_for(doc: Document, tex: str) -> str:
    tex = tex.strip()
    if not tex or len(tex) > 4000 or _REFUSED.search(tex):
        raise DocumentError('invalid or unsafe math preview')
    inner = None
    for start, end in [('$$', '$$'), ('$', '$'), (r'\(', r'\)'), (r'\[', r'\]')]:
        if tex.startswith(start) and tex.endswith(end) and len(tex) > len(start) + len(end):
            inner = tex[len(start):-len(end)]
            break
    if inner is None:
        match = _ENV.fullmatch(tex)
        if match:
            inner = match[2]
    # One formula: no second delimiter (after an even run of backslashes: \\[2pt] in align is a line break) and no
    # second display environment; matrices and cases inside it are fine.
    if inner is None or _SECOND.search(inner):
        raise DocumentError('select one complete math formula')
    preamble = resolve_inside(doc.folder, doc.main_file).read_text(encoding='utf-8', errors='replace')
    copied = []
    for raw in preamble.splitlines():
        line = raw.strip()
        if r'\begin{document}' in line and not _DEFINITION.match(line):
            break
        if match := _PACKAGE.fullmatch(line):
            packages = [p.strip() for p in match[1].split(',')]
            if all(p in _PACKAGES for p in packages):
                copied.append(r'\usepackage{' + ','.join(packages) + '}')
        elif _DEFINITION.match(line) and len(line) <= 300:
            if _REFUSED.search(line):
                raise DocumentError('unsafe command in preview macros')
            copied.append(line)
    return ('\\documentclass[preview,border=2pt,varwidth]{standalone}\n'
            '\\usepackage{amsmath,amssymb}\n' + '\n'.join(copied) +
            '\n\\begin{document}\n' + tex + '\n\\end{document}\n')


def preview_math(doc: Document, paths: Paths, tex: str) -> bytes:
    if len(tex) > 4000:
        raise DocumentError('math preview is limited to 4000 characters')
    source = source_for(doc, tex)
    cache = getattr(paths, 'cache', paths.root / 'cache') / 'write-preview'
    with _LOCK:
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / (hashlib.sha256(source.encode()).hexdigest() + '.pdf')
        if target.is_file():
            return target.read_bytes()
        with tempfile.TemporaryDirectory(prefix='litforge-preview-') as folder:
            snippet = replace(doc, folder=Path(folder), main_file='snippet.tex', draft=True)
            (snippet.folder / snippet.main_file).write_text(source, encoding='utf-8')
            problems = wc.scan_paths(snippet)
            if problems:
                raise DocumentError(problems[0].message)
            command = wc.tectonic_command(paths) if doc.engine == 'tectonic' else wc.xelatex_command()
            if command is None:
                raise DocumentError('install Tectonic in Settings → Optional tools to preview math')
            result = wc.compile_document(snippet, paths, timeout_s=30)
            if not result.ok or not result.pdf:
                raise DocumentError(result.errors[0].message if result.errors else 'math preview failed')
            data = (snippet.folder / result.pdf).read_bytes()
        target.write_bytes(data)
        files = sorted(cache.glob('*.pdf'), key=lambda p: p.stat().st_mtime_ns)
        for old in files[:-500]:
            old.unlink(missing_ok=True)
        return data


def figure(doc: Document, name: str) -> tuple[bytes, str, str]:
    path = resolve_inside(doc.folder, name)
    types = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}
    if not path.suffix:
        for suffix in types:
            candidate = resolve_inside(doc.folder, name + suffix)
            if candidate.is_file():
                path = candidate
                break
    elif path.suffix.lower() not in types:
        raise DocumentError('figure previews must be PNG, JPEG or PDF')
    if not path.is_file():
        raise LookupError('figure not found')
    if path.stat().st_size > MAX_FIGURE_BYTES:
        raise DocumentError('the figure is larger than 25 MB')
    return path.read_bytes(), types[path.suffix.lower()], 'inline'
