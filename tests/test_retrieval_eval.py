"""Retrieval track, sub-step D: metrics, cross-validation and the eval report.

Unit tests use hand-made results. The regression test runs the real
cross-validated evaluation on the tfidf backend.
"""

import json
from pathlib import Path

import pytest

from retrieval import evaluate as ev

ROOT = Path(__file__).resolve().parent.parent
QUERIES = ROOT / "01c_retrieval_queries.jsonl"
LOGS = ROOT / "01b_failure_logs.jsonl"

# Cross-validated values measured on tfidf (printed by the regression test),
# minus 0.05 and rounded down: one query moves a fold's P@5 by about 0.2, so
# a tighter floor would fail on any honest change.
FLOOR_CV_P5 = 0.88  # measured 0.9375 (tfidf, glossary without query-matched phrases)
FLOOR_CV_REJECTION = 0.95  # measured 1.0000


# --- metrics --------------------------------------------------------------------


def test_precision_counts_a_missing_result_as_a_miss():
    m = ev.query_metrics(("a", "b", "c"), ["a", "x", "b"])
    assert (m["p3"], m["p5"]) == (2 / 3, 2 / 5)  # 3 results: at most 3/5
    assert m["mrr"] == 1.0 and m["recall5"] == 2 / 3
    m = ev.query_metrics(("a", "b"), ["x", "a"])
    assert (m["p3"], m["p5"], m["mrr"]) == (1 / 3, 1 / 5, 1 / 2)
    m = ev.query_metrics(("a",), [])
    assert (m["p3"], m["p5"], m["mrr"], m["recall5"]) == (0, 0, 0, 0)
    assert m["rejected"]


def test_only_the_top_5_count():
    m = ev.query_metrics(("f",), ["a", "b", "c", "d", "e", "f"])
    assert (m["p5"], m["mrr"], m["recall5"]) == (0, 0, 0)


def test_aggregate_separates_answerable_from_unanswerable():
    rows = [
        (("a", "b"), ["a", "b", "x"]),  # answerable, hit
        (("a",), []),  # answerable, falsely rejected
        ((), []),  # unanswerable, correctly rejected
        ((), ["a"]),  # unanswerable, wrongly answered
    ]
    agg = ev.aggregate(rows)
    assert agg["answerable"] == 2 and agg["unanswerable"] == 2
    assert agg["p3"] == pytest.approx((2 / 3 + 0) / 2)
    assert agg["p5"] == pytest.approx((2 / 5 + 0) / 2)
    assert agg["rejection_accuracy"] == 0.5
    assert agg["false_rejections"] == 1
    assert ev.objective(agg) == pytest.approx(agg["p5"] + 0.5)


# --- folds and selection -----------------------------------------------------------


def labels(n_answerable=18, n_empty=6):
    return {f"q{i:02d}": ((f"x{i}",) if i < n_answerable else ())
            for i in range(n_answerable + n_empty)}  # fmt: skip


def test_folds_are_stratified_disjoint_balanced_and_seeded():
    lab = labels()
    folds = ev.make_folds(lab, n_folds=4, seed=42)
    assert sorted(q for f in folds for q in f) == sorted(lab)
    assert [len(f) for f in folds] == [6, 6, 6, 6]
    for f in folds:
        assert any(not lab[q] for q in f)  # every fold has an unanswerable one
    assert folds == ev.make_folds(lab, n_folds=4, seed=42)
    assert folds != ev.make_folds(lab, n_folds=4, seed=7)


def test_tau_candidates_are_ten_quantiles_of_the_given_scores():
    taus = ev.tau_candidates([0.1, 0.2, 0.3, 0.9])
    assert len(taus) == 10 and taus == sorted(taus)
    assert 0.1 <= taus[0] and taus[-1] <= 0.9


def fake_cache():
    # combo -> qid -> ranked (id, score); combo "good" ranks the answer first.
    lab = labels(4, 2)
    good = {
        q: ([(lab[q][0], 0.9), ("z", 0.8)] if lab[q] else [("z", 0.2)]) for q in lab
    }
    bad = {q: [("z", 0.5)] for q in lab}
    return lab, {("bad",): bad, ("good",): good}


