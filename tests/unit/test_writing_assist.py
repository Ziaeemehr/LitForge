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
    assert tuple(row) == ("write_rewrite", "write-v2", "pending")
    call = conn.execute("SELECT task, prompt_version FROM model_calls").fetchone()
    assert tuple(call) == ("write_rewrite", "write-v2")


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
    assert "\\textbff{x}" in user and "Undefined control sequence." in user
    assert "first_line" not in user and "12" not in user  # models copied line numbers into the fix
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


def test_evidence_ids_become_keys_when_json_turned_the_backslash_into_an_escape(env) -> None:
    # Real run: the model wrote "\textcite{ev_…}" in JSON, which decodes to a tab + "extcite"; the evidence id
    # was not replaced, so the change came back flagged "unknown citation key" and pre-rejected.
    conn, paths, doc, span = env
    text = "The coupling gain was high across subjects."

    def answer(messages):
        ids = __import__("re").findall(r'<evidence id="([^"]+)"', messages[-1]["content"])
        return {"text": text[:-1] + f" \textcite{{{ids[0]}}}.", "citations": [  # \t: a tab, as JSON decodes it
            {"evidence_id": ids[0], "quote": "the coupling gain was 3.25 across 40 subjects"}]}

    gw, _ = gateway(conn, answer=answer)
    result = wa.run(conn, gw, doc, wa.prepare(conn, doc, "cite", text), "main.tex")
    key = citation_key(conn, "p1")
    new = "".join(h["new"] for h in result["hunks"])
    assert f"\\cite{{{key}}}" in new and "ev_" not in new  # \textcite is not used by this document
    assert result["citations"] and result["citations"][0]["key"] == key
    assert all(h["flags"] == [] for h in result["hunks"])


@pytest.mark.parametrize("written, text, expected", [
    # \textcite needs biblatex: in a document that does not use it, the inserted citation is a plain \cite.
    ("\\textcite{EV}", "The coupling gain was high across subjects.", "\\cite{KEY}"),
    # A document that already uses \textcite keeps the model's choice.
    ("\\textcite{EV}", "As \\textcite{old2020} said, the coupling gain was high.", "\\textcite{KEY}"),
    # An over-escaped backslash would be a line break followed by the word "textcite" (real run).
    ("\\\\cite{EV}", "The coupling gain was high across subjects.", "\\cite{KEY}"),
])
def test_inserted_citation_commands_compile_in_the_document(env, written, text, expected) -> None:
    conn, paths, doc, span = env

    def answer(messages):
        ids = __import__("re").findall(r'<evidence id="([^"]+)"', messages[-1]["content"])
        return {"text": text[:-1] + " " + written.replace("EV", ids[0]) + ".", "citations": [
            {"evidence_id": ids[0], "quote": "the coupling gain was 3.25 across 40 subjects"}]}

    gw, _ = gateway(conn, answer=answer)
    result = wa.run(conn, gw, doc, wa.prepare(conn, doc, "cite", text), "main.tex")
    new = wa.applied_text(text, result["hunks"], [h["index"] for h in result["hunks"]])
    assert new.endswith(" " + expected.replace("KEY", citation_key(conn, "p1")) + ".")
    assert "\\\\" not in new


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
    assert old[hunks[0]["start"]:hunks[0]["end"]] == old
    assert [w for w in hunks[0]["words"] if w["op"] != "equal"] == [{"op": "del", "text": "خوب"},
                                                                    {"op": "ins", "text": "عالی"}]
    assert wa.applied_text(old, hunks, [0]) == new


def _check(old: str, new: str, hunks: list[dict]) -> None:
    for hunk in hunks:
        assert old[hunk["start"]:hunk["end"]] == hunk["old"]
        assert "".join(w["text"] for w in hunk["words"] if w["op"] != "ins") == hunk["old"]
        assert "".join(w["text"] for w in hunk["words"] if w["op"] != "del") == hunk["new"]
    assert wa.applied_text(old, hunks, []) == old


def test_each_changed_sentence_is_its_own_hunk() -> None:
    old = "The model was fitted to data. It gave good results. We thank everyone."
    new = "The model was fitted carefully to the data. It gave very good results. We thank everyone."
    hunks = wa.diff_hunks(old, new)
    _check(old, new, hunks)
    assert [h["old"] for h in hunks] == ["The model was fitted to data. ", "It gave good results. "]
    assert wa.applied_text(old, hunks, [0, 1]) == new
    assert wa.applied_text(old, hunks, [1]) == ("The model was fitted to data. It gave very good results. "
                                                "We thank everyone.")
    # A rewritten phrase reads as one deletion and one insertion, not word by word.
    assert [w["op"] for w in wa.diff_hunks("It is a big red car.", "It is one small blue car.")[0]["words"]] == [
        "equal", "del", "ins", "equal"]


