"""Retrieval track, sub-step C: index, search and the /failures API.

Runs offline on the tfidf backend. Ids appear here (tests) but never in
retrieval/ (see test_no_log_or_query_id_literals_in_retrieval_code).
"""

import json
import logging
import re
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from broker.api import create_app
from retrieval import bm25, clean, embed, index, search

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "01b_failure_logs.jsonl"
RAW = [
    json.loads(x) for x in LOGS.read_text(encoding="utf-8").splitlines() if x.strip()
]
# Strings that must never be stored, returned or logged.
SECRETS = (
    "Bearer ",
    "PASSWORD=",
    "EXAMPLE-NOT-A-REAL-TOKEN",
    "example-password-not-real",
)
OFF_TOPIC = "how do I reset my account password"


@pytest.fixture(scope="module")
def idx():
    return index.build_index(LOGS, backend="tfidf")


@pytest.fixture(scope="module")
def client(idx, tmp_path_factory):
    db = tmp_path_factory.mktemp("retrieval") / "broker.db"
    with TestClient(create_app(db, retrieval_index=idx)) as c:
        yield c


def no_secret(text: str) -> bool:
    return not any(s in text for s in SECRETS)


def first_group(result) -> str:
    return result.results[0]["cause_group"]


# --- BM25 -------------------------------------------------------------------------


def test_bm25_ranks_the_matching_document_first_and_scores_unknown_terms_zero():
    docs = [["nccl", "timeout"], ["disk", "full"], ["nccl", "nccl", "abort", "rank"]]
    model = bm25.BM25(docs)
    scores = model.scores(["nccl"])
    assert scores[1] == 0 and scores[0] > 0 and scores[2] > 0
    assert model.scores(["unseen"]).tolist() == [0.0, 0.0, 0.0]
    assert model.scores(["disk"]).argmax() == 1


def test_bm25_counts_a_repeated_query_term_once():
    # Glossary expansion may repeat a word the user typed; that must not
    # double its weight.
    model = bm25.BM25([["oom", "kill"], ["disk"]])
    assert model.scores(["oom", "oom"]).tolist() == model.scores(["oom"]).tolist()


def test_bm25_idf_is_never_negative():
    model = bm25.BM25([["a"], ["a"], ["a", "b"]])  # "a" is in every document
    assert (model.scores(["a"]) >= 0).all()


# --- embeddings ---------------------------------------------------------------------


def test_tfidf_embeddings_are_l2_normalised_and_deterministic():
    e = embed.TfidfEmbedder()
    e.fit(["cuda out of memory", "no space left on device", "nccl timeout"])
    a = e.embed(["out of memory on gpu", "nothing known here zzz"])
    assert a.shape[0] == 2
    assert np.isclose(np.linalg.norm(a[0]), 1.0)
    assert np.array_equal(
        a, e.embed(["out of memory on gpu", "nothing known here zzz"])
    )


def test_backend_selection(monkeypatch, caplog):
    monkeypatch.setenv("RETRIEVAL_BACKEND", "tfidf")
    assert embed.select_backend().name == "tfidf"
    monkeypatch.setenv("RETRIEVAL_BACKEND", "nope")
    with pytest.raises(ValueError):
        embed.select_backend()
    # auto without a cached MiniLM: tfidf, with exactly one warning.
    monkeypatch.setenv("RETRIEVAL_BACKEND", "auto")
    monkeypatch.setattr(embed, "MiniLMEmbedder", _unavailable)
    with caplog.at_level(logging.WARNING, logger="retrieval.embed"):
        assert embed.select_backend().name == "tfidf"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_an_explicit_minilm_request_fails_loudly_when_unavailable(monkeypatch):
    monkeypatch.setattr(embed, "MiniLMEmbedder", _unavailable)
    with pytest.raises(RuntimeError, match="MiniLM"):
        embed.select_backend("minilm")


def _unavailable():
    raise OSError("not in the local cache")


@pytest.mark.skipif(not embed.minilm_available(), reason="MiniLM not cached locally")
def test_minilm_backend_when_cached():
    e = embed.MiniLMEmbedder()
    a = e.embed(["cuda out of memory", "cuda out of memory"])
    assert np.isclose(np.linalg.norm(a[0]), 1.0) and np.allclose(a[0], a[1])


# --- params ------------------------------------------------------------------------


