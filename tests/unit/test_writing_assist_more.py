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

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
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
    # "\frac" and "\neq" sent without doubled backslashes arrive as a form feed and a newline.
    result, _ = run_image(env, "equation-image", {"replacement": "\\[ \frac{a}{b} \neq \nu \\]"})
    assert result["hunks"][0]["new"] == "\\[\n  \\frac{a}{b} \\neq \\nu\n\\]"


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
