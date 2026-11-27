"""Business logic as plain functions that take a db connection.

The HTTP layer (api.py) stays thin and only maps these results and exceptions
to status codes, so the replay script and tests can call the same functions.

Every state change runs inside db.transaction() and writes an `events` row in
that same transaction, so the audit log can never disagree with the data.
Scheduling is NOT done here: api.py runs one engine pass after a submit,
cancel or complete has committed (SPEC_DECISIONS.md #8, #9), so business
logic and scheduling stay in separate transactions and modules.
"""

import hashlib
import sqlite3
import uuid
from datetime import UTC, datetime

from broker import costs, engine
from broker.config import Config
from broker.db import transaction
from broker.engine import TimeWentBackwards
from broker.models import JobSubmit, SimAdvance
from broker.provider import FakeProvider
from broker.scheduler import queue_order
from broker.state import Conflict, NotFound, end_job, get_now, log_event

# NotFound and Conflict live in state.py (the scheduler raises them too);
# api.py reaches them as service.NotFound / service.Conflict.


class InvalidInput(Exception):
    """A value Pydantic can't check alone, e.g. a window that depends on now (HTTP 422)."""


# --- small helpers -----------------------------------------------------------


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def fingerprint(team: str, gpus: int, estimated_minutes: int, priority: str) -> str:
    # Built from values AFTER validation and defaults, so an omitted priority
    # and an explicit "normal" give the same fingerprint.
    raw = f"{team}|{gpus}|{estimated_minutes}|{priority}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def wait_seconds(job: sqlite3.Row, now: int) -> int:
    if job["started_at"] is not None:
        return job["started_at"] - job["submitted_at"]
    if job["status"] == "queued":
        return now - job["submitted_at"]
    # Cancelled before it ever started: it waited until it was cancelled. The
    # brief counts cancelled jobs in wait time, so this must not be null.
    return job["ended_at"] - job["submitted_at"]


def job_to_dict(job: sqlite3.Row, now: int) -> dict:
    out = dict(job)
    out["wait_seconds"] = wait_seconds(job, now)
    return out


def _read_job(conn, job_id: str) -> sqlite3.Row:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise NotFound(f"job {job_id} not found")
    return job


# --- submit ------------------------------------------------------------------


def _find_by_key(conn, team: str, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE team = ? AND idempotency_key = ?", (team, key)
    ).fetchone()


def _existing_or_conflict(existing: sqlite3.Row, fp: str) -> sqlite3.Row:
    if existing["request_fingerprint"] != fp:
        raise Conflict("idempotency key reused with different request")
    return existing


def submit_job(conn, req: JobSubmit) -> tuple[dict, bool]:
    """Create a job, or return the existing one for a repeated idempotency key.

    Returns (job, created). created=False means an earlier identical request
    already made this job (HTTP 200 instead of 201).
    """
    fp = fingerprint(req.team, req.gpus, req.estimated_minutes, req.priority)
    try:
        with transaction(conn):
            now = get_now(conn)
            if req.idempotency_key is not None:
                existing = _find_by_key(conn, req.team, req.idempotency_key)
                if existing is not None:
                    return job_to_dict(_existing_or_conflict(existing, fp), now), False
            job_id = new_id("job")
            conn.execute(
                """INSERT INTO jobs (id, team, idempotency_key, request_fingerprint,
                                     gpus, estimated_minutes, priority, status,
                                     submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
                (
                    job_id,
                    req.team,
                    req.idempotency_key,
                    fp,
                    req.gpus,
                    req.estimated_minutes,
                    req.priority,
                    now,
                ),
            )
            log_event(
                conn,
                now,
                "job",
                job_id,
                "job_submitted",
                team=req.team,
                gpus=req.gpus,
                priority=req.priority,
            )
            return job_to_dict(_read_job(conn, job_id), now), True
    except sqlite3.IntegrityError:
        # Defensive: BEGIN IMMEDIATE already serializes check-then-insert, so
        # two submits with one key cannot normally race. If the UNIQUE
        # constraint fires anyway, the transaction has rolled back; re-read
        # in a new one and apply the same rules. Anything else re-raises.
        if req.idempotency_key is None:
            raise
        with transaction(conn):
            existing = _find_by_key(conn, req.team, req.idempotency_key)
            if existing is None:
                raise
            return job_to_dict(
                _existing_or_conflict(existing, fp), get_now(conn)
            ), False


# --- read --------------------------------------------------------------------


def get_job(conn, job_id: str) -> dict:
    return job_to_dict(_read_job(conn, job_id), get_now(conn))


def list_queue(conn, policy: str) -> list[dict]:
    now = get_now(conn)
    return [
        {**job_to_dict(job, now), "position": i}
        for i, job in enumerate(queue_order(conn, now, policy), start=1)
    ]


# --- lifecycle ---------------------------------------------------------------


def cancel_job(conn, job_id: str) -> dict:
    with transaction(conn):
        now = get_now(conn)
        job = _read_job(conn, job_id)
        if job["status"] == "cancelled":
            return job_to_dict(job, now)  # no-op, not a transition: no event
        end_job(conn, job, "cancelled", "user_cancel", now)
        return job_to_dict(_read_job(conn, job_id), now)


def complete_job(conn, job_id: str, outcome: str) -> dict:
    with transaction(conn):
        now = get_now(conn)
        job = _read_job(conn, job_id)
        end_job(conn, job, outcome, None, now)
        return job_to_dict(_read_job(conn, job_id), now)


# --- simulator only ------------------------------------------------------------


def attach_plan(conn, job_id: str, actual_minutes: int, outcome: str) -> None:
    """Record a replayed job's hidden truth: it ends actual_minutes after it
    starts, with this outcome. Only the engine reads it (sim_job_plans).

    Simulator only, deliberately NOT an HTTP route: a user who could set it
    could make their own job "finish" whenever they liked. No event is
    written either, so the hidden duration never shows up where the broker
    (or anyone reading the audit log) could use it.
    """
    with transaction(conn):
        _read_job(conn, job_id)  # NotFound for an unknown job
        conn.execute(
            "INSERT INTO sim_job_plans (job_id, actual_minutes, outcome) VALUES (?, ?, ?)",
            (job_id, actual_minutes, outcome),
        )


# --- simulated clock ---------------------------------------------------------


def to_iso(ts: int) -> str:
    # Converts a stored simulated timestamp; this never reads the real clock.
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sim_now(conn) -> dict:
    now = get_now(conn)
    return {"now_ts": now, "now_iso": to_iso(now)}


def sim_advance(conn, req: SimAdvance, cfg: Config, provider: FakeProvider) -> dict:
    # "now" is read before advance_to() locks, so two parallel {"minutes": 10}
    # calls add 10 or 20 minutes depending on timing (if both read the same
    # "now", the second is a no-op). Accepted: only the simulator drives the
    # clock, one call at a time. to_ts has no such ambiguity.
    target = req.to_ts if req.to_ts is not None else get_now(conn) + req.minutes * 60
    try:
        engine.advance_to(conn, target, cfg, provider)
    except TimeWentBackwards as e:
        raise Conflict(f"time never goes backwards: {e}") from e
    return sim_now(conn)


# --- nodes and cost ----------------------------------------------------------


def _running_jobs(conn, node_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, gpus FROM jobs WHERE node_id = ? AND status = 'running'
           ORDER BY id""",
        (node_id,),
    ).fetchall()


