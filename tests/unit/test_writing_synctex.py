"""SyncTeX lookups against a real Tectonic output (tests/fixtures/synctex: en.main.tex compiled with --synctex,
folder renamed to /doc). Word positions were read from the PDF with pdftotext -bbox."""
from __future__ import annotations

from pathlib import Path

import pytest

from litforge.writing import synctex

FIXTURE = Path(__file__).parents[1] / "fixtures" / "synctex" / "en.synctex.gz"


@pytest.fixture(scope="module")
def sync() -> synctex.SyncTex:
    return synctex.load(FIXTURE, Path("/doc"))


def covers(box: dict[str, float], x: float, y: float) -> bool:
    return box["x"] - 1 <= x <= box["x"] + box["w"] + 1 and box["y"] - 1 <= y <= box["y"] + box["h"] + 1


def test_inputs_are_document_files_only(sync) -> None:
    assert sorted(sync.files.values()) == ["main.tex", "methods.tex"]


@pytest.mark.parametrize("file, line, x, y", [
    ("main.tex", 4, 140, 153),        # "Alpha" (first line of the paragraph)
    ("main.tex", 5, 340, 165),        # "omega." on the second visual line
    ("main.tex", 7, 160, 189),        # "Third"
    ("main.tex", 9, 290, 212),        # E = mc^2 (no records on line 9: the nearest later line is used)
    ("methods.tex", 1, 180, 288),     # text from \input{methods}
])
def test_forward_highlights_the_line_in_the_pdf(sync, file: str, line: int, x: float, y: float) -> None:
    hit = synctex.forward(sync, file, line)
    assert hit is not None and hit["page"] == 1
    assert any(covers(b, x, y) for b in hit["boxes"]), hit


def test_forward_unknown_file_or_line(sync) -> None:
    assert synctex.forward(sync, "nope.tex", 3) is None
    assert synctex.forward(sync, "main.tex", 999) is not None  # past the end: the nearest line before it


@pytest.mark.parametrize("x, y, file, line", [
    (145, 153, "main.tex", 4),
    (340, 165, "main.tex", 5),
    (160, 189, "main.tex", 7),
    (290, 212, "main.tex", 10),
    (180, 288, "methods.tex", 1),
])
def test_inverse_finds_the_source_line(sync, x: float, y: float, file: str, line: int) -> None:
    assert synctex.inverse(sync, 1, x, y) == {"file": file, "line": line}


def test_inverse_in_the_margin_takes_the_nearest_line(sync) -> None:
    assert synctex.inverse(sync, 1, 30, 189) == {"file": "main.tex", "line": 7}
    assert synctex.inverse(sync, 9, 100, 100) is None


def test_inputs_outside_the_folder_are_ignored(tmp_path: Path) -> None:
    import gzip
    data = ("SyncTeX Version:1\nInput:1:/elsewhere/x.tex\nInput:2:/doc/../etc/y.tex\nInput:3:/doc/a.tex\n"
            "Input:4:/doc/.build/main.bbl\n"  # the bibliography TeX writes: the editor cannot open it"
            "Output:pdf\nMagnification:1000\nUnit:1\nX Offset:0\nY Offset:0\nContent:\n{1\n"
            "(3,1:6578176,6578176:6578176,657817,0\ng3,1:6578176,6578176\n)\n}1\n")
    path = tmp_path / "s.synctex.gz"
    path.write_bytes(gzip.compress(data.encode()))
    sync = synctex.load(path, Path("/doc"))
    assert sync.files == {3: "a.tex"}
    assert synctex.inverse(sync, 1, 150, 99) == {"file": "a.tex", "line": 1}


def test_forward_drops_boxes_inside_other_boxes(tmp_path: Path) -> None:
    import gzip
    sp = 65781.76

    def n(v: float) -> int:
        return round(v * sp)

    # one 300 pt wide line at baseline 100 and a subscript a bit lower, both from line 2 (seen with math in a
    # Persian paragraph: the subscript got its own small highlight on top of the line's)
    data = ("SyncTeX Version:1\nInput:1:/doc/main.tex\nOutput:pdf\nMagnification:1000\nUnit:1\nX Offset:0\n"
            "Y Offset:0\nContent:\n{1\n"
            f"(1,3:{n(100)},{n(100)}:{n(300)},{n(8)},{n(2)}\n"
            f"g1,2:{n(120)},{n(100)}\n"
            f"(1,2:{n(200)},{n(101.5)}:{n(10)},{n(4)},{n(1)}\n"
            f"k1,2:{n(205)},{n(101.5)}:{n(1)}\n"
            ")\n)\n}1\n")
    path = tmp_path / "s.synctex.gz"
    path.write_bytes(gzip.compress(data.encode()))
    hit = synctex.forward(synctex.load(path, Path("/doc")), "main.tex", 2)
    assert hit is not None and len(hit["boxes"]) == 1 and hit["boxes"][0]["w"] == 300


def _write(tmp_path: Path, body: str, head: str = "") -> Path:
    import gzip
    data = ("SyncTeX Version:1\nInput:1:/doc/main.tex\nOutput:pdf\n" + (head or "Magnification:1000\nUnit:1\n"
            "X Offset:0\nY Offset:0\n") + "Content:\n{1\n" + body + "}1\n")
    path = tmp_path / "s.synctex.gz"
    path.write_bytes(gzip.compress(data.encode()))
    return path


def test_size_limit_is_checked_while_decompressing(tmp_path: Path) -> None:
    import gzip
    path = tmp_path / "bomb.synctex.gz"
    path.write_bytes(gzip.compress(b"x" * 5000))  # small file, large content
    with pytest.raises(synctex.SyncTexError, match="too large"):
        synctex.load(path, Path("/doc"), max_bytes=1000)


def test_truncated_or_broken_files_raise_a_clear_error(tmp_path: Path) -> None:
    import gzip
    path = tmp_path / "half.synctex.gz"
    path.write_bytes(gzip.compress(b"SyncTeX Version:1\n" * 200)[:40])  # being written
    with pytest.raises(synctex.SyncTexError):
        synctex.load(path, Path("/doc"))


def test_relative_inputs_are_read_against_the_folder(tmp_path: Path) -> None:
    import gzip
    data = ("SyncTeX Version:1\nInput:1:./main.tex\nInput:2:sub/part.tex\nInput:3:../out.tex\nOutput:pdf\n"
            "Magnification:1000\nUnit:1\nX Offset:0\nY Offset:0\nContent:\n{1\n}1\n")
    path = tmp_path / "s.synctex.gz"
    path.write_bytes(gzip.compress(data.encode()))
    assert synctex.load(path, Path("/doc")).files == {1: "main.tex", 2: "sub/part.tex"}


def test_magnification_and_offsets_are_applied(tmp_path: Path) -> None:
    sp = 65781.76
    body = (f"(1,1:{round(100 * sp)},{round(200 * sp)}:{round(50 * sp)},{round(8 * sp)},0\n"
            f"g1,1:{round(110 * sp)},{round(200 * sp)}\n)\n")
    plain = synctex.load(_write(tmp_path, body), Path("/doc"))
    scaled = synctex.load(_write(tmp_path, body, f"Magnification:2000\nUnit:1\nX Offset:{round(10 * sp)}\n"
                                                 f"Y Offset:{round(5 * sp)}\n"), Path("/doc"))
    assert plain.records[0].x == pytest.approx(100) and plain.records[0].y == pytest.approx(200)
    assert scaled.records[0].x == pytest.approx(210) and scaled.records[0].y == pytest.approx(405)
