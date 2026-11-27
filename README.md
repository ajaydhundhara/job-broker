# Job Broker

An HTTP service that queues GPU jobs from teams, places them on simulated
nodes, starts and stops nodes to control cost and reports cost per team,
plus a retrieval endpoint that returns similar past failures and their
resolutions.

**Scope:** the whole brief (`Take_Home_Exercise.docx`): requirements 1–9
and the Retrieval / AI track. Interruptible nodes, optional in the brief,
are not used.

### Feature status

Labels: **implemented** (works as the brief asks), **policy choice** (our
answer where the brief leaves it open), **simulation-only** (exists because
nothing real runs), **test-only** (exists only so tests can be exact),
**known limitation**.

| Feature | Label | Where |
|---|---|---|
| Submit a job, idempotency key per team (req. 1) | implemented | `POST /jobs`, *Decisions* |
| Placement on a node with free GPUs, launch if none fits, keeps working when the provider fails (req. 2) | implemented | *Scheduler policy*, *Retry policies* |
| Larger node type as a fallback, at most 2× the price | policy choice | D3, D13 |
| Idle reaper, N minutes configurable (req. 3) | implemented | *Idle reaper* |
| A ready node lives at least 10 minutes before it is reaped | policy choice | D1 |
| Lifecycle queued → running → succeeded / failed / cancelled, cancel while queued or running (req. 4) | implemented | `docs/state_machine.md` |
| Fairness: `fair` queue order by 24 h GPU usage, a per-team node cap, eviction of another team's idle node at the cap (req. 5) | policy choice | D6, D12 |
| Priority only orders the queue; it never preempts | policy choice | *Queue order* |
| One team per node (Security: no two teams on one machine) | policy choice | D13 |
| Cost report: GPU-hours and cost per team for a window (req. 6) | implemented | `GET /reports/cost`, *Billing* |
| Idle cost charged to the node's owning team, failed launches to `provider_overhead` | policy choice | *Cost attribution* |
| Restart without losing jobs, nodes or cost history (req. 7) | implemented | *Restart recovery* |
| Trace replay, wait percentiles, cost, fair vs fifo (req. 8) | implemented | *Running the replay*, `reports/replay_results.md` |
| HTTP API (req. 9) | implemented | *API* |
| Failure retrieval for a log or a question (Retrieval track) | implemented | *Retrieval* |
| Fake provider: boot 4–8 min, ~3% never ready, capacity bursts, billing | simulation-only | `broker/provider.py`, D4, D10 |
| `/sim/now`, `/sim/advance`, `/jobs/{id}/complete`, hidden job plans (`attach_plan`) | simulation-only | *API*, *Running the replay* |
| `scheduler_enabled`, `reaper_enabled`, `unavailable_windows`, `never_ready_launches` | test-only | `broker/config.py`, D7 |
| MiniLM backend: optional, never installed or evaluated here | known limitation | *Retrieval* |
| No auth; one process; interruptible nodes and preemption not built; guessable job ids | known limitation | *Known limitations* |

## Run

```bash
pip install -r requirements.txt
```

```bash
uvicorn broker.api:app --reload
```

The database file comes from the `BROKER_DB` environment variable (default
`./broker.db`); it is created on first start. Other settings
(`broker/config.py`, `Config.from_env`):

| Variable | Default | Meaning |
|---|---|---|
| `BROKER_MAX_NODES` (old name `BROKER_MAX_LIVE_NODES`) | 10 | live-node cap |
| `BROKER_IDLE_MINUTES` (old name `BROKER_IDLE_TIMEOUT_MINUTES`) | 10 | idle reaper timeout |
| `BROKER_POLICY` | `fair` | queue policy, `fair` or `fifo` |
| `BROKER_SEED` | 42 | fake provider seed |

FastAPI's `/docs` page is the full API reference.

## API

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Liveness check: `{"status": "ok"}`. |
| POST | `/jobs` | Submit a job (team, gpus 1/2/4/8, estimated_minutes, priority, optional idempotency_key). 201 new, 200 repeat. |
| GET | `/jobs/{job_id}` | One job with all fields plus `wait_seconds`. |
| POST | `/jobs/{job_id}/cancel` | Cancel a queued or running job. |
| POST | `/jobs/{job_id}/complete` | *Internal.* Mark a running job succeeded or failed. |
| GET | `/queue` | Queued jobs in scheduling order, with position and wait so far. |
| GET | `/sim/now` | Current simulated time (`now_ts`, `now_iso`). |
| POST | `/sim/advance` | *Internal.* Move simulated time forward by `minutes` or to `to_ts`. |
| GET | `/nodes` | Live nodes with GPUs used/free (derived from running jobs) and cost so far; `?include_terminated=true` adds ended nodes. |
| GET | `/reports/cost` | GPU-hours, busy/idle/total cost per team, provider overhead and grand total for `[from_ts, to_ts)` (defaults: 0 to now; `from_ts >= to_ts` is 422). |
| POST | `/failures/search` | Similar past failures and their resolutions for a log or a question (see *Retrieval*, API). |
| GET | `/failures/{failure_id}` | One past failure, redacted, with its group and resolution; 404 if unknown. |

Errors: 404 unknown job or failure id, 409 a conflict with the current state
(below), 422 invalid input, 503 `/failures` when no logs file is configured.

### Decisions

- **Idempotency.** `idempotency_key` is scoped per team. A repeat with the same
  key and the same request returns the existing job (200, no new row); the same
  key with a different request is 409. "Same request" compares a sha256 of
  `team|gpus|estimated_minutes|priority` after validation and defaults, so an
  omitted priority equals `"normal"`. Unknown fields are rejected (422), so a
  typo cannot silently change what is compared.
- **Strict input.** `gpus` and the minute fields must be real integers:
  `"4"`, `4.0` and `true` are 422.
