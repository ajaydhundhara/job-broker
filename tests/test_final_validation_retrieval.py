"""Final validation of retrieval behaviour (FINAL_ASSESSMENT_VALIDATION_TASK §4-6).

§4 resolution inference, §5 cause grouping, §6 log / question classification.
Fixture corpora are small, hand-written and live in this file; their
expected-cause labels (CAUSE) are written independently of the grouping rule
(normalised resolution text) so the two can be compared.

Gaps found are pinned as strict xfail tests stating the DESIRED behaviour:
they fail today (so xfail passes) and turn into errors the day the behaviour
changes, forcing the xfail to be removed. No rule, threshold or glossary entry
was changed to make anything here pass.
"""

import json
from itertools import combinations

import pytest
from fastapi.testclient import TestClient

from broker.api import create_app
from retrieval import clean, index, search
from retrieval.quality import DEFAULT_LOGS

OOM = "Reduce batch size or request a larger GPU class."
DISK = "Scratch disk full: clean /tmp and write checkpoints to the volume."
TOK = "Service token expired: renew the team token."
SECRET_VALUES = ("hunter2SECRETvalue", "abcDEF1234567890xyz", "eyJhbGciOiJIUzI1NiJ9")


def write_corpus(tmp_path, rows) -> str:
    path = tmp_path / "logs.jsonl"
    path.write_text("".join(json.dumps({"id": i, "team": "t", "log": log,
                                        "resolution": res}) + "\n"
                            for i, log, res in rows))  # fmt: skip
    return path


# --- §4 resolution inference ---------------------------------------------------------

INFERENCE_ROWS = [
    ("A1", "RuntimeError: CUDA error: out of memory while running forward pass at step 10", OOM),
    ("A2", "torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB", OOM),
    ("B1", "Checkpoint save failed: no space left on device on /scratch", DISK),
    ("B2", "write /scratch/out.log: no space left on device", DISK),
    ("C1", "401 Unauthorized: token expired at 2025-03-04T18:40:00Z", TOK),
    ("C2", "AuthError: bearer token expired, rejected by auth service", TOK),
    # recorded, but worded like the OOM group: inference must not touch it
    ("C3", "CUDA error: out of memory while running forward pass", TOK),
    ("E1", "RuntimeError: CUDA error: out of memory while running forward pass at step 77", ""),
    ("E2", "kernel module nvsomething failed to load firmware blob", ""),
    ("E3", "CUDA out of memory while saving checkpoint: no space left on device", ""),
    ("E4", "Checkpoint upload failed: 403 Forbidden, bucket policy denies write for this token", ""),
    ("E5", ("INFO epoch 3 started password=hunter2SECRETvalue\n"
           "Authorization: Bearer abcDEF1234567890xyz\n"
           "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkw.c2lnbmF0dXJlMTIzNDU2\n"
           "RuntimeError: CUDA error: out of memory at step 5"), ""),
]  # fmt: skip


@pytest.fixture(scope="module")
def inf_index(tmp_path_factory):
    path = write_corpus(tmp_path_factory.mktemp("inf"), INFERENCE_ROWS)
    return index.build_index(path, backend="tfidf")


@pytest.fixture(scope="module")
def inf_client(inf_index, tmp_path_factory):
    db = tmp_path_factory.mktemp("inf_db") / "broker.db"
    with TestClient(create_app(db, retrieval_index=inf_index)) as c:
        yield c


def group_scores(idx, log_id) -> dict[str, float]:
    """Best-member score of an empty-resolution log against each recorded group,
    computed the way build_index does (for evidence, not re-implementing it)."""
    s = index._log_scores(idx.embedder, idx.log_bm25, idx.log_vecs,
                          " ".join(idx.signal[log_id]), idx.params)  # fmt: skip
    out = {}
    for g in idx.groups:
        recorded = [m for m in g.members if idx.status[m] == "recorded"]
        if recorded:
            out[g.resolution] = round(
                max(float(s[idx.position[m]]) for m in recorded), 6
            )
    return out