def test_params_load_per_backend_and_reject_unknown_keys(tmp_path):
    # The shipped file holds an entry per backend (tuned by retrieval.evaluate);
    # what matters here is that the entry is the one loaded, not its values.
    shipped = json.loads(index.PARAMS_PATH.read_text())["tfidf"]
    assert index.load_params("tfidf") == index.Params(**shipped)
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"tfidf": {"tau": 0.5}}))
    assert index.load_params("tfidf", path).tau == 0.5
    assert index.load_params("minilm", path) == index.Params()
    path.write_text(json.dumps({"tfidf": {"tua": 0.5}}))
    with pytest.raises(ValueError, match="tua"):
        index.load_params("tfidf", path)


# --- ingestion -----------------------------------------------------------------------


def test_no_secret_is_stored_anywhere_in_the_index(idx):
    stored = [r.log for r in idx.records] + [r.resolution for r in idx.records]
    stored += idx.log_docs + idx.group_docs
    stored += [x for lines in idx.signal.values() for x in lines]
    assert all(no_secret(s) for s in stored)
    vocab = set(idx.log_bm25.idf) | set(idx.group_bm25.idf)
    for fake in ("EXAMPLE-NOT-A-REAL-TOKEN-feed", "example-password-not-real"):
        assert not set(clean.tokenize(fake)[:1]) & vocab, fake  # the joined form


def test_every_log_belongs_to_one_group_and_groups_follow_resolutions(idx):
    assert sorted(idx.group_of) == sorted(r.id for r in idx.records)
    for g in idx.groups:
        recorded = {
            " ".join(idx.by_id[m].resolution.lower().split())
            for m in g.members
            if idx.status[m] == "recorded"
        }
        assert len(recorded) <= 1  # one group = one resolution text


def test_empty_resolutions_join_the_out_of_memory_group_and_are_inferred_unless_ambiguous(
    idx,
):
    # SPEC_DECISIONS #17: every empty log joins its best group for retrieval;
    # its fix is inferred only when that is the ONLY group at or above
    # ASSIGN_MIN. F910 also reaches the driver/framework and NCCL groups, so it
    # is ambiguous: status none, no fix shown.
    empty = [r for r in idx.records if not r.resolution.strip()]
    assert len(empty) == 3
    for r in empty:
        assert re.search(r"(?i)out of memory|oom", " ".join(idx.signal[r.id]))
        group = idx.group(r.id)
        assert "batch size" in group.resolution.lower()
        qualifying = [g for g, s in idx.inference[r.id] if s >= idx.params.assign_min]
        assert qualifying[0] == group.id
        expected = "inferred" if len(qualifying) == 1 else "none"
        assert idx.status[r.id] == expected, (r.id, idx.inference[r.id][:3])
        if expected == "inferred":
            assert idx.inferred[r.id] >= idx.params.assign_min
    assert {r.id: idx.status[r.id] for r in empty} == {
        "F910": "none", "F911": "inferred", "F912": "inferred"}  # fmt: skip


def test_a_low_similarity_empty_resolution_stays_a_singleton(tmp_path):
    rows = [
        {"id": "a1", "team": "t", "log": "CUDA error: out of memory", "resolution": "Fix A."},
        {"id": "a2", "team": "t", "log": "OOM when allocating", "resolution": "Fix A."},
        {"id": "b1", "team": "t", "log": "quantum flux capacitor misaligned", "resolution": ""},
    ]  # fmt: skip
    path = tmp_path / "logs.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    small = index.build_index(path, backend="tfidf")
    assert small.status["b1"] == "none"
    assert small.group("b1").members == ("b1",)
    assert small.group("b1").resolution == ""


def test_identical_text_with_different_resolutions_is_a_conflict(idx):
    assert len(idx.conflicts) == 1
    ids = idx.conflicts[0].ids
    assert len(ids) == 2
    a, b = (idx.by_id[i] for i in ids)
    assert a.log == b.log and a.resolution != b.resolution
    assert sorted(idx.conflicts[0].resolutions) == sorted([a.resolution, b.resolution])
    assert idx.group_of[a.id] != idx.group_of[b.id]  # each keeps its own


# --- search ------------------------------------------------------------------------------


def test_an_off_topic_question_returns_nothing(idx):
    out = search.search(idx, OFF_TOPIC)
    assert out.no_match and out.results == []
    assert out.message == "No similar past failure found"
    assert out.input_type == "question"


