"""The simulation engine: the only code that moves simulated time forward.

Time does not tick. advance_to() jumps from one event time to the next (a
node becoming ready or failing, a planned job end, a reap deadline, a launch
retry) and processes each instant as one TIMESTAMP GROUP, in its own
transaction (SPEC_DECISIONS.md #9), in a fixed order (#5):

    1. provider transitions   (nodes become ready / fail)
    2. reconciliation         (broker vs provider records)
    3. planned job ends       (trace replay: the only reader of sim_job_plans)
    4. idle reaper
    5. scheduler
    repeated until nothing changes, then COMMIT.

A crash keeps every committed group and rolls back the rest; calling
advance_to() again resumes from the stored clock. Nothing is done twice,
because every step only acts on state that is still due.

advance_to(now) - "zero minutes" - moves nothing but runs one group at now:
the API calls it after each submit / cancel / complete, so a job that fits
starts at once.
"""

import sqlite3

from broker import reaper, scheduler
from broker.config import Config
from broker.db import transaction
from broker.provider import FakeProvider
from broker.state import end_job, get_now, log_event

MAX_ROUNDS_PER_GROUP = 1000  # a group normally settles in 2-3 rounds
MAX_GROUPS_PER_ADVANCE = 1_000_000


class TimeWentBackwards(Exception):
    """Raised when asked to move the clock to a time before "now"."""


class SimulationStuck(RuntimeError):
    """A bound was hit: a bug kept creating work. Raised instead of hanging."""


# --- moving time -------------------------------------------------------------------


def advance_to(conn: sqlite3.Connection, target_ts: int, cfg: Config,
               provider: FakeProvider | None = None) -> int:  # fmt: skip
    """Move simulated time to target_ts, processing every event on the way."""
    provider = provider or FakeProvider(cfg)
    with transaction(conn):
        start = get_now(conn)
        if target_ts < start:
            raise TimeWentBackwards(f"target {target_ts} is before now {start}")
        _run_group(conn, start, cfg, provider)  # anything already due at now

    for _ in range(MAX_GROUPS_PER_ADVANCE):
        with transaction(conn):
            now = get_now(conn)
            t = next_event_time(conn, now, cfg, provider)
            if t is None or t > target_ts:
                break
            _set_clock(conn, t)
            _run_group(conn, t, cfg, provider)
    else:
        raise SimulationStuck(
            f"more than {MAX_GROUPS_PER_ADVANCE} groups before {target_ts}"
        )

    with transaction(conn):
        now = get_now(conn)
        if target_ts > now:  # another advance may already be past us
            _set_clock(conn, target_ts)
            _run_group(conn, target_ts, cfg, provider)
        if target_ts > start:
            log_event(conn, target_ts, "system", None, "clock_advanced",
                      from_ts=start, to_ts=target_ts)  # fmt: skip
    return max(target_ts, start)


def run_pass(conn, cfg: Config, provider: FakeProvider | None = None) -> None:
    """One group at the current time, without moving the clock."""
    with transaction(conn):
        _run_group(conn, get_now(conn), cfg, provider or FakeProvider(cfg))


def next_event_time(conn, now: int, cfg: Config, provider: FakeProvider) -> int | None:
    """The earliest time after now at which something is planned."""
    candidates = [
        provider.next_transition_time(conn, now),
        conn.execute(
            """SELECT MIN(j.started_at + p.actual_minutes * 60)
               FROM jobs j JOIN sim_job_plans p ON p.job_id = j.id
               WHERE j.status = 'running' AND j.started_at + p.actual_minutes * 60 > ?""",
            (now,),
        ).fetchone()[0],
    ]
    if cfg.reaper_enabled:
        candidates.append(reaper.next_reap_time(conn, now, cfg))
    if cfg.scheduler_enabled and _has_queued_jobs(conn):
        candidates.append(scheduler.next_retry_time(conn, now, cfg))
    future = [t for t in candidates if t is not None]
    return min(future) if future else None


# --- one timestamp group -----------------------------------------------------------


def _run_group(conn, now: int, cfg: Config, provider: FakeProvider) -> None:
    """Apply everything due at or before now. Caller holds the transaction."""
    for _ in range(MAX_ROUNDS_PER_GROUP):
        changed = _apply_provider_transitions(conn, now, provider)
        changed |= reconcile(conn, now, provider)
        changed |= _end_planned_jobs(conn, now)
        if cfg.reaper_enabled:
            changed |= reaper.reap(conn, now, cfg, provider)
        if cfg.scheduler_enabled:
            changed |= scheduler.schedule(conn, now, cfg, provider)
        if not changed:
            return
    raise SimulationStuck(
        f"group at t={now} did not settle in {MAX_ROUNDS_PER_GROUP} rounds"
    )


def _apply_provider_transitions(conn, now: int, provider: FakeProvider) -> bool:
    """Mirror the provider's ready/failed transitions into `nodes`."""
    rows = provider.due_transitions(conn, now)
    for row in rows:
        outcome = provider.apply_transition(conn, row)
        ts = row["due_at"]
        if outcome == "ready":
            # Billing started at launch; the idle clock starts at ready.
            conn.execute(
                """UPDATE nodes SET state = 'ready', ready_at = ?, idle_since = ?
                   WHERE id = ? AND state = 'launching'""",
                (ts, ts, row["id"]),
            )
            log_event(
                conn, ts, "node", row["id"], "node_ready", node_type=row["node_type"]
            )
        else:
            # Billed until the provider gives up; costs.py puts a node that
            # never ran a job into provider_overhead.
            conn.execute(
                """UPDATE nodes SET state = 'failed', terminated_at = ?,
                       end_reason = 'launch_failed'
                   WHERE id = ? AND state = 'launching'""",
                (ts, row["id"]),
            )
            log_event(conn, ts, "node", row["id"], "launch_failed",
                      node_type=row["node_type"], reason="never became ready")  # fmt: skip
    return bool(rows)


