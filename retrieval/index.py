"""Ingestion: records -> signal text -> per-log and per-group indexes.

build_index(logs_path, backend, params), in this order:
1. load the JSONL, skipping malformed lines; redact BEFORE anything is stored
2. learn the noise templates; signal text of each log
3. per-log documents (signal text + resolution) -> BM25 + embedding matrix
4. cause groups by normalised resolution; empty resolutions are inferred
   from the most similar group or left as singletons; conflicts recorded
5. per-group documents (members' signal lines + resolution) -> BM25 + matrix

No vector database: 59 documents fit in a NumPy matrix, and a search is one
matrix-vector product (see README "Retrieval").
"""

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field, fields
from pathlib import Path

import numpy as np

from retrieval import clean, embed
from retrieval.bm25 import BM25
from retrieval.quality import DEFAULT_LOGS, Record, load_records, normalise_resolution

logger = logging.getLogger(__name__)
PARAMS_PATH = Path(__file__).with_name("params.json")


@dataclass(frozen=True)
class Params:
    """Every tunable number, in one place. Fixed defaults until sub-step D."""

    w_dense: float = 0.5  # hybrid = w_dense * dense + (1 - w_dense) * lexical
    lex_k: float = 5.0  # lexical = bm25 / (bm25 + lex_k), squashed into [0, 1)
    g_member: float = 0.7  # group = g_member * best member + rest * group document
    tau: float = 0.20  # below this best group score: "no similar past failure"
    assign_min: float = 0.30  # an empty resolution joins a group only above this
    noise_df: float = clean.DEFAULT_NOISE_DF


def load_params(backend: str, path: Path = PARAMS_PATH) -> Params:
    """Params for one backend (scores differ in scale between backends)."""
    data = (
        json.loads(Path(path).read_text(encoding="utf-8"))
        if Path(path).exists()
        else {}
    )
    values = data.get(backend, {})
    known = {f.name for f in fields(Params)}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(f"unknown parameter(s) in {path}: {', '.join(unknown)}")
    return Params(**values)


def hybrid(dense: np.ndarray, lexical_raw: np.ndarray, p: Params) -> np.ndarray:
    """Dense cosine clipped to [0, 1] mixed with a squashed BM25 score.

    BM25 is unbounded; x / (x + lex_k) maps it into [0, 1) so the two halves
    are on one scale and TAU means the same thing for every query length.
    """
    lexical = lexical_raw / (lexical_raw + p.lex_k)
    return p.w_dense * np.clip(dense, 0.0, 1.0) + (1 - p.w_dense) * lexical


@dataclass(frozen=True)
class Group:
    id: str
    resolution: str  # "" for a singleton with nothing recorded or inferred
    members: tuple[str, ...]


@dataclass(frozen=True)
class Conflict:
    ids: tuple[str, ...]
    resolutions: tuple[str, ...]


@dataclass
class Index:
    backend: str
    params: Params
    embedder: object
    records: list[Record]
    malformed: list[tuple[int, str]]
    noise: dict[str, int]
    signal: dict[str, list[str]]
    log_docs: list[str]
    log_bm25: BM25
    log_vecs: np.ndarray
    groups: list[Group]
    group_docs: list[str]
    group_bm25: BM25
    group_vecs: np.ndarray
    status: dict[str, str]  # recorded | inferred | none
    inferred: dict[str, float]  # id -> similarity to the group it joined
    conflicts: list[Conflict]
    by_id: dict[str, Record] = field(init=False)
    group_of: dict[str, str] = field(init=False)
    _groups: dict[str, Group] = field(init=False)
    position: dict[str, int] = field(init=False)  # row in the per-log matrices

    def __post_init__(self):
        self.by_id = {r.id: r for r in self.records}
        self.position = {r.id: i for i, r in enumerate(self.records)}
        self._groups = {g.id: g for g in self.groups}
        self.group_of = {m: g.id for g in self.groups for m in g.members}

    def group(self, log_id: str) -> Group:
        return self._groups[self.group_of[log_id]]


def _log_scores(embedder, bm25: BM25, vecs: np.ndarray, text: str, p: Params):
    query = embedder.embed([text])[0]
    return hybrid(vecs @ query, bm25.scores(clean.tokenize(text)), p)


