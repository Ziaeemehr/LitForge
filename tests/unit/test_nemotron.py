from litforge.parsing import nemotron


def test_is_persian() -> None:
    assert nemotron.is_persian('123 x') is None
    assert nemotron.is_persian('This is a sufficiently long English sentence.') is False
    assert nemotron.is_persian('این یک متن فارسی است که تعداد کافی حروف دارد') is True
    assert nemotron.is_persian('ﺍ' * 25) is True


def test_inline_math() -> None:
    assert nemotron.inline_math(r'A \( x+1 \) and \[ y=2 \]') == 'A $x+1$ and $$y=2$$'
    assert nemotron.inline_math('plain $x$') == 'plain $x$'


def test_table_markdown() -> None:
    assert nemotron.table_markdown('<table><tr><td>A|B</td><td>C<br>D</td></tr></table>') == (
        '| A\\|B | C D |\n| --- | --- |')
    html = '<table><tr><td rowspan="2">A</td></tr></table>'
    assert nemotron.table_markdown(html) == html
    assert nemotron.table_markdown('<table></table>') == ''
    assert nemotron.table_markdown('plain') == 'plain'


def test_blocks_markdown() -> None:
    blocks = [{'class': cls, 'content': text} for cls, text in [
        ('Page-header', 'skip'), ('Page-footer', 'skip too'), ('Title', 'Title'),
        ('Section-header', '## Heading'), ('Text', r'Text \(x\)'), ('Picture', 'ignore'),
        ('Chart', ''), ('Formula', r'\[x=1\] (2)'), ('Formula', 'prose'),
        ('Table', '<table><tr><td>A</td></tr></table>'), ('Text', '')]]
    assert nemotron.blocks_markdown(blocks) == (
        '# Title\n\n## Heading\n\nText $x$\n\n[Figure]\n\n[Figure]\n\n'
        '$$\nx=1 \\tag{2}\n$$\n\nprose\n\n| A |\n| --- |')


def test_release_works_while_a_page_read_holds_the_model_lock() -> None:
    # read() holds the lock while _load() releases an older model: a plain Lock deadlocked here.
    with nemotron._lock:
        nemotron._loaded.update(folder="old", device="cpu")
        nemotron.release()
    assert nemotron._loaded == {}


def test_escaped_brackets_are_citations_unless_they_hold_latex() -> None:
    assert nemotron.inline_math(r"BN \[16\], see \[41, 13\]") == "BN [16], see [41, 13]"
    assert nemotron.inline_math(r"\[x^2 = y\] and \(\alpha\)") == r"$$x^2 = y$$ and $\alpha$"


def test_prose_joins_the_page_lines_and_repairs_hyphenation() -> None:
    assert nemotron.prose("continuous transfor-\nmations of\nJansen-\nRit") == \
        "continuous transformations of Jansen-Rit"


def test_tidy_formula_closes_environments_and_collapses_repeated_rows() -> None:
    row = r"Attention(Q,K,V)=softmax(\frac{QK^T}{\sqrt{d_k}})V"
    assert nemotron.tidy_formula("\\begin{array}{ c }\n" + row + " \\\\\n" + row) == row
    assert nemotron.tidy_formula(r"\begin{aligned} a &= b") == "\\begin{aligned} a &= b\n\\end{aligned}"
    assert nemotron.tidy_formula(r"\begin{array}{c} a \\ b \end{array}") == r"\begin{array}{c} a \\ b \end{array}"


def test_formula_latex_reads_one_or_several_spans_and_the_number() -> None:
    assert nemotron.formula_latex(r"\(x=1\) (3)") == ("x=1", "3")
    assert nemotron.formula_latex(r"\(a=1\)<br>\(b=2\)") == ("\\begin{gathered}\na=1 \\\\\nb=2\n\\end{gathered}", None)
    assert nemotron.formula_latex(r"\(L=1\) (ODESolve(z)) (3)") is None  # prose mixed in: kept as text
    assert nemotron.formula_latex("m0 ← 0 (Initialize 1st moment vector)") is None
