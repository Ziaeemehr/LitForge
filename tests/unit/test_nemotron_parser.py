from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from litforge.config import ParsingConfig, Paths
from litforge.parsing import nemotron
from litforge.parsing.nemotron_parser import NemotronParser, PersianDocument


class FakeEngine:
    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls = 0

    def read(self, png: bytes) -> tuple[str, tuple[int, int]]:
        image = pymupdf.Pixmap(png)
        assert image.width <= nemotron.MAX_SIZE[0] and image.height <= nemotron.MAX_SIZE[1]
        raw = str(self.calls)
        self.calls += 1
        return raw, (image.width, image.height)

    def blocks(self, raw: str, size: tuple[int, int]) -> list[dict[str, Any]]:
        return self.pages[int(raw)]


def block(cls: str, content: str = '', y: float = 0.2) -> dict[str, Any]:
    return {'class': cls, 'content': content, 'text': content, 'bbox': [0.1, y, 0.9, y + 0.1]}


@pytest.fixture()
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / 'paper.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((60, 240), 'Synthetic figure')
        doc.save(path)
    return path


def test_structure(pdf: Path, tmp_path: Path) -> None:
    html = '<table><tr><th>Method</th><th>Score</th></tr><tr><td>A</td><td>1</td></tr></table>'
    merged = '<table><tr><td colspan="2">Merged</td></tr></table>'
    engine = FakeEngine([[block('Page-header', 'Journal'), block('Title', 'A title'),
                          block('Section-header', '## **Abstract**'), block('Text', r'Value \(x\)'),
                          block('Section-header', '### _Methods_'), block('Formula', r'\[x=2\] (3)'),
                          block('Formula', 'ordinary prose'), block('Table', html, 0.4),
                          block('Caption', 'Table 1: Scores', 0.51), block('Table', merged, 0.7),
                          block('Picture', y=0.2), block('Caption', 'Fig. 2: Example', 0.31),
                          block('Caption', 'An ordinary note'), block('Page-footer', '1')]])
    progress: list[str] = []
    parser = NemotronParser(Paths(tmp_path), engine, progress.append)
    out = tmp_path / 'out'
    doc = parser.parse(pdf, out, ParsingConfig())
    assert doc.parser == 'nemotron' and doc.parser_version == nemotron.NAME
    assert [(s.heading, s.kind, s.level) for s in doc.sections] == [
        (None, 'front', 1), ('Abstract', 'abstract', 1), ('Methods', 'body', 2)]
    assert doc.sections[0].paragraphs[0].text == 'A title'
    assert doc.sections[1].paragraphs[0].text == 'Value $x$'
    assert doc.sections[1].paragraphs[0].bbox == pytest.approx((60, 160, 540, 240))
    equation = next(a for a in doc.assets if a.kind == 'equation')
    assert equation.data == {'latex': 'x=2'}
    assert r'$$x=2 \tag{3}$$' in doc.full_text()
    tables = [a for a in doc.assets if a.kind == 'table']
    assert tables[0].data == {'html': html, 'markdown': nemotron.table_markdown(html)}
    assert tables[0].caption == 'Table 1: Scores'
    assert 'Method | Score\nA | 1' in doc.full_text()
    assert tables[1].data == {'html': merged, 'markdown': merged}
    figure = next(a for a in doc.assets if a.kind == 'figure')
    assert figure.path == 'assets/figure_p001_1.png'
    assert (out / figure.path).is_file()
    assert figure.caption == 'Fig. 2: Example' and figure.quality == 'uncertain'
    assert doc.furniture == ['Journal', '1']
    assert doc.sections[-1].paragraphs[-1].kind == 'text'
    assert doc.pages[0].image_count == 1 and doc.pages[0].column_count == 1
    assert doc.pages[0].char_count == sum(len(p.text) for s in doc.sections for p in s.paragraphs)
    assert parser.parse(pdf, out, ParsingConfig()) == doc
    assert engine.calls == 1 and progress == ['page 1/1', 'page 1/1']
    cache = json.loads((out / 'nemotron.json').read_text())
    assert cache['revision'] == nemotron.REVISION and cache['pages']['1']['raw'] == '0'


