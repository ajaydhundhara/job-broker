"""Evaluate retrieval: P@3/P@5, MRR, recall, rejection; choose parameters honestly.

    python -m retrieval.evaluate --queries 01c_retrieval_queries.jsonl
        [--backend auto|minilm|tfidf] [--extra data/my_queries.jsonl]

Honest selection: parameters are chosen by grid search INSIDE 4-fold
cross-validation (stratified, seeded), and the headline is the score on the
held-out folds. The score of the parameters finally chosen on all queries is
reported too, labelled optimistic. TAU candidates come from training scores
only. Output is a pure function of the inputs: byte-identical every run.
"""

import argparse
import json
import random
from dataclasses import asdict, replace
from itertools import product
from pathlib import Path

import numpy as np

from retrieval import embed, search
from retrieval.index import PARAMS_PATH, Params, build_index
from retrieval.quality import DEFAULT_LOGS

GRID = {
    "w_dense": (0.3, 0.5, 0.7),
    "g_member": (0.5, 0.7, 1.0),
    "lex_k": (2.0, 5.0, 10.0),
}
TAU_QUANTILES = tuple(round(0.05 + 0.1 * i, 2) for i in range(10))  # 5%..95%
N_FOLDS, SEED, K = 4, 42, 5
OUT_DIR = Path("reports")
_METRICS = ("p3", "p5", "mrr", "recall5", "rejection_accuracy")


def _r(x):
    return None if x is None else round(float(x), 6)


# --- data -------------------------------------------------------------------------