def test_selection_picks_the_best_combo_and_a_tau_between_the_groups():
    lab, cache = fake_cache()
    chosen = ev.select(cache, list(cache), list(lab), lab)
    assert chosen["combo"] == ("good",)
    assert 0.2 < chosen["tau"] <= 0.9  # rejects the 0.2 queries, keeps the 0.9 ones
    assert chosen["train"]["rejection_accuracy"] == 1.0


def test_selection_never_looks_at_queries_outside_its_training_set():
    lab, cache = fake_cache()
    train = sorted(lab)[:4]
    before = ev.select(cache, list(cache), train, lab)
    for combo in cache:  # poison every query not in the training set
        for q in set(lab) - set(train):
            cache[combo][q] = [("poison", 0.99)]
    assert ev.select(cache, list(cache), train, lab) == before


def test_applying_tau_keeps_only_results_at_or_above_it():
    ranked = [("a", 0.9), ("b", 0.9), ("c", 0.4), ("d", 0.1)]
    assert ev.cut(ranked, 0.4) == ["a", "b", "c"]
    assert ev.cut(ranked, 0.95) == []


# --- the real evaluation -----------------------------------------------------------


@pytest.fixture(scope="module")
def results():
    return ev.run(QUERIES, logs=LOGS, backend="tfidf")


def test_cut_on_cached_results_equals_a_real_search_with_the_final_params(results):
    # The grid applies TAU to cached rankings; the API runs search() itself.
    assert results["consistency_check"] == "ok"


def test_cross_validated_scores_stay_above_their_floors(results):
    cv = results["cross_validation"]["summary"]
    print(f"\nmeasured CV P@5 {cv['p5']['mean']:.4f}, "
          f"rejection accuracy {cv['rejection_accuracy']['mean']:.4f}")  # fmt: skip
    assert FLOOR_CV_P5 is not None and FLOOR_CV_REJECTION is not None, "floors not set"
    assert cv["p5"]["mean"] >= FLOOR_CV_P5
    assert cv["rejection_accuracy"]["mean"] >= FLOOR_CV_REJECTION


def test_every_ablation_is_reported(results):
    names = [a["name"] for a in results["ablations"]]
    for expected in ("BM25 only", "dense only", "hybrid", "hybrid + groups",
                     "+ glossary (full system)", "without noise filter"):  # fmt: skip
        assert expected in names, expected


def test_the_eval_writes_identical_json_twice(tmp_path):
    for name in ("a", "b"):
        ev.main(["--queries", str(QUERIES), "--logs", str(LOGS), "--backend", "tfidf",
                 "--out-dir", str(tmp_path / name), "--params-out", str(tmp_path / f"{name}.json")])  # fmt: skip
    a = (tmp_path / "a" / "retrieval_eval.json").read_bytes()
    assert a == (tmp_path / "b" / "retrieval_eval.json").read_bytes()
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()
    data = json.loads(a)
    assert "runtime" not in json.dumps(data) and "elapsed" not in json.dumps(data)
    md = (tmp_path / "a" / "retrieval_eval.md").read_text()
    for section in ("## Headline", "## Full set", "## Ablations", "## Per query"):
        assert section in md, section


def test_a_missing_extra_file_is_reported_not_invented(tmp_path):
    out = ev.run(QUERIES, logs=LOGS, backend="tfidf", extra=tmp_path / "none.jsonl")
    assert out["extra"] == {"path": "none.jsonl", "status": "not provided"}
    assert not (tmp_path / "none.jsonl").exists()


def test_an_extra_file_is_scored_with_the_final_params(tmp_path):
    extra = tmp_path / "mine.jsonl"
    extra.write_text(json.dumps({"qid": "U1", "query": "my gpu ran out of memory",
                                 "expected_ids": ["F001"]}) + "\n")  # fmt: skip
    out = ev.run(QUERIES, logs=LOGS, backend="tfidf", extra=extra)
    assert out["extra"]["status"] == "scored"
    assert [q["qid"] for q in out["extra"]["per_query"]] == ["U1"]