def test_a_full_70_line_log_finds_its_own_group_first(idx):
    raw = max(RAW, key=lambda r: r["log"].count("\n"))
    assert raw["log"].count("\n") + 1 >= 70
    out = search.search(idx, raw["log"])
    assert out.input_type == "log"
    assert first_group(out) == idx.group_of[raw["id"]]


def test_every_log_finds_its_own_group_first(idx):
    conflicted = {i for c in idx.conflicts for i in c.ids}
    for raw in RAW:
        out = search.search(idx, raw["log"])
        allowed = {idx.group_of[i] for i in conflicted} if raw["id"] in conflicted else {
            idx.group_of[raw["id"]]}  # fmt: skip
        assert first_group(out) in allowed, raw["id"]


def _shift_digits(text: str) -> str:
    return re.sub(r"\d", lambda m: str((int(m.group()) + 3) % 10), text)


def test_a_log_with_its_numbers_changed_still_finds_its_group(idx):
    conflicted = {i for c in idx.conflicts for i in c.ids}
    changed = 0
    for raw in RAW:
        if raw["id"] in conflicted or not re.search(
            r"\d", " ".join(idx.signal[raw["id"]])
        ):
            continue
        out = search.search(idx, _shift_digits(raw["log"]))
        assert first_group(out) == idx.group_of[raw["id"]], raw["id"]
        changed += 1
    assert changed >= 30  # steps, shards, versions, sizes, ranks


def test_results_are_group_by_group_never_below_tau_and_at_most_k(idx):
    for text in ("my job ran out of memory", "NCCL watchdog timeout on allreduce",
                 "disk is full", RAW[0]["log"]):  # fmt: skip
        for k in (1, 3, 5, 20):
            out = search.search(idx, text, k=k)
            assert len(out.results) <= k
            groups = [r["cause_group"] for r in out.results]
            # each group's members come together, never interleaved
            assert groups == sorted(groups, key=groups.index)
            assert all(r["score"] >= idx.params.tau for r in out.results)
            scores = [r["score"] for r in out.results]
            assert scores == sorted(scores, reverse=True)


def test_search_is_deterministic(idx):
    for text in ("loss became nan", RAW[5]["log"], OFF_TOPIC):
        assert search.search(idx, text) == search.search(idx, text)
    again = index.build_index(LOGS, backend="tfidf")
    assert search.search(again, "loss became nan") == search.search(
        idx, "loss became nan"
    )


def test_a_question_with_an_exit_code_is_expanded(idx):
    out = search.search(idx, "what does exit code 137 mean?")
    assert out.input_type == "question"
    # 137 is SIGKILL, not proof of OOM (P0-03): termination terms, no OOM terms.
    assert "sigkill" in out.query and "oomkilled" not in out.query


def test_a_pasted_log_is_noise_filtered_even_with_a_question_mark(idx):
    log = ("INFO epoch 3 started\nDEBUG heartbeat ok\n"
           "GET /v1/models?team=nlp failed\nRuntimeError: CUDA error: out of memory")  # fmt: skip
    out = search.search(idx, log)
    assert "heartbeat" not in out.query and "epoch" not in out.query
    assert "out of memory" in out.query


def test_a_conflicting_match_carries_a_note(idx):
    log = idx.by_id[idx.conflicts[0].ids[0]].log
    out = search.search(idx, log)
    noted = [r for r in out.results if r["id"] in idx.conflicts[0].ids]
    assert len(noted) == 2
    assert all("conflicting resolutions recorded for an identical log" in r["note"]
               for r in noted)  # fmt: skip


def test_inferred_and_missing_resolutions_say_so(idx):
    # The first empty log (F910) is ambiguous under SPEC_DECISIONS #17, so the
    # inferred case uses the first INFERRED empty log; the ambiguous one is
    # checked too: no fix, and a note naming its candidate groups.
    empty = [r for r in idx.records if not r.resolution.strip()]
    inferred = next(r for r in empty if idx.status[r.id] == "inferred")
    out = search.search(idx, inferred.log)
    hit = next(r for r in out.results if r["id"] == inferred.id)
    assert hit["resolution_status"] == "inferred"
    assert hit["resolution"] == idx.group(inferred.id).resolution
    assert "inferred" in hit["note"]
    ambiguous = next(r for r in empty if idx.status[r.id] == "none")
    out = search.search(idx, ambiguous.log)
    hit = next(r for r in out.results if r["id"] == ambiguous.id)
    assert hit["resolution_status"] == "none"
    assert hit["resolution"] is None
    assert "not inferred" in hit["note"]


