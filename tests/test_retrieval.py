"""Retrieval track: cleaning, redaction and the learned noise filter (sub-step A).

Unit tests use small hand-written strings; the corpus tests run on the real
01b_failure_logs.jsonl. Ids may appear here (tests), never in retrieval/.
"""

import json
import re
from pathlib import Path

import pytest

from retrieval import clean, glossary, quality

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "01b_failure_logs.jsonl"

# Fake secrets, one per form the brief lists. None may survive redact().
FAKE_TOKEN = "abcDEF123-fake.token_value"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl"
SECRET_CASES = [
    (f"DEBUG request headers: Authorization: Bearer {FAKE_TOKEN}", FAKE_TOKEN),
    (f"retrying with Bearer {FAKE_TOKEN}", FAKE_TOKEN),
    ("DEBUG env SERVICE_PASSWORD=hunter2-not-real", "hunter2-not-real"),
    ("db_password: s3cr3t-value", "s3cr3t-value"),
    ("MY_SECRET = top-secret-42", "top-secret-42"),
    ("passwd=letmein99", "letmein99"),
    ("API-KEY: key-0123456789", "key-0123456789"),
    ("export GITHUB_TOKEN=ghp_fake0123456789", "ghp_fake0123456789"),
    ('config {"api_key": "sk-fake-0123", "lr": 0.1}', "sk-fake-0123"),
    ('{"Password": "pa ss word"}', "pa ss word"),
    ('{"refresh_token": 123456789}', "123456789"),
    (f"cookie session={FAKE_JWT}", FAKE_JWT),
]


# --- redaction -----------------------------------------------------------------


@pytest.mark.parametrize("text, secret", SECRET_CASES)
def test_every_secret_form_is_redacted(text, secret):
    out, count = clean.redact_count(text)
    assert secret not in out
    assert "[REDACTED]" in out
    assert count >= 1
    assert clean.redact(text) == out


def test_the_key_of_a_secret_pair_goes_too():
    # "PASSWORD=" or "Bearer " left in place would still say what was there,
    # and sub-step C checks those strings never appear in any response.
    out = clean.redact("SERVICE_PASSWORD=x1 and Authorization: Bearer abc123def")
    assert "PASSWORD" not in out and "Bearer" not in out


def test_redaction_counts_each_secret_and_is_idempotent():
    text = "\n".join(t for t, _ in SECRET_CASES)
    out, count = clean.redact_count(text)
    assert count == len(SECRET_CASES)
    assert clean.redact_count(out) == (out, 0)  # nothing left to find


@pytest.mark.parametrize("text", [
    "AuthError: bearer token expired, rejected by auth service",  # a real error
    "401 Unauthorized: token expired at 2025-03-04T18:40:00Z",
    "403 Forbidden: access token expired for this API scope",
    "Registry credentials expired: rotate the pull secret and relaunch.",
    "jwt expired while calling /v1/models",
    "RuntimeError: torch.cuda.OutOfMemoryError in model.forward.call",
    "NCCL error: unhandled system error, NCCL version 2.16.x",
])  # fmt: skip
def test_error_text_that_only_mentions_secrets_is_kept(text):
    # Over-redacting these would delete the very line that names the cause.
    assert clean.redact_count(text) == (text, 0)


# --- line templates -------------------------------------------------------------


@pytest.mark.parametrize("line, expected", [
    ("INFO epoch 40 started", "info epoch <num> started"),
    ("NCCL error: unhandled system error, NCCL version 2.16.x",
     "nccl error: unhandled system error, nccl version <num>"),
    ("WARN slow dataloader: 7981 ms per batch", "warn slow dataloader: <num> ms per batch"),
    ("Tried to allocate 20.00 MiB (GPU 0; 7.5 GiB total)",
     "tried to allocate <num> mib (gpu <num>; <num> gib total)"),
    ("write /var/lib/jobs/speech/out.log: no space left on device",
     "write <path>: no space left on device"),
    ("No such file or directory: '/data/nlp/train/shard-4107.parquet'",
     "no such file or directory: <str>"),
    ('loading "model-v2" failed', "loading <str> failed"),
    ("worker 3f2a9c1d crashed at 0x7ffe12", "worker <hex> crashed at <hex>"),
    ("the cafe added a decade", "the cafe added a decade"),  # words, not hex
    ("  INFO   retrying  ", "info retrying"),
])  # fmt: skip
def test_template(line, expected):
    assert clean.template(line) == expected


