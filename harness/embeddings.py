"""Embedding interfaces for the memory pipeline (Iteration-2 A4, plan §5-A4 Task 3).

Two implementations of one callable contract (``Embedder``):
``text -> unit vector``:

* ``DeterministicHashEmbedder`` — seeded feature hashing (SHA-256, signed
  accumulators, process-stable). Used by tests and deterministic CI runs.
* ``RealSemanticEmbedder`` — wraps an injectable semantic-embedding backend
  (a local model or service). Used by the real eval/live condition; the
  backend is wired by experiments, never imported here.

There is NO vector database: retrieval is brute-force cosine over stored
vectors (``harness.memory`` scans its episode/turn embeddings directly).

COMPARISON RULE (plan §5-A4 Task 3, invariant 13): during any comparison of
memory conditions, ``VERBATIM_RAG`` and ``STRUCTURED_MEMORY`` MUST use the
SAME semantic backend — the one ``Embedder`` instance injected into
``MemoryAgent`` is shared by both policy paths; a policy change never swaps
the embedder.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Callable, Protocol, runtime_checkable

__all__ = [
    "Embedder",
    "DeterministicHashEmbedder",
    "RealSemanticEmbedder",
    "cosine",
]


@runtime_checkable
class Embedder(Protocol):
    """Callable contract shared by every embedding implementation."""

    def __call__(self, text: str) -> list[float]:
        """Embed ``text`` into a unit vector."""
        ...


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "the", "and", "you", "your", "that", "this", "with", "have", "was",
        "are", "for", "not", "but", "all", "can", "out", "get", "just",
        "like", "about", "really", "what", "when", "where", "how", "why",
        "there", "here", "from", "they", "them", "she", "him", "her", "will",
        "would", "could", "should", "into", "over", "than", "then", "very",
    }
)


class DeterministicHashEmbedder:
    """Seeded feature-hashing embedder — deterministic across processes.

    Maps text to a unit vector via SHA-256-signed accumulators over
    lower-cased alphanumeric tokens (never Python's randomized ``hash()``),
    so identical text always yields an identical vector on any machine.
    Empty text maps to ``e_0``.
    """

    def __init__(self, *, dim: int = 64, seed: int = 0) -> None:
        self.dim = dim
        self.seed = seed

    def __call__(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha256(f"{self.seed}:{tok}".encode("utf-8")).digest()
            idx = int.from_bytes(digest[:8], "little") % self.dim
            sign = 1.0 if digest[8] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]


class RealSemanticEmbedder:
    """Real semantic embedder wrapping an injectable backend.

    The backend is a batch callable ``list[str] -> list[list[float]]``
    (e.g. a local sentence-transformer model or an OpenAI-compatible
    embedding endpoint). It is injected at construction by experiments and
    the live condition; this module never imports a model or talks to a
    service. No vector DB — vectors are stored as plain arrays and compared
    with brute-force cosine.

    NOTE for eval comparisons: a single ``RealSemanticEmbedder`` instance
    must be shared by the ``VERBATIM_RAG`` and ``STRUCTURED_MEMORY``
    conditions (plan §5-A4 Task 3) — pass the same instance to the
    ``MemoryAgent`` used for both.
    """

    def __init__(
        self,
        backend: Callable[[list[str]], list[list[float]]],
        *,
        dim: int | None = None,
    ) -> None:
        self._backend = backend
        self.dim = dim

    def __call__(self, text: str) -> list[float]:
        return self._backend([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Batch embed — the backend's native shape."""
        return self._backend(texts)


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors (zero-padded to a common length)."""
    if not a or not b:
        return 0.0
    dim = max(len(a), len(b))
    va = a + [0.0] * (dim - len(a))
    vb = b + [0.0] * (dim - len(b))
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