def _end_planned_jobs(conn, now: int) -> bool:
    """End running jobs whose planned duration (hidden trace truth) is up."""
    due = conn.execute(
        """SELECT j.*, p.outcome AS planned_outcome,
                  j.started_at + p.actual_minutes * 60 AS planned_end
           FROM jobs j JOIN sim_job_plans p ON p.job_id = j.id
           WHERE j.status = 'running' AND j.started_at + p.actual_minutes * 60 <= ?
           ORDER BY planned_end, j.created_seq""",
        (now,),
    ).fetchall()
    for job in due:
        outcome = job["planned_outcome"]
        reason = "replay_cancel" if outcome == "cancelled" else "planned"
        end_job(conn, job, outcome, reason, job["planned_end"])
    return bool(due)


# --- reconciliation (decision 11) --------------------------------------------------


def reconcile(conn, now: int, provider: FakeProvider) -> bool:
    """Make the broker's records agree with the provider's. Caller holds the tx.

    Normally both are updated in one transaction and never disagree; this
    catches the cases where they do (an edit by hand, a bug, a crash between
    a real provider call and our commit).
    """
    changed = False
    orphans = conn.execute(
        """SELECT p.id FROM provider_nodes p LEFT JOIN nodes n ON n.id = p.id
           WHERE n.id IS NULL AND p.state IN ('pending', 'ready') ORDER BY p.id"""
    ).fetchall()
    for row in orphans:  # billed at the provider, unknown to us: stop the bill
        provider.terminate_node(conn, row["id"], now)
        log_event(conn, now, "node", row["id"], "orphan_terminated")
        changed = True

    stale = conn.execute(
        """SELECT n.id, p.state AS provider_state, p.terminated_at AS provider_end
           FROM nodes n JOIN provider_nodes p ON p.id = n.id
           WHERE n.state IN ('launching', 'ready')
             AND p.state IN ('terminated', 'failed') ORDER BY n.id"""
    ).fetchall()
    for row in stale:
        _mark_ended(conn, row, now)
        changed = True

    # We ended it, the provider still bills it: end it there too.
    still_billed = conn.execute(
        """SELECT n.id, n.state, n.terminated_at FROM nodes n
           JOIN provider_nodes p ON p.id = n.id
           WHERE n.state IN ('terminated', 'failed')
             AND p.state IN ('pending', 'ready') ORDER BY n.id"""
    ).fetchall()
    for row in still_billed:
        provider.terminate_node(conn, row["id"], now)
        log_event(conn, now, "node", row["id"], "provider_bill_stopped",
                  broker_state=row["state"], broker_ended_at=row["terminated_at"])  # fmt: skip
        changed = True

    # The provider made it ready but we still say launching: catch up, at the
    # provider's ready time (its billing and idle clock started then).
    behind = conn.execute(
        """SELECT n.id, n.node_type, p.ready_at_planned FROM nodes n
           JOIN provider_nodes p ON p.id = n.id
           WHERE n.state = 'launching' AND p.state = 'ready' ORDER BY n.id"""
    ).fetchall()
    for row in behind:
        ts = min(row["ready_at_planned"], now)
        conn.execute(
            """UPDATE nodes SET state = 'ready', ready_at = ?, idle_since = ?
               WHERE id = ? AND state = 'launching'""",
            (ts, ts, row["id"]),
        )
        log_event(conn, ts, "node", row["id"], "node_ready",
                  node_type=row["node_type"], reconciled=True)  # fmt: skip
        changed = True
    return changed


def _mark_ended(conn, row: sqlite3.Row, now: int) -> None:
    failed = row["provider_state"] == "failed"
    state, reason = (
        ("failed", "launch_failed") if failed else ("terminated", "provider_reconciled")
    )
    ended = row["provider_end"] if row["provider_end"] is not None else now
    conn.execute(
        "UPDATE nodes SET state = ?, terminated_at = ?, end_reason = ? WHERE id = ?",
        (state, ended, reason, row["id"]),
    )
    # A job can't keep running on a node that is gone.
    lost = conn.execute(
        "SELECT * FROM jobs WHERE node_id = ? AND status = 'running' ORDER BY created_seq",
        (row["id"],),
    ).fetchall()
    for job in lost:
        end_job(conn, job, "failed", "node_lost", max(ended, job["started_at"]))
    log_event(conn, now, "node", row["id"], "node_reconciled", state=state,
              end_reason=reason, jobs_lost=[j["id"] for j in lost])  # fmt: skip


def reconcile_now(conn, cfg: Config, provider: FakeProvider | None = None) -> None:
    """Run reconciliation once, in its own transaction (app startup)."""
    with transaction(conn):
        reconcile(conn, get_now(conn), provider or FakeProvider(cfg))


# --- helpers -----------------------------------------------------------------------


def _set_clock(conn, ts: int) -> None:
    conn.execute("UPDATE sim_clock SET now_ts = ? WHERE id = 1", (ts,))


def _has_queued_jobs(conn) -> bool:
    row = conn.execute("SELECT 1 FROM jobs WHERE status = 'queued' LIMIT 1").fetchone()
    return row is not None