def test_inference_threshold_is_the_saved_assign_min(inf_index):
    assert inf_index.params.assign_min == 0.3
    assert inf_index.params == index.load_params("tfidf")


def test_case1_a_clear_resemblance_is_inferred_with_its_score(inf_index):
    assert inf_index.status["E1"] == "inferred"
    assert inf_index.group("E1").resolution == OOM
    assert inf_index.inferred["E1"] == group_scores(inf_index, "E1")[OOM]
    assert inf_index.inferred["E1"] > 0.7


def test_case2_a_weak_match_stays_none(inf_index):
    scores = group_scores(inf_index, "E2")
    assert max(scores.values()) < inf_index.params.assign_min
    assert inf_index.status["E2"] == "none"
    assert search.resolution(inf_index, "E2") is None


def test_case3_two_close_groups_are_measured(inf_index):
    scores = sorted(group_scores(inf_index, "E3").values(), reverse=True)
    assert scores[0] >= scores[1] >= inf_index.params.assign_min  # both qualify
    assert inf_index.status["E3"] == "inferred"  # today: the higher one wins


@pytest.mark.xfail(strict=True, reason="no top-two margin check: an ambiguous "
                   "log is inferred and its note names only the winning group")  # fmt: skip
def test_case3_desired_an_ambiguous_match_is_not_inferred_silently(inf_index):
    note = search.get_failure(inf_index, "E3")["note"] or ""
    assert inf_index.status["E3"] == "none" or "runner-up" in note


@pytest.mark.xfail(strict=True, reason="assign_min 0.3 lets shared words "
                   "('checkpoint', 'token') infer a fix for a different root cause")  # fmt: skip
def test_case4_desired_misleading_shared_vocabulary_is_not_inferred(inf_index):
    # A 403 from a bucket policy is neither a full disk nor an expired token.
    assert inf_index.status["E4"] == "none"


def test_case4_actual_behaviour_is_recorded(inf_index):
    assert inf_index.status["E4"] == "inferred"
    assert inf_index.group("E4").resolution == DISK
    assert inf_index.params.assign_min <= inf_index.inferred["E4"] < 0.45


def test_case5_a_recorded_resolution_is_never_overwritten(inf_index):
    assert inf_index.status["C3"] == "recorded"
    assert search.resolution(inf_index, "C3") == TOK
    assert "C3" not in inf_index.inferred


def test_case6_inferred_is_visible_in_the_api(inf_client, inf_index):
    body = inf_client.get("/failures/E1").json()
    assert body["resolution_status"] == "inferred"
    assert body["resolution"] == OOM
    assert f"similarity {inf_index.inferred['E1']:.3f}" in body["note"]
    hits = inf_client.post("/failures/search", json={"text": "CUDA out of memory"})
    statuses = {h["id"]: h["resolution_status"] for h in hits.json()["results"]}
    assert statuses.get("E1") == "inferred" and statuses.get("A1") == "recorded"


def test_case7_an_unavailable_resolution_is_null_not_invented(inf_client):
    body = inf_client.get("/failures/E2").json()
    assert body["resolution"] is None
    assert body["resolution_status"] == "none"
    assert body["note"] == "no resolution recorded for this failure"


def test_case8_secrets_are_redacted_before_inference_and_indexing(
    inf_index, inf_client
):
    assert (
        inf_index.status["E5"] == "inferred"
    )  # inference still works on redacted text
    stored = json.dumps([inf_index.by_id["E5"].log, inf_index.signal["E5"],
                         inf_index.log_docs, inf_index.group_docs])  # fmt: skip
    assert not any(s in stored for s in SECRET_VALUES)
    assert inf_index.by_id["E5"].redactions >= 3
    api = (
        inf_client.get("/failures/E5").text
        + inf_client.post(
            "/failures/search",
            json={"text": "out of memory password=hunter2SECRETvalue"},
        ).text
    )
    assert not any(s in api for s in SECRET_VALUES)


