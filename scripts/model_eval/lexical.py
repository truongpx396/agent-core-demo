"""A small deterministic BM25, used wherever the harness needs a ranking that does NOT depend on any model under test.

The reranker is scored on a FIXED candidate list per question (specs/014 Story 1, scenario 4): if the list came from whichever embedding
model happens to be in use, a reranker would be graded on that model's mistakes. A lexical ranking is model-independent, reproducible,
and exactly what the BEIR protocol uses for the first stage it reranks.
"""
import math
import re
from collections import Counter

import numpy as np

_TOKEN = re.compile(r"\w+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class Bm25:
    """Okapi BM25 (k1=1.5, b=0.75) over a fixed list of documents."""

    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self._docs = [Counter(tokenize(d)) for d in documents]
        self._len = np.array([sum(c.values()) for c in self._docs], dtype=float)
        self._avg = float(self._len.mean()) if len(documents) else 0.0
        df: Counter[str] = Counter()
        for counts in self._docs:
            df.update(counts.keys())
        n = len(documents)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> np.ndarray:
        out = np.zeros(len(self._docs))
        terms = [t for t in tokenize(query) if t in self._idf]
        for i, counts in enumerate(self._docs):
            norm = self.k1 * (1 - self.b + self.b * self._len[i] / (self._avg or 1.0))
            out[i] = sum(self._idf[t] * counts[t] * (self.k1 + 1) / (counts[t] + norm) for t in terms if t in counts)
        return out

    def top(self, query: str, k: int) -> list[int]:
        """Indices of the k best documents, best first; ties broken by document order so the result is deterministic."""
        s = self.scores(query)
        return [int(i) for i in np.argsort(-s, kind="stable")[:k]]
