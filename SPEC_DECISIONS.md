# Specification decisions

The one consistent specification for the scheduler, provider, reaper and
event engine. Where it differs from earlier behaviour, the tests were changed
to match it; `tests/TEST_MIGRATION_MAP.md` lists every such change.

Labels used below:

- **[brief]** a requirement of the original assignment (`Take_Home_Exercise.docx`);
- **[policy]** a design decision of ours, not required by the brief;
- **[test]** a setting that exists only so tests can be exact.

All times are integer seconds of simulated time. Intervals are half-open
`[from, to)` unless stated.

---

## 1. Minimum node lifetime and the idle reaper

Two separate rules, both 600 seconds, that must not be confused:

| Rule | Kind | What it does |
|---|---|---|
| Minimum **billing**: a node is billed at least 600 s | [brief] | `billable_seconds = max(lifetime, 600)`, applied once the node has ended (`broker/costs.py`) |
| Minimum **physical lifetime**: a ready node is kept at least 600 s | [policy] | the reaper never terminates a node before `launched_at + 600` |

Why the policy: the first 10 minutes are paid for anyway, so keeping the node
until then costs nothing and may serve another job.

A ready node with no running jobs becomes eligible for reaping at

    eligible_at = max(launched_at + 600, idle_since + idle_timeout_seconds + 1)

("idle for strictly more than the timeout"). Example: launched 0, idle since
0, timeout 300 s → idle-eligible at 301, lifetime-eligible at 600 → reaped at
**600**, not before.

- The lifetime rule applies to nodes that became ready. A node that never
  became ready is `failed` (Decision 11) and is never reaped.
- Billing is independent: a node reaped at 600 is billed 600 s; one reaped at
  601 is billed 601 s.
- Settings: `Config.idle_minutes` (default 10, env `BROKER_IDLE_MINUTES`),
  `Config.min_node_lifetime_s` (default 600).
- A queued job can protect a node from the reaper (Decision 5).

## 2. Launch failure and retry policies

Two failure kinds with two separate retry policies, both counted **per node
type** and both derived from the `events` table (so they survive a restart):

**A. Never ready** [brief: ~3% of launches, reported failed after 15 min].
The node is `failed` at `launched_at + 900`. Retry delay after the k-th
consecutive never-ready failure of that node type (consecutive = since a node
of that type last became ready):

| k | 1 | 2 | 3 | 4 | 5+ |
|---|---|---|---|---|---|
| delay (s) | 0 | 60 | 120 | 240 | 480 (cap) |

So the first failure is retried at once (launch at 900 → fails 900 → relaunch at 900).
Settings: `never_ready_backoff_base_s=60`, `never_ready_backoff_max_s=480`.

**B. CapacityUnavailable** [brief: comes in bursts]. The provider refuses the
launch; nothing is created or billed. Retry delay after the k-th consecutive
refusal for that node type (consecutive = since that type's last successful
launch): `min(60 · 2^(k-1), 900)` s → 60, 120, 240, 480, 900, 900, ...
Settings: `capacity_backoff_base_s=60`, `capacity_backoff_max_s=900`.

No uncontrolled loops: a type that is backing off is not tried again in the
same pass or before its delay; the engine wakes exactly at the delay's end.
Jobs stay `queued` throughout; only scheduling, cancel or completion changes
them.

## 3. CapacityUnavailable fallback and event history

[policy] When the preferred node type is refused or backing off, the scheduler
tries the next larger type in the **same pass**, if the cost policy allows it
(Decision 13: at most `max_fallback_price_ratio` = 2.0 × the preferred hourly
price). gpu-m → gpu-l is allowed (8.50 / 4.50 = 1.89); gpu-s → gpu-m is not
(4.50×), so a 1-GPU job waits for gpu-s instead of paying 4.5× more.

Example: a 2-GPU job, gpu-m unavailable, gpu-l available. Exactly three
events, in this order:

1. `job_submitted`
2. `capacity_unavailable` (entity `provider`, details `{node_type: "gpu-m", k: 1}`)
3. `node_launched` (details `{node_type: "gpu-l", ...}`)

The job stays `queued` while the gpu-l node is launching. The refused request
is represented only by event 2; there is no separate "launch requested"
event. Event names are lower_snake_case throughout (`events.event_type`).

## 4. Deterministic provider outages

Two sources of CapacityUnavailable, both stateless (no in-memory state):

- [test] **Explicit windows**, per node type, half-open:
  `Config.unavailable_windows = (("gpu-m", 100, 300), ...)`, i.e. unavailable
  when `from_ts <= now < to_ts`. At 99 gpu-m launches, at 100 and 299 it is
  refused, at 300 it launches again. YAML form:
  `provider: {unavailable_windows: {gpu-m: [{from_ts: 100, to_ts: 300}]}}`.