# --- §5 cause grouping -------------------------------------------------------------------

GROUPING_ROWS = [
    # 1. same cause, identical resolution, different log wording
    ("G1a", "RuntimeError: CUDA error: out of memory", "Reduce batch size."),
    ("G1b", "OOM when allocating tensor with shape [32, 4096]", "Reduce batch size."),
    # 2. same cause, resolution differs only in case / spacing
    ("G2", "torch.cuda.OutOfMemoryError: CUDA out of memory", "  reduce   BATCH size. "),
    # 3. different causes, the same generic resolution
    ("G3a", "Watchdog caught collective operation timeout: WorkNCCL(ALLREDUCE)", "Relaunch the job."),
    ("G3b", "Heartbeat missed 3 times; node marked lost", "Relaunch the job."),
    # 4. same cause, different resolution wording
    ("G4a", "write /scratch/out.log: no space left on device", "Clean /tmp."),
    ("G4b", "tar: Error writing archive: No space left on device", "Free scratch space and retry."),
    # 5. identical log, conflicting recorded resolutions
    ("G5a", "Timed out waiting for response (30000 ms)", "Provider slow: retry with backoff."),
    ("G5b", "Timed out waiting for response (30000 ms)", "Dataset service overloaded: lower workers."),
    # 6. singleton, no resolution, unlike anything
    ("G6", "kernel module nvsomething failed to load firmware blob", ""),
    # 7. empty resolution that should join the OOM group
    ("G7", "RuntimeError: CUDA error: out of memory at step 12", ""),
    # 8. empty resolution that should not join any group
    ("G8", "SSL: CERTIFICATE_VERIFY_FAILED while fetching weights", ""),
]  # fmt: skip

# Independent expected root causes, from the log text alone. G5a/G5b are one
# ambiguous cause (identical logs); the grouping rule cannot know which fix holds.
CAUSE = {"G1a": "gpu_oom", "G1b": "gpu_oom", "G2": "gpu_oom", "G7": "gpu_oom",
         "G3a": "nccl_timeout", "G3b": "node_lost",
         "G4a": "disk_full", "G4b": "disk_full",
         "G5a": "timeout", "G5b": "timeout",
         "G6": "firmware", "G8": "tls"}  # fmt: skip


def pairwise(group_of: dict[str, str], cause: dict[str, str]) -> dict:
    """Pairwise precision / recall: a pair is predicted together when it shares a
    group, and truly together when it shares an expected cause."""
    ids = sorted(cause)
    pred = {p for p in combinations(ids, 2) if group_of[p[0]] == group_of[p[1]]}
    true = {p for p in combinations(ids, 2) if cause[p[0]] == cause[p[1]]}
    tp = pred & true
    return {
        "precision": round(len(tp) / len(pred), 6) if pred else None,
        "recall": round(len(tp) / len(true), 6) if true else None,
        "false_merges": sorted(pred - true),
        "false_splits": sorted(true - pred),
    }


@pytest.fixture(scope="module")
def grp_index(tmp_path_factory):
    path = write_corpus(tmp_path_factory.mktemp("grp"), GROUPING_ROWS)
    return index.build_index(path, backend="tfidf")


def test_grouping_cases_1_2_identical_and_case_spacing_variants_share_a_group(
    grp_index,
):
    g = grp_index.group_of
    assert g["G1a"] == g["G1b"] == g["G2"]


def test_grouping_case3_a_generic_resolution_merges_different_causes(grp_index):
    assert grp_index.group_of["G3a"] == grp_index.group_of["G3b"]  # a false merge


def test_grouping_case4_reworded_resolution_splits_one_cause(grp_index):
    assert grp_index.group_of["G4a"] != grp_index.group_of["G4b"]  # a false split


def test_grouping_case5_identical_logs_with_conflicting_fixes(grp_index):
    assert grp_index.group_of["G5a"] != grp_index.group_of["G5b"]
    assert [c.ids for c in grp_index.conflicts] == [("G5a", "G5b")]
    for log_id in ("G5a", "G5b"):
        assert search.CONFLICT_NOTE in search.get_failure(grp_index, log_id)["note"]


