"""Shared state changes for jobs and nodes, used by service and scheduler.

Every function here runs inside the caller's transaction and writes an
`events` row for the change, so the audit log never disagrees with the data.
They live in their own module so service.py (which calls the scheduler) and
scheduler.py (which starts jobs) don't import each other.
"""

import json
import sqlite3

from broker.provider import UnknownNode

# The only status changes allowed. Every real change is checked against this.
# (running -> queued, for requeue, is not needed yet: no preemption, and
# interruptible nodes are not used.)
ALLOWED_TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"succeeded", "failed", "cancelled"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}


class NotFound(Exception):
    """The requested job does not exist (HTTP 404)."""


class Conflict(Exception):
    """The request is valid but clashes with current state (HTTP 409)."""


def check_transition(old: str, new: str) -> None:
    if new not in ALLOWED_TRANSITIONS[old]:
        raise Conflict(f"cannot go from {old} to {new}")


def get_now(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT now_ts FROM sim_clock WHERE id = 1").fetchone()[0]


def log_event(conn, ts, entity_type, entity_id, event_type, **details) -> None:
    conn.execute(
        """INSERT INTO events (ts, entity_type, entity_id, event_type, details)
           VALUES (?, ?, ?, ?, ?)""",
        (ts, entity_type, entity_id, event_type,
         json.dumps(details) if details else None),
    )  # fmt: skip


def running_job_count(conn, node_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE node_id = ? AND status = 'running'", (node_id,)
    ).fetchone()[0]


def start_job(conn, job: sqlite3.Row, node_id: str, now: int) -> None:
    """queued -> running on node_id. The caller has checked the GPUs fit."""
    check_transition(job["status"], "running")
    conn.execute(
        """UPDATE jobs SET status = 'running', node_id = ?, started_at = ?
           WHERE id = ?""",
        (node_id, now, job["id"]),
    )
    # The node is busy now; the reaper's idle countdown stops.
    conn.execute("UPDATE nodes SET idle_since = NULL WHERE id = ?", (node_id,))
    log_event(conn, now, "job", job["id"], "job_started",
              node_id=node_id, waited_seconds=now - job["submitted_at"])  # fmt: skip


def end_job(conn, job: sqlite3.Row, status: str, reason: str | None, now: int) -> None:
    """Move a job to a final status at time `now`."""
    check_transition(job["status"], status)
    conn.execute(
        "UPDATE jobs SET status = ?, ended_at = ?, end_reason = ? WHERE id = ?",
        (status, now, reason, job["id"]),
    )
    node_now_idle = False
    if job["status"] == "running" and running_job_count(conn, job["node_id"]) == 0:
        # "GPUs used" is derived, so the only node field to keep in step is
        # idle_since: the reaper's clock starts when the last job leaves.
        conn.execute(
            "UPDATE nodes SET idle_since = ? WHERE id = ?", (now, job["node_id"])
        )
        node_now_idle = True
    log_event(conn, now, "job", job["id"], f"job_{status}",
              from_status=job["status"], node_id=job["node_id"],
              node_now_idle=node_now_idle, reason=reason)  # fmt: skip


def terminate_node(conn, provider, node_id: str, now: int, end_reason: str,
                   event_type: str, **details) -> None:  # fmt: skip
    """Give a node back to the provider and record it. Billing ends at now.

    Provider first, then our record, both in the caller's transaction. A node
    the provider doesn't know (e.g. inserted directly by a test) is still
    ended on our side, and the event says so.
    """
    try:
        provider.terminate_node(conn, node_id, now)
        known = True
    except UnknownNode:
        known = False
    conn.execute(
        """UPDATE nodes SET state = 'terminated', terminated_at = ?, end_reason = ?
           WHERE id = ?""",
        (now, end_reason, node_id),
    )
    log_event(conn, now, "node", node_id, event_type,
              end_reason=end_reason, known_to_provider=known, **details)  # fmt: skip
