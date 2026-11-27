# Reviewer-only validation data

`reviewer_retrieval_queries.jsonl` is a **final-validation holdout**, separate
from the public evaluation set `01c_retrieval_queries.jsonl`.

- It is never read by `retrieval/evaluate.py`'s parameter search, by pytest,
  or by anything that sets `retrieval/params.json`, the glossary, thresholds
  or cleaning rules. It is scored only by
  `python -m tests.reviewer_holdout_eval`, which loads the committed
  parameters unchanged and checks that `params.json` was not written.
- Labels were written from the corpus (`01b_failure_logs.jsonl`) before the
  first run, using the same convention as the public set (all logs that share
  the cause). An empty `expected_ids` means no record in the corpus answers it.
- Its author also wrote the system, so it is independent of the tuning but
  not blind to the corpus or the design (see `reports/final_validation_report.md`).
- Secrets in it are fake (`FAKEtestONLY...`) and are redacted in every output.

If this repository is published, this file is public too; keep a private copy
elsewhere if it must stay unseen.