def test_persian_sentences_split_at_persian_question_mark() -> None:
    old = "این روش دقیق است. آیا سریع هم هست؟ بله، خیلی."
    new = "این روش بسیار دقیق است. آیا سریع هم هست؟ بله، تا حدی."
    hunks = wa.diff_hunks(old, new)
    _check(old, new, hunks)
    assert [h["old"] for h in hunks] == ["این روش دقیق است. ", "بله، خیلی."]
    assert wa.applied_text(old, hunks, [0, 1]) == new


@pytest.mark.parametrize("text", [
    "As in Fig.~\\ref{fig:a.b}, the gain was 3.5 times higher, e.g. in model A. ",
    "The error $x. y$ is small (Smith et al. 2020) and \\cite{a.b} agrees. ",
    "The value \\(a. b\\) holds, i.e. always. ",
])
def test_no_split_inside_math_braces_numbers_or_abbreviations(text: str) -> None:
    assert wa.sentences(text) == [text]


def test_lines_end_pieces_and_whitespace_alone_is_no_change() -> None:
    old = "\\item One. Two\n\\item Three.\n\nNext paragraph."
    assert wa.sentences(old) == ["\\item One. ", "Two\n", "\\item Three.\n\n", "Next paragraph."]
    assert wa.diff_hunks(old, "\\item One.  Two\n\\item   Three.\n\nNext paragraph.") == []
    assert wa.sentences("") == [] and [h["new"] for h in wa.diff_hunks("", "\nx\n")] == ["\nx\n"]
    added = wa.diff_hunks("One. Two.", "One. New one. Two.")
    assert [(h["old"], h["new"]) for h in added] == [("", "New one. ")]
    assert wa.applied_text("One. Two.", added, [0]) == "One. New one. Two."


def test_fix_names_the_error_line_without_numbers(env) -> None:
    conn, paths, doc, _ = env
    req = wa.prepare(conn, doc, "fix", "a\n\\sectoin{x}\nb\n", error="Undefined control sequence.",
                     first_line=7, error_line=8)
    user = req.messages[-1]["content"]
    assert "<error_line>\\sectoin{x}</error_line>" in user and "7" not in user and "8" not in user


def test_lr_wrapper_is_kept_out_of_the_model_text_and_put_back(env) -> None:
    conn, paths, doc, _ = env
    seen: list[list[dict[str, str]]] = []
    gw, _ = gateway(conn, answer=lambda m: seen.append(m) or {"replacement": "The model fits 40 patients.",
                                                               "note": "clearer"})
    text = "\\lr{The model that we use was fitted to the data of 40 patients.}\n"
    result = wa.run(conn, gw, doc, wa.prepare(conn, doc, "clearer", text), "main.tex")
    assert "\\lr" not in seen[0][-1]["content"]
    assert result["flags"] == []
    accepted = [h["index"] for h in result["hunks"]]
    assert wa.applied_text(text, result["hunks"], accepted) == "\\lr{The model fits 40 patients.}\n"


def test_persian_translation_of_an_lr_passage_drops_the_wrapper_without_a_flag(env) -> None:
    conn, paths, doc, _ = env
    gw, _ = gateway(conn, {"replacement": "مدل روی داده‌های ۴۰ بیمار برازش شد.", "note": "fa"})
    text = "\\lr{The model was fitted to the data of 40 patients.}"
    result = wa.run(conn, gw, doc, wa.prepare(conn, doc, "translate-fa", text), "main.tex")
    assert not any("dropped" in f for f in result["flags"])
    accepted = [h["index"] for h in result["hunks"]]
    assert wa.applied_text(text, result["hunks"], accepted) == "مدل روی داده‌های ۴۰ بیمار برازش شد."


@pytest.mark.parametrize("text, wrapped", [
    ("\\lr{a {b} c}", True), ("  \\lr{a}\n", True), ("\\lr{a} b \\lr{c}", False), ("x \\lr{a}", False),
    ("\\lr{a", False), ("\\lr{a \\} b}", True)])
def test_lr_wrapper_detection(text: str, wrapped: bool) -> None:
    assert (wa.lr_wrapper(text) is not None) is wrapped


def test_persian_selection_without_evidence_explains_why(env) -> None:
    conn, paths, doc, _ = env
    with pytest.raises(wa.AssistError, match="Persian text rarely matches"):
        wa.prepare(conn, doc, "cite", "شبکه‌های مغزی نزدیک به نقطه‌ی بحرانی کار می‌کنند.")


