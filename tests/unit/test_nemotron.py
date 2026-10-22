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
