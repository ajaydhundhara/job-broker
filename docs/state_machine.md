# State machines

Three entities have a state: jobs (`jobs.status`), the broker's nodes
(`nodes.state`) and the fake provider's nodes (`provider_nodes.state`). This
lists every transition the code performs, who performs it, and where each
invariant is enforced. No other transition exists.

## Jobs (`jobs.status`)

Allowed changes are `state.ALLOWED_TRANSITIONS`; `state.check_transition`
raises `Conflict` (HTTP 409) for anything else, before anything is written.

| from | to | trigger | component | terminal? |
|---|---|---|---|---|
| — | queued | `POST /jobs` | `service.submit_job` | no |
| queued | running | placement on a ready node of the team with free GPUs | `scheduler` → `state.start_job` | no |
| queued | cancelled | `POST /jobs/{id}/cancel` | `service.cancel_job` | yes |
| running | succeeded / failed | `POST /jobs/{id}/complete` (internal) | `service.complete_job` | yes |
| running | succeeded / failed / cancelled | replay: planned end from `sim_job_plans` | `engine._end_planned_jobs` | yes |
| running | cancelled | `POST /jobs/{id}/cancel` | `service.cancel_job` | yes |
| running | failed (`node_lost`) | its node is gone at the provider | `engine.reconcile` | yes |

Repeats: cancelling a cancelled job is a no-op (200, no event); completing a
finished job, cancelling a succeeded or failed one, and completing a queued
one are 409 with no event.

## Broker nodes (`nodes.state`)

Map: `state.NODE_TRANSITIONS`. Writers guard in their `UPDATE ... WHERE
state = ...` or, for `terminate_node`, by checking first.

| from | to | `end_reason` | trigger | component | terminal? |
|---|---|---|---|---|---|
| — | launching | | a queued job needs capacity | `scheduler._record_launch` (same transaction as the provider launch) | no |
| launching | ready | | provider boot time reached | `engine._apply_provider_transitions` | no |
| launching | failed | `launch_failed` | provider gave up (never ready, 15 min) | `engine._apply_provider_transitions` | yes |
| launching | ready | | reconcile: provider already ready | `engine.reconcile` | no |
| ready | terminated | `idle_timeout` | idle past the timeout, not wanted by its team | `reaper` → `state.terminate_node` | yes |
| ready | terminated | `evicted` | node cap reached, another team waits | `scheduler._evict_for_room` → `state.terminate_node` | yes |
| launching / ready | terminated | `provider_reconciled` | reconcile: provider already terminated | `engine.reconcile` | yes |
| launching / ready | failed | `launch_failed` | reconcile: provider already failed | `engine.reconcile` | yes |

`state.terminate_node` accepts only a `ready` node with no running job;
otherwise `Conflict`, nothing written (an ended node keeps its billing end;
a busy node is never ended under its jobs).

## Provider nodes (`provider_nodes.state`)

Map: `state.PROVIDER_TRANSITIONS`.

| from | to | trigger | component | terminal? |
|---|---|---|---|---|
| — | pending | `launch_node` | `FakeProvider.launch_node` | no |
| pending | ready / failed | planned ready / failure time | `FakeProvider.apply_transition` | failed: yes |
| pending / ready | terminated | the broker ends it; an orphan; reconcile finds the broker already ended it | `FakeProvider.terminate_node` | yes |

Broker and provider agree when their states pair as in
`state.BROKER_STATE_FOR_PROVIDER`: pending–launching, ready–ready,
terminated–terminated, failed–failed. `engine.reconcile` (every timestamp
group, and once at startup) repairs every disagreement it can:

| broker | provider | repair |
|---|---|---|
| (missing) | pending / ready | provider terminated (`orphan_terminated`) |
| launching / ready | terminated / failed | broker ended; its running jobs fail with `node_lost` (`node_reconciled`) |
| terminated / failed | pending / ready | provider terminated (`provider_bill_stopped`) |
| launching | ready | broker ready at the provider's ready time (`node_ready`, `reconciled`) |

## Not in this system (N/A)

- running → queued (requeue): no preemption; `jobs.requeue_count` is always 0.
- Provider reclaim of interruptible nodes: launches are never interruptible
  (`provider_nodes.reclaim_at` and `end_reason = 'provider_reclaimed'` exist
  only for migrated v1 data).
- `end_reason = 'manual'`: no route terminates a node by hand.

## Invariants and where they are enforced

| invariant | enforced by |
|---|---|
| terminal jobs are never rescheduled | `check_transition` (no edge out of a terminal status); the scheduler reads only `status = 'queued'` |
| a queued job holds no node; a running job has one and a start time | schema CHECKs on `jobs` |
| a finished job uses no GPUs | GPUs used are derived from running jobs only (`state.used_gpus`). Its `node_id` is kept as history: cost per team attributes node time through it |
| a running job is on a ready node of its own team, within `gpus_total` | `state.start_job` (rejects); `replay.check_invariants` (detects) |
| assignments point to existing jobs and nodes | foreign key `jobs.node_id → nodes.id` (`PRAGMA foreign_keys = ON`) |
| live nodes ≤ `max_nodes` | scheduler; `replay.check_invariants` |
| broker and provider node states agree | `engine.reconcile` (repairs); `replay.check_invariants` (detects) |
| an ended node has an end time | schema CHECK on `nodes` |
| duplicate operations write no duplicate events | the transition checks above; `UNIQUE (team, idempotency_key)` for submits |

`replay.check_invariants` runs after every advance with `--check-invariants`
and in the tests (`tests/test_state_machine.py`, `tests/test_replay_scenario.py`).
