# Final validation report: Job Broker take-home

Validation pass against `FINAL_ASSESSMENT_VALIDATION_TASK.md`. Every number
below was measured in this pass unless it is labelled *existing report*.
Source inspection and executed tests are kept apart: "inspected" means read
in the code, "tested" means a test or command was run and its output is
quoted.

**Pre-submission update.** Section 0 records a later review that changed
retrieval inference and classification, the README and the docs. It
supersedes the earlier sections where they say so ("*Update:*"); the rest
of the report is the original validation pass, kept as measured.

**Repository snapshots.** The report covers three points in the history of
`main`:

| Snapshot | Commit | What it is | Sections |
|---|---|---|---|
| Original validation pass | `6bd616b` | the code that sections 1–12 measured; the pass's own test, holdout and report files were then committed as `645007f` | 1–12 |
| Pre-submission review baseline | `645007f` ("final test") | the starting point of the review | 0 |
| Reviewed result | `72b2d7e` ("Infer fixes from failure evidence only and withhold them when ambiguous") | the review's code, tests and reports; the summary (§1) and verdict (§12) describe this state | 0, 1, 12 |

Later commits change documentation only (README wording and this report).

## 0. Pre-submission review (update)

Baseline: HEAD `645007fe4913fcdac37571481017824b20f5da84` ("final test") on
`main`, equal to `origin/main` at the time (read from `.git/`). The
review's changes were committed as `72b2d7e`; it was not pushed from here.

### Status of the issues found in the validation pass

