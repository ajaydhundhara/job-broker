"""P0-02: one classifier decides whether search input is a log or a question.

Strong log evidence (several lines, log levels, stack traces, exception
names, exit codes, structured records) wins over punctuation; a "?" inside a
URL is not a question mark. Logs are noise-filtered and never expanded;
questions are expanded with the glossary.

Every log below also contains a glossary phrase ("out of memory", "permission
denied", ...), so wrongly expanding it would change the query.
"""

from pathlib import Path

import pytest

from retrieval import clean, glossary, index, search

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "01b_failure_logs.jsonl"


@pytest.fixture(scope="module")
def idx():
    return index.build_index(LOGS, backend="tfidf")


LOG_INPUTS = {
    "multi-line log with ?token=": (
        "INFO starting job\n"
        "GET https://artifacts.internal/v1/ckpt?token=abc123XYZ&team=nlp\n"
        "RuntimeError: CUDA error: out of memory"
    ),
    "stack trace with URLs": (
        "Traceback (most recent call last):\n"
        '  File "/app/train.py", line 88, in <module>\n'
        "    fetch('https://data.internal/shard?id=7&sig=xyz')\n"
        "PermissionError: [Errno 13] Permission denied: '/data/shard-7'"
    ),
    "structured log with ?": (
        '{"ts": "2026-01-01T00:00:00Z", "level": "error", '
        '"msg": "download failed: out of memory", '
        '"url": "https://hub.internal/model?rev=main"}'
    ),
    "one-line error with a URL query": (
        "ERROR fetch https://hub.internal/weights?rev=3 failed: out of memory"
    ),
    "exception plus exit code": (
        "OSError: disk full writing https://store.internal/put?part=2, "
        "process exited with exit code 1"
    ),
}

QUESTION_INPUTS = {
    "question ending in ?": "my training job ran out of memory, what should I do?",
    "why did ... fail": "why did my job fail with permission denied",
    "short symptom, no ?": "gpu out of memory",
}


def log_query(idx, text):
    redacted = clean.redact(text)
    return "\n".join(clean.signal_lines(redacted, idx.noise))


@pytest.mark.parametrize("name", LOG_INPUTS)
def test_log_evidence_wins_over_a_question_mark(idx, name):
    text = LOG_INPUTS[name]
    kind, query = search.prepare_query(idx, text)
    assert kind == "log", name
    assert query == log_query(idx, text), "a log is cleaned, never expanded"


@pytest.mark.parametrize("name", QUESTION_INPUTS)
def test_questions_are_expanded(idx, name):
    text = QUESTION_INPUTS[name]
    kind, query = search.prepare_query(idx, text)
    assert kind == "question", name
    assert query == glossary.expand(text) != text, "a question gets glossary terms"


def test_a_url_question_mark_triggers_no_expansion(idx):
    text = LOG_INPUTS["multi-line log with ?token="]
    _, query = search.prepare_query(idx, text)
    for term in ("cuda out of memory", "outofmemoryerror", "allocate"):
        assert term not in query


def test_search_reports_the_same_input_type(idx):
    for text in LOG_INPUTS.values():
        assert search.search(idx, text).input_type == "log"
    for text in QUESTION_INPUTS.values():
        assert search.search(idx, text).input_type == "question"


# --- the classifier's tiers -------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    # 1. structural evidence
    ("why did this fail?\nRuntimeError: CUDA error: out of memory", "log"),
    ("    at com.example.Loader.read(Loader.java:42)", "log"),
    ('{"level": "warn", "msg": "is the disk full?"}', "log"),
    ("level=error component=loader msg=timeout", "log"),
    ("2026-01-01T10:00:00Z step 300 loss nan", "log"),
    ("[error] could not reach https://h/x?y=1", "log"),
    ("error: model download failed", "log"),
    ("torch.cuda.OutOfMemoryError: CUDA out of memory", "log"),
    # 2. a question cue beats log words inside one line
    ("what does errno 13 mean?", "question"),
    ("is OOMKilled the same as exit code 137?", "question"),
    ("ValueError on startup, any idea?", "question"),
    # a "?" inside a URL is not a question mark
    ("download from https://h/a?b=1 failed with exit code 1", "log"),
    ("download from https://h/a?b=1 failed", "question"),  # no log evidence at all
    # 3. exit codes in their usual forms
    ("Container exited with code 137", "log"),
    ("process exit status 2", "log"),
    # 4. short symptoms
    ("loss became nan after warmup", "question"),
    ("Error when pulling the image", "question"),
])  # fmt: skip
def test_classify_tiers(text, expected):
    assert clean.classify(text) == expected


def test_classify_is_deterministic_and_ignores_surrounding_space():
    for text in [*LOG_INPUTS.values(), *QUESTION_INPUTS.values()]:
        assert clean.classify(text) == clean.classify(f"  \n{text}\n  ")