def build_index(logs_path=DEFAULT_LOGS, backend: str | None = None,
                params: Params | None = None) -> Index:  # fmt: skip
    # 1. load and redact (load_records redacts every field as it reads)
    records, malformed = load_records(logs_path)
    if not records:
        raise ValueError(f"no usable records in {logs_path}")
    embedder = embed.select_backend(backend)
    params = params or load_params(embedder.name)

    # 2. learned noise, signal text
    noise = clean.learn_noise([r.log for r in records], params.noise_df)
    signal = {r.id: clean.signal_lines(r.log, noise) for r in records}

    # 3. per-log documents
    log_docs = [" ".join(signal[r.id] + ([r.resolution] if r.resolution.strip() else []))
                for r in records]  # fmt: skip
    embedder.fit(log_docs)
    log_bm25 = BM25([clean.tokenize(d) for d in log_docs])
    log_vecs = embedder.embed(log_docs)

    # 4. cause groups by normalised resolution
    by_key = defaultdict(list)
    for r in records:
        if r.resolution.strip():
            by_key[normalise_resolution(r.resolution)].append(r)
    status = {r.id: "recorded" for r in records if r.resolution.strip()}
    keys = sorted(by_key)
    members = {k: [r.id for r in by_key[k]] for k in keys}
    pos = {r.id: i for i, r in enumerate(records)}
    inferred, singletons = {}, []
    for r in records:
        if r.resolution.strip():
            continue
        # How alike is this log to each group? Its BEST member (max), as in
        # search: a cause group is worded differently on purpose, so the mean
        # is dragged down by siblings that share the cause but not the words
        # (measured: an OOMKilled log scored 0.76 against its look-alike, a
        # group mean of 0.17). ASSIGN_MIN guards against a weak best match.
        scores = _log_scores(
            embedder, log_bm25, log_vecs, " ".join(signal[r.id]), params
        )
        best_key, best = None, -1.0
        for k in keys:
            s = float(np.max([scores[pos[m.id]] for m in by_key[k]]))
            if s > best:  # ties keep the first key in sorted order
                best_key, best = k, s
        if best_key is not None and best >= params.assign_min:
            members[best_key].append(r.id)
            status[r.id] = "inferred"
            inferred[r.id] = round(best, 6)
        else:
            status[r.id] = "none"
            singletons.append(r.id)
        logger.info("empty resolution: %s -> %s", r.id, status[r.id])
    width = len(str(len(keys) + len(singletons)))
    groups = [Group(f"g{i:0{width}d}", by_key[k][0].resolution.strip(), tuple(members[k]))
              for i, k in enumerate(keys, 1)]  # fmt: skip
    groups += [Group(f"g{len(keys) + i:0{width}d}", "", (log_id,))
               for i, log_id in enumerate(singletons, 1)]  # fmt: skip

    # identical signal text, different recorded resolutions: a conflict.
    # Each log keeps its own resolution; the API adds a note to both.
    same = defaultdict(list)
    for r in records:
        if r.resolution.strip():
            same["\n".join(signal[r.id])].append(r)
    conflicts = [
        Conflict(tuple(r.id for r in m), tuple(r.resolution for r in m))
        for m in same.values()
        if len({normalise_resolution(r.resolution) for r in m}) > 1
    ]

    # 5. per-group documents
    resolution_of = {g.id: g.resolution for g in groups}
    group_docs = [" ".join([x for m in g.members for x in signal[m]]
                           + ([resolution_of[g.id]] if resolution_of[g.id] else []))
                  for g in groups]  # fmt: skip
    group_bm25 = BM25([clean.tokenize(d) for d in group_docs])
    group_vecs = embedder.embed(group_docs)

    return Index(
        backend=embedder.name, params=params, embedder=embedder, records=records,
        malformed=malformed, noise=noise, signal=signal, log_docs=log_docs,
        log_bm25=log_bm25, log_vecs=log_vecs, groups=groups, group_docs=group_docs,
        group_bm25=group_bm25, group_vecs=group_vecs, status=status,
        inferred=inferred, conflicts=conflicts,
    )  # fmt: skip


def index_from_env() -> Index | None:
    """The API's index: FAILURE_LOGS_PATH, else the default file.

    An explicitly configured path that is missing fails startup (a config
    error must be loud). A missing DEFAULT file only disables /failures (503),
    so a broker deployed without the logs still schedules jobs.
    """
    configured = os.environ.get("FAILURE_LOGS_PATH")
    path = Path(configured or DEFAULT_LOGS)
    if not path.exists():
        if configured:
            raise FileNotFoundError(f"FAILURE_LOGS_PATH={configured} does not exist")
        logger.warning("no failure logs at %s; /failures is disabled", path)
        return None
    return build_index(path)
