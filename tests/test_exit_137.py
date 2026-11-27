"""P0-03: exit code 137 / SIGKILL is not proof of OOM.

137 = 128 + signal 9 (SIGKILL). Out of memory is one possible cause; a
preemption, an operator kill or a timeout are others. The glossary may add
OOM vocabulary only when the text itself carries memory evidence (OOMKilled,
OOM killer, out of memory, memory or cgroup limit exceeded). Checked at two
levels: glossary.expand alone, and the query search.prepare_query builds.
"""

import re
from collections import Counter
from pathlib import Path

import pytest

from retrieval import glossary, index, search

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "01b_failure_logs.jsonl"

OOM_TERMS = ("oom", "oomkilled", "cuda out of memory", "outofmemoryerror")
TERMINATION_TERMS = ("sigkill",)  # what a bare 137 / SIGKILL may add

NO_MEMORY_EVIDENCE = (
    "Process exited with code 137",
    "Container terminated with exit code 137",
    "Process killed with SIGKILL",
)
MEMORY_EVIDENCE = (
    "OOMKilled",
    "Killed process by out of memory killer",
    "Container exceeded memory limit and was terminated",
)


@pytest.fixture(scope="module")
def idx():
    return index.build_index(LOGS, backend="tfidf")


def added(text: str) -> str:
    """Only what the glossary appended (the original text is kept as is)."""
    expanded = glossary.expand(text)
    assert expanded.startswith(text)
    return expanded[len(text) :]


def has_term(text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text.lower())


@pytest.mark.parametrize("text", NO_MEMORY_EVIDENCE)
def test_a_bare_137_or_sigkill_expands_to_termination_not_oom(text):
    extra = added(text)
    assert not [t for t in OOM_TERMS if has_term(extra, t)], extra
    assert all(has_term(extra + " " + text, t) for t in TERMINATION_TERMS), extra


@pytest.mark.parametrize("text", NO_MEMORY_EVIDENCE)
def test_the_scored_query_gets_no_oom_terms_without_memory_evidence(idx, text):
    _, query = search.prepare_query(idx, text)
    new = [t for t in OOM_TERMS if has_term(query, t) and not has_term(text, t)]
    assert new == [], query


@pytest.mark.parametrize("text", MEMORY_EVIDENCE)
def test_memory_evidence_expands_to_oom(text):
    assert has_term(added(text), "oom"), added(text)


def oom_group(idx) -> str:
    """The cause group of the corpus logs that themselves say out of memory."""
    evidence = re.compile(r"(?i)out of memory|oomkilled")
    groups = Counter(idx.group_of[r.id] for r in idx.records if evidence.search(r.log))
    return groups.most_common(1)[0][0]


@pytest.mark.parametrize("text", MEMORY_EVIDENCE)
def test_oom_incidents_rank_first_with_memory_evidence(idx, text):
    out = search.search(idx, text)
    assert not out.no_match, text
    assert out.results[0]["cause_group"] == oom_group(idx), out.results[0]
