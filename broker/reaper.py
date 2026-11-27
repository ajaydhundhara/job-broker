"""The idle reaper: give back nodes that have sat empty too long.

SPEC_DECISIONS.md #1 and #5. A READY node with no running jobs is terminated
(end_reason 'idle_timeout', event 'node_reaped') at

    eligible_at = max(launched_at + min_node_lifetime_s,
                      idle_since + idle_timeout_s + 1)

i.e. once it has been idle MORE than the timeout and has lived the minimum
lifetime (the first 10 minutes are billed anyway, so keeping it that long is
free). The engine jumps to that exact second, so a node is reaped on time
even inside one long advance.

Never reaped: a node that is not ready (launching ones are about to be used,
failed and terminated ones are gone), a node with a running job, and a node a
queued job of its own team fits on (the scheduler runs right after the
reaper in the same transaction and will place that job).
"""

import sqlite3

from broker.config import Config
from broker.state import terminate_node

# COALESCE: a ready, empty node with no idle_since (only possible from data
# inserted by hand) counts as idle since it became ready.
_IDLE_READY_NODES = """
    SELECT n.*, COALESCE(n.idle_since, n.ready_at) AS idle_from
    FROM nodes n
    WHERE n.state = 'ready'
      AND NOT EXISTS (SELECT 1 FROM jobs j
                      WHERE j.node_id = n.id AND j.status = 'running')
    ORDER BY n.id
"""


def eligible_at(node: sqlite3.Row, cfg: Config) -> int:
    return max(
        node["launched_at"] + cfg.min_node_lifetime_s,
        node["idle_from"] + cfg.idle_timeout_s + 1,
    )


def _wanted_by_queued_job(conn, node: sqlite3.Row) -> bool:
    # The node is empty, so all its GPUs are free.
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE status = 'queued' AND team = ? AND gpus <= ? LIMIT 1",
        (node["team"], node["gpus_total"]),
    ).fetchone()
    return row is not None


def reap(conn, now: int, cfg: Config, provider) -> bool:
    """Terminate every idle node past its eligible time. True if any was."""
    reaped = False
    for node in conn.execute(_IDLE_READY_NODES).fetchall():
        if eligible_at(node, cfg) > now or _wanted_by_queued_job(conn, node):
            # No event for a protected node: this check runs every pass.
            continue
        terminate_node(conn, provider, node["id"], now, "idle_timeout", "node_reaped",
                       idle_seconds=now - node["idle_from"],
                       idle_minutes=cfg.idle_minutes)  # fmt: skip
        reaped = True
    return reaped


def next_reap_time(conn, now: int, cfg: Config) -> int | None:
    """The earliest reap time after now (engine wake-up), or None."""
    future = [
        t for n in conn.execute(_IDLE_READY_NODES) if (t := eligible_at(n, cfg)) > now
    ]
    return min(future) if future else None