- [brief] **Random bursts** for realism: time is cut into
  `outage_bucket_s` (600 s) buckets; a burst starts in bucket s with
  probability `outage_start_prob` (0.02) and lasts `randint(2, 6)` buckets,
  both drawn from `rng("outage", f"{node_type}:{s}")`. Per node type, so a
  fallback type can help.

[test] Deterministic never-ready launches: `Config.never_ready_launches = (2,)`
makes the 2nd launch (by launch number) never become ready, regardless of the
random draw. `never_ready_prob` and the boot delay (`boot_min_s`,
`boot_max_s`) can also be fixed.

## 5. Reaper before scheduler, with protection

Order inside each timestamp group (Decision 9):

1. due provider transitions (node ready / node failed);
2. reconciliation with the provider (Decision 11);
3. planned job ends (trace replay only);
4. idle reaper;
5. scheduler;
6. repeat 1-5 until nothing changes, then commit.

The reaper never terminates a node that the scheduler would use: before
reaping a ready, empty node it checks for a **queued job of the same team
that fits** (`job.gpus <= node.gpus_total`). Placement on a ready node is
never limited by fairness (the fair cap limits only new launches), so any such
job is eligible and the scheduler places it in step 5 of the same group.

"Pending allocation": placement is immediate (queued → running in one
transaction), so there is no reserved-but-not-started state on a ready node.
A queued fitting job is the only form of pending allocation, and it protects
the node. Reservations on *launching* nodes (Decision 13) are not reaped
because launching nodes are never reaped.

## 6. Deterministic queue ordering

Ties are broken by a persistent creation sequence, not the job id (job ids
were random when this was decided; since #16 they are derived from this
same sequence, but the order is still defined by `created_seq`):

- schema v2 adds `jobs.created_seq` (UNIQUE); a trigger assigns
  `MAX(created_seq) + 1` on insert, under the write lock, so it is gapless,
  unique and survives restarts. Existing rows are numbered by `rowid`
  (arrival order) in the migration.
- `'fifo'`: priority (high > normal > low), submitted_at, created_seq.
- `'fair'` (default, env `BROKER_POLICY`): priority, the team's GPU-seconds
  in the last 24 h (ascending), submitted_at, created_seq. Usage counts each
  job's `[started_at, ended_at or now)` clipped to `[now - 86400, now)`.
- Priority only orders the queue; a high-priority job never preempts a
  running one [policy: no checkpointing guarantee, wasted spend].

`GET /queue` shows the same order the scheduler uses.

## 7. Test-only switches

`Config.scheduler_enabled` and `Config.reaper_enabled`, both default `True`.
They are constructor arguments of `Config` (passed to `create_app`), not
environment variables and not HTTP operations. The API-contract tests turn
both off, so jobs stay where the test put them; every behaviour that needs the
scheduler or reaper is tested with them on (`test_scheduler.py`,
`test_reaper.py`, `test_engine.py`, the YAML scheduler/reaper cases).

## 8. Zero-minute advance

`POST /sim/advance {"minutes": 0}` (or `to_ts` = now) returns 200 and:

- does not move the clock and writes no `clock_advanced` event;
- processes anything already due at now (once: state changes are idempotent,
  e.g. a node already `ready` is not made ready again);
- runs a reaper + scheduler pass, so an eligible queued job can start.

With nothing to do it writes no events at all. Negative minutes stay 422,
a `to_ts` in the past stays 409.

## 9. One transaction per timestamp group

`advance_to(target)` repeats: find the next event time `t <= target`; in one
transaction set the clock to `t` and run the group (Decision 5); commit. At
the end, a last group at `target` and one `clock_advanced` event (from, to).

- Ties at one timestamp are processed in a fixed order: provider transitions
  by (time, node id); job ends by (planned end, created_seq); queue by
  Decision 6; nodes by id.
- A crash leaves every committed group intact and rolls back the rest; the
  clock is at the last committed `t`. Running `advance_to(target)` again
  resumes from there. Nothing is duplicated, because every step acts only on
  state that is still due (a started job is no longer queued, a ready node is
  no longer pending, billing is derived from node timestamps).
- Inside one group the rounds are bounded (1000); the whole advance is bounded
  (1,000,000 groups). A handler that keeps creating work raises instead of
  looping forever.
- After a crash mid-advance there is no `clock_advanced` event for the
  partial move; the clock row itself is correct.

## 10. Seeded random generators only

No global `random.random()` / `choice()` / `randint()`. The provider receives
an injected generator factory, `rng(purpose, key) -> random.Random`; the
default is `random.Random(f"{seed}:{purpose}:{key}")`.