def test_lines_that_differ_only_in_numbers_share_a_template():
    a = clean.template("RuntimeError: NCCL communicator was aborted on rank 3")
    b = clean.template("RuntimeError: NCCL communicator was aborted on rank 12")
    assert a == b


# --- classify (was is_log; same inputs, same outcomes) ----------------------------


@pytest.mark.parametrize("text, expected", [
    ("line one\nline two", "log"),
    ("WARN slow dataloader: 81 ms per batch", "log"),
    ("RuntimeError: CUDA error: out of memory", "log"),
    ("container finished with exit code 137", "log"),
    ("OSError: [Errno 28] No space left on device", "log"),
    ("failed with errno 13", "log"),
    ("my training job ran out of memory, what should I do?", "question"),
    ("Error when pulling the image from the registry", "question"),  # no class name
    ("why is my loss nan", "question"),
    ("  how do I fix a full disk?  \n", "question"),  # trailing newline is not "multi-line"
])  # fmt: skip
def test_classify_former_is_log_cases(text, expected):
    assert clean.classify(text) == expected


# --- tokenize ---------------------------------------------------------------------


def test_tokenize_lowercases_and_splits_on_non_alphanumerics():
    assert clean.tokenize("CUDA error: out_of memory!") == [
        "cuda", "error", "out", "of", "memory"]  # fmt: skip


def test_tokenize_emits_the_joined_form_of_hyphenated_words():
    assert clean.tokenize("all-reduce timeout") == [
        "allreduce",
        "all",
        "reduce",
        "timeout",
    ]


def test_tokenize_keeps_numbers():
    assert clean.tokenize("exit code 137, then 401") == [
        "exit",
        "code",
        "137",
        "then",
        "401",
    ]


@pytest.mark.parametrize("word, stem", [
    ("timeouts", "timeout"), ("killed", "kill"), ("loading", "load"),
    ("crashes", "crash"), ("devices", "device"), ("boxes", "box"),
    ("loss", "loss"), ("process", "process"),  # no "s" stripped after "s"
    ("nodes", "node"),  # 5 letters: stripped
    ("runs", "runs"), ("used", "used"),  # 4 letters or fewer: kept
    ("fp16s", "fp16s"),  # not purely alphabetic: kept
])  # fmt: skip
def test_tokenize_strips_light_suffixes_only_from_long_words(word, stem):
    assert clean.tokenize(word) == [stem]


def test_tokenize_treats_logs_and_questions_alike():
    # The same rules on both sides, so "timeouts" in a question meets
    # "timeout" in a log.
    assert clean.tokenize("Timeouts") == clean.tokenize("timeout")


# --- the learned noise filter ---------------------------------------------------------


def test_noise_is_learned_from_document_frequency():
    # Each log's error differs in a word (not a number, which templating hides).
    logs = [f"INFO epoch {i} started\nDEBUG heartbeat ok\nDEBUG heartbeat ok\n"
            f"Rare{chr(65 + i)}Error: boom" for i in range(10)]  # fmt: skip
    logs.append("DEBUG heartbeat ok\nINFO only info here")
    noise = clean.learn_noise(logs, noise_df=0.5)
    # df counts logs, not lines: "heartbeat ok" is in all 11 logs (twice in 10).
    assert noise == {"info epoch <num> started": 10, "debug heartbeat ok": 11}
    assert clean.signal_lines(logs[0], noise) == ["RareAError: boom"]


def test_a_template_at_exactly_the_threshold_is_not_noise():
    logs = ["common line\nA", "common line\nB", "C", "D"]  # df 2/4 = 0.5
    assert clean.learn_noise(logs, noise_df=0.5) == {}
    assert clean.learn_noise(logs, noise_df=0.49) == {"common line": 2}


