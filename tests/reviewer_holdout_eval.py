"""Score the reviewer-only holdout queries with the FINAL, unchanged parameters.

    python -m tests.reviewer_holdout_eval [--queries tests/data/reviewer_retrieval_queries.jsonl]
        [--out reports/reviewer_query_results.json]

Read-only with respect to retrieval: the index is built exactly as the API
builds it (build_index with the committed retrieval/params.json for the
backend), nothing is fitted, and params.json is hashed before and after to
prove it was not written. The holdout is never passed to retrieval.evaluate's
grid search, so it cannot influence parameter selection; it is also kept out
of the cross-validation headline in reports/retrieval_eval.*.

Metrics reuse retrieval.evaluate.query_metrics / aggregate (k = 5), so the
numbers mean what the public evaluation's numbers mean. Added here: first
relevant rank, per-query pass/fail, false-acceptance rate, per-category
summary. Query text in the output is redacted with retrieval.clean.redact.

Pass rule (stated before running): an answerable query passes when its top 5
has as many relevant ids as it can hold (P@5 == min(|expected|, 5) / 5); an
unanswerable query passes when nothing is returned.
"""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from retrieval import clean, search
from retrieval.evaluate import K, aggregate, query_metrics
from retrieval.index import PARAMS_PATH, build_index, load_params

DEFAULT_QUERIES = Path("tests/data/reviewer_retrieval_queries.jsonl")
DEFAULT_OUT = Path("reports/reviewer_query_results.json")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def run(queries_path: Path = DEFAULT_QUERIES, backend: str = "tfidf") -> dict:
    before = _sha(PARAMS_PATH)
    idx = build_index(backend=backend)  # params from params.json, as the API does
    assert idx.params == load_params(idx.backend), "index not on the final params"
    queries = load(Path(queries_path))
    per_query, pairs = [], []
    for q in queries:
        expected = tuple(q["expected_ids"])
        res = search.search(idx, q["query"], K)
        returned = [h["id"] for h in res.results]
        m = query_metrics(expected, returned)
        first = next((n for n, i in enumerate(returned[:K], 1) if i in expected), None)
        if expected:
            ceiling = min(len(expected), K) / K
            passed = (not m["rejected"]) and abs(m["p5"] - ceiling) < 1e-9
        else:
            ceiling, passed = None, m["rejected"]
        body = json.dumps(res.response())
        per_query.append({
            "qid": q["qid"],
            "category": q.get("category"),
            "query_redacted": clean.redact(q["query"]),
            "input_type": res.input_type,
            "answerable": bool(expected),
            "expected_ids": list(expected),
            "returned_ids": returned,
            "returned_scores": [h["score"] for h in res.results],
            "returned_groups": [h["cause_group"] for h in res.results],
            "first_relevant_rank": first,
            "p3": round(m["p3"], 6) if expected else None,
            "p5": round(m["p5"], 6) if expected else None,
            "p5_ceiling": ceiling,
            "recall5": round(m["recall5"], 6) if expected else None,
            "mrr": round(m["mrr"], 6) if expected else None,
            "rejected": m["rejected"],
            "pass": passed,
            "response_contains_redaction_marker": clean.REDACTED in body,
            "response_redacted_secret_free": clean.redact(body) == body,
        })  # fmt: skip
        pairs.append((expected, returned))

    agg = aggregate(pairs)
    unans = [r for r in per_query if not r["answerable"]]
    ans = [r for r in per_query if r["answerable"]]
    agg["false_acceptances"] = sum(not r["rejected"] for r in unans)
    agg["false_acceptance_rate"] = round(agg["false_acceptances"] / len(unans), 6)
    agg["false_rejection_rate"] = round(agg["false_rejections"] / len(ans), 6)
    agg["passed"] = sum(r["pass"] for r in per_query)
    agg["p5_ceiling_mean"] = round(sum(r["p5_ceiling"] for r in ans) / len(ans), 6)

    by_cat = defaultdict(list)
    for r in per_query:
        by_cat[r["category"]].append(r)
    categories = {}
    for cat, rs in sorted(by_cat.items()):
        a = [r for r in rs if r["answerable"]]
        categories[cat] = {
            "queries": len(rs),
            "passed": sum(r["pass"] for r in rs),
            "p3": round(sum(r["p3"] for r in a) / len(a), 6) if a else None,
            "p5": round(sum(r["p5"] for r in a) / len(a), 6) if a else None,
            "rejected": sum(r["rejected"] for r in rs),
        }
    after = _sha(PARAMS_PATH)
    assert before == after, "params.json changed during the holdout run"
    return {
        "split": "reviewer-holdout (final validation only; never used for tuning)",
        "queries_file": str(queries_path),
        "backend": idx.backend,
        "k": K,
        "params": {k: getattr(idx.params, k) for k in idx.params.__dataclass_fields__},
        "params_json_sha256": before,
        "pass_rule": "answerable: P@5 == min(|expected|,5)/5; unanswerable: rejected",
        "metrics": agg,
        "categories": categories,
        "per_query": per_query,
    }


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--backend", default="tfidf")
    args = p.parse_args(argv)
    result = run(args.queries, args.backend)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    m = result["metrics"]
    print(json.dumps({k: m[k] for k in sorted(m)}, indent=1))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
