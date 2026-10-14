from litforge.parsing.base import Asset, PageInfo, Paragraph, ParsedDocument, Section
from litforge.parsing.canonical import to_markdown
from litforge.parsing.vision import clean_markdown, fix_headings, plausible, suspicious_pages


def _page(index: int, lines: int = 40, short: int = 2) -> PageInfo:
    return PageInfo(index=index, label=None, width=595, height=842, char_count=2000, image_count=0,
                    line_count=lines, short_line_count=short)


def _doc() -> ParsedDocument:
    return ParsedDocument(
        parser="pymupdf", parser_version="1", page_count=4,
        pages=[_page(1), _page(2), _page(3, lines=40, short=25), _page(4)],
        sections=[
            Section(heading="Intro", kind="body", paragraphs=[
                Paragraph(text="Plain text on page one.", page=1),
                Paragraph(text="A=\n1\nexp", kind="equation", page=2),
            ]),
            Section(heading="Methods", kind="body", paragraphs=[
                Paragraph(text="Methods text on page two.", page=2),
                Paragraph(text="garbled", page=3),
                Paragraph(text="Clean text on page four.", page=4),
            ]),
        ],
        assets=[Asset(kind="figure", label="Figure 1", caption="Figure 1. A plot.", page=2, bbox=None,
                      path="assets/figure_p002_1.png"),
                Asset(kind="table", label="Table 1", caption="Table 1. Values.", page=4, bbox=None,
                      path="assets/table_p004_1.png")],
    )


def test_suspicious_pages_are_those_with_math_tables_or_broken_lines() -> None:
    # Page 2 has an equation, page 3 mostly fragment lines, page 4 a table; page 1 is clean prose.
    assert suspicious_pages(_doc()) == [2, 3, 4]
    # An equation the parser missed still leaves its number on a line of its own.
    doc = _doc()
    doc.sections[0].paragraphs[0] = Paragraph(text="A=\n\n(2.5)", page=1)
    assert suspicious_pages(doc) == [1, 2, 3, 4]


def test_clean_markdown_unwraps_fences_and_latex_headings() -> None:
    raw = "```markdown\n\\section{Results}\nText $x$.\n\\subsection{2.2 Integrate}\n$$\na=b \\tag{1}\n$$\n```"
    assert clean_markdown(raw) == "## Results\nText $x$.\n### 2.2 Integrate\n$$\na=b \\tag{1}\n$$"


def test_clean_markdown_turns_invented_images_into_figure_markers() -> None:
    raw = ("Text before.\n\n| A | B |\n|---|---|\n| ![](image1.png) | ![](image2.png) |\n\nFigure 3. Panels.\n\n"
           "![Pre Pulse](#)\n\n| x | y |\n|---|---|\n| 1 | 2 |")
    assert clean_markdown(raw) == ("Text before.\n\n[Figure]\n\nFigure 3. Panels.\n\n[Figure]\n\n"
                                   "| x | y |\n|---|---|\n| 1 | 2 |")
    assert clean_markdown("spikes differ (see [Figure] S7).") == "spikes differ (see Figure S7)."


def test_clean_markdown_makes_a_formula_alone_on_its_line_a_display_equation() -> None:
    assert clean_markdown("as\n\n$K_{1}=\\frac{2}{1-r^{2}}$\n\nand") == "as\n\n$$\nK_{1}=\\frac{2}{1-r^{2}}\n$$\n\nand"
    assert clean_markdown("$x=1$ (21)") == "$$\nx=1 \\tag{21}\n$$"
    assert clean_markdown("$a$ and $b$ are") == "$a$ and $b$ are"  # prose with inline math stays


def test_plausible_rejects_a_transcription_that_drops_or_invents_the_page() -> None:
    page_text = ("The membrane potential of each neuron follows the synaptic current received from presynaptic "
                 "cells; spikes are emitted when the threshold is crossed, after which the potential resets. "
                 "Excitatory coupling between oscillators promotes synchrony only when the phase response "
                 "curve has suitable shape, which depends on intrinsic currents and adaptation strength.")
    assert plausible(page_text + " $V$", page_text)
    assert not plausible("A completely different story about cats and dogs.", page_text)
    assert not plausible("", page_text)
    assert plausible("$$x=1$$", "x = 1")  # too little text on the page to compare: accept


def test_to_markdown_uses_page_transcriptions_and_keeps_figure_crops() -> None:
    page = "## Intro\n$$\nA = 1 \\tag{1}\n$$\n\n[Figure]\nFigure 1. A plot.\n\nMore."
    md = to_markdown(_doc(), "T", page_md={2: page})
    assert "$$\nA = 1 \\tag{1}\n$$" in md and "A=\n1\nexp" not in md  # the transcription replaces page 2
    assert "Methods text on page two." not in md
    assert "![Figure 1](assets/figure_p002_1.png)" in md and "[Figure]" not in md  # marker -> the crop
    assert md.index("Plain text on page one.") < md.index("A = 1") < md.index("garbled") < md.index("page four")
    assert md.count("## Methods") == 1  # the section starting on page 2 keeps its heading for later pages
    assert md.count("<!-- page 2 -->") == 1


def test_fix_headings_drops_invented_ones_and_restores_numbers() -> None:
    page_text = ("the summation being performed over all the spikes\n2.2 Integrate-and-Fire Neurons. Another class of\n"
                 "models commonly used")
    md = ("# Synaptic Interaction and Neuronal Models\n\n## Synaptic Interaction\n\nThe summation being performed.\n\n"
          "## Integrate-and-Fire Neurons\n\nAnother class of models.")
    expected = "The summation being performed.\n\n## 2.2 Integrate-and-Fire Neurons\n\nAnother class of models."
    assert fix_headings(md, page_text) == expected
    assert fix_headings("# Results\nText", "3 Results\nText") == "## 3 Results\nText"  # one title per note: no H1
    # Content the model put on a heading line (a caption, an equation) stays, as plain text.
    caption = "Figure 6: Firing times for a fully connected network of 100 neurons with excitatory coupling"
    assert fix_headings(f"## {caption}", "unrelated page text") == caption
    assert fix_headings("# (STA) $$s(t)=1$$", "unrelated page text") == "(STA) $$s(t)=1$$"
