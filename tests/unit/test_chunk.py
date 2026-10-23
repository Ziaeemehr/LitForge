from litforge.indexing.chunk import approx_tokens, chunk_document
from litforge.parsing.base import PageInfo, Paragraph, ParsedDocument, Section

SENT = "The model was fitted to data with careful validation. "


def _doc(sections: list[Section]) -> ParsedDocument:
    pages = [PageInfo(index=i, label=None, width=595, height=842, char_count=1000, image_count=0)
             for i in (1, 2, 3)]
    return ParsedDocument(parser="t", parser_version="1", page_count=3, pages=pages, sections=sections)


def test_chunks_respect_target_and_overlap() -> None:
    paras = [Paragraph(text=f"P{i}. " + SENT * 6, page=1 + i % 3) for i in range(20)]
    doc = _doc([Section(heading="2 Methods", kind="body", paragraphs=paras)])
    chunks = chunk_document(doc, "k", target=200, overlap=40)
    assert len(chunks) > 3
    assert all(c.token_count <= 200 + 40 + approx_tokens(SENT) for c in chunks)
    # Overlap: each chunk after the first starts with the tail of the previous one.
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.text.split("\n\n")[0] in prev.text
    assert all(c.section_path == "2 Methods" for c in chunks)


def test_page_spans_and_long_paragraph_split() -> None:
    long_para = Paragraph(text=SENT * 100, page=2)
    doc = _doc([Section(heading="Results", kind="body", paragraphs=[long_para])])
    chunks = chunk_document(doc, "k", target=150, overlap=0)
    assert len(chunks) > 5
    assert all(c.page_start == c.page_end == 2 for c in chunks)
    assert all(s.page == 2 for c in chunks for s in c.spans)


def test_references_skipped_and_captions_separate() -> None:
    doc = _doc([
        Section(heading="Intro", kind="body", paragraphs=[
            Paragraph(text="Body text here.", page=1),
            Paragraph(text="Figure 1: A caption.", kind="caption", page=1),
        ]),
        Section(heading="References", kind="references",
                paragraphs=[Paragraph(text="[1] Someone 2020.", page=3)]),
    ])
    chunks = chunk_document(doc, "k")
    assert [c.kind for c in chunks] == ["text", "caption"]
    with_refs = chunk_document(doc, "k", index_references=True)
    assert with_refs[-1].kind == "references"


def test_ids_are_stable_and_key_dependent() -> None:
    doc = _doc([Section(heading="A", kind="body", paragraphs=[Paragraph(text="x " * 50, page=1)])])
    assert [c.id for c in chunk_document(doc, "k1")] == [c.id for c in chunk_document(doc, "k1")]
    assert chunk_document(doc, "k1")[0].id != chunk_document(doc, "k2")[0].id


def test_nested_section_paths() -> None:
    doc = _doc([
        Section(heading="2 Methods", kind="body", level=1, paragraphs=[Paragraph(text="a", page=1)]),
        Section(heading="2.1 Model", kind="body", level=2, paragraphs=[Paragraph(text="b", page=1)]),
        Section(heading="3 Results", kind="body", level=1, paragraphs=[Paragraph(text="c", page=1)]),
    ])
    assert [c.section_path for c in chunk_document(doc, "k")] == [
        "2 Methods", "2 Methods > 2.1 Model", "3 Results"]


def test_ligatures_spelled_out_with_aligned_spans() -> None:
    doc = _doc([Section(heading="Signiﬁcance", kind="body", paragraphs=[
        Paragraph(text="An eﬃcient ﬂow.", page=1), Paragraph(text="Deﬁnitely.", page=2)])])
    (chunk,) = chunk_document(doc, "k")
    assert chunk.text == "An efficient flow.\n\nDefinitely." and chunk.section_path == "Significance"
    assert [chunk.text[s.start:s.end] for s in chunk.spans] == ["An efficient flow.", "Definitely."]
