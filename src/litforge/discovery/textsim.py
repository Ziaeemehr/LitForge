"""Text similarity between a seed and candidates: TF-IDF (always) and embeddings (optional).

Embeddings use fastembed (ONNX, CPU; install with `uv sync --extra embed`). The model is
downloaded once into data/cache/models.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from litforge.discovery.keyphrases import STOPWORDS

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z][a-z0-9\-]+", text.lower()) if t not in STOPWORDS and len(t) > 2]


def tfidf_similarity(seed: str, docs: Sequence[str]) -> list[float]:
    """Cosine similarity of `seed` to each doc, with IDF computed over seed + docs."""
    tokenized = [_tokens(seed)] + [_tokens(d) for d in docs]
    n = len(tokenized)
    df: Counter[str] = Counter(t for toks in tokenized for t in set(toks))
    idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}

    def vec(toks: list[str]) -> dict[str, float]:
        tf = Counter(toks)
        return {t: (1 + math.log(c)) * idf[t] for t, c in tf.items()}

    def cos(a: dict[str, float], b: dict[str, float]) -> float:
        dot = sum(v * b.get(t, 0.0) for t, v in a.items())
        na, nb = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
        return dot / (na * nb) if na and nb else 0.0

    seed_vec = vec(tokenized[0])
    return [round(cos(seed_vec, vec(toks)), 4) for toks in tokenized[1:]]


class Embedder(Protocol):
    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def embedding_similarity(embedder: Embedder, seed: str, docs: Sequence[str]) -> list[float]:
    vectors = embedder.embed([seed, *docs])

    def unit(v: list[float]) -> list[float]:
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    s = unit(vectors[0])
    return [round(sum(a * b for a, b in zip(s, unit(v), strict=True)), 4) for v in vectors[1:]]


class FastEmbedder:
    """Lazy wrapper around fastembed.TextEmbedding."""

    def __init__(self, model: str = DEFAULT_EMBEDDING_MODEL, cache_dir: Path | None = None,
                 device: str = "cpu") -> None:
        from fastembed import TextEmbedding

        self.name = model
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if device == "cuda" else None
        self._model = TextEmbedding(model, cache_dir=str(cache_dir) if cache_dir else None, providers=providers)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(x) for x in v] for v in self._model.embed(list(texts))]


def load_embedder(model: str | None, cache_dir: Path | None, device: str = "cpu") -> Embedder | None:
    """Return an embedder, or None when disabled or fastembed is not installed."""
    if not model:
        return None
    try:
        return FastEmbedder(model, cache_dir, device)
    except ImportError:
        return None
