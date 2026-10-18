from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lens_fakes import seed

from litforge.analysis.gateway import ModelGateway
from litforge.config import Config, ModelProfileConfig, Paths
from litforge.llm.budget import Pricing
from litforge.llm.router import CloudApprovalRequired
from litforge.screening.llm import FakeLlm
from litforge.storage.db import open_db
from litforge.writing import assist as wa
from litforge.writing import documents as wd
from litforge.writing.bib import citation_key

PERSIAN = r"در این مطالعه \lr{TVB} با $\alpha=0.5$ استفاده شد \cite{old2020} و در شکل \ref{fig:a} آمده است."


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "data", exports_root=tmp_path / "exports")
    paths.ensure()
    conn = open_db(paths.db)
    span = seed(conn, "p1", "Results: the coupling gain was 3.25 across 40 subjects in the whole-brain model.")
    conn.execute("UPDATE papers SET authors_json=?, year=2021 WHERE id='p1'", (json.dumps(["Ann Lee"]),))
    doc = wd.create_document(conn, paths, "demo", "Draft")
    yield conn, paths, doc, span
    conn.close()


def gateway(conn, reply: dict[str, Any] | None = None, answer=None, config: Config | None = None):
    fake = FakeLlm(answer or (lambda messages: reply))
    return ModelGateway(conn, config or Config(), providers={"local": fake}), fake


def suggest(env, action: str, text: str, reply: dict[str, Any], **kw: Any) -> dict[str, Any]:
    conn, paths, doc, _ = env
    gw, _ = gateway(conn, reply)
    return wa.run(conn, gw, doc, wa.prepare(conn, doc, action, text, **kw), "main.tex")


def test_rewrite_hunks_and_record(env) -> None:
    conn, *_ = env
    result = suggest(env, "shorter", "We did a very long and slow analysis of data.",
                     {"replacement": "We analysed the data.", "note": "shorter"})
    assert result["flags"] == [] and len(result["hunks"]) == 1
    hunk = result["hunks"][0]
    assert wa.applied_text(result["original"], result["hunks"], [0]) == "We analysed the data."
    assert hunk["flags"] == []
    row = conn.execute("SELECT task, prompt_version, status FROM writing_suggestions").fetchone()
    assert tuple(row) == ("write_rewrite", "write-v1", "pending")
    call = conn.execute("SELECT task, prompt_version FROM model_calls").fetchone()
    assert tuple(call) == ("write_rewrite", "write-v1")


def test_invented_numbers_flagged(env) -> None:
    result = suggest(env, "clearer", "The gain rose in most subjects.",
                     {"replacement": "The gain rose by 42% in 18 subjects."})
    flags = " ".join(f for h in result["hunks"] for f in h["flags"])
    assert "42" in flags and "18" in flags


def test_unknown_and_unbacked_cite_keys_flagged(env) -> None:
    conn, *_ = env
    key = citation_key(conn, "p1")
    result = suggest(env, "clearer", "Models work.", {"replacement": f"Models work \\cite{{{key},ghost99}}."})
    flags = " ".join(f for h in result["hunks"] for f in h["flags"])
    assert "unknown citation key: ghost99" in flags
    assert f"not backed by returned evidence: {key}" in flags
    assert "number" not in flags  # digits in keys are not numbers


def test_dropped_latex_math_cite_and_ref_flagged(env) -> None:
    result = suggest(env, "translate-en", PERSIAN, {"replacement": "In this study TVB was used, as the figure shows."})
    flags = " ".join(result["flags"])
    assert "\\lr" in flags and "math" in flags and "old2020" in flags and "fig:a" in flags


def test_persian_selection_with_latex_survives(env) -> None:
    kept = PERSIAN.replace("استفاده شد", "به کار رفت")
    result = suggest(env, "clearer", PERSIAN, {"replacement": kept})
    assert result["flags"] == []
    assert all(h["flags"] == [] for h in result["hunks"])
    assert wa.applied_text(PERSIAN, result["hunks"], [h["index"] for h in result["hunks"]]) == kept
    assert wa.applied_text(PERSIAN, result["hunks"], []) == PERSIAN


def test_fix_error_sends_only_the_excerpt(env) -> None:
    conn, paths, doc, _ = env
    seen: list[list[dict[str, str]]] = []
    gw, _ = gateway(conn, answer=lambda m: seen.append(m) or {"replacement": "\\textbf{x}\n", "note": "fixed"})
    req = wa.prepare(conn, doc, "fix", "\\textbff{x}\n", error="Undefined control sequence.", first_line=12)
    result = wa.run(conn, gw, doc, req, "main.tex")
    assert wa.applied_text(req.text, result["hunks"], [0]) == "\\textbf{x}\n"
    user = seen[0][-1]["content"]
    assert "\\textbff{x}" in user and 'first_line="12"' in user and "Undefined control sequence." in user
    with pytest.raises(wa.AssistError):
        wa.prepare(conn, doc, "fix", "x", error="")