def load_queries(path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            raw = json.loads(line)
            rows.append({"qid": raw["qid"], "query": raw["query"],
                         "expected": tuple(raw["expected_ids"])})  # fmt: skip
    return rows


# --- metrics ---------------------------------------------------------------------


def query_metrics(expected: tuple, returned: list[str]) -> dict:
    """One query. Precision divides by k, so a missing result is a miss."""
    top = returned[:K]
    hits = [i in expected for i in top]
    first = next((n for n, h in enumerate(hits, 1) if h), None)
    return {
        "p3": sum(hits[:3]) / 3,
        "p5": sum(hits) / K,
        "mrr": 1 / first if first else 0.0,
        "recall5": sum(hits) / len(expected) if expected else 0.0,
        "rejected": not returned,
    }


def aggregate(rows) -> dict:
    """rows: [(expected, returned)]. P@k etc. over answerable queries only."""
    answerable = [query_metrics(e, r) for e, r in rows if e]
    empty = [r for e, r in rows if not e]
    out = {k: _r(np.mean([m[k] for m in answerable])) if answerable else None
           for k in ("p3", "p5", "mrr", "recall5")}  # fmt: skip
    out["rejection_accuracy"] = _r(np.mean([not r for r in empty])) if empty else None
    out["false_rejections"] = sum(m["rejected"] for m in answerable)
    out["answerable"], out["unanswerable"] = len(answerable), len(empty)
    return out


def objective(agg: dict) -> float:
    """Mean P@5 on answerable + rejection accuracy on unanswerable (the brief)."""
    return round((agg["p5"] or 0.0) + (agg["rejection_accuracy"] or 0.0), 9)


# --- folds, TAU, selection -------------------------------------------------------------


def make_folds(
    labels: dict, n_folds: int = N_FOLDS, seed: int = SEED
) -> list[list[str]]:
    """Stratified: answerable and unanswerable are dealt separately, so every
    fold gets some of each; the unanswerable ones are dealt from the other end
    so fold sizes stay equal."""
    rng = random.Random(seed)
    answerable = sorted(q for q, e in labels.items() if e)
    empty = sorted(q for q, e in labels.items() if not e)
    rng.shuffle(answerable)
    rng.shuffle(empty)
    folds = [[] for _ in range(n_folds)]
    for i, q in enumerate(answerable):
        folds[i % n_folds].append(q)
    for i, q in enumerate(empty):
        folds[n_folds - 1 - i % n_folds].append(q)
    return [sorted(f) for f in folds]


def tau_candidates(best_scores: list[float]) -> list[float]:
    return sorted({_r(t) for t in np.quantile(best_scores, TAU_QUANTILES)})


def cut(ranked: list[tuple[str, float]], tau: float) -> list[str]:
    """What search() returns at this TAU: ranked results are in score order,
    so 'stop at the first one below TAU' equals 'keep those at or above'."""
    if not ranked or ranked[0][1] < tau:
        return []
    return [i for i, s in ranked if s >= tau]


def select(cache: dict, combos: list, qids: list[str], labels: dict) -> dict:
    """The best (combo, TAU) on these queries ONLY.

    Ties: higher objective, then higher P@3, then (within one combo) the
    median tied TAU, so the cut sits mid-gap rather than at an edge, then
    grid order.
    """
    best = None
    for order, combo in enumerate(combos):
        ranked = {q: cache[combo][q] for q in qids}
        taus = tau_candidates([r[0][1] if r else 0.0 for r in ranked.values()])
        scored = []
        for tau in taus:
            agg = aggregate([(labels[q], cut(ranked[q], tau)) for q in qids])
            scored.append(((objective(agg), agg["p3"] or 0.0), tau, agg))
        top = max(s[0] for s in scored)
        tied = [s for s in scored if s[0] == top]
        key, tau, agg = tied[(len(tied) - 1) // 2]
        candidate = (
            (-key[0], -key[1], order),
            {"combo": combo, "tau": tau, "train": agg},
        )
        if best is None or candidate[0] < best[0]:
            best = candidate
    return best[1]


# --- one variant: cache rankings, cross-validate, select on all ---------------------------


def _params(base: Params, names: tuple, combo: tuple, tau: float) -> Params:
    return replace(base, tau=tau, **dict(zip(names, combo, strict=True)))


def evaluate_variant(
    idx, queries, grid: dict, *, use_groups=True, use_glossary=True
) -> dict:
    labels = {q["qid"]: q["expected"] for q in queries}
    text = {q["qid"]: q["query"] for q in queries}
    names = tuple(grid)
    combos = list(product(*grid.values()))
    # TAU only decides where results stop, not their order: search once per
    # combo with TAU=0 (k results always), apply each TAU afterwards.
    cache = {}
    for combo in combos:
        p = _params(idx.params, names, combo, 0.0)
        cache[combo] = {
            q: [(h["id"], h["score"]) for h in search.search(
                idx, text[q], K, params=p, use_groups=use_groups,
                use_glossary=use_glossary).results]
            for q in labels
        }  # fmt: skip
    folds = make_folds(labels)
    fold_rows = []
    for n, test in enumerate(folds):
        train = [q for q in sorted(labels) if q not in test]
        chosen = select(cache, combos, train, labels)
        got = aggregate([(labels[q], cut(cache[chosen["combo"]][q], chosen["tau"]))
                         for q in test])  # fmt: skip
        fold_rows.append({"fold": n, "test_qids": test,
                          "params": dict(zip(names, chosen["combo"], strict=True)) | {"tau": chosen["tau"]},
                          "test": got})  # fmt: skip
    summary = {}
    for m in (*_METRICS, "false_rejections"):
        vals = [f["test"][m] for f in fold_rows if f["test"][m] is not None]
        summary[m] = {
            "mean": _r(np.mean(vals)),
            "min": _r(min(vals)),
            "max": _r(max(vals)),
        }
    final = select(cache, combos, sorted(labels), labels)
    final_params = _params(idx.params, names, final["combo"], final["tau"])
    per_query = []
    for q in queries:
        returned = cut(cache[final["combo"]][q["qid"]], final["tau"])
        m = query_metrics(q["expected"], returned)
        per_query.append({"qid": q["qid"], "query": q["query"], "expected": list(q["expected"]),
                          "top5": [[i, i in q["expected"]] for i in returned[:K]],
                          "p3": _r(m["p3"]) if q["expected"] else None,
                          "p5": _r(m["p5"]) if q["expected"] else None,
                          "rejected": m["rejected"]})  # fmt: skip
    return {
        "cross_validation": {"folds": fold_rows, "summary": summary},
        "full_set": {"params": asdict(final_params), "metrics": final["train"]},
        "per_query": per_query,
        "_final_params": final_params,
    }


def _check(idx, queries, out, **switches) -> str:
    """The cached-cut shortcut must equal a real search with the final params."""
    p = out["_final_params"]
    for q, row in zip(queries, out["per_query"], strict=True):
        real = [
            h["id"]
            for h in search.search(idx, q["query"], K, params=p, **switches).results
        ]
        if real[:K] != [i for i, _ in row["top5"]]:
            return f"mismatch on {q['qid']}"
    return "ok"


# --- the whole run ------------------------------------------------------------------


def _ablations(idx, queries, logs, backend) -> list[dict]:
    rows = []
    fixed = {"g_member": (1.0,)}  # no groups: g_member has no effect
    specs = [
        ("BM25 only", {"w_dense": (0.0,), "lex_k": GRID["lex_k"], **fixed}, False, False, idx),
        ("dense only", {"w_dense": (1.0,), "lex_k": (5.0,), **fixed}, False, False, idx),
        ("hybrid", {"w_dense": GRID["w_dense"], "lex_k": GRID["lex_k"], **fixed}, False, False, idx),
        ("hybrid + groups", GRID, True, False, idx),
        ("+ glossary (full system)", GRID, True, True, idx),
        ("without noise filter", GRID, True, True,
         build_index(logs, backend, replace(idx.params, noise_df=1.0))),
    ]  # fmt: skip
    for name, grid, groups, gloss, index in specs:
        out = evaluate_variant(
            index, queries, grid, use_groups=groups, use_glossary=gloss
        )
        rows.append(_ablation_row(name, index.backend, out))
    for other in embed.BACKENDS[1:]:  # each backend, full system
        if other == idx.backend:
            continue
        if other == "minilm" and not embed.minilm_available():
            rows.append({"name": f"full system, {other}", "backend": other,
                         "status": "not available (MiniLM not installed or not cached)"})  # fmt: skip
            continue
        index = build_index(logs, other)
        rows.append(_ablation_row(f"full system, {other}", other,
                                  evaluate_variant(index, queries, GRID)))  # fmt: skip
    return rows


def _ablation_row(name, backend, out) -> dict:
    cv, full = out["cross_validation"]["summary"], out["full_set"]["metrics"]
    return {"name": name, "backend": backend, "status": "evaluated",
            "cv": {m: cv[m]["mean"] for m in ("p3", "p5", "rejection_accuracy")},
            "full_set": {m: full[m] for m in ("p3", "p5", "rejection_accuracy")}}  # fmt: skip


def run(queries_path, *, logs=DEFAULT_LOGS, backend=None, extra=None) -> dict:
    queries = load_queries(queries_path)
    idx = build_index(logs, backend, Params())  # defaults; the grid varies them
    main = evaluate_variant(idx, queries, GRID)
    result = {
        "method": {
            "queries": Path(queries_path).name, "logs": Path(logs).name,
            "backend": idx.backend, "k": K, "folds": N_FOLDS, "seed": SEED,
            "grid": {k: list(v) for k, v in GRID.items()},
            "tau_candidates": f"quantiles {list(TAU_QUANTILES)} of the TRAINING "
                              "queries' best scores",
            "objective": "mean P@5 (answerable) + rejection accuracy (unanswerable)",
            "fixed": {"assign_min": idx.params.assign_min, "noise_df": idx.params.noise_df},
        },
        "cross_validation": main["cross_validation"],
        "full_set": main["full_set"],
        "per_query": main["per_query"],
        "consistency_check": _check(idx, queries, main),
        "ablations": _ablations(idx, queries, logs, backend),
        "extra": _extra(idx, extra, main["_final_params"]),
    }  # fmt: skip
    return result


def _extra(idx, path, params) -> dict | None:
    if path is None:
        return None
    path = Path(path)
    if not path.exists():
        return {"path": path.name, "status": "not provided"}
    queries = load_queries(path)
    rows, per_query = [], []
    for q in queries:
        returned = [
            h["id"] for h in search.search(idx, q["query"], K, params=params).results
        ]
        rows.append((q["expected"], returned))
        m = query_metrics(q["expected"], returned)
        per_query.append({"qid": q["qid"], "query": q["query"],
                          "top5": [[i, i in q["expected"]] for i in returned[:K]],
                          "p3": _r(m["p3"]) if q["expected"] else None,
                          "p5": _r(m["p5"]) if q["expected"] else None,
                          "rejected": m["rejected"]})  # fmt: skip
    return {"path": path.name, "status": "scored", "metrics": aggregate(rows),
            "per_query": per_query}  # fmt: skip


# --- reports ----------------------------------------------------------------------------


def _fmt(x) -> str:
    return "–" if x is None else f"{x:.3f}"


def _query_table(rows) -> list[str]:
    out = ["| qid | query | top 5 (✓ relevant) | P@3 | P@5 | rejected |",
           "|---|---|---|---|---|---|"]  # fmt: skip
    for q in rows:
        top = " ".join(f"{i}{'✓' if ok else '✗'}" for i, ok in q["top5"]) or "–"
        query = q["query"].replace("|", "\\|")
        out.append(f"| {q['qid']} | {query} | {top} | {_fmt(q['p3'])} | {_fmt(q['p5'])} "
                   f"| {'yes' if q['rejected'] else 'no'} |")  # fmt: skip
    return out


def render_markdown(r: dict) -> str:
    m, cv, full = r["method"], r["cross_validation"], r["full_set"]
    s = cv["summary"]
    out = [
        "# Retrieval evaluation",
        "",
        (
            f"Queries `{m['queries']}`, logs `{m['logs']}`, backend **{m['backend']}**, "
            f"k = {m['k']}. Generated by `python -m retrieval.evaluate`."
        ),
        "",
        "## Headline (cross-validated)",
        "",
        (
            f"Parameters chosen by grid search on the training folds only; scores are "
            f"on the held-out fold ({m['folds']} folds, stratified, seed {m['seed']}). "
            "Mean over folds, with the fold range."
        ),
        "",
        "| metric | mean | fold min – max |",
        "|---|---|---|",
    ]
    names = {"p3": "P@3", "p5": "P@5", "mrr": "MRR", "recall5": "recall@5",
             "rejection_accuracy": "rejection accuracy",
             "false_rejections": "false rejections (per fold)"}  # fmt: skip
    for k, label in names.items():
        out.append(
            f"| {label} | {_fmt(s[k]['mean'])} | {_fmt(s[k]['min'])} – {_fmt(s[k]['max'])} |"
        )
    fm = full["metrics"]
    fp = full["params"]
    out += [
        "",
        "## Full set (optimistic: parameters chosen on these same queries)",
        "",
        f"Chosen parameters, saved to `retrieval/params.json` under `{m['backend']}`: "
        + ", ".join(f"{k}={fp[k]}" for k in ("w_dense", "g_member", "lex_k", "tau")),
        f"(fixed: assign_min={fp['assign_min']}, noise_df={fp['noise_df']}).",
        "",
        "| P@3 | P@5 | MRR | recall@5 | rejection accuracy | false rejections |",
        "|---|---|---|---|---|---|",
        (f"| {_fmt(fm['p3'])} | {_fmt(fm['p5'])} | {_fmt(fm['mrr'])} | {_fmt(fm['recall5'])} "
        f"| {_fmt(fm['rejection_accuracy'])} | {fm['false_rejections']} of {fm['answerable']} |"),
        "",
        f"Consistency check (cached ranking + TAU cut = a real search): {r['consistency_check']}.",
        "",
        "## Per fold",
        "",
        "| fold | test queries | chosen w_dense / g_member / lex_k / tau | P@3 | P@5 | rejection |",
        "|---|---|---|---|---|---|",
    ]  # fmt: skip
    for f in cv["folds"]:
        p, t = f["params"], f["test"]
        out.append(f"| {f['fold']} | {', '.join(f['test_qids'])} | {p['w_dense']} / "
                   f"{p['g_member']} / {p['lex_k']} / {p['tau']} | {_fmt(t['p3'])} | "
                   f"{_fmt(t['p5'])} | {_fmt(t['rejection_accuracy'])} |")  # fmt: skip
    out += ["", "## Ablations", "",
            ("Each variant gets the same cross-validated selection over the grid "
            "(with fixed values where a parameter has no effect)."), "",
            "| variant | CV P@3 | CV P@5 | CV rejection | full P@3 | full P@5 | full rejection |",
            "|---|---|---|---|---|---|---|"]  # fmt: skip
    for a in r["ablations"]:
        if a["status"] != "evaluated":
            out.append(f"| {a['name']} | {a['status']} | | | | | |")
            continue
        c, f = a["cv"], a["full_set"]
        out.append(f"| {a['name']} | {_fmt(c['p3'])} | {_fmt(c['p5'])} | "
                   f"{_fmt(c['rejection_accuracy'])} | {_fmt(f['p3'])} | {_fmt(f['p5'])} | "
                   f"{_fmt(f['rejection_accuracy'])} |")  # fmt: skip
    out += ["", "## Per query (final parameters, full set)", ""] + _query_table(
        r["per_query"]
    )
    out += ["", "## Unseen queries (--extra)", ""]
    e = r["extra"]
    if e is None:
        out.append("Not requested (`--extra` not given).")
    elif e["status"] != "scored":
        out.append(f"`{e['path']}`: {e['status']}.")
    else:
        em = e["metrics"]
        out += [(f"`{e['path']}`, scored with the final parameters (never used to choose them): "
                f"P@3 {_fmt(em['p3'])}, P@5 {_fmt(em['p5'])}, rejection accuracy "
                f"{_fmt(em['rejection_accuracy'])}."), ""] + _query_table(e["per_query"])  # fmt: skip
    out += [
        "",
        "## Method",
        "",
        (
            "- P@k: for each query with expected ids, the share of the top-k ids that "
            "are expected; a missing result counts as a miss. Averaged over answerable "
            "queries. MRR and recall@5 use the top 5."
        ),
        (
            "- Rejection accuracy: share of queries with no expected ids that got no "
            "result. False rejections: answerable queries that got none."
        ),
        f"- Grid: {m['grid']}; TAU candidates: {m['tau_candidates']}.",
        (
            f"- Objective: {m['objective']}. Ties: higher objective, then P@3, then the "
            "median tied TAU, then grid order."
        ),
        f"- Fixed (not tuned): {m['fixed']}.",
    ]
    return "\n".join(out) + "\n"


def _public(r: dict) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("_")}


def save_params(result: dict, path: Path = PARAMS_PATH) -> None:
    """Write the chosen parameters under the backend's name; keep the others."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    backend = result["method"]["backend"]
    data[backend] = result["full_set"]["params"]
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--logs", type=Path, default=DEFAULT_LOGS)
    parser.add_argument("--backend", choices=embed.BACKENDS, default=None)
    parser.add_argument("--extra", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--params-out", type=Path, default=PARAMS_PATH)
    args = parser.parse_args(argv)
    result = _public(
        run(args.queries, logs=args.logs, backend=args.backend, extra=args.extra)
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "retrieval_eval.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (args.out_dir / "retrieval_eval.md").write_text(
        render_markdown(result), encoding="utf-8"
    )
    save_params(result, args.params_out)
    s, f = result["cross_validation"]["summary"], result["full_set"]["metrics"]
    print(f"backend {result['method']['backend']}: CV P@3 {_fmt(s['p3']['mean'])} "
          f"P@5 {_fmt(s['p5']['mean'])} (folds {_fmt(s['p5']['min'])}-{_fmt(s['p5']['max'])}) "
          f"rejection {_fmt(s['rejection_accuracy']['mean'])} | full set P@3 {_fmt(f['p3'])} "
          f"P@5 {_fmt(f['p5'])} rejection {_fmt(f['rejection_accuracy'])}")  # fmt: skip
    print(f"wrote {args.out_dir / 'retrieval_eval.md'}, {args.out_dir / 'retrieval_eval.json'}, "
          f"{args.params_out}")  # fmt: skip


if __name__ == "__main__":
    main()
