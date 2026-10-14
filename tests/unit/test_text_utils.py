from litforge.indexing.fts import build_match
from litforge.metadata.extract import normalize_doi, normalize_title
from litforge.parsing.pymupdf_parser import HEADING_WORDS_RE, is_caption, join_lines


def test_normalize_doi() -> None:
    assert normalize_doi("https://doi.org/10.1000/ABC.12).") == "10.1000/abc.12"
    assert normalize_doi("doi: 10.1/x;") == "10.1/x"


def test_normalize_title() -> None:
    assert normalize_title("Neural-Mass  Models: A Review!") == "neural mass models a review"


def test_dehyphenation() -> None:
    assert join_lines(["simu-", "lation based", "Jansen-", "Rit model"]) == "simulation based Jansen-Rit model"


def test_caption_and_heading_patterns() -> None:
    assert is_caption("Figure 3: Posterior")
    assert is_caption("Fig. 2. Results")
    assert is_caption("Table 1 | Summary")
    assert is_caption("Table S1 Parameters", first_line_bold=True)
    assert not is_caption("Table S1 Parameters")
    assert not is_caption("Table 3 shows that all three options are better")  # prose, seen in ResNet
    assert not is_caption("Table 3.2 lists")
    assert not is_caption("Figures show that the model")
    assert HEADING_WORDS_RE.match("2 Methods")
    assert HEADING_WORDS_RE.match("References")
    assert HEADING_WORDS_RE.match("IV. RESULTS AND DISCUSSION")
    assert not HEADING_WORDS_RE.match("Methods were applied to all subjects")


def test_build_match_is_injection_safe() -> None:
    match, terms = build_match('neural mass AND "Jansen-Rit model" -x NEAR(')
    assert match == '"neural" "mass" "AND" "Jansen Rit model" "x" "NEAR"'
    assert terms[:2] == ["neural", "mass"]
    assert build_match("  ") == ("", [])
