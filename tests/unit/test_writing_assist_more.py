"""Title/abstract suggestions and LaTeX from an image (equation or table)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from litforge.analysis.gateway import ModelGateway
from litforge.config import Config, ModelProfileConfig, Paths
from litforge.projects import create_project
from litforge.screening.llm import FakeLlm
from litforge.storage.db import open_db
from litforge.writing import assist as wa
from litforge.writing import documents as wd


def png(width: int = 2, height: int = 2) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\0" + b"\xff" * 3 * width for _ in range(min(height, 4)))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


PNG = png()
BODY = (r"\section{Introduction} We fit a whole-brain model to 40 subjects and found a coupling gain of 3.25. "
        r"\section{Methods} The model uses $\alpha = 0.5$ \cite{lee2021}.")


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "data", exports_root=tmp_path / "exports")
    paths.ensure()
    conn = open_db(paths.db)
    create_project(conn, paths, "demo")
    doc = wd.create_document(conn, paths, "demo", "Draft")
    yield conn, doc
    conn.close()


class FakeVision:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply, self.images, self.messages = reply, [], []

    def check(self) -> None:
        pass

    def vision_json(self, messages: list[dict[str, Any]], schema: dict[str, Any], images: list[bytes],
                    **_: Any) -> dict[str, Any]:
        self.images.append(images)
        self.messages.append(messages)
        return self.reply


def run_text(env, action: str, text: str, reply: dict[str, Any], **kw: Any) -> tuple[dict[str, Any], FakeLlm]:
    conn, doc = env
    fake = FakeLlm(lambda messages: reply)
    gw = ModelGateway(conn, Config(), providers={"local": fake})
    return wa.run(conn, gw, doc, wa.prepare(conn, doc, action, text, **kw), "main.tex"), fake


def run_image(env, action: str, reply: dict[str, Any], image: bytes = PNG, **kw: Any):
    conn, doc = env
    fake = FakeVision(reply)
    gw = ModelGateway(conn, Config(), providers={"local_vision": fake})  # type: ignore[dict-item]
    return wa.run(conn, gw, doc, wa.prepare(conn, doc, action, "", image=image, **kw), "main.tex"), fake


# ---- title and abstract -----------------------------------------------------------------------------------


def test_title_rewrites_the_current_title_from_the_document(env) -> None:
    conn, doc = env
    req = wa.prepare(conn, doc, "title", "A draft", context=BODY)
    assert req.task == "write_summary"
    system, user = req.messages[0]["content"], req.messages[1]["content"]
    assert "title" in system.lower() and "same language" in system
    assert "<current>A draft</current>" in user and "coupling gain of 3.25" in user
    reply = {"replacement": "Coupling gain in a whole-brain model of 40 subjects"}
    result, _ = run_text(env, "title", "A draft", reply, context=BODY)
    assert result["original"] == "A draft"
    assert wa.applied_text(result["original"], result["hunks"], [h["index"] for h in result["hunks"]]) == \
        "Coupling gain in a whole-brain model of 40 subjects"
    assert not any(h["flags"] for h in result["hunks"])  # 40 is in the document


def test_abstract_flags_numbers_and_citations_not_in_the_document(env) -> None:
    result, _ = run_text(env, "abstract", "", {"replacement": "We fit 40 subjects; gain rose by 12\\% \\cite{ghost}."},
                         context=BODY)
    flags = " ".join(f for h in result["hunks"] for f in h["flags"])
    assert "12" in flags and "40" not in flags
    assert "ghost" in flags


def test_abstract_may_start_from_an_empty_environment(env) -> None:
    result, _ = run_text(env, "abstract", "\n", {"replacement": "We fit a model."}, context=BODY)
    assert result["original"] == "\n" and result["hunks"]
    new = wa.applied_text(result["original"], result["hunks"], [h["index"] for h in result["hunks"]])
    assert new == "\nWe fit a model.\n" or new.strip() == "We fit a model."


def test_title_needs_document_text(env) -> None:
    conn, doc = env
    with pytest.raises(wa.AssistError, match="no text"):
        wa.prepare(conn, doc, "title", "Draft", context="  \\maketitle  ")


def test_summary_context_is_capped(env) -> None:
    conn, doc = env
    req = wa.prepare(conn, doc, "abstract", "", context="word " * 20_000)
    assert len(req.messages[1]["content"]) < wa.MAX_CONTEXT + 500


def test_other_actions_still_need_a_selection(env) -> None:
    conn, doc = env
    with pytest.raises(wa.AssistError, match="select"):
        wa.prepare(conn, doc, "clearer", "  ")


# ---- LaTeX from an image ----------------------------------------------------------------------------------


def test_equation_from_image_goes_to_the_vision_model(env) -> None:
    result, fake = run_image(env, "equation-image",
                             {"replacement": "\\begin{equation}\n  E = mc^2\n\\end{equation}", "note": "one equation"})
    assert fake.images == [[PNG]]
    assert result["task"] == "write_image" and result["original"] == ""
    assert len(result["hunks"]) == 1 and result["hunks"][0]["flags"] == []  # numbers come from the image
    assert result["hunks"][0]["new"] == "\\begin{equation}\n  E = mc^2\n\\end{equation}"
    assert any("image" in f for f in result["flags"])  # a reminder to check it against the image
    conn, _ = env
    assert conn.execute("SELECT task FROM model_calls").fetchone()[0] == "write_image"


def test_image_result_goes_on_its_own_lines(env) -> None:
    result, _ = run_image(env, "equation-image", {"replacement": "\\[ x \\]"}, pad_before=True, pad_after=True)
    assert result["hunks"][0]["new"] == "\n\\[\n  x\n\\]\n"


def test_image_reply_json_escapes_are_repaired(env) -> None:
    # A reply with no real backslash at all did not escape its JSON: "\frac" and "\neq" arrived as a form feed
    # and a newline.
    result, _ = run_image(env, "equation-image", {"replacement": "\frac{a}{b} \neq \nu"})
    assert result["hunks"][0]["new"] == "\\frac{a}{b} \\neq \\nu"


def test_table_from_image_prompt_and_flags(env) -> None:
    conn, doc = env
    req = wa.prepare(conn, doc, "table-image", "", image=PNG)
    assert "tabular" in req.messages[0]["content"] and req.images == [PNG]
    result, _ = run_image(env, "table-image", {"replacement": "\\begin{tabular}{ll}\na & b\n\\end{tabular"})
    assert any("braces" in f for f in result["flags"])


def test_image_must_be_an_image(env) -> None:
    conn, doc = env
    with pytest.raises(wa.AssistError, match="PNG, JPEG"):
        wa.prepare(conn, doc, "equation-image", "", image=b"%PDF-1.4 not an image")
    with pytest.raises(wa.AssistError, match="paste or choose"):
        wa.prepare(conn, doc, "equation-image", "")
    with pytest.raises(wa.AssistError, match="at most"):
        wa.prepare(conn, doc, "equation-image", "", image=PNG + b"\0" * wa.MAX_IMAGE)
    with pytest.raises(wa.AssistError, match="could not be read"):
        wa.prepare(conn, doc, "equation-image", "", image=b"\x89PNG\r\n\x1a\n" + b"\0" * 64)


def test_decode_image_accepts_data_urls_and_plain_base64() -> None:
    raw = base64.b64encode(PNG).decode()
    assert wa.decode_image("data:image/png;base64," + raw) == PNG
    assert wa.decode_image(raw) == PNG
    assert wa.decode_image("") == b""
    with pytest.raises(wa.AssistError, match="image"):
        wa.decode_image("data:image/png;base64,@@not base64@@")


def test_image_preview_uses_the_vision_model_and_cloud_needs_approval(env) -> None:
    conn, doc = env
    req = wa.prepare(conn, doc, "equation-image", "", image=PNG)
    local = wa.preview(ModelGateway(conn, Config()), req)
    assert local.model == "qwen2.5vl:7b" and not local.remote and "1 images" in local.image_description
    config = Config()
    config.privacy.allow_cloud = True
    config.models.policy = "hybrid"
    config.models.profiles["cloud"] = ModelProfileConfig(provider="openai_compatible", model="gpt-x", remote=True,
                                                         vision=True, base_url="https://api.example.com/v1",
                                                         api_key_env="X_KEY")
    cloud = wa.preview(ModelGateway(conn, config), req, profile="cloud")
    assert cloud.requires_approval  # the user's image is treated as unpublished work


# ---- loop 1 (real run): one-line replies and copied equation numbers -------------------------------------


def test_layout_puts_an_equation_on_its_own_lines_without_its_number() -> None:
    got = wa.layout_latex("\\begin{equation} \\frac{a}{b} = c, \\qquad \\tau = 0.25 \\end{equation} (1)")
    assert got == "\\begin{equation}\n  \\frac{a}{b} = c, \\qquad \\tau = 0.25\n\\end{equation}"
    assert wa.layout_latex("(2.1) \\[ x \\]") == "\\[\n  x\n\\]"


def test_layout_breaks_a_table_into_rows() -> None:
    one_line = ("\\begin{table}[ht] \\centering \\begin{tabular}{l p{2cm} c} \\hline Model & Coupling & Acc (\\%) \\\\ "
                "\\hline Kuramoto & 0.42 & 81.3 \\\\ Hopf & 0.90 & 92.1 \\\\ \\hline \\end{tabular} \\caption{} "
                "\\end{table}")
    assert wa.layout_latex(one_line) == "\n".join([
        "\\begin{table}[ht]", "\\centering", "\\begin{tabular}{l p{2cm} c}", "  \\hline",
        "  Model & Coupling & Acc (\\%) \\\\", "  \\hline", "  Kuramoto & 0.42 & 81.3 \\\\",
        "  Hopf & 0.90 & 92.1 \\\\", "  \\hline", "\\end{tabular}", "\\caption{}", "\\end{table}"])


def test_layout_keeps_align_rows_and_never_leaves_blank_lines() -> None:
    got = wa.layout_latex("\\begin{align}\n\n a &= b \\\\[2pt] c &= d\n\n\\end{align}")
    assert got == "\\begin{align}\n  a &= b \\\\[2pt]\n  c &= d\n\\end{align}"
    assert "\n\n" not in got


def test_layout_leaves_text_with_comments_alone() -> None:
    text = "\\begin{equation} x % the state\n = y \\end{equation}"
    assert wa.layout_latex(text) == text


def test_image_reply_is_laid_out(env) -> None:
    result, _ = run_image(env, "equation-image", {"replacement": "\\begin{equation} E = mc^2 \\end{equation} (1)"})
    assert result["hunks"][0]["new"] == "\\begin{equation}\n  E = mc^2\n\\end{equation}"


def test_a_missing_abstract_comes_as_a_new_environment(env) -> None:
    # Neither bundled template has an abstract; the page asks for one to be inserted after \maketitle.
    result, _ = run_text(env, "abstract", "", {"replacement": "We fit a model."}, context=BODY, new_env=True,
                         pad_after=True)
    assert result["original"] == ""
    assert result["hunks"][0]["new"] == "\\begin{abstract}\nWe fit a model.\n\\end{abstract}\n"


def test_new_env_is_only_for_an_abstract(env) -> None:
    conn, doc = env
    with pytest.raises(wa.AssistError, match="abstract"):
        wa.prepare(conn, doc, "title", "", context=BODY, new_env=True)


# ---- round 3 (Codex review and the Persian real run) ------------------------------------------------------


def test_image_size_reads_png_jpeg_gif_and_webp_headers() -> None:
    import struct

    assert wa.image_size(png(30, 20)) == (30, 20)
    jpeg = (b"\xff\xd8\xff\xe0\x00\x04ab" + b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", 50, 70) + b"\0" * 12 +
            b"\xff\xd9")
    assert wa.image_size(jpeg) == (70, 50)
    gif = b"GIF89a" + struct.pack("<HH", 9, 8) + b"\0" * 10 + b";"
    assert wa.image_size(gif) == (9, 8)
    webp = b"RIFF" + b"\0" * 4 + b"WEBPVP8X" + b"\0" * 8 + (99).to_bytes(3, "little") + (49).to_bytes(3, "little")
    assert wa.image_size(webp) == (100, 50)
    assert wa.image_size(b"\x89PNG\r\n\x1a\n") is None  # a signature alone is not an image
    assert wa.image_size(png()[:-12]) is None  # truncated: no IEND


def test_image_must_be_whole_and_not_enormous(env) -> None:
    conn, doc = env
    with pytest.raises(wa.AssistError, match="could not be read"):
        wa.prepare(conn, doc, "equation-image", "", image=b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    with pytest.raises(wa.AssistError, match="too many pixels"):
        wa.prepare(conn, doc, "equation-image", "", image=png(20_000, 20_000))
    assert wa.prepare(conn, doc, "equation-image", "", image=png()).images == [png()]


def test_oversized_image_payload_is_refused_before_decoding(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("decoded")

    monkeypatch.setattr(wa.base64, "b64decode", boom)
    with pytest.raises(wa.AssistError, match="too large"):
        wa.decode_image("data:image/png;base64," + "A" * (wa.MAX_IMAGE * 4 // 3 + 100))


def test_escapes_kept_when_the_reply_escaped_backslashes_itself(env) -> None:
    # A reply with real backslashes escaped its JSON: its newlines and tabs are real (Codex review).
    result, _ = run_image(env, "equation-image", {"replacement": "\\begin{align}\nu &= 1 \\\\\n\tx &= 2\n\\end{align}"})
    assert result["hunks"][0]["new"] == "\\begin{align}\n  u &= 1 \\\\\n  x &= 2\n\\end{align}"
    result, _ = run_image(env, "equation-image", {"replacement": "\\[ \frac{a}{b} \\]"})  # \f is never real
    assert result["hunks"][0]["new"] == "\\[\n  \\frac{a}{b}\n\\]"


def test_layout_never_breaks_inside_a_group() -> None:
    text = "\\begin{equation}\\text{a\\label{x}b} \\\\ \\mbox{c \\hline d}\\end{equation}"
    assert wa.layout_latex(text) == ("\\begin{equation}\n  \\text{a\\label{x}b} \\\\\n  \\mbox{c \\hline d}\n"
                                     "\\end{equation}")


def test_layout_drops_blank_lines_even_with_comments() -> None:
    got = wa.layout_latex("\\begin{equation}\nx=1 % note\n\n  \n\\end{equation}")
    assert got == "\\begin{equation}\nx=1 % note\n\\end{equation}"


def test_summary_math_and_citations_go_behind_markers(env) -> None:
    conn, doc = env
    req = wa.prepare(conn, doc, "abstract", "", context=BODY)
    user = req.messages[1]["content"]
    assert "\\alpha" not in user and "\\cite{lee2021}" not in user and "\u27e6" in user
    marker = next(i + 1 for i, m in enumerate(req.markers) if m.startswith("$"))
    result, _ = run_text(env, "abstract", "", {"replacement": f"We set \u27e6{marker}\u27e7 in 40 subjects."},
                         context=BODY)
    assert "$\\alpha = 0.5$" in result["hunks"][0]["new"] and not result["flags"]


def test_title_prompt_asks_for_the_subject_not_the_headings(env) -> None:
    conn, doc = env
    assert "section headings" in wa.prepare(conn, doc, "title", "x", context=BODY).messages[0]["content"]