| # | Issue (sections below) | Status | What changed |
|---|---|---|---|
| 1 | Hard negatives / false positives (§4, §11.1) | **unchanged, documented** | No scoring change: every variant that removes resolution text from the relevance documents failed the CV floors (table below). Pinned in `tests/test_retrieval_false_positives.py`; README *Known limits* updated. |
| 2 | Ambiguous inference assigned silently (§5, case 3) | **fixed** | Inference scores use failure evidence only (signal lines, no resolution text). Inferred only when exactly one group reaches `ASSIGN_MIN`; two or more: ambiguous, status `none`, no fix, note lists the candidates (SPEC_DECISIONS #17). |
| 3 | Shared vocabulary infers a wrong fix (§5, case 4) | **unchanged** | E4 still reaches only the disk group (0.360; token 0.229), so it is still inferred. Strict xfail kept, with an updated reason. |
| 4 | One-line docker daemon error is a "question" (§7) | **fixed** | `Error response from <component>:` is a record start (SPEC_DECISIONS #18). Strict xfail replaced by 5 positive and 4 negative examples plus a redaction check. |
| 5 | README "idle cost never goes negative" (§3, §11.5) | **fixed** | README *Cost attribution*: non-negative over a window starting at launch; a later window can show −1 micro-dollar, displayed as $0.00. |
| 6 | AI-tool and time disclosure missing (§9, §12.1) | **not in the repository (author's decision)** | README *Submission notes* states the interface (HTTP API, no UI), what was not built, and next steps. The AI-tool, time and hours disclosure the brief asks for is not in the README, at the author's request; it has to be supplied with the submission. |
| 7 | Grouping is a heuristic (§6, §11.3) | **unchanged, documented** | Grouping rules unchanged; README *Retrieval as implemented* now calls it a resolution-based heuristic and cites the pairwise numbers. |

### Part A: why the scoring was not changed (public queries only, tfidf)

Each variant was cross-validated with the official folds and parameter grid
(scratch scripts; no repository code changed for them).

| Variant | CV P@3 | CV P@5 | CV rejection | Floors (P@5 ≥ 0.88, rej ≥ 0.95) |
|---|---|---|---|---|
| Current (signal + resolution in log and group documents) | 0.938 | 0.938 | 1.000 | met |
| V1: signal-only log and group documents | 0.812 | 0.812 | 0.750 | **fail** |
| V2: signal-only log documents | 0.875 | 0.875 | 0.750 | **fail** |
| V3: signal-only group documents | 0.875 | 0.875 | 0.750 | **fail** |

An evidence-only acceptance gate was also examined and rejected: answerable
public questions score as low on failure evidence as new negatives do
(one answerable at 0.401 against negatives at 0.424–0.452), so no
threshold separates them without tuning to individual queries.

New regression tests (`tests/test_retrieval_false_positives.py`, examples
written for this review, not from the public or holdout sets): 4 pass
(2 absent failure families rejected, a disk-full paraphrase, a pasted NCCL
log with the result contract), 6 strict xfails stating the desired
behaviour (3 how-to questions sharing only fix wording, a license-server
failure, 2 paraphrases with no shared words).

### Part B: inference, before and after

| Record | Before (score, status) | After: candidates ≥ 0.3 | After status |
|---|---|---|---|
| F910 (real) | g07 0.482, inferred | g07 0.491, g03 0.366, g05 0.330, g08 0.317 | **none (ambiguous)**, no fix shown |
| F911 (real) | g07 0.806, inferred | g07 0.911 (next 0.292) | inferred |
| F912 (real) | g07 0.806, inferred | g07 0.910 (next 0.292) | inferred |
| E1 clear | 0.770, inferred | g1 0.891 | inferred |
| E2 weak | 0.272, none | none (best 0.228) | none (singleton) |
| E3 two groups | disk 0.572 inferred | g2 0.649, g1 0.449 | **none (ambiguous)** |
| E4 shared words | disk 0.398, inferred | g2 0.360 | inferred (**still wrong**, xfail) |
| E6 same evidence, two fixes | (new) | g1 0.827, g2 0.730 | none (ambiguous) |

Alternatives measured and rejected: making an ambiguous log a singleton
(CV rejection 0.750, below the floor) and a top-two margin (no labelled data
to pick its value). Group membership is unchanged, so search results and
every retrieval metric are unchanged.

The evaluator builds its index with default `Params()`, the API with
`retrieval/params.json`; inference can therefore differ between them. This
was pre-existing and is documented (SPEC_DECISIONS #17), not changed.

### Metrics before and after (kept separate)

| Set | Metric | Before | After |
|---|---|---|---|
| Public CV (`retrieval.evaluate`, tfidf) | P@3 / P@5 / rejection | 0.938 / 0.938 / 1.000 | 0.938 / 0.938 / 1.000 (`retrieval_eval.*` and `params.json` byte-identical) |
| Public full set | P@3 / P@5 / rejection | 0.944 / 0.944 / 1.000 | 0.944 / 0.944 / 1.000 |
| New regression tests | pass / strict xfail | n/a | 4 / 6 |
| Holdout (read-only audit, run last) | P@3 / P@5 / MRR | 0.979 / 0.9625 / 1.000 | 0.979 / 0.9625 / 1.000 |
| Holdout | rejection / false acceptances | 0.625 / 3 of 8 | 0.625 / 3 of 8 |

The only holdout change: R20 (the docker daemon line) is now classified as
a log; same top 5, score 0.871 → 0.892. Nothing was adjusted after the
audit.

### Commands run (repository root, `RETRIEVAL_BACKEND=tfidf`)

| Command | Result |
|---|---|
| `pytest -q -rsx` | 815 passed, 1 skipped (MiniLM not cached), 7 xfailed, 1 warning (Starlette `httpx` deprecation), 66.2 s |
| `ruff check .` / `ruff format --check .` | "All checks passed!" / "70 files already formatted" |
| `python -m retrieval.evaluate --queries 01c_retrieval_queries.jsonl --out-dir <scratch> --params-out <scratch copy>` | exit 0; outputs identical to the committed ones |
| `python -m retrieval.quality` | exit 0; `reports/retrieval_data_quality.md` regenerated (inferred table: F910 ambiguous) |
| `python -m broker.trace 01a_trace.csv --out <scratch>` | identical |
| `python -m replay.run_replay --trace 01a_trace.csv --out-dir <scratch>` | exit 0, 103.0 s; all three outputs identical |
| `uvicorn broker.api:app --port 8766` (scratch DB) + `curl` | `/failures/F910`: status `none`, resolution null, ambiguous note; `/failures/F911`: inferred; docker line → `input_type: log`, registry group; `/docs` 200; empty `POST /jobs` 422 |
| `python -m tests.reviewer_holdout_eval` (last) | exit 0; metrics above; `reports/reviewer_query_results.json` regenerated by the scorer |

Integrity: `01a_trace.csv`, `01b_failure_logs.jsonl`,
`01c_retrieval_queries.jsonl`, `Take_Home_Exercise.docx`, the holdout file,
`requirements*.txt` and `pyproject.toml` match their HEAD blobs (git blob
hashes computed from the files and read from `.git/objects`). No dependency
added. A scan of reports and docs for the fake test secrets and common key
patterns found nothing. The temporary `.claude/launch.json` was deleted.

### Files changed in this review

| File | Change |
|---|---|
| `retrieval/index.py` | evidence-only inference matrix; `Index.inference`, `Index.candidates`; ambiguity rule |
| `retrieval/search.py` | ambiguous note |
| `retrieval/clean.py` | `Error response from <component>:` record start |
| `retrieval/quality.py` | inferred-resolutions table shows the decision and candidates |
| `tests/test_retrieval_false_positives.py` | new |
| `tests/test_final_validation_retrieval.py` | case 3 and daemon xfails → passing tests; `RECORDED_ROWS` fixture; case 4 xfail kept |
| `tests/test_retrieval_search.py` | 2 tests changed for the ambiguity rule (approved) |
| `README.md`, `SPEC_DECISIONS.md` (#17, #18), `tests/TEST_MIGRATION_MAP.md` | docs |
| `reports/retrieval_data_quality.md`, `reports/reviewer_query_results.json`, this report | regenerated / updated |

## 1. Executive summary

**Verdict at `72b2d7e`: READY WITH CAVEATS.** This summary describes the
reviewed result; the original pass's figures are in sections 3–9 and its
changes in section 0.

- **Core service and cost accounting:** correct on every case checked.
  18 cost tests pass, including report-level additivity over 37 s, 600 s and
  1000 s cuts with five node situations and three teams. The README's
  idle-cost wording now matches the code (a window starting after launch can
  show −1 micro-dollar, displayed as $0.00).
- **Public retrieval evaluation (tfidf, cross-validated):** P@3 0.938,
  P@5 0.938, rejection 1.000, above the floors (P@5 ≥ 0.88, rejection
  ≥ 0.95); unchanged by the review.
- **40 holdout queries** (read-only audit, unchanged parameters, `tfidf`):
  - answerable queries hit their ceiling: P@3 0.979, P@5 0.9625, MRR 1.000,
    0 false rejections;
  - rejection is the weak point: 5 of 8 unanswerable queries rejected
    (0.625), against 1.000 on the public set. Unchanged by the review.
- **Behaviour gaps from the original pass:**
  - fixed: an ambiguous inference is no longer assigned silently (F910 is
    now ambiguous and shows no fix; SPEC_DECISIONS #17);
  - fixed: a one-line `Error response from <component>:` error is a log
    (SPEC_DECISIONS #18);
  - not fixed: shared vocabulary with a single wrong group still infers that
    group's fix (case 4, strict xfail);
  - not fixed, documented: how-to questions that only share a fix's wording,
    and some absent failure families, pass TAU; some paraphrases are
    rejected (`tests/test_retrieval_false_positives.py`, 6 strict xfails).
- **Tests at `72b2d7e`:** 815 passed, 1 skipped (MiniLM not cached),
  7 strict xfails; `ruff check` and `ruff format --check` clean.
- **MiniLM:** implemented (`retrieval/embed.py`) but not evaluated in this
  environment (the model is not installed or cached); every number is tfidf.
- **Submission:** the brief's AI-tool, time and hours disclosure is not in
  the repository (author's decision); it has to be supplied with the
  submission.

## 2. Repository state

*Snapshot at the original validation pass (`6bd616b`); later snapshots are
listed in the introduction.*

Read from `.git/` files; no git command was run (project rule).

| Item | Value |
|---|---|
| Remote | `https://github.com/ajaydhundhara/job-broker.git` (`origin`) |
| Branch | `main` (`.git/HEAD`) |
| HEAD (original pass) | `6bd616b5b7b45a44254557b79f11cc4a12b65b28`, "Replace replay report Analysis TODOs with written analysis from reports/replay_analysis.md" |
| `origin/main` | same commit (`.git/refs/remotes/origin/main`) |
| Working tree | new, uncommitted files from this pass only (section 10). Run `git status` to confirm; it was not run here. |

Environment: macOS (Darwin 25.6.0), Python 3.12.13, fastapi 0.165.0,
scikit-learn 1.9.1, numpy 2.5.5, pydantic 2.14.4. Retrieval backend `tfidf`
(`RETRIEVAL_BACKEND=tfidf`). MiniLM is implemented but not evaluated in
this environment: the model is not installed or cached on this machine.

## 3. Cost evaluation and accounting

**Inspected.** `broker/costs.py` is the only place cost is computed;
`service.cost_report` and `service.list_nodes` both call it.

- **Windows:** half-open `[from_ts, to_ts)`.
- **Billing:** per second, from `launched_at` to `terminated_at`, or to now
  for a live node. Money is integer micro-dollars.
- **Rounding:** a window's cost is the running total at its end minus the
  running total at its start, each floored once. So any split adds up exactly.
- **Minimum bill:** 600 s for a node that ended younger than that. It is
  charged at its last billed second, so it lands in exactly one window.
- **Busy share:** job GPUs ÷ node GPUs × run time. Idle is total minus busy.
- **Attribution:** the whole node goes to its owning team. Never-ready
  launches (`state = 'failed'`) go to `provider_overhead`.

**Tested:** `tests/test_cost_validation.py` (new, 18 tests) plus the
existing `tests/test_costs.py` (23 tests).

| # | Case | Expected | Actual |
|---|---|---|---|
| 1 | Per-second price × allocation, mixed fleet (gpu-l / gpu-m / gpu-s, 3 teams, [0, 5000)) | vision 4000 s × 8.5 M / 3600 = 9,444,444; speech 4689 s × 4.5 M / 3600 + 600 s × 1 M / 3600 = 6,027,916; nlp 166,666; overhead 1,125,000 | identical (exact micro-dollars) |
| 1b | GPU-seconds = requested GPUs × run time | vision 2400 + 8×1399 + 2×457 = 14,506 | 14,506 |
| 2 | Minimum bill once, only below 600 s: lifetimes 1 / 599 / 600 / 601 / 900 s | billed 600 / 600 / 600 / 601 / 900, also as a sum of 5000 one-second windows | identical |
| 2b | Live node has no minimum until it ends | 120 s live, 600 s once terminated at 120 | identical |
| 3 | Adjacent windows additive (report level), cuts every 37 s, 600 s and 1000 s | per-team gpu-s, busy, idle, total, overhead and grand total sum to the whole window | identical, exact to the micro-dollar |
| 4a | Node launched exactly at `to_ts` | not in `[0, to_ts)`; its first second is in `[to_ts, to_ts+1)` (277 micros) | identical |
| 4b | Node terminated exactly at `from_ts` (with a 480 s top-up) | 0 in `[from_ts, …)`; 600 s billed in the earlier window | identical |
| 4c | Jobs ending at `from_ts` / starting at `to_ts` | 0 GPU-seconds; all of `[1000, 2000)` idle = 1,250,000 | identical |
| 5 | busy + idle == total; grand = Σ teams + overhead | every window tested (60 windows) | holds in all |
| 6 | Failed (never-ready) launch | 900 s billed, no top-up, in `provider_overhead`, in no team | identical |
| 6b | Reclaimed / provider-ended node (`terminated`, `provider_reconciled`) | billed to its team until `terminated_at`, topped up if < 600 s | identical (456 s → 600 s) |
| 7 | Idle bounds and rounding | idle ≥ 0 in every window that starts at launch; ≥ −1 micro in any window | min over all one-second windows: −1 micro (nodes n-l, n-s, n-r); never below |
| 8 | Repeated report requests | identical | identical (service and API) |
| 9 | `/nodes` `cost_so_far` vs `/reports/cost` | Σ node totals == grand total in micros; in dollars within ½ cent per figure | identical in micros |
| 9b | Live lifecycle through the API, scheduler and reaper on (seed 7, idle 2 min: submit 4, complete 1, cancel 1, advance 70 min) | report == report again; no negative figure; busy + idle within 1 cent of total | holds. Hand check: vision 0.17 (gpu-s, 600 s) + 1.70 (gpu-l, 721 s) = 1.87; speech 9.92; nlp 9.92; grand total 21.70 vs parts 21.71, the documented per-figure rounding |

**Interruptible / reclaimed nodes:** interruptible nodes are not used, which
is the documented scope, so reclaim *pricing* (35%) is N/A. Provider-ended
nodes are covered by case 6b.

**Policy vs guarantee.**

- **Guarantees, tested:**
  - exact integer arithmetic;
  - additivity across any split of time;
  - the top-up lands exactly once;
  - busy + idle == total;
  - grand total = Σ teams + overhead;
  - determinism.
- **Policy choices, documented but not "correct" by necessity:**
  - the 600 s minimum is charged at the last second, and only once a node has ended;
  - idle cost goes to the owning team;
  - never-ready launches go to `provider_overhead`;
  - dollars are rounded half-up per figure, so displayed parts can differ
    from the displayed total by 0.01.

**Disagreement found (docs only).** README "Cost attribution" says
"idle cost never goes negative". The code comment (`broker/costs.py`, in
`node_cost`) and case 7 show that a window starting after launch can show
idle of **−1 micro-dollar**. It displays as $0.00, since
`micros_to_dollars(-1) == 0.0`. Smallest correction: change the README
sentence to "never negative over a window that starts at launch; a later
window can show −1 micro-dollar from rounding (displayed as $0.00)". Not
applied: documentation wording is your call.

**Remaining risks.**

- The lifecycle run is one seed and a short run. The full-trace replay
  (`--check-invariants` exists) is the broader check.
- Dollar figures are rounded independently, which is documented.

## 4. Independent retrieval evaluation

**Holdout:** `tests/data/reviewer_retrieval_queries.jsonl`, 40 queries,
marked `"split": "reviewer-holdout"` with category and rationale fields
(`tests/data/README.md`).

- **Mix:** 32 answerable and 8 unanswerable. The answerable ones include
  9 pasted log lines, 1 question followed by a log, and 1 log with a fake
  bearer token. The 8 unanswerable ones are 6 unrelated questions and
  2 failures that are not in the corpus.
- **Wording:** includes terms absent from the glossary and the corpus, such
  as VRAM, ImagePullBackOff, ENOSPC, EACCES, spot/preempted and libcudart.12.
- **Copying:** no public query was copied.

**Protocol.**

- **Scorer:** `python -m tests.reviewer_holdout_eval`.
  - It builds the index exactly as the API does, with
    `build_index(backend="tfidf")` loading the committed `retrieval/params.json`.
  - It asserts the index params equal `load_params("tfidf")`.
  - It hashes `params.json` before and after (sha256
    `89905b5c…9050f9`, unchanged).
  - It reuses `retrieval.evaluate.query_metrics` / `aggregate` with k = 5.
- **Cross-check:** the official
  `python -m retrieval.evaluate --extra tests/data/reviewer_retrieval_queries.jsonl`
  was run into a scratch directory.
  - Same metrics, and the same top 5 for all 40 queries.
  - Its public section is identical to `reports/retrieval_eval.md`.
  - From source: `run()` selects parameters from `--queries` only, and
    `_extra()` scores the extra file afterwards with those final parameters,
    so the holdout cannot influence selection.
- **Not tuned:** no parameter, glossary entry, threshold or cleaning rule
  was changed after the run. The holdout is not part of the cross-validation
  headline.
- **Honesty note:** the queries and labels were written by the same agent
  that helped build the system, after reading the corpus. They are
  independent of the tuning, not blind to the design.

**Parameters used (`tfidf`):** `w_dense=0.5, g_member=0.7, lex_k=2.0,
tau=0.419598, assign_min=0.3, noise_df=0.15`.

| Metric | Holdout (new) | Public set, *existing report* |
|---|---|---|
| Queries (answerable / unanswerable) | 40 (32 / 8) | 24 (18 / 6) |
| P@3 | **0.979167** | CV 0.938, full 0.944 |
| P@5 | **0.9625** (ceiling 0.9625) | CV 0.938, full 0.944 |
| MRR | **1.000** | CV 0.9375 |
| Recall@5 | **0.809028** (ceiling 0.809) | CV 0.753 |
| False rejections (answerable) | **0 / 32** (0.000) | CV 0.25 per fold (1 total) |
| Rejection accuracy (unanswerable) | **0.625** (5 / 8) | CV 1.000 |
| False acceptances (unanswerable) | **3 / 8** (0.375) | 0 |
| Pass (rule below) | 37 / 40 | n/a |

**Pass rule, fixed before the run:**

- An answerable query passes when its top 5 holds as many relevant ids as it
  can: P@5 == min(|expected|, 5) / 5.
- An unanswerable query passes when nothing is returned.

**Ceilings.**

- P@5's ceiling is below 1 only for the two timeout queries, which have just
  2 relevant logs (F900/F901): 0.4 each.
- Recall@5 cannot exceed 5 / |expected|: 5/9 for the 9-log OOM family, 5/6
  for 6-log groups, 1.0 for the timeout pair. The mean of those ceilings is
  0.809, which is the score measured. Every answerable query got its maximum.

**Category summary.**

| Category | Queries | Passed | P@3 | P@5 | Rejected |
|---|---|---|---|---|---|
| gpu_oom | 4 | 4 | 1.000 | 1.000 | 0 |
| cuda_compat | 4 | 4 | 1.000 | 1.000 | 0 |
| nccl | 3 | 3 | 1.000 | 1.000 | 0 |
| disk_full | 3 | 3 | 1.000 | 1.000 | 0 |
| dataset_path | 3 | 3 | 1.000 | 1.000 | 0 |
| registry_auth | 3 | 3 | 1.000 | 1.000 | 0 |
| service_credentials | 3 | 3 | 1.000 | 1.000 | 0 |
| node_reclaim | 3 | 3 | 1.000 | 1.000 | 0 |
| divergence | 3 | 3 | 1.000 | 1.000 | 0 |
| timeout_ambiguous | 2 | 2 | 0.667 | 0.400 (ceiling) | 0 |
| mixed_question_log | 1 | 1 | 1.000 | 1.000 | 0 |
| unrelated | 6 | 5 | – | – | 5 |
| unrelated_failure | 2 | 0 | – | – | 0 |

**Failed queries and likely causes.** Diagnosed with `prepare_query` and
`search(..., use_glossary=False)`; nothing was changed.

| Query | Returned | Top score vs τ 0.4196 | Cause |
|---|---|---|---|
| R37 "explain what a learning rate is" | g11 divergence ×5 | 0.524 | Lexical overlap with the group's resolution text ("lower the learning rate"), which is part of the group document. No glossary involvement (same score without it). |
| R38 "segmentation fault in our custom C++ extension" | g03 CUDA compat ×5 | 0.561 | Glossary entry `segfault → … "undefined symbol"` (`retrieval/glossary.py`) pulls it onto F050/F053. Without the glossary it is rejected. |
| R39 "SSL certificate verify failed while downloading model weights" | g10 token ×5 | 0.453 | Lexical overlap ("verify", "failed", "model") with the auth group; just above τ. No glossary involvement. |

What this means: τ was chosen so the 6 easy public negatives are rejected,
and it does not generalise to hard negatives. Those are in-domain wording, or
a failure family absent from the corpus. A fix (a resolution-text weight in
the group document, a narrower segfault entry, or a confidence margin) must
be validated on new data, not on these 8 queries.

**Classification on the holdout:**

- R20, a one-line `Error response from daemon: …`, and R31, a bare
  `Timed out waiting for response (30000 ms)`, were classified as questions
  and glossary-expanded. Both were still retrieved correctly.
- R32 (question plus pasted log) was classified as a log, which is the
  documented policy.

Per-query evidence (machine-readable: `reports/reviewer_query_results.json`):

| qid | category | query (redacted, truncated) | type | expected | returned (top 5) | first hit | P@3 | P@5 | pass |
|---|---|---|---|---|---|---|---|---|---|
| R01 | gpu_oom | ran out of VRAM during the forward pass | question | OOM family (9) | F002, F005, F001, F003, F910 | 1 | 1.000 | 1.000 | pass |
| R02 | gpu_oom | CUDA OOM, tried to allocate 2 GiB | question | OOM family (9) | F910, F002, F005, F001, F003 | 1 | 1.000 | 1.000 | pass |
| R03 | gpu_oom | 2025-03-06T10:02:11Z ERROR trainer: torch.cuda.OutOfMemoryError: CUDA … | log | OOM family (9) | F910, F002, F005, F001, F003 | 1 | 1.000 | 1.000 | pass |
| R04 | gpu_oom | batch too big for the card, crashes right away | question | OOM family (9) | F910, F002, F005, F001, F003 | 1 | 1.000 | 1.000 | pass |
| R05 | cuda_compat | cudnn shared library not found when importing torch | question | g03 (6) | F049, F052, F054, F051, F050 | 1 | 1.000 | 1.000 | pass |
| R06 | cuda_compat | undefined symbol in cublas after upgrading pytorch | question | g03 (6) | F049, F052, F050, F053, F051 | 1 | 1.000 | 1.000 | pass |
| R07 | cuda_compat | nvidia driver is older than what the framework build needs | question | g03 (6) | F054, F051, F050, F053, F049 | 1 | 1.000 | 1.000 | pass |
| R08 | cuda_compat | ImportError: libcudart.so.12: cannot open shared object file: No such … | log | g03 (6) | F049, F052, F051, F054, F050 | 1 | 1.000 | 1.000 | pass |
| R09 | nccl | allreduce watchdog timeout across ranks | question | g05 (6) | F027, F030, F028, F029, F025 | 1 | 1.000 | 1.000 | pass |
| R10 | nccl | distributed training stuck, collective op never finishes | question | g05 (6) | F027, F030, F028, F029, F025 | 1 | 1.000 | 1.000 | pass |
| R11 | nccl | RuntimeError: NCCL error in: ProcessGroupNCCL.cpp:1207, unhandled syst… | log | g05 (6) | F028, F029, F025, F026, F030 | 1 | 1.000 | 1.000 | pass |
| R12 | disk_full | ENOSPC while writing the checkpoint | question | g09 (6) | F031, F035, F036, F032, F033 | 1 | 1.000 | 1.000 | pass |
| R13 | disk_full | the volume filled up and tar could not write the archive | question | g09 (6) | F031, F035, F036, F033, F034 | 1 | 1.000 | 1.000 | pass |
| R14 | disk_full | OSError: [Errno 28] No space left on device: '/scratch/ckpt/step_9000.… | log | g09 (6) | F031, F035, F036, F033, F034 | 1 | 1.000 | 1.000 | pass |
| R15 | dataset_path | training shard parquet not found | question | g01 (6) | F014, F018, F016, F017, F013 | 1 | 1.000 | 1.000 | pass |
| R16 | dataset_path | EACCES reading the labels csv from the mounted dataset | question | g01 (6) | F013, F015, F017, F014, F018 | 1 | 1.000 | 1.000 | pass |
| R17 | dataset_path | FileNotFoundError: [Errno 2] No such file or directory: '/data/speech/… | log | g01 (6) | F014, F018, F016, F013, F015 | 1 | 1.000 | 1.000 | pass |
| R18 | registry_auth | docker pull says unauthorized for the internal registry | question | g08 (6) | F012, F007, F008, F009, F010 | 1 | 1.000 | 1.000 | pass |
| R19 | registry_auth | pod stuck in ImagePullBackOff | question | g08 (6) | F007, F008, F009, F010, F011 | 1 | 1.000 | 1.000 | pass |
| R20 | registry_auth | Error response from daemon: pull access denied for registry.internal/m… | question | g08 (6) | F009, F010, F011, F007, F008 | 1 | 1.000 | 1.000 | pass |
| R21 | service_credentials | JWT expired when calling the model API | question | g10 (6) | F046, F044, F048, F043, F045 | 1 | 1.000 | 1.000 | pass |
| R22 | service_credentials | bearer token no longer valid, auth service refuses it | question | g10 (6) | F045, F047, F043, F044, F048 | 1 | 1.000 | 1.000 | pass |
| R23 | service_credentials | [REDACTED] ⏎ AuthError: bearer token expired, rejected by auth service | log | g10 (6) | F045, F047, F043, F044, F048 | 1 | 1.000 | 1.000 | pass |
| R24 | node_reclaim | spot instance preempted with a two minute warning | question | g04 (6) | F039, F037, F041, F040, F042 | 1 | 1.000 | 1.000 | pass |
| R25 | node_reclaim | worker lost its heartbeat and the node was marked lost | question | g04 (6) | F040, F042, F037, F041, F039 | 1 | 1.000 | 1.000 | pass |
| R26 | node_reclaim | WARN agent: termination notice received, instance will be reclaimed in… | log | g04 (6) | F039, F038, F037, F041, F040 | 1 | 1.000 | 1.000 | pass |
| R27 | divergence | loss exploded to infinity | question | g11 (6) | F024, F021, F020, F019, F022 | 1 | 1.000 | 1.000 | pass |
| R28 | divergence | NaN gradients halfway through training | question | g11 (6) | F024, F022, F021, F020, F019 | 1 | 1.000 | 1.000 | pass |
| R29 | divergence | ERROR step 5121: loss=nan grad_norm=inf, stopping | log | g11 (6) | F024, F022, F021, F020, F019 | 1 | 1.000 | 1.000 | pass |
| R30 | timeout_ambiguous | upstream request timed out after 30 seconds and kept retrying | question | F900, F901 | F900, F901 | 1 | 0.667 | 0.400 | pass |
| R31 | timeout_ambiguous | Timed out waiting for response (30000 ms) | question | F900, F901 | F901, F900 | 1 | 0.667 | 0.400 | pass |
| R32 | mixed_question_log | why did my job fail? ⏎ 2025-03-06T11:00:00Z ERROR NCCL communicator wa… | log | g05 (6) | F030, F028, F029, F025, F026 | 1 | 1.000 | 1.000 | pass |
| R33 | unrelated | how much does a gpu-l node cost per hour | question | — (unanswerable) | — | – | – | – | pass |
| R34 | unrelated | what is the office wifi password | question | — (unanswerable) | — | – | – | – | pass |
| R35 | unrelated | book a meeting room for friday afternoon | question | — (unanswerable) | — | – | – | – | pass |
| R36 | unrelated | how do I request a bigger GPU quota for my team | question | — (unanswerable) | — | – | – | – | pass |
| R37 | unrelated | explain what a learning rate is | question | — (unanswerable) | F022, F023, F021, F020, F019 | – | – | – | **FAIL** |
| R38 | unrelated_failure | segmentation fault in our custom C++ extension | question | — (unanswerable) | F050, F053, F049, F052, F054 | – | – | – | **FAIL** |
| R39 | unrelated_failure | SSL certificate verify failed while downloading model weights | question | — (unanswerable) | F046, F043, F045, F047, F044 | – | – | – | **FAIL** |
| R40 | unrelated | is the cluster down today | question | — (unanswerable) | — | – | – | – | pass |

## 5. Resolution inference

*Update:* superseded by section 0, Part B (now 56 recorded, 2 inferred,
1 none; F910 ambiguous). The text below is the original pass.

**Corpus status counts (tested):** 56 recorded, 3 inferred, 0 none.

| Inferred | Score | Group | Runner-up group (score) | Margin |
|---|---|---|---|---|
| F910 | 0.481990 | g07 batch size | g03 driver/framework (0.384043) | **0.098** |
| F911 | 0.805709 | g07 batch size | g01 dataset path (0.348233) | 0.457 |
| F912 | 0.805545 | g07 batch size | g01 dataset path (0.348216) | 0.457 |

**Rule (inspected, `retrieval/index.py` `build_index`, step 4):** an empty
resolution joins the group of its single most similar recorded member (max
hybrid score, not the mean) when that score is at least `assign_min = 0.30`
(`retrieval/params.json`). Otherwise it stays a singleton with status `none`.
There is **no margin check**: the threshold alone decides. For all three real
records the runner-up is also above 0.30, so the threshold alone does not
handle an ambiguous top two. F910's margin is 0.098.

**Tested** on a 12-record fixture corpus in
`tests/test_final_validation_retrieval.py`:

| # | Case | Result |
|---|---|---|
| 1 | Clear resemblance (E1) | `inferred`, OOM group, 0.770; score equals the recomputed best-member score ✔ |
| 2 | Weak similarity to all (E2, best 0.272 < 0.30) | `none`, resolution `None` ✔ |
| 3 | Two close groups (E3: OOM 0.412 vs disk 0.572, both ≥ 0.30) | `inferred` to disk; the note names only the winner. **Gap**: strict xfail `test_case3_desired_…` |
| 4 | Misleading shared vocabulary (E4: bucket-policy 403; disk 0.398, token 0.298) | `inferred` to *disk full*, a different root cause. **Gap**: strict xfail `test_case4_desired_…`; actual behaviour pinned in `test_case4_actual_…` |
| 5 | Recorded resolution worded like another group (C3) | stays `recorded` with its own fix; not in `inferred` ✔ |
| 6 | Inferred visible in the API | `/failures/E1`: `resolution_status: "inferred"`, note "… resolution inferred from its most similar cause group (similarity 0.770)"; search hits carry the status ✔ |
| 7 | Unavailable resolution | `resolution: null`, status `none`, note "no resolution recorded for this failure" ✔ |
| 8 | Redaction before inference and indexing (E5: `password=`, `Authorization: Bearer`, `jwt=`) | ≥ 3 redactions; no secret in the stored log, signal lines, log or group documents, `/failures/E5` or a search echoing the secret; E5 still inferred from its redacted text ✔ |

Recorded and inferred evidence stay distinct: `resolution_status`
(`recorded` / `inferred` / `none`) and the note are on every hit, and an
inferred record shows its group's fix, never an invented one.

**Proposed change (not implemented: behaviour-changing).** Assign only when
the best score is at least `assign_min` **and** beats the runner-up group by a
margin, or else keep the inference but add "runner-up: <group> (<score>)" to
the note. Two caveats:

- A margin of about 0.1 would turn F910 into `none`.
- The value would have to be chosen by cross-validation on labelled
  empty-resolution cases, which the corpus does not have (3 records). Any
  value picked from these fixtures would be fitted to them.

Case 4 needs a higher `assign_min` or a resolution-aware check, with the
same caveat.

**Remaining risks:** with 3 empty records, inference is barely exercised by
real data. The threshold (0.30) was fixed by hand, not fitted.

## 6. Cause grouping

**Rule tested:** groups are keys of `normalise_resolution` (lower case,
whitespace collapsed; `retrieval/quality.py`). Matching resolution text is
a **heuristic, not a definition of root cause**: it is only as good as the
consistency of whoever typed the resolutions.

**Independent labels.** These are written from the log text alone, in the
tests and separate from the algorithm:

- the fixture mapping `CAUSE`, 12 records;
- `REAL_CAUSE` for all 59 real records.

**Metric:** pairwise precision and recall. A pair is predicted together when
it shares a group, and truly together when it shares a label.

| Corpus | Precision | Recall | False merges | False splits |
|---|---|---|---|---|
| Fixture (8 cases) | 0.857 (6/7) | 0.750 (6/8) | G3a NCCL timeout + G3b heartbeat lost (same generic fix "Relaunch the job.") | G4a/G4b disk full with reworded fixes; G5a/G5b identical logs with conflicting fixes |
| Real corpus vs log-only labels | 0.833 (130/156) | 0.992 (130/131) | g07: CUDA OOM (6) + exit-137 OOMKilled host memory (3), 18 pairs; g04: reclaim notices (4) + "Heartbeat missed; node marked lost" (2), 8 pairs | F900/F901: byte-identical logs, conflicting fixes, so two groups |

**Fixture cases (all tested ✔):**

1. Identical fix, different wording: same group.
2. Case and whitespace variants: same group.
3. A generic fix merges different causes (false merge).
4. A reworded fix splits one cause (false split).
5. Identical log with conflicting fixes: two groups, one `Conflict`, and
   both carry the note "conflicting resolutions recorded for an identical log".
6. A singleton with no resolution: `none`, its own group.
7. An inferred record joins the OOM group.
8. An unrelated empty record joins nothing.

**Reading the real-corpus result.**

- **Labels:** the log-only labels are a second opinion, not ground truth.
  The public labels (Q05, Q19) also put OOMKilled with GPU OOM, so the
  shipped grouping matches the provided labels. Under a stricter reading,
  host memory (exit 137 OOMKilled) and GPU memory are different causes with
  a shared remedy.
- **Heartbeat loss:** it is grouped as an interruptible reclaim only because
  its recorded fix says so.
- **Grouping rules:** unchanged. Changing them to raise a synthetic score
  would be tuning.

**Conflicting resolutions:** each log keeps its own recorded fix. Search
returns both, from two groups, each with the conflict note. Nothing is
silently picked.

**Remaining risks:**

- One typo or rewording in a future resolution splits a cause (case 4).
- A generic fix ("relaunch") merges unrelated causes (case 3).

## 7. Mixed log / question classification

**Classifier (inspected, `retrieval/clean.py` `classify`).** It works in
tiers:

1. **Structural log evidence:** several lines, a stack trace, JSON or 3+
   `key=value`, or a line starting with a timestamp, a level or
   `SomeError:`. Any of these makes it a log.
2. **Question cue:** a question word first, or a sentence-ending `?`.
   A `?` inside a URL is removed first. This makes it a question.
3. **Log words in one line:** an exception class, an exit code, errno or a
   level. This makes it a log.
4. **Anything else** is a question.

It runs on redacted text. `search.prepare_query` redacts first; a log is
reduced to its signal lines and never expanded; a question is
glossary-expanded.

**Tested:** 14 cases. For each, `classify`, `prepare_query` and the API
`input_type` all agree, and neither the response nor the scored text
contains a secret.

| # | Input | Expected | Result |
|---|---|---|---|
| 1 | "why is my GPU running out of memory" | question | ✔ |
| 2 | "is the scratch disk full?" | question | ✔ |
| 3 | "what does exit code 137 mean?" | question, expanded | ✔ expanded with `sigkill`, not `oomkilled` (documented: 137 alone is not proof of OOM) |
| 4 | "RuntimeError: CUDA error: out of memory" | log | ✔ |
| 5 | two lines with ISO timestamps and levels | log | ✔ cleaned to signal lines, no expansion |
| 6 | multiline log with `/api/model?revision=3` | log | ✔ (also the one-line `ERROR fetch …?revision=3 failed`) |
| 7 | "why did this fail?" + pasted log | log | ✔ policy: structural evidence wins, so the whole input is treated as a log and not expanded |
| 8 | JSON log record | log | ✔ |
| 9 | `level=error component=… msg=… step=…` | log | ✔ |
| 10 | "disk full on scratch" | question | ✔ |
| 11 | "how do I fix NCCL error: unhandled system error" | question | ✔ |
| 12 | `ERROR open /srv/why/how/config.yaml: …` | log | ✔ |
| 13 | bearer + `password=` + JWT + AuthError | log | ✔ all three secrets redacted before classification; none in the response |
| 14 | `""`, `"   "`, `"\n\t  \n"` | API 422 | ✔ (`classify` returns "question", as documented) |

*Update:* fixed, see section 0 (SPEC_DECISIONS #18).

**Gap found (strict xfail, not fixed).** A one-line docker daemon error,
`Error response from daemon: pull access denied …`, has no level, timestamp
or `SomeError:` prefix, so it is classified as a question and
glossary-expanded. Retrieval still returns the right group (holdout R20,
score 0.871).

Proposed smallest change, behaviour-changing, so not applied: treat a line
that starts with `Error`/`error` followed by text and a colon as a record
start. It needs regression checks against short symptoms such as
"Error when pulling the image", which an existing test keeps a question.

**Remaining risks:** the classifier is syntax-only. A bare one-line message
with no log syntax, like R31's timeout line, is a question by design; the
glossary then adds only related terms.

## 8. Full verification

*Update:* the re-run after the pre-submission changes is in section 0.

All commands were run from the repository root with the venv above and
`RETRIEVAL_BACKEND=tfidf`. Generated reports were written to a scratch
directory and compared byte for byte with the committed ones, so the
committed reports were not rewritten.

| Check | Command | Result | Evidence |
|---|---|---|---|
| Tests | `pytest -q -rsx` | **pass**: 800 passed, 1 skipped, 3 xfailed, 1 warning, 64.7 s (final run; 61.2 s earlier) | skip: MiniLM not cached (`tests/test_retrieval_search.py:116`); the 3 xfails are the gaps above; warning: Starlette `httpx` deprecation |
| Lint | `ruff check .` | **pass** | "All checks passed!" |
| Format | `ruff format --check .` | **pass** | "69 files already formatted" (final run) |
| Retrieval evaluation | `python -m retrieval.evaluate --queries 01c_retrieval_queries.jsonl --out-dir <scratch> --params-out <scratch copy>` | **pass**, exit 0 | CV P@3 0.938, P@5 0.938 (folds 0.750–1.000), rejection 1.000; full set 0.944 / 0.944 / 1.000. `retrieval_eval.md`, `.json` and `params.json` identical. Floors (CV P@5 ≥ 0.88, rejection ≥ 0.95) met. |
| Holdout evaluation | `python -m tests.reviewer_holdout_eval` | exit 0 | section 4; `params.json` hash unchanged |
| Data quality | `python -m retrieval.quality --out <scratch>`; `python -m broker.trace 01a_trace.csv --out <scratch>` | **pass**, exit 0 | both identical to `reports/retrieval_data_quality.md` and `reports/data_quality.md` |
| Replay report | `python -m replay.run_replay --trace 01a_trace.csv --out-dir <scratch>` | **pass**, exit 0, 102.8 s wall | `replay_results.md`, `.json` and `data_quality.md` identical; 0 TODOs; the Analysis section equals `reports/replay_analysis.md` verbatim. One policy, one seed: 5.1 s. |
| API smoke test | `uvicorn broker.api:app --port 8765` (fresh `BROKER_DB` in scratch), then `curl` | **pass** | `/health` 200; `/docs` 200; `POST /jobs` 201, repeat with the same `idempotency_key` 200, same key with a different body 409; `/queue` 200; `/sim/advance` 200; `/nodes` 200 (gpu-m ready at 374 s, 2 jobs); `/reports/cost` 200, $4.50 for 1 h of gpu-m (busy 4.03 + idle 0.47); `/failures/search` log, question and off-topic (no_match) 200; fake bearer token absent from the response; `/failures/F910` 200 (inferred); `/failures/F999` 404; blank text 422; `from_ts == to_ts` 422; `gpus: 3` 422; cancel 200 |
| YAML cases vs live server | `BROKER_BASE_URL=http://127.0.0.1:8765 BROKER_DB=<its db> python -m pytest tests/test_yaml_cases.py` | **pass**: 121 passed, 18 skipped | skips: cases that set config, provider or a restart, which a live server cannot |

First smoke attempt: I sent the idempotency key as an HTTP header, and two
jobs were created. The API takes it as the body field `idempotency_key`
(README API table), and the retest with the body field behaved as
documented. Not a defect.

## 9. UI and submission requirements

The original brief (`Take_Home_Exercise.docx`) was inspected (text extracted).

- **React or custom UI required? No.** The interface requirement is "a small
  HTTP API (submit, get, cancel, list queue/nodes, cost report). Define it
  yourself and document it." The brief never mentions a UI, a frontend or
  React.
- **How the API is demonstrated:**
  - FastAPI's Swagger UI at `/docs` and the schema at `/openapi.json`
    (README: "FastAPI's `/docs` page is the full API reference");
  - the README API table, which `tests/test_docs.py` checks against the
    served routes;
  - the HTTP-level tests (`tests/test_api.py`, `tests/test_retrieval_search.py`);
  - the YAML cases that run against a live server;
  - the generated reports.
- **Submission method:** a public Git repository link, or a `git bundle`.
  The remote is a GitHub https URL.
  - **Visibility:** not verified here (no network check). Open the URL
    signed out before sending.
  - **History:** the brief says "please do not squash your history". The
    reflog shows 3 commits on `main`.
- **Run within 10 minutes using only the repository:** the suite takes about 65 s.
  `requirements.txt` plus `pip install ruff` is enough; ruff is not in
  `requirements.txt`, as AGENTS.md notes.
- **Secrets and local artifacts:**
  - `.gitignore` excludes `*.db`, WAL/SHM files, `.venv/`, caches and
    `.DS_Store`.
  - No `.env` file exists, and `.gitignore` has no `.env` entry (none is
    needed today).
  - A scan for key, token and private-key patterns (AWS, GitHub, OpenAI-style,
    PEM) found nothing.
  - Test secrets are fake and redacted in every output;
    `reports/reviewer_query_results.json` contains none.
- **README status:**
  - present: setup, run, API, decisions, replay, retrieval, tests, known
    limitations;
  - **missing:** the brief's "Tell us which [AI tools] you used and for
    what", the start time, finish time and hours worked, and "write down what
    you cut". Known limitations and next steps exist (README "Known
    limitations", "Known limits and next steps"); the AI and time disclosure
    does not exist anywhere in the repository.
  - *Update:* README *Submission notes* now covers the interface and what
    was not built. The AI-tool, time and hours disclosure is left out of the
    repository at the author's request.

## 10. Changed files

*Update:* the pre-submission review's files are listed in section 0.

| File | Change | Why | Behaviour impact |
|---|---|---|---|
| `tests/test_cost_validation.py` | new, 18 tests | cost cases not covered by `test_costs.py` | none (tests only) |
| `tests/test_final_validation_retrieval.py` | new, 35 tests + 3 strict xfails | inference, grouping and classification validation | none (tests only) |
| `tests/data/reviewer_retrieval_queries.jsonl` | new, 40 holdout queries | independent evaluation | none; read only by the holdout scorer |
| `tests/data/README.md` | new | marks the holdout reviewer-only | none |
| `tests/reviewer_holdout_eval.py` | new script (not collected by pytest) | holdout metrics with the final params, read-only | none |
| `reports/reviewer_query_results.json` | new, generated | machine-readable per-query results | none |
| `reports/final_validation_report.md` | new | this report | none |

No source file under `broker/`, `retrieval/` or `replay/`, no parameter, no
glossary entry and no existing test was changed. A temporary
`.claude/launch.json`, used to start the smoke-test server, was deleted
afterwards. Nothing was committed or pushed.

## 11. Known limitations and risks

*Update:* 2 is partly fixed (ambiguity) and 5 is fixed; see section 0.

1. **Hard negatives.** Rejection accuracy is 0.625 on the holdout's 8
   unanswerable queries vs 1.000 on the public 6. τ does not separate
   in-domain off-topic questions or absent failure families (R37–R39).
2. **Inference has no ambiguity check.** F910's runner-up is 0.098 behind.
   A shared-vocabulary log can be inferred to a wrong fix (fixture E4).
3. **Resolution-text grouping is a heuristic.** On the real corpus it merges
   host-memory OOMKilled with GPU OOM, and heartbeat loss with reclaim. It
   splits an identical-log pair. A generic or reworded fix merges or splits
   causes.
4. **Small synthetic data.** 59 logs, 24 public and 40 holdout queries. The
   holdout was written by an author who knows the corpus and design. None of
   this supports a production-readiness claim.
5. **Docs nit.** The README says idle cost "never goes negative"; a window
   starting after launch can show −1 micro-dollar (displayed $0.00).

## 12. Verdict

**At `72b2d7e`: READY WITH CAVEATS.** The service, cost accounting and
public-set retrieval meet their documented contracts and floors; every
generated report reproduces byte for byte or was regenerated by its own
command; two of the three behaviour gaps are fixed. Caveats:

1. The brief's AI-tool, start and finish time, and hours disclosure is not
   in the repository (author's decision); supply it with the submission.
2. Hard negatives: holdout rejection 0.625; fix-wording false positives and
   the case 4 inference gap remain, pinned as strict xfails.
3. The holdout file and its results are committed; if the repository is
   public, they are public too. Check the repository's visibility before
   sending the link.

*Original verdict (at `6bd616b`):* READY WITH CAVEATS, with the disclosure,
the holdout decision and the README idle-cost sentence as open items. The
idle-cost sentence and the known-limitations notes were addressed in the
review (section 0).
