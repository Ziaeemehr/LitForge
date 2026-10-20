from __future__ import annotations

import json
from dataclasses import replace

import pytest

from litforge.writing import compile as wc
from litforge.writing import preview as wp
from litforge.writing.documents import DocumentError
from tests.unit.test_writing_compile import env as _env

env = _env


@pytest.mark.parametrize('tex', ['$x$', '$$x$$', r'\(x\)', r'\[x\]'] + [
    rf'\begin{{{name}{star}}}x\end{{{name}{star}}}'
    for name in ('equation', 'align', 'gather', 'multline', 'eqnarray', 'displaymath', 'math') for star in ('', '*')])
def test_forms(env, tex):
    _, _, doc, _, _ = env
    assert tex in wp.source_for(doc, tex)


@pytest.mark.parametrize('command', [
    'input', 'include', 'openin', 'openout', 'write', 'read', 'immediate', 'catcode', 'csname', 'usepackage',
    'documentclass', 'special', 'directlua', 'includegraphics', 'jobname', 'loop', 'def', 'gdef', 'edef', 'xdef',
    'begin{document}', 'end{document}'])
def test_refused_commands(env, command):
    _, _, doc, _, _ = env
    with pytest.raises(DocumentError):
        wp.source_for(doc, '$\\' + command + '{x}$')
    (doc.folder / doc.main_file).write_text('\\newcommand{\\foo}{\\' + command + '{x}}\n')
    with pytest.raises(DocumentError):
        wp.source_for(doc, '$x$')


@pytest.mark.parametrize('tex', ['x', '$x$ $y$', '$$x$', r'\begin{itemize}x\end{itemize}', '$' + 'x' * 4000 + '$'])
def test_bad_forms(env, tex):
    with pytest.raises(DocumentError):
        wp.source_for(env[2], tex)


def test_source_macros_packages(env):
    _, _, doc, _, _ = env
    doc = replace(doc, main_file='other.tex')
    (doc.folder / doc.main_file).write_text(
        '\\usepackage[foo]{bm,physics}\n\\usepackage{evil}\n'
        '\\newcommand{\\foo}{x}\n\\DeclareMathOperator*{\\argmax}{argmax}\n'
        '\\newcommand{\\long}{' + 'x' * 300 + '}\n\\begin{document}\n\\newcommand{\\after}{y}')
    source = wp.source_for(doc, '$x$')
    assert source.startswith(r'\documentclass[preview,border=2pt,varwidth]{standalone}')
    assert r'\usepackage{bm,physics}' in source and 'evil' not in source
    assert r'\newcommand{\foo}{x}' in source and r'\DeclareMathOperator*' in source
    assert r'\long' not in source and r'\after' not in source


def test_compile_cache_and_pruning(env, monkeypatch):
    _, paths, doc, record, _ = env
    original = wc.compile_document
    seen = []

    def compile(doc, paths, **kwargs):
        seen.append((doc, (doc.folder / doc.main_file).read_text(), kwargs))
        return original(doc, paths, **kwargs)

    monkeypatch.setattr(wc, 'compile_document', compile)
    cache = paths.root / 'cache' / 'write-preview'
    cache.mkdir(parents=True)
    for i in range(501):
        (cache / f'{i}.pdf').write_bytes(b'old')
    assert wp.preview_math(doc, paths, '$x$').startswith(b'%PDF')
    assert len(list(cache.glob('*.pdf'))) == 500
    assert seen[0][0].draft and seen[0][0].main_file == 'snippet.tex'
    assert seen[0][2] == {'timeout_s': wp.TIMEOUT_S}
    assert '$x$' in seen[0][1]
    assert '--reruns' in json.loads(record.read_text())['argv']
    record.unlink()
    assert wp.preview_math(doc, paths, '$x$').startswith(b'%PDF')
    assert not record.exists() and len(seen) == 1


def test_missing_and_error(env, monkeypatch):
    _, paths, doc, _, mode = env
    mode.write_text('error')
    with pytest.raises(DocumentError, match='Undefined control sequence'):
        wp.preview_math(doc, paths, '$x$')
    monkeypatch.setattr(wc, 'tectonic_command', lambda _: None)
    with pytest.raises(DocumentError, match='install Tectonic in Settings'):
        wp.preview_math(doc, paths, '$x$')


def test_path_scan(env):
    _, paths, doc, _, _ = env
    with pytest.raises(DocumentError, match='outside'):
        wp.preview_math(doc, paths, r'$\bibliography{/etc/passwd}$')


@pytest.mark.parametrize('tex', [r'\[ \begin{pmatrix} a & b \\ c & d \end{pmatrix} \]',
                                 r'\begin{equation} f(x) = \begin{cases} 1 & x > 0 \\ 0 & \text{else} \end{cases}'
                                 r' \end{equation}',
                                 r'\begin{align} A &= \begin{bmatrix} 1 \end{bmatrix} \\[2pt] B &= 2 \end{align}'])
def test_matrices_and_cases_inside_a_formula_are_previewed(env, tex):
    # Common in real equations; a nested environment is not a second formula.
    assert tex in wp.source_for(env[2], tex)


@pytest.mark.parametrize('tex', [r'$\^^69nput{x}$', r'$\@@input x$', r'$\InputIfFileExists{x}{}{}$',
                                 r'$\verbatiminput{x}$', r'$\lstinputlisting{x}$', r'$\RequirePackage{x}$',
                                 r'$\makeatletter x$', r'$\ExplSyntaxOn x$', r'$\newread\r x$', r'$\endinput$',
                                 r'$\pdffilesize{x}$', r'$\openin x$', r'$\IfFileExists{x}{}{}$', r'$\LoadClass{x}$'])
def test_encoded_and_file_reading_commands_are_refused(env, tex):
    # ^^69 is "i" in TeX's byte notation, so \^^69nput is \input; names mentioning files are refused outright.
    with pytest.raises(DocumentError):
        wp.source_for(env[2], tex)


def test_a_cache_hit_does_not_wait_for_a_running_compile(env, monkeypatch):
    _, paths, doc, _, _ = env
    first = wp.preview_math(doc, paths, '$x$')
    assert wp._LOCK.acquire(timeout=1)  # a long compile holds the lock
    try:
        assert wp.preview_math(doc, paths, '$x$') == first
    finally:
        wp._LOCK.release()


def test_a_preview_gives_up_waiting_for_a_stuck_one(env, monkeypatch):
    _, paths, doc, _, _ = env
    monkeypatch.setattr(wp, 'WAIT_S', 0.2)
    assert wp._LOCK.acquire(timeout=1)
    try:
        with pytest.raises(DocumentError, match='still rendering'):
            wp.preview_math(doc, paths, '$y$')
    finally:
        wp._LOCK.release()


def test_ordinary_macro_names_are_not_refused(env):
    (env[2].folder / env[2].main_file).write_text('\\newcommand{\\evaluate}{E}\n\\begin{document}\n')
    assert '\\evaluate' in wp.source_for(env[2], '$\\evaluate$')