def test_signal_lines_fall_back_when_everything_is_noise():
    noise = {"info epoch <num> started": 9, "warn slow dataloader: <num> ms per batch": 9,
             "debug heartbeat ok": 9}  # fmt: skip
    # Only noise left: keep the lines that are not INFO/DEBUG.
    log = "INFO epoch 1 started\nWARN slow dataloader: 10 ms per batch\nDEBUG heartbeat ok"
    assert clean.signal_lines(log, noise) == ["WARN slow dataloader: 10 ms per batch"]
    # Only INFO/DEBUG noise: keep the whole log rather than return nothing.
    log = "INFO epoch 1 started\nDEBUG heartbeat ok"
    assert clean.signal_lines(log, noise) == [
        "INFO epoch 1 started",
        "DEBUG heartbeat ok",
    ]


def test_signal_lines_drop_blank_lines_and_keep_order():
    assert clean.signal_lines("\n  B second  \n\nA first\n", {}) == [
        "B second",
        "A first",
    ]


# --- the real corpus -----------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus():
    records, malformed = quality.load_records(LOGS)
    return records, malformed


def test_the_corpus_loads_and_every_field_is_redacted(corpus):
    records, malformed = corpus
    assert len(records) == 59 and malformed == []
    raw = LOGS.read_text(encoding="utf-8")
    assert "EXAMPLE-NOT-A-REAL-TOKEN" in raw and "example-password-not-real" in raw
    stored = json.dumps([r.__dict__ for r in records])
    for leaked in ("EXAMPLE-NOT-A-REAL-TOKEN", "example-password-not-real",
                   "PASSWORD=", "Bearer ", "Authorization"):  # fmt: skip
        assert leaked not in stored, leaked
    assert sum(r.redactions for r in records) > 0


def test_the_noise_filter_removes_boilerplate_and_keeps_a_signal_line(corpus):
    records, _ = corpus
    logs = [r.log for r in records]
    noise = clean.learn_noise(logs, clean.DEFAULT_NOISE_DF)
    for boilerplate in ("debug heartbeat ok", "info epoch <num> started",
                        "warn deprecated flag --use_fp<num>, use --precision"):  # fmt: skip
        assert boilerplate in noise, boilerplate
    for r in records:
        kept = clean.signal_lines(r.log, noise)
        assert kept, r.id
        for line in kept:
            assert not re.search(r"heartbeat ok|epoch \d+ started|deprecated flag", line), (
                r.id, line)  # fmt: skip


def test_malformed_lines_are_skipped_and_reported(tmp_path):
    good = {"id": "X1", "team": "t", "log": "E: boom", "resolution": "fix it"}
    path = tmp_path / "logs.jsonl"
    path.write_text("\n".join([
        json.dumps(good),
        "{not json",
        json.dumps({"id": "X2", "team": "t", "log": "E"}),  # no resolution
        json.dumps({"id": "X3", "team": "t", "log": "", "resolution": ""}),  # empty log
        json.dumps({**good, "log": "again"}),  # repeated id
        "",  # blank lines are not records
        json.dumps({"id": "X4", "team": "t", "log": "E", "resolution": None}),
    ]) + "\n", encoding="utf-8")  # fmt: skip
    records, malformed = quality.load_records(path)
    assert [r.id for r in records] == ["X1"]
    assert [m[0] for m in malformed] == [2, 3, 4, 5, 7]
    assert "duplicate id" in malformed[3][1]