Why a keyed factory rather than one `random.Random(seed)` stream: a single
stream's next number depends on how many draws came before it, which lives in
memory. After a restart the stream would start over and the simulation would
diverge from an uninterrupted run. A key per decision (launch number, outage
bucket) gives the same draw however the run is split. String seeds are hashed
with SHA-512 by `random`, so they don't depend on `PYTHONHASHSEED`.

The static test allows seeded `random.Random(...)` and rejects module-level
random calls. A replay with the same seed twice gives identical results.

## 11. Termination and failure reasons

`nodes.end_reason` (schema v2):

| Situation | state | end_reason |
|---|---|---|
| idle reaper terminates the node | terminated | `idle_timeout` |
| operator terminates it by hand | terminated | `manual` |
| interruptible node reclaimed (not used, see README) | terminated | `provider_reclaimed` |
| provider reports it terminated, found by reconciliation | terminated | `provider_reconciled` |
| node never became ready | **failed** | `launch_failed` |
| [policy] evicted at the 10-node cap to make room | terminated | `evicted` |

`evicted` is not in the given list; we added it because eviction is neither
an idle timeout nor a manual action. Migration from v1 maps
`idle_reaped → idle_timeout` (v1 used it for evictions too; they can't be told
apart afterwards) and `interrupted → provider_reclaimed`.

Reconciliation (each group and at startup):

- provider node `pending`/`ready` with no broker row → terminated at the
  provider, event `orphan_terminated`;
- broker node `launching`/`ready` whose provider node is `terminated` →
  `terminated`, `provider_reconciled`, `terminated_at` from the provider;
  provider `failed` → `failed`, `launch_failed`. Event `node_reconciled`.
- A broker node with no provider row (inserted directly by tests) is left
  alone.

## 12. Fair-share cap and eviction

[policy] With policy `'fair'`, while another team has queued jobs, a team may
not launch a new node if it already holds at least
`ceil(max_nodes / number of teams with queued or running jobs)` live nodes.
It never loses nodes it has; only new launches are limited.

A team at its cap **does not evict** another team's idle node: its job stays
queued and the other team's node stays. Eviction at the 10-node cap is only
done for a team under its fair cap, and only of another team's ready, empty,
unprotected node that has lived at least 600 s (longest idle first).
`Config(evict_at_cap=False)` turns eviction off (kept from the earlier
design, so existing behaviour stays available).

If no node can be evicted for one team's job, the scheduler goes on to the
next queued job rather than stopping the pass: evictable nodes depend on the
team asking (a team's own idle nodes are never evicted for it).

## 13. Cost-aware placement and packing

In order:

1. Place on a ready node of the **same team** with enough free GPUs; best fit
   (fewest free GPUs left, then lowest node id). Never on another team's node
   (asserted). A job runs on exactly one node. Backfill: a later small job may
   start while an earlier big one waits.
2. Count booting capacity: queued jobs that fit on the team's launching nodes
   (best fit, queue order) need no new node.
3. For the rest, launch the **cheapest type that fits the job**
   (1 → gpu-s $1.00, 2 or 4 → gpu-m $4.50, 8 → gpu-l $8.50).
4. **Pack only when the node cap makes it necessary**: if the jobs still
   needing a node outnumber the free node slots, launch the smallest type that
   holds the team's remaining demand (up to 8 GPUs). Five 1-GPU jobs with
   plenty of slots → five gpu-s ($5.00/h), not one gpu-l ($8.50/h); with 2
   free slots → one gpu-l.
5. Fallback to a larger type when the chosen type is refused or backing off,
   up to 2× its price (Decision 3).

Estimated minutes are not used for placement (no rule needs them); actual
minutes (`sim_job_plans`) are never read by the scheduler, which a test
enforces. Never more than `max_nodes` (10) [brief] live (launching + ready)
nodes.

## 14. Sequential node ids

`node-00001`, `node-00002`, ... Number n = 1 + the number of provider nodes,
read and inserted in the same `BEGIN IMMEDIATE` transaction, so concurrent
launches can't get the same id (tested with parallel connections) and the
sequence continues after a restart. The text primary key is unchanged. Job ids
are sequential too since #16. The YAML matcher `ANY_NODE_ID` is `^node-\d{5}$`.

## 15. Clock advances are serialized

`service.sim_advance` holds `ADVANCE_LOCK` (a process-wide `threading.Lock`)
while it reads now, computes the target and runs the whole `advance_to()`.
Before, `{"minutes": n}` read now outside any transaction, so two parallel
calls could both target now + n and lose n minutes.

- Relative advances are additive: k parallel `+n` move the clock by the sum.
- Absolute `to_ts` takes the same lock (target and 409 rule unchanged), so a
  relative target can never be overtaken while it is computed.
- The per-group transactions inside `advance_to()` are unchanged; submits,
  cancels and completes don't take the lock and still run between groups.
- Lock order is always lock, then `BEGIN IMMEDIATE`, so no deadlock.
- One process only (the documented deployment). A cross-process file lock
  was considered and not taken: POSIX-only and an extra file, for a
  deployment we don't support. `threading.Lock` is not FIFO, so which
  waiting caller goes next is not defined; the totals are.
- Tested with forced interleavings (`tests/test_clock_advance.py`).

## 16. Sequential job ids; a replay is a function of its inputs

Job ids were `job-` + 12 hex from `uuid4()`, the only randomness not drawn
from the seed. Two replays of the same trace, or a replay restarted halfway,
then differed in every job id and in every event that names one, although
nothing else differed (checked by renaming ids in arrival order).

Now `service.next_job_id` gives `job-` + the next `created_seq` as 12
zero-padded hex digits (`job-000000000001`, ...). It is read inside the
submit's `BEGIN IMMEDIATE`, under the write lock, as node ids are (#14), and
the `jobs_created_seq` trigger gives the row that same number. The format is
unchanged (`^job-[0-9a-f]{12}$`); no schema change. Jobs migrated from v1
keep their random ids, and new ids continue after the migrated sequence. A
repeated idempotency key reuses its job and takes no number.

Replay inputs, all explicit: the trace rows (`trace.load_trace`, sorted by
time and line), the `Config` (seed, policy, idle minutes, provider
parameters), and a new database whose clock starts at the first row. Time
is only `sim_clock`; provider draws are keyed by seed, purpose and node id
or outage bucket; the provider's state, the retry backoffs and the plans
are all in the database; every query that orders has a total `ORDER BY`.
Nothing depends on the process, so `PYTHONHASHSEED` doesn't matter (tested
with two processes).