def test_a_local_model_stuck_in_a_loop_gets_a_readable_error(env) -> None:
    from litforge.llm.base import LlmError

    conn, paths, doc, _ = env

    def stuck(messages):
        raise LlmError('Ollama error 500: {"error":"prediction aborted, token repeat limit reached"}')

    gw, _ = gateway(conn, answer=stuck)
    with pytest.raises(LlmError, match="stuck repeating itself"):
        wa.run(conn, gw, doc, wa.prepare(conn, doc, "translate-fa", "The model."), "main.tex")


def test_rewrite_sends_markers_for_citations_and_math_and_restores_them(env) -> None:
    conn, paths, doc, _ = env
    seen: list[list[dict[str, str]]] = []
    text = r"We used TVB \cite{old2020} with $\alpha=0.5$ as in Section~\ref{sec:a}."

    def answer(messages):
        seen.append(messages)
        return {"replacement": "TVB \u27e61\u27e7 was used with \u27e62\u27e7 (Section~\u27e63\u27e7).", "note": "ok"}

    gw, _ = gateway(conn, answer=answer)
    result = wa.run(conn, gw, doc, wa.prepare(conn, doc, "clearer", text), "main.tex")
    sent = seen[0][-1]["content"]
    assert "\\cite" not in sent and "$" not in sent and "\u27e61\u27e7" in sent
    assert result["flags"] == []
    accepted = [h["index"] for h in result["hunks"]]
    assert wa.applied_text(text, result["hunks"], accepted) == \
        r"TVB \cite{old2020} was used with $\alpha=0.5$ (Section~\ref{sec:a})."


def test_a_lost_or_invented_marker_is_flagged(env) -> None:
    result = suggest(env, "shorter", r"A claim \cite{old2020} here.",
                     {"replacement": "A claim \u27e69\u27e7.", "note": ""})
    assert "citations dropped: old2020" in result["flags"]
    assert "the model invented markers that stand for nothing" in result["flags"]


def test_protect_skips_nested_spans() -> None:
    masked, markers = wa.protect(r"$x \ref{a}$ and \cite{b}")
    assert masked == "\u27e61\u27e7 and \u27e62\u27e7" and markers == [r"$x \ref{a}$", r"\cite{b}"]


def test_reply_budget_grows_with_the_selection(env) -> None:
    conn, paths, doc, _ = env
    short = wa.prepare(conn, doc, "shorter", "One sentence.")
    long = wa.prepare(conn, doc, "fix", "x" * 3000, error="Undefined control sequence.")
    assert wa.output_cap(short) == 1024 + 13 and wa.output_cap(long) == 4024
    assert wa.output_cap(wa.prepare(conn, doc, "fix", "x" * 12000, error="e")) == 8192
    gw, _ = gateway(conn, {"replacement": "x" * 3000})
    assert wa.preview(gw, long).max_output_tokens == 4024


@pytest.mark.parametrize("action", ["fix", "shorter", "cite"])
def test_the_first_system_message_describes_the_reply_schema(env, action: str) -> None:
    conn, paths, doc, _ = env
    req = wa.prepare(conn, doc, action, "Results: the coupling gain was 3.25 across 40 subjects.",
                     error="Undefined control sequence.")
    first = req.messages[0]
    assert first["role"] == "system" and first["content"].endswith(json.dumps(req.schema, sort_keys=True))


def test_unbalanced_braces_are_flagged() -> None:
    # seen in the real run: a closing brace of \lr{...} silently dropped
    assert any("braces" in f for f in wa.latex_flags("\\lr{a b}.", "\\lr{a b."))
    assert not any("braces" in f for f in wa.latex_flags("\\lr{a b}.", "\\lr{a}."))
    assert not any("braces" in f for f in wa.latex_flags("50\\% \\{x\\}", "50\\% \\{y\\}"))  # escaped braces


@pytest.mark.parametrize("old, new, flagged", [
    ("{a}", "}a{", True),                       # same count, broken grouping
    ("a \\\\{b}", "a \\\\{b", True),            # \\ is a line break; the brace after it is real
    ("50\\%{a}", "50\\%{a", True),              # \% is not a comment
    ("\\verb|{|", "\\verb|{| x", False),        # literal brace inside \verb
    ("\\begin{verbatim}{\\end{verbatim}", "\\begin{verbatim}{ \\end{verbatim}", False),
    ("x % {", "y % {", False),                  # a brace in a comment does not count
])
def test_brace_guard_reads_latex(old: str, new: str, flagged: bool) -> None:
    assert any("braces" in f for f in wa.latex_flags(old, new)) is flagged