def test_grouping_cases_6_7_8_singletons_and_inferred_members(grp_index):
    assert grp_index.status["G6"] == "none"
    assert grp_index.group("G6").members == ("G6",)
    assert grp_index.status["G7"] == "inferred"
    assert grp_index.group_of["G7"] == grp_index.group_of["G1a"]
    assert grp_index.status["G8"] == "none"


def test_grouping_against_independent_labels(grp_index):
    m = pairwise(grp_index.group_of, CAUSE)
    assert m["false_merges"] == [("G3a", "G3b")]
    assert m["false_splits"] == [("G4a", "G4b"), ("G5a", "G5b")]
    assert (m["precision"], m["recall"]) == (
        0.857143,
        0.75,
    )  # 6 of 7 predicted pairs, 6 of 8 true


# The real corpus, labelled from its LOG TEXT only (not its resolutions): a
# second opinion, not ground truth. Differences from the resolution groups:
# exit-137 OOMKilled is host memory, not GPU memory; a missed heartbeat is not
# by itself proof of an interruptible reclaim; "pull access denied ... may
# require login" may be a missing repository rather than expired credentials
# (kept with registry auth here, as the log allows both).
REAL_CAUSE = {
    **dict.fromkeys(["F001", "F002", "F003", "F004", "F005", "F910"], "gpu_oom"),
    **dict.fromkeys(["F006", "F911", "F912"], "host_oom_killed"),
    **dict.fromkeys(["F049", "F050", "F051", "F052", "F053", "F054"], "cuda_compat"),
    **dict.fromkeys(["F025", "F026", "F027", "F028", "F029", "F030"], "nccl"),
    **dict.fromkeys(["F031", "F032", "F033", "F034", "F035", "F036"], "disk_full"),
    **dict.fromkeys(["F013", "F014", "F015", "F016", "F017", "F018"], "dataset_access"),
    **dict.fromkeys(["F007", "F008", "F009", "F010", "F011", "F012"], "registry_pull"),
    **dict.fromkeys(["F043", "F044", "F045", "F046", "F047", "F048"], "token_expired"),
    **dict.fromkeys(["F037", "F038", "F039", "F041"], "reclaim_notice"),
    **dict.fromkeys(["F040", "F042"], "heartbeat_lost"),
    **dict.fromkeys(["F019", "F020", "F021", "F022", "F023", "F024"], "divergence"),
    **dict.fromkeys(["F900", "F901"], "timeout_unknown"),
}


def test_real_corpus_grouping_against_log_only_labels():
    idx = index.build_index(DEFAULT_LOGS, backend="tfidf")
    assert set(REAL_CAUSE) == set(idx.by_id)  # every record labelled once
    m = pairwise(idx.group_of, REAL_CAUSE)
    merged = {
        tuple(sorted({REAL_CAUSE[a], REAL_CAUSE[b]})) for a, b in m["false_merges"]
    }
    assert merged == {
        ("gpu_oom", "host_oom_killed"),
        ("heartbeat_lost", "reclaim_notice"),
    }
    assert m["false_splits"] == [("F900", "F901")]
    assert (m["precision"], m["recall"]) == (0.833333, 0.992366)  # 130/156, 130/131


# --- §6 log / question classification ----------------------------------------------------

