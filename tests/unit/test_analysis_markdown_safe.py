from litforge.analysis.markdown_safe import excerpt, inert


def test_inert_escapes_active_syntax_and_keeps_one_line() -> None:
    assert inert("<tag> [link](url) `x` | **b** &lt;\n# heading") == (
        r"\<tag\> \[link\]\(url\) \`x\` \| \*\*b\*\* \&lt; \# heading"
    )
    assert inert(r"\[link](url)") == r"\\\[link\]\(url\)"


def test_excerpt_caps_at_a_word_boundary() -> None:
    text = "word " * 100
    quote = excerpt(text)
    assert len(quote) <= 400
    assert quote.endswith("word…")
    assert excerpt("x" * 400) == "x" * 400
    assert excerpt("x" * 401) == "…"
    assert excerpt("short\nquote") == "short quote"
