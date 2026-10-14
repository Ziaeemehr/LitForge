"""Turn a title/abstract into a few short, precise search queries.

A whole abstract as one query fails: search APIs require all terms, so only the paper itself
matches. Instead, candidate phrases are runs of content words (RAKE-style); they are scored by
frequency, length and presence in the title, and the best ones are paired into short queries.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

_STOPWORD_TEXT = """
a about above after again against all also although am among an and any are as at be because been
before being below between both but by can could did do does doing down during each either few for
from further had has have having here how however i if in into is it its itself just may might more
most much must my no nor not now of off on once only or other our out over own per same should
since so some such than that the their them then there these they this those through thus to too
under until up upon us very via was we were what when where whether which while who whom why will
with within without would yet you your paper study approach approaches method methods methodology
result results show shows shown present presents propose proposes proposed provide provides using
use used based new novel work works recent recently significant significantly different various
several many important first second well high low large small often given allows allow enable
enables develop developed demonstrate demonstrates one two three wide widely existing current
field fields science interest domain domains way ways finally additional thereby aim aims
describe describes described interpret estimate estimating infer inferring validate validated
widely difficult intractable apply applied obtain obtained achieve achieves consider considered
"""
STOPWORDS = set(_STOPWORD_TEXT.split())

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\-]*")


def _runs(text: str) -> list[list[str]]:
    """Maximal runs of content words inside each sentence/clause."""
    runs: list[list[str]] = []
    for sentence in re.split(r"[.;:!?,()\[\]]", text):
        run: list[str] = []
        for word in _WORD.findall(sentence):
            lw = word.lower()
            if lw in STOPWORDS or len(lw) < 3:
                if run:
                    runs.append(run)
                run = []
            else:
                run.append(lw)
        if run:
            runs.append(run)
    return runs


def candidate_phrases(text: str, max_words: int = 3) -> list[str]:
    """Every 1..max_words-gram inside a content-word run (so 'neural mass models describe' yields
    'neural mass models' as well as 'mass models describe')."""
    out: list[str] = []
    for run in _runs(text):
        for n in range(1, max_words + 1):
            out += [" ".join(run[i : i + n]) for i in range(len(run) - n + 1)]
    return out


def keyphrases_by_embedding(text: str, embedder: Any, k: int = 5, diversity: float = 0.3) -> list[str]:
    """KeyBERT-style: candidate n-grams closest in meaning to the whole text, diversified with
    maximal marginal relevance so the picks cover different aspects."""
    # Multi-word phrases (or hyphenated terms) only: single words are too generic as queries.
    candidates = sorted({p for p in candidate_phrases(text) if " " in p or "-" in p})
    if not candidates:
        return []
    vectors = embedder.embed([text, *candidates])

    def unit(v: list[float]) -> list[float]:
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    doc, cands = unit(vectors[0]), [unit(v) for v in vectors[1:]]
    relevance = [sum(a * b for a, b in zip(doc, c, strict=True)) for c in cands]
    chosen: list[int] = []
    while len(chosen) < min(k, len(candidates)):
        best, best_value = -1, -math.inf
        for i, rel in enumerate(relevance):
            if i in chosen or any(candidates[i] in candidates[j] or candidates[j] in candidates[i] for j in chosen):
                continue
            redundancy = max((sum(a * b for a, b in zip(cands[i], cands[j], strict=True)) for j in chosen),
                             default=0.0)
            value = (1 - diversity) * rel - diversity * redundancy
            if value > best_value:
                best, best_value = i, value
        if best < 0:
            break
        chosen.append(best)
    return [candidates[i] for i in chosen]


def keyphrases(title: str | None, abstract: str | None, k: int = 5) -> list[str]:
    text = f"{title or ''}. {abstract or ''}"
    phrases = candidate_phrases(text)
    freq = Counter(phrases)
    word_freq = Counter(w for p in phrases for w in p.split())
    title_lower = (title or "").lower()
    scores: dict[str, float] = {}
    for phrase, count in freq.items():
        words = phrase.split()
        score = count + sum(word_freq[w] for w in words) / len(words) * 0.5
        # Multi-word phrases are far more specific than single (often generic) words.
        score *= 1 + 0.8 * (len(words) - 1) if len(words) > 1 else 0.5
        if phrase in title_lower:
            score *= 2
        scores[phrase] = score
    ranked = sorted(scores, key=lambda p: (-scores[p], p))
    chosen: list[str] = []
    for phrase in ranked:
        if any(phrase in c or c in phrase for c in chosen):
            continue
        chosen.append(phrase)
        if len(chosen) == k:
            break
    return chosen


def _overlaps(a: str, b: str) -> bool:
    def stem(w: str) -> str:  # crude plural folding: "stimulations" ~ "stimulation"
        return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w

    wa, wb = {stem(w) for w in a.split()}, {stem(w) for w in b.split()}
    return len(wa & wb) / min(len(wa), len(wb)) > 0.5


def build_queries(title: str | None, abstract: str | None, n: int = 3, embedder: Any = None) -> list[str]:
    """One query per key phrase (diverse aspects), plus the two best phrases combined (precise).

    Phrases from the statistical and (when available) the embedding method are interleaved, and
    overlapping ones dropped: the two methods catch different aspects. Results are re-ranked by
    text similarity afterwards, so broad queries don't hurt precision.
    """
    text = f"{title or ''}. {abstract or ''}".strip(". ")
    lists = [keyphrases(title, abstract, k=2 * n)]
    if embedder is not None:
        lists.insert(0, keyphrases_by_embedding(text, embedder, k=2 * n))
    phrases: list[str] = []
    for i in range(2 * n):
        for lst in lists:
            if i < len(lst) and len(phrases) < n and not any(_overlaps(lst[i], p) for p in phrases):
                phrases.append(lst[i])
    if len(phrases) >= 2:
        return [*phrases, f"{phrases[0]} {phrases[1]}"]
    return phrases