Trade-off: ids are guessable and show how many jobs exist. The broker has
no auth (README "No auth"), so this exposes nothing new; ids are
also not unique across two databases, which nothing relies on.

Tested by `tests/test_replay_restart.py`: the same scenario twice, in two
processes, and restarted mid-boot, mid-failing-launch, mid-backoff, with
jobs running and queued, and before the drain, compared on every table and
the cost report; `tests/test_no_wall_clock.py` now also rejects `uuid`,
`secrets` and `os.urandom` in `broker/`.

## 17. Inference uses failure evidence and never guesses between groups

An empty resolution used to join the group of its most similar recorded log
whenever that hybrid score was at least `ASSIGN_MIN` (0.3), and the group's
fix was shown as `inferred`. Two problems (final validation): the score
included each recorded log's resolution text, so a fix's wording could
make a log look like a group; and a log that matched several groups got the
best one's fix silently (F910's runner-up was 0.098 behind).

Now:

- Inference scores use a separate signal-lines-only matrix (BM25 + the
  embedding; tfidf is refitted on the signal lines). Search keeps its
  documents (signal + resolution): removing the resolution there failed the
  CV floors.
- `Index.inference[id]` holds every recorded group's score, best first;
  `Index.candidates(id)` those at or above `ASSIGN_MIN`.
- One candidate: `inferred`, as before. Two or more: ambiguous, status
  `none`, `resolution: null`, and a note listing the candidates. None: a
  singleton, as before.
- An ambiguous log still joins its best group, so group membership and
  group documents, and therefore every search result, are unchanged.

No new number: the rule reuses `ASSIGN_MIN`. Rejected alternatives:

- a top-two margin: no labelled data to choose it (3 empty logs);
- an ambiguous log as a singleton: CV rejection fell to 0.750 (the
  training folds' TAU shifted and admitted a public negative);
- a new status value: it would change the API's `resolution_status`
  contract; the note carries the detail instead.

Effect here: F911 and F912 stay inferred (0.911, 0.910; next group 0.292);
F910 becomes ambiguous (0.491, with three more groups between 0.317 and
0.366), so its fix is withheld although a reader would call it OOM. A log
that shares words with only one wrong group is still inferred
(`tests/test_final_validation_retrieval.py` case 4, strict xfail).

The evaluator builds its index with default `Params()` and the API with
`retrieval/params.json`, so inference can differ between the two; with
membership unchanged this affects only which fix is displayed, not any
retrieval metric.

Tested by `tests/test_final_validation_retrieval.py` (§4) and
`tests/test_retrieval_search.py`.

## 18. "Error response from <component>:" is a log record

A pasted one-line docker error (`Error response from daemon: pull access
denied …`) has no timestamp, level or `SomeError:` prefix, so it was
classified as a question and glossary-expanded. `clean._RECORD_START` now
also accepts `Error response from <component>:` in any case, optionally
after a `tool: ` prefix. The component and its colon are required, so
"Error when pulling the image" and "error response times are high" stay
questions, and a line that starts with a question word is unaffected.
Tested in `tests/test_final_validation_retrieval.py` (§6).
