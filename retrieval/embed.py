"""Embedding backends: one interface, two implementations.

    embed(texts) -> np.ndarray, one L2-normalised row per text,
    so cosine similarity is a plain dot product.

- MiniLMEmbedder: sentence-transformers all-MiniLM-L6-v2 on CPU, loaded ONLY
  from the local cache. It never downloads: startup and queries stay offline.
  Optional extra: pip install -r requirements-embeddings.txt
- TfidfEmbedder: scikit-learn TF-IDF on word 1-2-grams (our tokenizer, so it
  stems like BM25) plus char_wb 3-5-grams (robust to spelling variants such as
  OOMKilled / OOM / out-of-memory), fitted on the corpus. Always available.

RETRIEVAL_BACKEND = minilm | tfidf | auto (default auto: MiniLM if cached,
else tfidf with one logged warning).
"""

import logging
import os

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from retrieval.clean import tokenize

logger = logging.getLogger(__name__)
BACKENDS = ("auto", "minilm", "tfidf")
MINILM_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# What a missing package (ImportError), an uncached model (OSError; the hub's
# LocalEntryNotFoundError is one) or a broken install raises.
_UNAVAILABLE = (ImportError, OSError, ValueError, RuntimeError)


def _normalise(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1.0, norms)  # an all-zero row stays zero


class TfidfEmbedder:
    name = "tfidf"

    def __init__(self):
        # token_pattern=None: tokenize() replaces sklearn's own splitter.
        self._word = TfidfVectorizer(
            tokenizer=tokenize, token_pattern=None, lowercase=False, ngram_range=(1, 2)
        )
        self._char = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5))

    def fit(self, corpus: list[str]) -> "TfidfEmbedder":
        self._word.fit(corpus)
        self._char.fit(corpus)
        return self

    def embed(self, texts: list[str]) -> np.ndarray:
        word = self._word.transform(texts).toarray()
        char = self._char.transform(texts).toarray()
        return _normalise(np.hstack([word, char]))


class MiniLMEmbedder:
    name = "minilm"

    def __init__(self):
        # Belt and braces: the hub library must not reach the network even if
        # a future version ignores local_files_only.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from sentence_transformers import SentenceTransformer  # optional extra

        self._model = SentenceTransformer(
            MINILM_MODEL, device="cpu", local_files_only=True
        )

    def fit(self, corpus: list[str]) -> "MiniLMEmbedder":
        return self  # pretrained: nothing to fit

    def embed(self, texts: list[str]) -> np.ndarray:
        vectors = self._model.encode(
            texts, batch_size=32, convert_to_numpy=True, normalize_embeddings=True
        )
        return _normalise(np.asarray(vectors, dtype=float))


def minilm_available() -> bool:
    try:
        MiniLMEmbedder()
    except _UNAVAILABLE:
        return False
    return True


def select_backend(name: str | None = None):
    """An unfitted embedder for the named backend (default: the environment)."""
    name = name or os.environ.get("RETRIEVAL_BACKEND", "auto")
    if name not in BACKENDS:
        raise ValueError(f"RETRIEVAL_BACKEND must be one of {BACKENDS}, not {name!r}")
    if name == "tfidf":
        return TfidfEmbedder()
    try:
        return MiniLMEmbedder()
    except _UNAVAILABLE as exc:
        if name == "minilm":  # asked for explicitly: fail loudly, never fall back
            raise RuntimeError(f"MiniLM backend unavailable: {exc}") from exc
        logger.warning(
            "MiniLM not available locally (%s); using tfidf", type(exc).__name__
        )
        return TfidfEmbedder()