def list_nodes(conn, include_terminated: bool = False) -> list[dict]:
    """Nodes with GPU use derived from their running jobs.

    By default ended nodes (terminated or failed) are hidden: they hold no
    jobs and are only interesting for cost, which /reports/cost covers.
    """
    now = get_now(conn)
    sql = "SELECT * FROM nodes"
    if not include_terminated:
        sql += " WHERE state NOT IN ('terminated', 'failed')"
    nodes = conn.execute(sql + " ORDER BY launched_at, id").fetchall()

    out = []
    for node in nodes:
        running = _running_jobs(conn, node["id"])
        used = sum(job["gpus"] for job in running)
        cost = costs.node_cost(conn, node, from_ts=0, to_ts=now, now=now)
        out.append(
            {
                "id": node["id"],
                "type": node["node_type"],
                "team": node["team"],
                "state": node["state"],
                "gpus_total": node["gpus_total"],
                "gpus_used": used,
                "gpus_free": node["gpus_total"] - used,
                "running_job_ids": [job["id"] for job in running],
                "launched_at": node["launched_at"],
                "ready_at": node["ready_at"],
                "idle_since": node["idle_since"],
                "terminated_at": node["terminated_at"],
                "cost_so_far": costs.micros_to_dollars(cost.total),
            }
        )
    return out


def cost_report(conn, from_ts: int = 0, to_ts: int | None = None) -> dict:
    """Cost per team for [from_ts, to_ts); to_ts defaults to now."""
    now = get_now(conn)
    if to_ts is None:
        to_ts = now
    if from_ts >= to_ts:
        raise InvalidInput(f"from_ts ({from_ts}) must be before to_ts ({to_ts})")

    report = costs.cost_by_team(conn, from_ts, to_ts, now)
    dollars = costs.micros_to_dollars
    # Each figure is rounded on its own from exact micros, so displayed parts
    # can differ from the displayed total by 0.01 (e.g. 1.13 + 3.38 vs 4.50).
    teams = [
        {
            "team": name,
            # GPU-hours are not money; a float is fine. 3 decimals so a
            # 1-GPU, 1-minute job shows 0.017 rather than 0.02.
            "gpu_hours": round(t.gpu_seconds / costs.SECONDS_PER_HOUR, 3),
            "busy_cost": dollars(t.busy),
            "idle_cost": dollars(t.idle),
            "total_cost": dollars(t.total),
        }
        for name, t in sorted(report.teams.items())
    ]
    return {
        "from_ts": from_ts,
        "to_ts": to_ts,
        "teams": teams,
        "provider_overhead": dollars(report.provider_overhead),
        "grand_total": dollars(report.grand_total),
    }