- **Limits.** `team` is 1–128 characters after trimming (non-ASCII allowed).
  `estimated_minutes` is 1–10080 (7 days; the brief says jobs run "minutes to
  days" and sets no maximum, so this bound is our choice). Outside these: 422.
- **Cancel.** Allowed from `queued` or `running`. Cancelling an already
  cancelled job returns it unchanged (200, no event). Cancelling a succeeded or
  failed job is 409. When the last running job leaves a node, the node's
  `idle_since` is set, which starts the idle reaper's countdown.
- **Lifecycle.** The only transitions are queued→running, queued→cancelled,
  running→succeeded, running→failed, running→cancelled (`ALLOWED_TRANSITIONS`
  in `broker/state.py`).
- **Queue order.** high > normal > low, then (policy `fair`, the default)
  the team's last-24 h GPU usage, then oldest submit, then creation order
  (`jobs.created_seq`) for jobs submitted in the same second (`queue_order()`
  in `broker/scheduler.py`, SPEC_DECISIONS.md #6).
- **`wait_seconds`.** started − submitted once started; now − submitted while
  queued; ended − submitted if cancelled before starting (it waited until it
  was cancelled; the brief counts cancelled jobs in wait time).
- **Time.** Simulated time never goes backwards: `/sim/advance` to an earlier
  `to_ts` is 409. `{"minutes": 0}` and `to_ts` equal to now return 200 and
  do not move the clock (no `clock_advanced` event), but run one scheduling
  pass, so anything already due happens (SPEC_DECISIONS.md #8).
- **Concurrent advances.** `/sim/advance` calls are serialized by one
  process-wide lock: two parallel `{"minutes": 10}` move the clock 20
  minutes, and an absolute `to_ts` waits for a running advance (then 409 if
  the clock is already past it). Each response reports the clock after its
  own advance; which caller goes first is not defined (SPEC_DECISIONS.md #15).
- **Cost window.** `[from_ts, to_ts)`: from inclusive, to exclusive, so
  adjacent windows never count a second twice. `from_ts >= to_ts` is 422.
  The defaults are `from_ts = 0` and `to_ts = now`, so on a new server whose
  clock is still at 0 the default window is empty and also 422: advance the
  clock first or pass `to_ts`.
- **Billing.** A node is billed per second from launch (boot time included)
  until termination, or until now if still alive, clipped to the window.
- **10-minute minimum.** Applies once a node has terminated: a node that lived
  under 600 s is topped up to 600 s. The top-up is charged at its last billed
  second (`terminated_at - 1`), so it counts in a window when
  `from_ts < terminated_at <= to_ts`: exactly one window of any split, and a
  node reaped at "now" shows it immediately. Live nodes have no minimum yet.
- **Cost attribution.** The whole node cost goes to the team that owns the node
  (only its jobs may run there), split into *busy* (each job's GPUs ÷ the
  node's GPUs × its run time) and *idle* (the rest: empty GPUs, boot time, idle
  before reaping, minimum top-up). Idle is charged to the owner because that
  team's demand caused the launch; showing it separately tells Finance where
  the waste is. Failed launches go to `provider_overhead`, charged to no team.
  A job's run is clipped to its node's life, so idle cost never goes negative.
- **Money.** Computed in integer micro-dollars. A window's cost is the
  node's running total at the window's end minus its running total at the
  start, each floored once, so cost windows add up exactly: [a, b) + [b, c)
  = [a, c) to the micro-dollar, and a node's whole bill is a single floor.
  The price: `idle` (= total − busy) can be −1 µ$ in a window that doesn't
  start at launch (shown as $0.00); we keep `total` exact instead of
  keeping `idle` non-negative. Converted to dollars only in responses, rounded half-up to cents
  (`micros_to_dollars` in `broker/costs.py`). Each figure is rounded on its own,
  so displayed parts can differ from the displayed total by 0.01 (busy 1.13 +
  idle 3.38 vs total 4.50). `gpu_hours` is shown with 3 decimals.
- **Concurrency.** Each request gets its own SQLite connection, closed when
  the request ends. It is opened with `check_same_thread=False` because FastAPI
  may run the dependency and the route on different thread-pool threads; the
  connection is still used by only that request, one step at a time. Writes
  are serialized by `BEGIN IMMEDIATE`, so the idempotency lookup and insert
  cannot interleave, and `UNIQUE (team, idempotency_key)` in the schema stops a
  duplicate even if the lookup misses: the insert is rolled back with its
  event and the existing job is returned (or 409 for a different request).
  `tests/test_idempotency.py` forces these interleavings on separate
  connections, with and without a missed lookup; `tests/test_concurrency.py`
  adds 50-round stress tests of 8 parallel submits (marker `slow`).
- **No auth.** Any caller can act on any team's jobs. `/jobs/{id}/complete` and
  `/sim/*` are internal (for tests and the simulator) and would not be exposed
  in production.

## Scheduling and simulation

Every job, node and provider-node state, each transition, the component
that performs it and where each invariant is enforced: [docs/state_machine.md](docs/state_machine.md).

The full rule set, with which parts are **[brief]** (the assignment),
**[policy]** (our design choice) and **[test]** (exists only for exact
tests), is in [SPEC_DECISIONS.md](SPEC_DECISIONS.md). This section is the
overview; the decision numbers (D1–D16) point there. How the tests changed
with it: [tests/TEST_MIGRATION_MAP.md](tests/TEST_MIGRATION_MAP.md).

### Architecture

| Module | Role |
|---|---|
| `broker/api.py` | HTTP routes; one SQLite connection per request; runs a scheduling pass after each state-changing request |
| `broker/service.py` | request logic (validation, idempotency, lifecycle) in one transaction each |
| `broker/engine.py` | the only code that moves simulated time; timestamp groups; reconciliation |
| `broker/scheduler.py` | queue order, placement, launches, caps, fallback, retry policies |
| `broker/reaper.py` | idle reaper |
| `broker/provider.py` | fake cloud provider (`launch_node`, `terminate_node`), seeded |
| `broker/state.py` | job/node state changes shared by all of the above; `ALLOWED_TRANSITIONS` |
| `broker/costs.py` | billing, derived from node and job timestamps |
| `broker/config.py` | `Config`: every setting in one frozen dataclass |
| `broker/trace.py` | trace loading, validation and the data-quality report |
| `broker/service.py` `attach_plan` | simulator only: a replayed job's hidden duration and outcome (no HTTP route) |
| `replay/run_replay.py` | replays the trace through the HTTP API; policy comparison CLI |
| `replay/metrics.py` | nearest-rank percentiles, per-team summaries and the burst-window metrics |

A request commits its own change first (e.g. the new job), then the API runs
`engine.run_pass()`, a zero-minute timestamp group, in a second transaction.
So a job that fits starts during its own `POST /jobs`, and a submit that needs
a node writes `job_submitted` then `node_launched`. If that pass fails, its
transaction rolls back, the error is logged, and the request still returns
the change it committed (201/200 with the job as saved), not a 500: a 500
would invite a retry, and a retry without an `idempotency_key` would submit
the job twice. The next request or `/sim/advance` runs the skipped pass. The
same applies to cancel and complete. `/sim/advance` itself still returns 500
on a failure, which is safe to retry (decision 9: it resumes from the last
committed group).

### Scheduler policy (D6, D12, D13)

- **Queue order.** Default `fair` (env `BROKER_POLICY`): priority (high >
  normal > low), then the team's GPU-seconds over the last 24 h (least
  first), then submit time, then `jobs.created_seq` (a persistent creation
  number, D6). `fifo`: the same without the usage term. Priority orders the
  queue only; nothing is ever preempted. `GET /queue` shows this order.
- **Placement.** A job only goes on a *ready* node of its *own team*
  [policy, the security rule; asserted in code]. Best fit: the node left with
  the fewest free GPUs, then lowest id. Free GPUs are derived from running
  jobs, so a node can't be over-committed. Smaller jobs may start behind a
  blocked big one (backfill).
- **Booting capacity.** Queued jobs that fit on their team's *launching*
  nodes need no new launch.
- **Launching.** The cheapest type that fits the job (1 → gpu-s, 2 or 4 →
  gpu-m, 8 → gpu-l). Five 1-GPU jobs get five gpu-s ($5.00/h), not one gpu-l
  ($8.50/h). Packing several jobs onto one larger node happens only when the
  jobs needing a node outnumber the free node slots.
- **Fallback.** When the preferred type is refused or backing off, the next
  larger type is tried in the same pass if it costs at most 2× as much:
  gpu-m → gpu-l yes (1.89×), gpu-s → gpu-m no (4.5×) (D3).
- **Caps.** Never more than 10 live (launching + ready) nodes [brief]. Under
  `fair`, while another team waits, a team may not launch beyond
  `ceil(10 / active teams)` nodes. At the 10-node cap, a team *under* its
  fair cap may evict another team's ready, empty node that has lived at
  least 600 s and no queued job of its team fits on (longest idle first,
  end reason `evicted`, event `node_evicted`). A capped team never evicts.
  `Config(evict_at_cap=False)` turns eviction off.
- **Actual durations** (`sim_job_plans`) are never read by the scheduler; a
  test checks the source.

### Retry policies (D2)

Both are per node type and counted from the `events` table, so they survive
a restart:

| Failure | Event | Retry delay after the k-th consecutive failure | Reset by |
|---|---|---|---|
| `CapacityUnavailable` (nothing launched or billed) | `capacity_unavailable` | `min(60·2^(k−1), 900)` s: 60, 120, 240, 480, 900 | `node_launched` of that type |
| never ready (failed after 15 min, billed) | `launch_failed` | 0, 60, 120, 240, 480 (cap) s | `node_ready` of that type |

A type that is backing off is not tried again until its delay ends; the
engine wakes up at exactly that second. Jobs stay `queued` throughout.

### Node lifecycle and end reasons (D11)

`launching` → `ready` → `terminated`, or `launching` → `failed` (never
ready). `nodes.end_reason`: `idle_timeout` (reaper), `evicted` (cap),
`launch_failed` (never ready), `provider_reconciled` (the provider ended it,
found by reconciliation), `manual`, `provider_reclaimed` (interruptible,
not used). Node ids are sequential, `node-00001`, … (D14); so are job ids,
`job-000000000001`, … (the creation sequence in hex, D16).

### Idle reaper (D1, D5)

A ready node with no running job is terminated (`node_reaped`,
`idle_timeout`) at

    eligible_at = max(launched_at + 600, idle_since + idle_minutes·60 + 1)

i.e. idle *more than* N minutes (default 10, env `BROKER_IDLE_MINUTES`; the
old name `BROKER_IDLE_TIMEOUT_MINUTES` still works) **and** at least 600 s
old [policy: the first 10 minutes are billed anyway]. Example: launched at
0, idle from 0, N = 5 → idle-eligible at 301, reaped at 600. The engine jumps
to that exact second, also inside one long advance.

- `idle_since` is `ready_at` when the node becomes ready, cleared when a job
  starts, set when the last job ends. It is stored, so reaping is the same
  after a restart.
- Never reaped: a node that is not ready, a node with a running job, or a
  node a queued job of its own team fits on (it gets that job in the same
  timestamp group, D5).

### Billing

Unchanged by the decisions; see *Billing*, *10-minute minimum*, *Cost
attribution* and *Money* above. Idle time before a reap is billed to the
owning team as `idle`. A node reaped at 600 s is billed 600 s; one reaped at
601 s, 601 s (the 600 s minimum bill [brief] and the 600 s minimum lifetime
[policy] are separate rules, D1).

### Provider simulation (D4, D10)

- Ready 4–8 min after launch; ~3% never become ready and are reported
  `failed` 15 min after launch [brief].
- `CapacityUnavailable` comes in bursts [brief]: time is cut into 10-minute
  buckets; a burst starts in a bucket with probability 2% and lasts 2–6
  buckets, per node type.
- Every random draw comes from `random.Random(f"{seed}:{purpose}:{key}")`,
  a fresh seeded generator per decision (keys: node id, outage bucket). No
  global `random.*` calls (a test scans for them). A single shared stream
  would depend on how many draws happened before, which a restart forgets;
  keyed draws are the same however a run is split. Seed: `BROKER_SEED`
  (default 42).
- [test] `Config.unavailable_windows=(("gpu-m", 100, 300),)` refuses gpu-m
  launches for `100 <= now < 300`; `never_ready_launches=(2,)` makes launch
  number 2 never ready; `boot_min_s = boot_max_s` fixes the boot delay.

### Event processing (D5, D8, D9)

`advance_to(target)` jumps from event time to event time (node ready or
failed, a planned job end, a reap time, a retry time). Each instant is one
**timestamp group**, processed in its own transaction, in this order and
repeated until nothing changes:

1. provider transitions (by time, then node id);
2. reconciliation with the provider;
3. planned job ends (trace replay; by planned end, then `created_seq`);
4. idle reaper;
5. scheduler.

Then a last group at `target` and one `clock_advanced` event. A zero-minute
advance moves nothing and writes no `clock_advanced`, but runs a group, so
an eligible job starts. Bounds: 1000 rounds per group, 1,000,000 groups per
advance; hitting one raises `SimulationStuck` instead of hanging. Advancing
1 minute 120 times gives the same result as 2 hours once (tested).

### Restart recovery

Everything is in SQLite: the clock, jobs, nodes, the provider's own nodes,
the events the retry policies count, and the plans. A restart builds a new
`FakeProvider`; with keyed generators and sequential node and job ids it
continues the same sequence. A crash mid-advance keeps every committed group and rolls
back the one in progress; calling `advance_to` again resumes from the stored
clock and does nothing twice, because each step acts only on state still
due (tested by injecting a crash). Reconciliation runs in every group and at
startup:

- a provider node with no broker record is terminated (`orphan_terminated`);
- a broker node the provider has ended is marked `terminated` /
  `provider_reconciled` (or `failed` / `launch_failed`), and its running
  jobs fail with `node_lost`.

Schema v1 databases are migrated to v2 on start (`created_seq` numbered in
arrival order; `idle_reaped → idle_timeout`, `interrupted →
provider_reclaimed`).

### Running the replay

```bash
python -m replay.run_replay --trace 01a_trace.csv
```

Results: [reports/replay_results.md](reports/replay_results.md) (every number
also in `reports/replay_results.json`, money in micro-dollars), and the trace's
data quality in [reports/data_quality.md](reports/data_quality.md). Options:
`--policies fair,fifo`, `--idle-minutes 10`, `--sweep-idle 2,10,30`,
`--seeds 42,43,44,45,46`, `--burst-window 2025-03-05T05:00Z,2025-03-05T08:00Z`,
`--keep-duplicates`, `--check-invariants`, `--out-dir reports`.
`python -m broker.trace 01a_trace.csv` writes only the data-quality report.
Candidate observations for the report's Analysis section, for author review
and tied to its tables, are in
[reports/replay_analysis_candidates.md](reports/replay_analysis_candidates.md)
(hand-written; `tests/test_replay_candidates.py` fails if a figure it cites is
no longer in the regenerated report).

How the replay works and the rules it follows:

- **Through the real API.** Each scenario gets a fresh SQLite file and a
  FastAPI `TestClient` (in process: no server, no real sleeps). For each row
  in time order: `POST /sim/advance {to_ts}`, then `POST /jobs`. On 201 the
  row's hidden truth (`actual_minutes`, `outcome`) is attached with
  `service.attach_plan`, a simulator-only function with no HTTP route (users
  must never set it). After the last row, time moves in 60-minute steps until
  nothing is queued or running and no node is alive (at most 14 extra days,
  then it fails listing what is stuck).
- **Every job runs `actual_minutes` after it starts**, then ends with the
  row's outcome. A `cancelled` row means "the user cancelled after
  `actual_minutes` of runtime", so it counts for both cost and wait time.
  Wait = `started_at − submitted_at`.
- **Invalid rows are rejected, not repaired**, by the same rules as
  `POST /jobs` (lines 88 and 393 ask for 3 and 16 GPUs). Non-UTC offsets are
  converted; rows are sorted by (UTC time, line); a timestamp without an
  offset would be rejected.
- **Duplicates collapse through the API's own idempotency.** Each row's
  `idempotency_key` is the sha256 of the normalised row, so an identical row
  (also one identical only after UTC conversion) gets 200 and the same job,
  and is counted. 8 of the 14 duplicates sit inside the burst, where they may
  be real resubmissions: `--keep-duplicates` adds the line number to the key
  so every row becomes its own job, and the results can be reported both ways.
- **Seeds.** Boot times and never-ready/outage draws are keyed by node launch
  number and time bucket (decision 10). Policies launch different nodes, so
  the same seed gives different draws under fair and fifo. Each scenario
  therefore runs once per seed; the report shows the median and min–max, and
  counts in how many seeds fair is lower / equal / higher than fifo. A
  difference is treated as real only if it holds in every seed.
- **Reproducible.** Same trace, options and seeds: byte-identical
  `replay_results.json` and `.md` (tested). The JSON holds no run time or
  timestamp; the runtime is printed to the console only.
- **Metrics:** nearest-rank percentiles by hand (rank = ceil(p/100 × n),
  1-based); medians across seeds are lower medians (an observed value, so
  micro-dollars stay integers); idle share = idle / grand total (provider
  overhead, i.e. never-ready nodes, is reported on its own).
- **Burst window (section 5).** Computed after the run from the jobs table;
  the scheduler is not involved. Queue at t = jobs with
  `submitted_at <= t < started_at`. `burst_queue`: the queue every 15 min from
  the window start to 2 h after its end (overall in the table, per team in
  the JSON); `burst_peak_queue` / `burst_peak_after_s`: the largest queue at
  any instant in the window and when it was first reached; `burst_recovery_s`:
  when the last job submitted in the window started, minus the window end
  (negative: before it ended). Reported for the scenarios at the main idle
  timeout.

### Known limitations

- **One process.** The engine runs inside requests; there is no background
  clock. Simulated time only moves on `/sim/advance` (or replay). Deploy
  one process with one worker: the advance lock is in memory, so a second
  process or `uvicorn --workers N` on the same database is not serialized.
- **Provider in the same database.** A launch and the broker's record of it
  commit together, which is only possible because the fake provider is local.
  Reconciliation is there for a real provider, but has only been tested
  against hand-made disagreements.
- **Fair-share is per launch, not per GPU.** The fair cap counts nodes, not
  GPUs; a team on gpu-l nodes holds more GPUs per slot.
- **Usage window.** Fair ordering uses the last 24 h of GPU-seconds,
  recomputed per pass with a query over all started jobs; fine for this
  scale, not indexed for millions of jobs.
- **Guessable job ids.** Job ids are the creation sequence (D16), so they
  show how many jobs exist; with no auth this exposes nothing new.
- **Not built:** preemption, interruptible nodes, autoscaling across regions,
  estimated minutes in placement.

## Retrieval

Given a failed job's log or a free-text question, the service returns the
most similar past failures and their resolutions.

Built in four sub-steps: cleaning and data quality (A), the glossary (B),
index, search and API (C), and evaluation with parameter selection (D).

### Retrieval as implemented

- **Backend.** `RETRIEVAL_BACKEND=auto` (the default) uses MiniLM only if it
  is installed and already in the local cache, otherwise tfidf
  (scikit-learn) with one warning; `tfidf` and `minilm` force one. MiniLM is
  not installed here, so the service runs, and every number below was
  measured, on tfidf. The backend in use is in every response.
- **Grouping assumption.** Logs with the same resolution text (case and
  spacing ignored) share a cause; results are ranked and returned group by
  group. Two different texts for one real cause would form two groups.
- **Inferred-resolution assumption.** A log with an empty resolution joins
  the group of its most similar log if that hybrid score is at least
  `ASSIGN_MIN = 0.3`, and is returned as `inferred` with a note; otherwise it
  is `none` with resolution `null` and the note "no resolution recorded for
  this failure". In this corpus 3 logs are inferred (see *Resolutions*).
- **Evaluation limits.** 24 labelled queries (18 answerable), all seen
  while building; numbers are tfidf only; one answerable query still fails;
  details under *Known limits and next steps*.

### Evaluation

```bash
python -m retrieval.evaluate --queries 01c_retrieval_queries.jsonl [--backend tfidf] [--extra data/my_queries.jsonl]
```

Writes [reports/retrieval_eval.md](reports/retrieval_eval.md) and `.json`,
and saves the chosen parameters to `retrieval/params.json` under the
backend's name. Two runs give byte-identical JSON (tested). The run takes
about 3 s.

**Headline: cross-validated, tfidf backend.** 24 queries: 18 answerable and
6 with nothing relevant.

| metric | mean over 4 folds | fold range |
|---|---|---|
| P@3 | **0.938** | 0.750 – 1.000 |
| P@5 | **0.938** | 0.750 – 1.000 |
| rejection accuracy | **1.000** | 1.000 – 1.000 |
| MRR | 0.938 | 0.750 – 1.000 |
| recall@5 | 0.753 | 0.625 – 0.833 |
| false rejections | 1 of 18 (in fold 2) | |

- **How it was measured.** Parameters (W_DENSE, G_MEMBER, LEX_K and 10 TAU
  candidates) are chosen by grid search on the 3 training folds only and
  scored on the held-out fold. The folds are stratified, with seed 42.
- **Recall@5 is capped by design.** A cause group has 6 or 9 logs and k is
  5, so recall@5 cannot reach 1.
- **The single held-out failure is "libcudnn missing when starting the
  job".** It gets no result in cross-validation (fold 2's TAU is 0.536),
  which accounts for fold 2's P@5 of 0.75.
- **Full set, optimistic** (parameters chosen on the same 24 queries):
  P@3 0.944, P@5 0.944, rejection accuracy 1.000.
  - Chosen: `w_dense=0.5, g_member=0.7, lex_k=2.0, tau=0.419598`, saved in
    `params.json` (fixed: `assign_min=0.3`, `noise_df=0.15`). Every
    cross-validation fold chose `0.3 / 0.5 / 2.0` with TAU 0.528–0.536.
  - The one miss is the same query: on the full set it ranks the
    out-of-memory group first. It is left unfixed on purpose: a fix
    written now would be fitted to a visible query.
- **Backend.** All numbers are tfidf. MiniLM is not installed or cached
  here, so it is not evaluated, and its `params.json` entry keeps the
  untuned defaults.
- **Ablations** (cross-validated P@5):

  | variant | CV P@5 |
  |---|---|
  | BM25 only | 0.575 |
  | dense only | 0.715 |
  | hybrid | 0.645 |
  | hybrid + groups | 0.887 |
  | + glossary | 0.938 |

  The glossary also lifts rejection accuracy from 0.750 to 1.000. Without
  the noise filter, CV is unchanged (P@5 0.938, rejection 1.000), but the
  full-set score rises from 0.944 to 1.000 for P@3 and P@5. The queries are
  single-line, so the filter acts only on the indexed documents. The filter
  stays on with `noise_df = 0.15`, a fixed value that is not tuned.
- **Regression floors** (`tests/test_retrieval_eval.py`): CV P@5 ≥ 0.88 and
  CV rejection accuracy ≥ 0.95. That is the measured value minus 0.05: one
  query moves a fold's P@5 by about 0.2.
- **`--extra`** scores your own query file, in the same format, with the
  final parameters as a separate "unseen" table. It is never used to choose
  anything. No such file exists yet.
- **A glossary fitted to the queries was found and removed.** The first
  evaluation gave 1.000 everywhere. A per-query check found that 11 of the
  18 answerable queries matched 13 glossary phrases. Those phrases had been
  added after the first draft, and several were unusual wordings that match
  a query exactly, such as `gradients infinite`, `libcudnn missing` and
  `dataset file missing`.
  - The glossary sits outside cross-validation, so CV could not catch this.
  - The 13 phrases were removed. Now no visible query matches a phrase that
    was not in the draft written before the queries were opened.
  - With them the score was 1.000; with the draft glossary, 0.887; now
    0.938. The 0.938 is the honest figure.

### Known limits and next steps

- **The eval set is tiny.** 18 answerable queries, about 5 per fold, so one
  query moves a fold by about 0.2. The fold range matters more than the
  third decimal.
- **The visible queries have been seen.** The glossary and cleaning have
  not been changed since. The failure above is documented, not fixed.
  `--extra` with new questions is the real test.
- **Lexical at heart.** tfidf can't bridge synonyms the glossary lacks;
  MiniLM (or another local embedding model) would, but is untested here.
- **Next steps:**
  - re-rank the top groups with a cross-encoder;
  - add an LLM summary of the fix, grounded only in the returned logs (in
    `<retrieved_log>` tags, per AGENTS.md);
  - learn from resolved tickets: every new resolution adds a labelled log
    and, over time, eval queries no one has tuned to.

The input is `01b_failure_logs.jsonl` at the repo root, next to the trace.
`FAILURE_LOGS_PATH` overrides it. The files are not moved, because they are
requirement files.

```bash
python -m retrieval.quality   # writes reports/retrieval_data_quality.md
```

[reports/retrieval_data_quality.md](reports/retrieval_data_quality.md)
checks each stated belief about the logs against the data, then lists the
cause groups, the inferred resolutions and the conflicts.

### API

| Route | Body / answer |
|---|---|
| `POST /failures/search` | `{text: 1..20000 chars, not blank, k: 1..20 = 5}` → `{input_type: log or question, backend, no_match, message, results: [{id, team, cause_group, error_lines, resolution, resolution_status: recorded, inferred or none, score, note}]}` |
| `GET /failures/{id}` | the redacted record, its signal lines, group and resolution; 404 if unknown |

- **When the index is built.** Once, in the app's lifespan.
  - `create_app(retrieval_index=...)` takes a prebuilt one, as tests do.
  - A missing default logs file disables only `/failures`, which answers
    503; the broker keeps scheduling.
  - A missing `FAILURE_LOGS_PATH` that was set explicitly fails startup.
  - A build takes about 0.03 s.
- **Backend.** `RETRIEVAL_BACKEND` is `minilm`, `tfidf` or `auto`
  (default).
  - `auto` uses MiniLM if it is installed and cached locally, otherwise
    tfidf, with one warning.
  - `minilm` fails loudly if the model is unavailable.
  - Nothing is ever downloaded: the model loads with `local_files_only`,
    with `HF_HUB_OFFLINE=1` as a backstop.
  - MiniLM needs `pip install -r requirements-embeddings.txt` and a model
    already in the local cache. It is not installed in this repo's
    environment, so every number here is tfidf.
  - The backend in use is returned in each response.
- **`score`** is the cause group's score, the number compared with TAU, so
  logs in one group share it. Within a group, logs are ordered by their own
  score.

### Pipeline

```
ingest: JSONL → redact → learn noise templates → signal lines
        → per-log docs (signal + resolution): BM25 + embedding matrix
        → cause groups (by resolution; empty ones inferred) + conflicts
        → per-group docs (members' signal lines + resolution): BM25 + matrix
query:  text → redact → log? signal lines : question? + glossary terms
        → per-log hybrid → per-group score → TAU threshold → group by group
```

- **Hybrid score.**
  `hybrid = W_DENSE · clip(cosine, 0, 1) + (1 − W_DENSE) · bm25 / (bm25 + LEX_K)`.
  BM25 is unbounded, so the squash puts both halves on one scale and TAU
  means the same thing for every query.
  - BM25 is written by hand (k1 = 1.5, b = 0.75, an IDF that is never
    negative).
  - Each distinct query term counts once, so a glossary term that repeats
    the user's word does not double its weight.
- **Group-first ranking.**
  `group = G_MEMBER · best member + (1 − G_MEMBER) · group document`.
  The label is "shares the cause", and logs with one cause are worded
  differently (NCCL "unhandled system error" vs "communicator was
  aborted"). So one strong hit on a member brings in its siblings.
  - Results are listed group by group.
  - The next group is included only if it also reaches TAU, and results are
    never padded with weak matches.
  - If no group reaches TAU, the answer is `no_match` with "No similar past
    failure found".
- **Log or question.** One classifier, `clean.classify`, decides. A log is
  reduced to its signal lines and never expanded; a question is expanded with
  the glossary. `input_type` reports the result. Tiers, first match wins:
  1. Structural log evidence → log: several non-blank lines, a stack trace,
     a structured record (JSON object, 3+ `key=value` fields), or a line
     starting like a record (timestamp, level, `SomeError:`).
  2. A question cue → question: a question or request word first (what,
     why, how, explain, …), or a `?` that ends a sentence. A `?` inside a
     URL does not count.
  3. Log words in one line → log: an exception class, an exit code (`exit
     code`, `exited with code N`, `exit status N`), `errno`, an upper-case
     level.
  4. Anything else → question (a short symptom such as "gpu out of memory").

  So `ERROR fetch https://h/x?rev=3 failed` is a log and gets no glossary
  terms, while "what does exit code 137 mean?" stays a question. A question
  with a log pasted under it counts as a log: the log is cleaned, and the
  question's words are not expanded.
- **Why no vector database.** 59 documents (plus 11 group documents) fit
  in a NumPy matrix, and a search is one matrix-vector product. Measured with
  tfidf: about 5 µs for the product, about 0.6 ms for a whole search
  (cleaning, BM25, embedding). A vector DB starts to pay off when:
  - the corpus reaches hundreds of thousands of logs (approximate nearest
    neighbour search);
  - logs arrive continuously and need incremental upserts without
    rebuilding;
  - several services share one index, or metadata filters (team, date)
    must run inside the search.

### Resolutions: recorded, inferred, none, conflicting

- **Cause groups** are built by normalised resolution text (case and
  spacing ignored).
- **An empty resolution** joins the group of its most similar member (the
  max per-log hybrid score) if that score is at least `ASSIGN_MIN`. It is
  then marked `inferred`, the group's resolution is shown, and a note says
  none was recorded. Otherwise it is a singleton with status `none`,
  resolution `null`, and the note "no resolution recorded for this
  failure".
  - The max rather than the mean: a cause group is worded differently on
    purpose. Measured here (tuned tfidf parameters), an OOMKilled log
    scored 0.806 against its look-alike but 0.188 as a group mean.
  - Here all three empty logs are out-of-memory logs and are inferred into
    the batch-size group, at 0.482, 0.806 and 0.806.
- **Conflicts:** identical signal text with different resolutions. Here
  that is one byte-identical pair, which keep their own resolutions; both
  carry the note "conflicting resolutions recorded for an identical log".

### Secrets

- **Redacted on load.** Each record is redacted as it is read, before
  anything is indexed, so no stored document, vocabulary entry, response or
  log line can hold a secret. This is tested against every corpus log used
  as a query, every `GET /failures/{id}`, and the index's own
  documents and vocabularies.
- **Queries are redacted too,** before cleaning, and neither queries nor
  results are logged.
- **Validation errors.** Pydantic's default 422 echoes the request input,
  which for `/failures` could be a pasted log with a token. A 422 handler
  scoped to `/failures` returns only `loc`, `msg` and `type`. Other routes
  keep FastAPI's default body.

### Glossary (`retrieval/glossary.py`)

- 26 entries. Drafted in sub-step B from general ML-ops knowledge and the
  error vocabulary in the logs, before the evaluation queries were opened.
  They were then edited by hand.
- 13 phrases that matched visible queries were removed in sub-step D (see
  "Evaluation").
- **Exit code 137 is not proof of OOM.** 137 = 128 + signal 9 (SIGKILL); the
  OOM killer is one cause, preemption, an operator kill or a timeout are
  others. So the entry was split in two:
  - a bare `137` / `SIGKILL` / `signal 9` adds termination terms only
    (`137 sigkill signal 9 killed terminated`);
  - OOM terms (`oomkilled oom 137 memory limit`) need memory evidence in
    the text: OOMKilled, OOM killer, out of memory killer, a memory or
    cgroup limit exceeded.

  In this corpus every real 137 is an OOM, so the split changed no visible
  eval result (the reports are byte-identical); it protects unseen inputs.
- A test checks that the module never refers to the queries file.
- Expansion only appends terms. Multi-word phrases may skip up to two
  harmless bridge words ("disk is completely full"). Single words must
  match exactly.

### Cleaning (`retrieval/clean.py`)

- **Redaction first.** `quality.load_records` redacts the team, log and
  resolution of every record as it is read, so no later step ever holds a
  secret. It covers:
  - Authorization headers;
  - `Bearer <token>`;
  - `KEY=VALUE`, `KEY: VALUE` and JSON pairs whose key contains password,
    passwd, secret, token or api_key/api-key;
  - JWT-shaped strings.

  The whole match is replaced, key included, since `SERVICE_PASSWORD=` alone
  still says what was there. `Bearer` must be followed by a token-shaped
  value, because `AuthError: bearer token expired` is a real error line.
  Redaction over-matches on purpose: `max_tokens=512` is redacted too.
- **Templates.** Lines are lower-cased, then quoted strings become `<str>`,
  paths `<path>`, hex ids `<hex>` (`0x…`, or 6+ hex characters with both a
  digit and a letter, so words like "added" survive) and numbers `<num>`
  (versions included).
- **Learned noise.** A template that appears in more than `NOISE_DF` (0.15)
  of the logs is boilerplate. The list is learned from the corpus, not
  written by hand, and is applied to pasted query logs as well.
  - If every line of a log is noise, the log keeps its non-INFO/DEBUG lines;
    if those are noise too, it keeps the whole log.
  - Risk: a cause common enough to cross 15% would be filtered out. The
    report flags any noise template that looks like an error. Here there
    are none, and the margin is 5 logs.
  - Decision: redacted DEBUG lines stay as signal lines, as specified.
    After redaction, 4 logs from 3 different causes share
    `DEBUG request headers: [REDACTED]` and `DEBUG env [REDACTED]`. These
    lines appear in too few logs to count as noise, so they add a little
    shared signal across unrelated causes. Dropping INFO/DEBUG lines
    whenever another signal line exists was rejected: it would also lose a
    real error logged at INFO level. Its effect is not measured on its
    own: the evaluation's one related ablation turns the whole noise filter
    off (see *Evaluation*).
- **Log or question.** `clean.classify`, with the four tiers described under
  *Pipeline*.
- **Tokens.** Logs and questions use the same rules:
  - lower-case words and numbers (137, 401 are kept);
  - `all-reduce` gives `allreduce`, `all` and `reduce`;
  - light suffix stripping (s, es, ed, ing), only on alphabetic words of
    5+ letters, and never an "s" after an "s" ("loss", "process").

## Database

SQLite via the standard-library `sqlite3` module (no ORM). Schema in
`broker/schema.sql`, connection helpers in `broker/db.py`, schema version in
`PRAGMA user_version` (currently 2; a v1 database is migrated on start, see
*Restart recovery*).

Tables:

- `sim_clock` – the one row holding simulated "now", so time resumes where it stopped after a restart.
- `nodes` – the broker's view of each rented machine: type, owner team, state, and the price snapshotted at launch.
- `jobs` – one row per submitted job and its lifecycle; the queue is just `jobs WHERE status = 'queued'`. `created_seq` (set by a trigger) is the persistent creation order.
- `sim_job_plans` – simulator-only hidden truth (actual duration and outcome); the scheduler never reads it.
- `provider_nodes` – the fake cloud provider's own state, kept apart from `nodes` because the provider is the source of truth for billing.
- `events` – append-only audit log of what happened and when, for debugging and operations.

Rules:

- **Derive, don't duplicate.** Values that can be computed are not stored, e.g. GPUs in use on a node is `SUM(jobs.gpus)` of its running jobs, so it can never drift.
- **Simulated time only.** Timestamps are integer seconds (UTC) taken from `sim_clock`; no code calls `datetime.now()` or `time.time()`.
- **No floats for money.** Prices are integer micro-dollars (1 USD = 1,000,000).
- Any change that touches more than one row runs inside one `transaction()` (`BEGIN IMMEDIATE … COMMIT`, rolled back on error).

### How to run the tests

From the repo root (`pyproject.toml` puts the root on the path):

```bash
pytest -q
```

```bash
ruff check . && ruff format --check .
```

The full-trace smoke test (`tests/test_replay.py::test_full_trace_smoke`,
about 5 s) and the repeated stress rounds in `tests/test_concurrency.py` carry
the marker `slow`; plain `pytest` runs them, `pytest -m "not slow"` skips them.

### Test configuration

Tests never read the `BROKER_*` environment: each builds its `Config`. The
API-contract tests (`tests/test_api.py`) use
`Config(scheduler_enabled=False, reaper_enabled=False)`, so jobs and nodes
stay where the test put them. Every scheduler, reaper and engine behaviour is
tested with both on (`test_scheduler.py`, `test_reaper.py`, `test_engine.py`,
`test_integration.py`, `test_replay_scenario.py`, `test_replay.py` and the
YAML cases). Unit tests turn
the random provider off (`never_ready_prob=0`, `outage_start_prob=0`) and fix
the boot delay, and ask for failures explicitly with `unavailable_windows` /
`never_ready_launches`; the determinism tests turn the randomness on.

### Test reliability

- **No wall clock, no global random.** `tests/test_no_wall_clock.py` scans
  `broker/` (Python AST and SQL) for wall-clock calls (`time.*`,
  `datetime.now/utcnow/today`, `uuid`, `secrets`, `os.urandom`, SQLite `CURRENT_TIMESTAMP` / `'now'`)
  and for any `random.*` use other than a seeded `random.Random(...)`, and runs a full workload with
  every wall-clock read from `broker.*` code made to fail
  (`tests/wall_clock_guard.py`). Every in-process YAML case runs under the
  same guard. Third-party libraries may still read the clock (servers use it
  for timeouts); that is allowed and not checked.
- **No vacuous passes.** Tests that need a scenario first assert it happened
  (e.g. `tests/test_integration.py` checks the workload really had failed
  launches, reaped nodes and planned ends; the crash test checks the crash
  really happened and rolled back a group). The guard and the two YAML cases
  that once passed with no scheduler were checked by breaking the code on
  purpose (clock read, cap of 11, nodes ready 1 s early): each made the
  intended tests fail.
- **Restart and billing.** `tests/test_integration.py` restarts the app at
  three points of a mixed workload and compares nodes, jobs and costs with
  an uninterrupted run, and checks that cost parts add up, that any split
  of a cost window adds up to the micro-dollar, and that billing stops at
  the last termination.
- **Replay determinism.** `tests/test_replay_restart.py` replays a trace with
  launch failures, outages, duplicates and reaping twice, in two processes
  with different `PYTHONHASHSEED`s, and with a restart (new app, provider and
  connection) mid-boot, mid-failing-launch, mid-backoff, with jobs running
  and queued, and before the drain. Every table (jobs incl. requeue counts,
  nodes, provider nodes, events with ids and timestamps, plans, clock) and
  the cost report must match the uninterrupted run exactly; nothing is
  renamed. Inputs and the argument: SPEC_DECISIONS.md #16.

### YAML test cases

Hand-written API cases live in `tests/cases/*.yaml`. `tests/test_yaml_cases.py`
loads every file and runs each case (or each `table` row) as one test through
`tests/case_runner.py`. The runner's fixture rules are in that module's docstring.

```bash
python -m pytest tests/test_yaml_cases.py -v
```

To run the same cases against a running server instead (each case wipes its
tables first; restart cases are skipped):

```bash
BROKER_BASE_URL=http://127.0.0.1:8765 BROKER_DB=./broker.db python -m pytest tests/test_yaml_cases.py -v
```