@pytest.mark.parametrize(('blocks', 'error'), [
    ([block('Text', 'این یک متن فارسی برای بررسی زبان سند است و باید تشخیص داده شود')], PersianDocument),
    ([block('Page-header', 'Only furniture')], ValueError),
    ([], ValueError),
])
def test_rejected_documents(pdf: Path, tmp_path: Path, blocks: list[dict[str, Any]],
                            error: type[Exception]) -> None:
    with pytest.raises(error):
        NemotronParser(Paths(tmp_path), FakeEngine([blocks])).parse(pdf, tmp_path / 'out', ParsingConfig())


def test_resume_and_revision(pdf: Path, tmp_path: Path) -> None:
    with pymupdf.open(pdf) as doc:
        doc.new_page(width=800, height=600)
        doc.save(tmp_path / 'two.pdf')
    out = tmp_path / 'out'
    out.mkdir()
    cache_path = out / 'nemotron.json'
    cache_path.write_text(json.dumps({'revision': nemotron.REVISION,
                                      'pages': {'1': {'raw': '0', 'size': [1500, 2000]}}}))
    engine = FakeEngine([[block('Text', 'Cached body')]])
    doc = NemotronParser(Paths(tmp_path), engine).parse(tmp_path / 'two.pdf', out, ParsingConfig())
    assert doc.page_count == 2 and engine.calls == 1
    assert set(json.loads(cache_path.read_text())['pages']) == {'1', '2'}
    cache_path.write_text(json.dumps({'revision': 'old', 'pages': {'1': {'raw': 'invalid', 'size': [1, 1]}}}))
    fresh = FakeEngine([[block('Text', 'Fresh body')]])
    doc = NemotronParser(Paths(tmp_path), fresh).parse(pdf, out, ParsingConfig())
    assert doc.full_text() == 'Fresh body' and fresh.calls == 1


def test_interrupted_parse(pdf: Path, tmp_path: Path) -> None:
    class InterruptedEngine(FakeEngine):
        def read(self, png: bytes) -> tuple[str, tuple[int, int]]:
            if self.calls:
                raise RuntimeError('interrupted')
            return super().read(png)

    with pymupdf.open(pdf) as doc:
        doc.new_page()
        doc.save(tmp_path / 'two.pdf')
    out = tmp_path / 'out'
    engine = InterruptedEngine([[block('Text', 'First page')]])
    with pytest.raises(RuntimeError, match='interrupted'):
        NemotronParser(Paths(tmp_path), engine).parse(tmp_path / 'two.pdf', out, ParsingConfig())
    assert set(json.loads((out / 'nemotron.json').read_text())['pages']) == {'1'}
    resumed = FakeEngine([[block('Text', 'Body')]])
    result = NemotronParser(Paths(tmp_path), resumed).parse(tmp_path / 'two.pdf', out, ParsingConfig())
    assert result.page_count == 2 and resumed.calls == 1


def test_lazy_engine(pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine([[block('Text', 'Body')]])
    created: list[Paths] = []

    def create(paths: Paths) -> FakeEngine:
        created.append(paths)
        return engine

    monkeypatch.setattr(nemotron, 'NemotronEngine', create)
    parser = NemotronParser(Paths(tmp_path))
    assert not created
    parser.parse(pdf, tmp_path / 'out', ParsingConfig())
    assert len(created) == 1


def test_caption_before_asset_and_text_classes(pdf: Path, tmp_path: Path) -> None:
    blocks = [block('Caption', 'FIGURE 1: Above', 0.1), block('Chart', y=0.21),
              block('Section-header', '# References'), block('Title', 'Late title')]
    blocks.extend(block(cls, r'Content \(x\)') for cls in ('Text', 'List-item', 'Footnote', 'Code'))
    doc = NemotronParser(Paths(tmp_path), FakeEngine([blocks])).parse(
        pdf, tmp_path / 'out', ParsingConfig())
    assert doc.assets[0].caption == 'FIGURE 1: Above'
    assert doc.sections[-1].kind == 'references' and doc.sections[-1].level == 1
    assert doc.sections[0].heading is None and doc.sections[0].paragraphs[-1].text == 'Late title'
    assert all(p.kind == 'text' and p.text == 'Content $x$' for p in doc.sections[-1].paragraphs)