def test_suggest_citations_keeps_only_verified_quotes(env) -> None:
    conn, paths, doc, span = env
    text = "The coupling gain was high across subjects."

    def answer(messages):
        ids = [i for i in __import__("re").findall(r'<evidence id="([^"]+)"', messages[-1]["content"])]
        return {"text": text[:-1] + f" \\cite{{{ids[0]}}}.", "citations": [
            {"evidence_id": ids[0], "quote": "the coupling gain was 3.25 across 40 subjects"}]}

    gw, _ = gateway(conn, answer=answer)
    result = wa.run(conn, gw, doc, wa.prepare(conn, doc, "cite", text), "main.tex")
    key = citation_key(conn, "p1")
    assert result["citations"][0]["key"] == key and result["citations"][0]["quote"].startswith("the coupling")
    assert f"\\cite{{{key}}}" in result["hunks"][0]["new"]
    assert result["hunks"][0]["flags"] == []  # 3.25/40 are not inserted; the cite is backed

    def invented(messages):
        ids = __import__("re").findall(r'<evidence id="([^"]+)"', messages[-1]["content"])
        return {"text": text[:-1] + f" \\cite{{{ids[0]}}}.", "citations": [
            {"evidence_id": ids[0], "quote": "the gain doubled in every patient"}]}

    gw, _ = gateway(conn, answer=invented)
    result = wa.run(conn, gw, doc, wa.prepare(conn, doc, "cite", text), "main.tex")
    assert result["citations"] == [] and "\\cite" not in "".join(h["new"] for h in result["hunks"])
    assert any("not found in the evidence" in f for f in result["flags"])


def test_cloud_route_requires_approval(env) -> None:
    conn, paths, doc, _ = env
    config = Config()
    config.models.policy = "ask_before_cloud"
    config.privacy.allow_cloud = True
    config.models.profiles["remote"] = ModelProfileConfig(
        provider="openai_compatible", model="large", remote=True,
        pricing=Pricing(input_per_million_usd=1, output_per_million_usd=2))
    from litforge.config import TaskModelConfig

    config.models.tasks["write_rewrite"] = TaskModelConfig(profile="remote")
    remote = FakeLlm(lambda m: {"replacement": "Short."})
    gw = ModelGateway(conn, config, providers={"remote": remote, "local": FakeLlm(lambda m: {})})
    req = wa.prepare(conn, doc, "shorter", "A long sentence here.")
    preview = wa.preview(gw, req)
    assert preview.remote and preview.requires_approval
    with pytest.raises(CloudApprovalRequired):
        wa.run(conn, gw, doc, req, "main.tex")
    assert remote.calls == 0
    assert wa.run(conn, gw, doc, req, "main.tex", approved=True)["hunks"]


def test_decide_records_and_snapshots(env) -> None:
    conn, paths, doc, _ = env
    result = suggest(env, "clearer", "one two\nthree four\n", {"replacement": "one 2\nthree four\nfive\n"})
    assert len(result["hunks"]) == 2
    decided = wa.decide(conn, doc, result["id"], [1])
    assert decided == {"status": "partial", "accepted": [1]}
    assert "1/2 accepted" in wd.list_snapshots(doc)[0]["note"]
    with pytest.raises(wa.AssistError):
        wa.decide(conn, doc, result["id"], [])


def test_selection_limits(env) -> None:
    conn, paths, doc, _ = env
    with pytest.raises(wa.AssistError):
        wa.prepare(conn, doc, "clearer", "   ")
    with pytest.raises(wa.AssistError):
        wa.prepare(conn, doc, "clearer", "x" * (wa.MAX_SELECTION + 1))
    with pytest.raises(wa.AssistError):
        wa.prepare(conn, doc, "poem", "x")


def test_repair_escapes_restores_latex_commands() -> None:
    original = r"\textbf{a} \lr{b} $\nu$ \cite{k}" + "\nnext line"
    broken = "\textbf{a} \\lr{b} $\nu$ \rule{1pt} \x08egin{x}\nnext line"
    fixed = wa.repair_escapes(broken, original)
    assert fixed == "\\textbf{a} \\lr{b} $\\nu$ \\rule{1pt} \\begin{x}\nnext line"


def test_fix_does_not_flag_the_broken_command_and_keeps_edges(env) -> None:
    conn, paths, doc, _ = env
    gw, _ = gateway(conn, {"replacement": "\\maketitle\n\\textbf{x}"})  # model dropped the final newline
    excerpt = "\\maketitle\n\\textbff{x}\n"
    req = wa.prepare(conn, doc, "fix", excerpt, error="Undefined control sequence.", first_line=10, error_line=11)
    result = wa.run(conn, gw, doc, req, "main.tex")
    assert result["flags"] == []
    assert wa.applied_text(excerpt, result["hunks"], [h["index"] for h in result["hunks"]]) == \
        "\\maketitle\n\\textbf{x}\n"


def test_cite_action_flags_changed_wording_and_added_commands(env) -> None:
    conn, paths, doc, span = env
    text = "The coupling gain was high across subjects."

    def answer(messages):
        ids = __import__("re").findall(r'<evidence id="([^"]+)"', messages[-1]["content"])
        return {"text": f"The coupling gain was very high \\cite{{{ids[0]}}}\\end across subjects.", "citations": [
            {"evidence_id": ids[0], "quote": "the coupling gain was 3.25 across 40 subjects"}]}

    gw, _ = gateway(conn, answer=answer)
    result = wa.run(conn, gw, doc, wa.prepare(conn, doc, "cite", text), "main.tex")
    assert "LaTeX commands added: \\end" in result["flags"]
    assert any("wording changed" in f for h in result["hunks"] for f in h["flags"])


def test_hunk_offsets_are_code_points_with_emoji() -> None:
    old, new = "نتیجه 😀 خوب است و مهم", "نتیجه 😀 عالی است و مهم"
    hunks = wa.diff_hunks(old, new)
    assert old[hunks[0]["start"]:hunks[0]["end"]] == "خوب"
    assert wa.applied_text(old, hunks, [0]) == new
