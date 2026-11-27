"""Okapi BM25, by hand: 59 documents need no search library.

score(q, d) = sum over distinct query terms t of
    idf(t) * tf(t, d) * (k1 + 1) / (tf(t, d) + k1 * (1 - b + b * |d| / avgdl))
idf(t) = ln((N - df + 0.5) / (df + 0.5) + 1), the "+1" variant, which is never
negative, so a term found in every document adds nothing instead of
subtracting.
"""

import math
from collections import Counter

import numpy as np


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.lengths = np.array([len(d) for d in docs], dtype=float)
        self.avgdl = float(self.lengths.mean()) if docs and self.lengths.sum() else 1.0
        n = len(docs)
        df = Counter(t for d in docs for t in set(d))
        self.idf = {
            t: math.log((n - c + 0.5) / (c + 0.5) + 1) for t, c in sorted(df.items())
        }

    def scores(self, query: list[str]) -> np.ndarray:
        """One score per document, in document order.

        Each DISTINCT query term counts once: glossary expansion may repeat a
        word the user already typed, and that must not double its weight.
        """
        out = np.zeros(len(self.tf))
        norm = self.k1 * (1 - self.b + self.b * self.lengths / self.avgdl)
        for term in sorted(set(query)):
            idf = self.idf.get(term)
            if idf is None:
                continue
            tf = np.array([d.get(term, 0) for d in self.tf], dtype=float)
            out += idf * tf * (self.k1 + 1) / (tf + norm)
        return out