CLASSIFY_CASES = [
    # (case, text, expected input type)
    ("1 plain symptom question", "why is my GPU running out of memory", "question"),
    ("2 explicit question mark", "is the scratch disk full?", "question"),
    ("3 exit-code question", "what does exit code 137 mean?", "question"),
    ("4 single-line exception", "RuntimeError: CUDA error: out of memory", "log"),
    ("5 multiline timestamps + levels",
     ("2025-03-06T10:00:00Z INFO loading shard 3\n"
     "2025-03-06T10:00:05Z ERROR write /scratch/ckpt: no space left on device"), "log"),
    ("6 multiline log with URL ?",
     ("INFO GET https://hub.internal/api/model?revision=3\n"
     "ERROR download failed: 401 Unauthorized: token expired"), "log"),
    ("7 question then pasted log",
     "why did this fail?\n2025-03-06T11:00:00Z ERROR NCCL communicator was aborted on rank 3",
     "log"),
    ("8 structured JSON log",
     '{"ts": "2025-03-06T10:00:00Z", "level": "error", "msg": "no space left on device"}',
     "log"),
    ("9 key=value log", "level=error component=trainer msg=nan_loss step=4077", "log"),
    ("10 short symptom phrase", "disk full on scratch", "question"),
    ("11 question with log vocabulary",
     "how do I fix NCCL error: unhandled system error", "question"),
    ("12 log line with why/how in a path",
     "ERROR open /srv/why/how/config.yaml: no such file or directory", "log"),
    ("13 log with bearer/password/JWT",
     ("Authorization: Bearer abcDEF1234567890xyz\n"
     "password=hunter2SECRETvalue jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkw.c2lnbmF0dXJlMTIzNDU2\n"
     "AuthError: bearer token expired, rejected by auth service"), "log"),
]  # fmt: skip
IDS = [c[0] for c in CLASSIFY_CASES]


@pytest.fixture(scope="module")
def real_index():
    return index.build_index(DEFAULT_LOGS, backend="tfidf")


@pytest.fixture(scope="module")
def real_client(real_index, tmp_path_factory):
    db = tmp_path_factory.mktemp("cls_db") / "broker.db"
    with TestClient(create_app(db, retrieval_index=real_index)) as c:
        yield c


@pytest.mark.parametrize("case, text, expected", CLASSIFY_CASES, ids=IDS)
def test_classifier_and_pipeline_agree(real_index, real_client, case, text, expected):
    assert clean.classify(clean.redact(text)) == expected
    kind, scored = search.prepare_query(real_index, text)
    assert kind == expected
    if expected == "log":  # cleaned to its signal lines, never glossary-expanded
        assert scored == "\n".join(
            clean.signal_lines(clean.redact(text), real_index.noise)
        )
    body = real_client.post("/failures/search", json={"text": text})
    assert body.status_code == 200
    assert body.json()["input_type"] == expected
    assert not any(s in body.text for s in SECRET_VALUES)
    assert not any(s in scored for s in SECRET_VALUES)


def test_case3_exit_code_question_is_expanded_without_memory_terms(real_index):
    kind, scored = search.prepare_query(real_index, "what does exit code 137 mean?")
    assert kind == "question" and "sigkill" in scored
    assert "oomkilled" not in scored  # 137 alone is not proof of OOM (glossary note)


def test_case6_a_url_question_mark_does_not_make_a_log_a_question():
    one_line = "ERROR fetch https://hub.internal/api/model?revision=3 failed"
    assert clean.classify(one_line) == "log"


def test_case13_redaction_runs_before_classification():
    text = CLASSIFY_CASES[12][1]
    assert not any(s in clean.redact(text) for s in SECRET_VALUES)


@pytest.mark.parametrize("blank", ["", "   ", "\n\t  \n"])
def test_case14_blank_input_is_rejected_by_the_api(real_client, blank):
    assert clean.classify(blank) == "question"  # documented: the API rejects it
    assert real_client.post("/failures/search", json={"text": blank}).status_code == 422


@pytest.mark.xfail(strict=True, reason="a one-line docker daemon error has no "
                   "level, timestamp or 'SomeError:' prefix, so it is classified as "
                   "a question and glossary-expanded (retrieval still finds g08)")  # fmt: skip
def test_desired_a_one_line_daemon_error_is_a_log():
    text = ("Error response from daemon: pull access denied for registry.internal/ml/eval, "
            "repository does not exist or may require login")  # fmt: skip
    assert clean.classify(text) == "log"