def test_the_report_is_reproducible(tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    quality.write_report(LOGS, a)
    quality.write_report(LOGS, b)
    assert a.read_bytes() == b.read_bytes()
    text = a.read_text()
    for section in ("## Records", "## Redactions", "## Raw vs signal lines",
                    "## Learned noise templates", "## Beliefs"):  # fmt: skip
        assert section in text, section
    assert "EXAMPLE-NOT-A-REAL-TOKEN" not in text and "example-password" not in text


# --- glossary (sub-step B) -------------------------------------------------------
# Mechanics are tested on a small glossary of the test's own, so editing the
# real entries never breaks these tests; the real one is checked for shape.

TOY = (
    glossary.Entry(("out of memory", "oom"), ("cuda out of memory", "oom"), "toy"),
    glossary.Entry(("disk full",), ("no space left on device",), "toy"),
    glossary.Entry(("nan",), ("nan", "divergence"), "toy"),
)


def test_expansion_appends_terms_and_keeps_every_original_word():
    q = "My job ran OUT of memory, why?"
    out = glossary.expand(q, TOY)
    assert out.startswith(q)  # nothing removed or rewritten
    assert out == q + " cuda out of memory oom"


def test_expansion_matches_whole_tokens_in_order():
    assert glossary.expand("the bloom filter", TOY) == "the bloom filter"  # not "oom"
    assert glossary.expand("nanny cam", TOY) == "nanny cam"  # not "nan"
    assert glossary.expand("memory out of", TOY) == "memory out of"  # order matters
    # Same tokenizer as everywhere: hyphens and suffixes still match.
    assert glossary.expand("out-of-memory", TOY).endswith(" cuda out of memory oom")
    assert glossary.expand("disks full", TOY).endswith("no space left on device")


def test_expansion_adds_each_term_once_in_glossary_order():
    out = glossary.expand("oom and nan, disk full, oom again", TOY)
    added = out.removeprefix("oom and nan, disk full, oom again ")
    assert added == "cuda out of memory oom no space left on device nan divergence"


def test_no_match_leaves_the_question_unchanged():
    assert glossary.expand("how do I reset my account password", TOY) == (
        "how do I reset my account password"
    )


def test_the_glossary_is_small_and_every_entry_says_why():
    entries = glossary.GLOSSARY
    assert 0 < len(entries) <= 30
    for e in entries:
        assert e.phrases and e.terms, e
        assert e.why.strip() and "\n" not in e.why, e  # a one-line reason
        assert all(glossary.tokens(p) for p in e.phrases), e  # each can match
    phrases = [p for e in entries for p in e.phrases]
    assert len(phrases) == len(set(phrases))  # a phrase belongs to one entry


def test_the_glossary_is_written_without_the_queries():
    source = (ROOT / "retrieval" / "glossary.py").read_text(encoding="utf-8")
    # No reference to the evaluation file (the word "queries" in prose is fine).
    for ref in ("01c", "retrieval_queries", ".jsonl"):
        assert ref not in source, ref
    assert "open(" not in source  # built from knowledge, not from a file


def test_the_glossary_table_lists_every_entry():
    table = glossary.to_markdown()
    assert table.count("\n| ") == len(glossary.GLOSSARY)


def test_a_multi_word_phrase_may_skip_up_to_two_bridge_words():
    assert glossary.expand("my disk is full", TOY).endswith("no space left on device")
    assert glossary.expand("disk is completely full", TOY).endswith(
        "no space left on device"
    )
    # Three bridge words is too far; a non-bridge word breaks the phrase.
    assert glossary.expand("disk is now completely full", TOY) == (
        "disk is now completely full"
    )
    assert glossary.expand("disk not full", TOY) == "disk not full"


def test_a_single_word_phrase_never_bridges():
    assert glossary.expand("oom", TOY).endswith("cuda out of memory oom")
    assert glossary.expand("bloom", TOY) == "bloom"


# classify (was is_question_like / expand_for_retrieval; same inputs, same
# outcomes; "" was False for both old functions and is now "question").
@pytest.mark.parametrize("text, expected", [
    ("what does exit code 137 mean?", "question"),
    ("why is training hanging", "question"),
    ("Explain the NCCL error", "question"),
    ("RuntimeError: CUDA error: out of memory", "log"),
    ("container finished with exit code 137", "log"),
    ("", "question"),
])  # fmt: skip
def test_classify_former_is_question_like_cases(text, expected):
    assert clean.classify(text) == expected


def test_a_clear_question_is_a_question_even_if_it_looks_like_a_log():
    # Former expand_for_retrieval cases: the question is expanded, the log is not.
    assert clean.classify("what does oom mean?") == "question"
    assert glossary.expand("what does oom mean?", TOY) == (
        "what does oom mean? cuda out of memory oom"
    )
    assert clean.classify("RuntimeError: oom while allocating") == "log"
    assert clean.classify("oom") == "question"
    assert glossary.expand("oom", TOY).endswith("oom")


def test_no_corpus_log_is_taken_for_a_question(corpus):
    records, _ = corpus
    assert [r.id for r in records if clean.classify(r.log) != "log"] == []
