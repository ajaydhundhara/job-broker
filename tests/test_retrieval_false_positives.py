"""Retrieval false positives and paraphrases: what the tfidf system does and
does not get right (pre-submission review, Part A).

The examples are new: not from 01c_retrieval_queries.jsonl and not from the
reviewer holdout. Each states why it is (un)answerable from
01b_failure_logs.jsonl. They were measured ONCE against the unchanged
retrieval; behaviour that is wrong today is a strict xfail stating the
desired outcome, so it can't be fixed or broken silently.

Why no scoring change: removing resolution text from the relevance documents
(signal-only log and/or group documents) dropped public CV P@5 to 0.81-0.88
and CV rejection to 0.75, below the 0.88 / 0.95 floors; and an
evidence-only acceptance gate can't separate the cases below (a fix-wording
how-to scores higher on failure evidence alone than some public answerable
questions). See reports/final_validation_report.md.
"""

import pytest

from retrieval import index, search
from retrieval.quality import DEFAULT_LOGS


@pytest.fixture(scope="module")
def idx():
    return index.build_index(DEFAULT_LOGS, backend="tfidf")


def group_with(idx, text: str) -> str:
    """The cause group whose resolution contains `text` (no group-id literals)."""
    (g,) = [g.id for g in idx.groups if text in g.resolution.lower()]
    return g


# --- unanswerable: how-to questions that only share a fix's wording -------------------
# Each names an action from a recorded resolution but describes no failure, so
# no log in the corpus answers it.

FIX_WORDING = [
    # "enable gradient checkpointing" is in the batch-size fix
    "how do I enable gradient checkpointing in my model code",
    # "pin a CUDA-compatible torch build in the image" is the driver fix
    "how should I pin the torch version in our Dockerfile",
    # "lower loader workers" is the dataset-service fix
    "how do I lower the number of data loader workers",
]


@pytest.mark.parametrize("text", FIX_WORDING)
@pytest.mark.xfail(strict=True, reason="resolution text is part of the relevance "
                   "documents (needed for the CV floors), so fix wording alone "
                   "lifts a how-to question above TAU")  # fmt: skip
def test_desired_a_how_to_that_only_shares_fix_wording_is_rejected(idx, text):
    assert search.search(idx, text).no_match


# --- unanswerable: failure families absent from the corpus ---------------------------
# No log mentions a tokenizer/fork deadlock or DNS. These are rejected today.
ABSENT = [
    "tokenizer deadlocked after the process forked",
    "DNS lookup failed for the metrics endpoint",
]


@pytest.mark.parametrize("text", ABSENT)
def test_an_absent_failure_family_returns_no_match(idx, text):
    out = search.search(idx, text)
    assert out.no_match and out.results == []
    assert out.message == search.NO_MATCH


@pytest.mark.xfail(strict=True, reason="no log mentions a license server, but "
                   "'job', 'starts' and 'unreachable' overlap enough with the "
                   "OOM group's text to pass TAU")  # fmt: skip
def test_desired_an_absent_license_server_failure_returns_no_match(idx):
    assert search.search(idx, "license server unreachable when the job starts").no_match


# --- answerable: paraphrases of known failures -----------------------------------------


def test_a_paraphrased_disk_full_failure_finds_the_disk_group(idx):
    # F031-F036: "No space left on device", "disk usage at 100% on /scratch".
    out = search.search(
        idx, "ran out of room on the scratch volume while saving weights"
    )
    assert out.results[0]["cause_group"] == group_with(idx, "scratch disk full")


@pytest.mark.xfail(strict=True, reason="no shared words with the OOM logs; the "
                   "glossary has no 'memory budget' / 'accelerator' entry, and "
                   "adding one would be tuning to this example")  # fmt: skip
def test_desired_a_paraphrased_gpu_memory_failure_finds_the_oom_group(idx):
    # F001-F005, F910: CUDA out of memory.
    out = search.search(idx, "the run went past its memory budget on the accelerator")
    assert out.results and out.results[0]["cause_group"] == group_with(
        idx, "batch size"
    )


@pytest.mark.xfail(strict=True, reason="'all-gather' and 'watchdog' are in F027 "
                   "but the score stays below TAU and the top group is wrong")  # fmt: skip
def test_desired_a_paraphrased_collective_hang_finds_the_nccl_group(idx):
    # F027: "Watchdog caught collective operation timeout ... ALLREDUCE".
    out = search.search(idx, "all-gather hung across workers until the watchdog fired")
    assert out.results and out.results[0]["cause_group"] == group_with(idx, "nccl")


# --- answerable: a pasted log, and the result contract --------------------------------


def test_a_pasted_log_finds_its_group_first_and_keeps_the_contract(idx):
    text = "2025-04-01T00:00:00Z ERROR RuntimeError: NCCL communicator was aborted on rank 2"
    out = search.search(idx, text, k=5)
    assert out.input_type == "log" and not out.no_match
    assert out.results[0]["cause_group"] == group_with(idx, "nccl")
    assert len(out.results) <= 5
    groups = [h["cause_group"] for h in out.results]
    # group-first: a group's members are contiguous, never interleaved
    assert groups == sorted(groups, key=groups.index)
    assert all(h["score"] >= idx.params.tau for h in out.results)
    assert search.search(idx, text, k=5).results == out.results  # deterministic
    assert set(out.response()) == {
        "input_type",
        "backend",
        "no_match",
        "message",
        "results",
    }