# --- API -------------------------------------------------------------------------------


def test_search_endpoint_shape(client):
    r = client.post("/failures/search", json={"text": "CUDA out of memory", "k": 3})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"input_type", "backend", "no_match", "message", "results"}
    assert body["backend"] == "tfidf" and not body["no_match"]
    assert 1 <= len(body["results"]) <= 3
    assert set(body["results"][0]) == {"id", "team", "cause_group", "error_lines",
                                       "resolution", "resolution_status", "score", "note"}  # fmt: skip


def test_no_secret_in_any_search_response_or_record(client):
    for raw in RAW:
        r = client.post("/failures/search", json={"text": raw["log"]})
        assert r.status_code == 200 and no_secret(r.text), raw["id"]
        g = client.get(f"/failures/{raw['id']}")
        assert g.status_code == 200 and no_secret(g.text), raw["id"]


def test_the_raw_logs_do_contain_the_secrets_these_tests_look_for():
    # Guards the test above against passing vacuously.
    text = "\n".join(r["log"] for r in RAW)
    assert all(s in text for s in SECRETS)


def test_a_422_never_echoes_the_input(client):
    secret = "Authorization: Bearer EXAMPLE-NOT-A-REAL-TOKEN-feed\nSERVICE_PASSWORD=x\n"
    text = secret * (20_001 // len(secret) + 1)
    r = client.post("/failures/search", json={"text": text})
    assert r.status_code == 422
    assert no_secret(r.text) and "SERVICE_PASSWORD" not in r.text
    assert r.json()["detail"][0]["loc"] == ["body", "text"]
    r = client.post(
        "/failures/search", json={"text": "x", "k": 5, "secret_token": "abc123xyz"}
    )
    assert r.status_code == 422 and "abc123xyz" not in r.text


@pytest.mark.parametrize("body", [
    {"text": ""}, {"text": "   \n "}, {"text": "x", "k": 0}, {"text": "x", "k": 21},
    {"text": "x", "k": "5"}, {"k": 5}, {"text": "x", "extra": 1},
])  # fmt: skip
def test_invalid_search_requests_are_422(client, body):
    assert client.post("/failures/search", json=body).status_code == 422


def test_other_routes_keep_the_default_422_body(client):
    r = client.post("/jobs", json={"team": "t", "gpus": 3, "estimated_minutes": 5})
    assert r.status_code == 422 and "input" in r.json()["detail"][0]


def test_get_failure(client, idx):
    raw = next(r for r in RAW if any(s in r["log"] for s in SECRETS))
    body = client.get(f"/failures/{raw['id']}").json()
    assert body["id"] == raw["id"] and body["cause_group"] == idx.group_of[raw["id"]]
    assert body["signal_lines"] == idx.signal[raw["id"]]
    assert "[REDACTED]" in body["log"]
    assert client.get("/failures/NOPE").status_code == 404


def test_missing_default_logs_file_gives_503_and_the_broker_still_works(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)  # the default relative path no longer exists
    monkeypatch.delenv("FAILURE_LOGS_PATH", raising=False)
    with TestClient(create_app(tmp_path / "b.db")) as c:
        assert c.get("/health").status_code == 200
        assert c.post("/failures/search", json={"text": "oom"}).status_code == 503
        assert c.get("/failures/x").status_code == 503


def test_an_explicit_missing_logs_path_fails_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("FAILURE_LOGS_PATH", str(tmp_path / "missing.jsonl"))
    with pytest.raises(FileNotFoundError), TestClient(create_app(tmp_path / "b.db")):
        pass


def test_nothing_secret_is_logged(caplog):
    with caplog.at_level(logging.DEBUG):
        built = index.build_index(LOGS, backend="tfidf")
        for raw in RAW:
            search.search(built, raw["log"])
    assert no_secret(caplog.text)


# --- hard rule: no ids in retrieval code ------------------------------------------------------


def test_no_log_or_query_id_literals_in_retrieval_code():
    pattern = re.compile(r"\b[FQ]\d{2,}\b")
    found = []
    for path in sorted((ROOT / "retrieval").glob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                found.append(f"{path.name}:{n}: {line.strip()}")
    assert found == []
