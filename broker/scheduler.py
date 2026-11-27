"""The scheduler: put queued jobs on nodes, and launch nodes when none fit.

schedule() is one pass, run inside the caller's transaction (one timestamp
group, see engine.py). The rules, with their reasons, are SPEC_DECISIONS.md
#2, #3, #6, #12 and #13:

  1. place   : queued job -> READY node of its own team, best fit; backfill.
  2. booting : queued jobs that fit on the team's LAUNCHING nodes need no
               new node (no launch per job while earlier launches boot).
  3. launch  : the cheapest node type that fits; pack only when the 10-node
               cap makes it necessary; fall back to a larger type (at most
               2x the price) when the preferred one is refused or backing
               off; respect the fair-share cap; evict another team's idle
               node only to make room at the cap.

This module never reads sim_job_plans (the hidden actual durations); a test
checks that.
"""

import sqlite3

from broker.config import GPUS, NODE_TYPES_BY_SIZE, PRICE_MICROS, Config
from broker.provider import CapacityUnavailable, FakeProvider
from broker.state import log_event, start_job, terminate_node

_PRIORITY_RANK = "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"
DAY_SECONDS = 24 * 3600


# --- queue order (decision 6) -----------------------------------------------------


def queue_order(conn, now: int, policy: str) -> list[sqlite3.Row]:
    """Queued jobs in the order the scheduler tries them (and GET /queue shows).

    fifo: priority, submitted_at, created_seq.
    fair: priority, then the team's GPU-seconds in the last 24 h (least
          first), then submitted_at, created_seq. A team that has used little
          goes ahead of one that has used a lot, within the same priority.
    Priority never preempts a running job; it only orders the queue.
    """
    if policy == "fifo":
        return conn.execute(
            f"""SELECT * FROM jobs WHERE status = 'queued'
                ORDER BY {_PRIORITY_RANK}, submitted_at, created_seq"""
        ).fetchall()
    return conn.execute(
        f"""WITH usage AS (
                SELECT team,
                       SUM(gpus * MAX(0, MIN(COALESCE(ended_at, :now), :now)
                                         - MAX(started_at, :now - {DAY_SECONDS}))) AS gpu_s
                FROM jobs WHERE started_at IS NOT NULL GROUP BY team)
            SELECT j.* FROM jobs j LEFT JOIN usage u ON u.team = j.team
            WHERE j.status = 'queued'
            ORDER BY {_PRIORITY_RANK.replace("priority", "j.priority")},
                     COALESCE(u.gpu_s, 0), j.submitted_at, j.created_seq""",
        {"now": now},
    ).fetchall()


# --- one pass ---------------------------------------------------------------------


def schedule(conn, now: int, cfg: Config, provider: FakeProvider) -> bool:
    """One scheduling pass. Returns True if it changed anything."""
    changed = _place_queued_jobs(conn, now, cfg)
    changed |= _launch_for_waiting_jobs(conn, now, cfg, provider)
    return changed


# --- 1. placement (decision 13.1) ------------------------------------------------


def _best_ready_node(conn, job: sqlite3.Row) -> sqlite3.Row | None:
    # Free GPUs are derived from running jobs, never stored, so they can't drift.
    return conn.execute(
        """SELECT n.id, n.team, n.gpus_total - COALESCE(SUM(j.gpus), 0) AS free
           FROM nodes n
           LEFT JOIN jobs j ON j.node_id = n.id AND j.status = 'running'
           WHERE n.state = 'ready' AND n.team = ?
           GROUP BY n.id
           HAVING free >= ?
           ORDER BY free, n.id
           LIMIT 1""",
        (job["team"], job["gpus"]),
    ).fetchone()


def _place_queued_jobs(conn, now: int, cfg: Config) -> bool:
    placed = False
    for job in queue_order(conn, now, cfg.policy):
        node = _best_ready_node(conn, job)
        if node is None:
            continue  # backfill: later, smaller jobs may still fit
        # Security rule: a team's jobs only ever run on that team's nodes.
        assert node["team"] == job["team"], (node["id"], job["id"])
        start_job(conn, job, node["id"], now)
        placed = True
    return placed


# --- 2. booting capacity (decision 13.2) ----------------------------------------


def _launching_capacity(conn) -> dict[str, list]:
    """node id -> [team, free GPUs] for nodes still booting (they hold no jobs)."""
    rows = conn.execute(
        "SELECT id, team, gpus_total FROM nodes WHERE state = 'launching' ORDER BY id"
    ).fetchall()
    return {r["id"]: [r["team"], r["gpus_total"]] for r in rows}


def _reserve(spare: dict[str, list], job: sqlite3.Row) -> bool:
    """Count this job against a booting node of its team (best fit)."""
    fits = [(free, node_id) for node_id, (team, free) in spare.items()
            if team == job["team"] and free >= job["gpus"]]  # fmt: skip
    if not fits:
        return False
    _free, node_id = min(fits)
    spare[node_id][1] -= job["gpus"]
    return True


# --- 3. launching (decisions 2, 3, 12, 13) ---------------------------------------


def _launch_for_waiting_jobs(conn, now, cfg: Config, provider) -> bool:
    spare = _launching_capacity(conn)
    unmatched = [
        j for j in queue_order(conn, now, cfg.policy) if not _reserve(spare, j)
    ]
    changed = False
    for i, job in enumerate(unmatched):
        if _reserve(spare, job):  # fits on a node launched earlier in this pass
            continue
        if _over_fair_cap(conn, job["team"], cfg):
            continue  # waits; and it may not evict others (decision 12)
        types = _viable_types(conn, now, cfg, job, unmatched[i:])
        if not types:
            continue  # every acceptable type is backing off
        if _live_node_count(conn) >= cfg.max_nodes:
            if not _evict_for_room(conn, job["team"], now, cfg, provider):
                # Not `break`: evictable nodes depend on the team asking (a
                # team's own idle nodes are never evicted for it), so a later
                # job of another team may still find room.
                continue
            changed = True
        node_id, node_type = _try_launch(conn, now, provider, job, types)
        changed = True  # a launch or a capacity_unavailable event
        if node_id is not None:
            spare[node_id] = [job["team"], GPUS[node_type] - job["gpus"]]
    return changed


def _viable_types(conn, now, cfg: Config, job, still_waiting) -> list[str]:
    """Node types to try for this job, cheapest acceptable first."""
    preferred = _smallest_fitting(job["gpus"])
    free_slots = cfg.max_nodes - _live_node_count(conn)
    if len(still_waiting) > free_slots:
        # Too few node slots for one node per job: pack this team's waiting
        # demand onto one larger node (decision 13.4). Otherwise one cheap node
        # per job: five gpu-s ($5.00/h) beat one gpu-l ($8.50/h).
        demand = sum(j["gpus"] for j in still_waiting if j["team"] == job["team"])
        preferred = _smallest_fitting(min(max(demand, job["gpus"]), GPUS["gpu-l"]))
    limit = PRICE_MICROS[preferred] * cfg.max_fallback_price_ratio
    candidates = [t for t in NODE_TYPES_BY_SIZE
                  if GPUS[t] >= GPUS[preferred] and PRICE_MICROS[t] <= limit]  # fmt: skip
    return [t for t in candidates if not backing_off(conn, t, now, cfg)]


def _smallest_fitting(gpus: int) -> str:
    return next(t for t in NODE_TYPES_BY_SIZE if GPUS[t] >= gpus)


def _try_launch(conn, now, provider, job, types) -> tuple[str | None, str | None]:
    """Launch the first type the provider accepts; record each refusal."""
    for node_type in types:
        try:
            node_id = provider.launch_node(conn, node_type, now)
        except CapacityUnavailable as e:
            k = _consecutive(conn, "capacity_unavailable", "node_launched", node_type)[
                0
            ]
            log_event(conn, now, "provider", None, "capacity_unavailable",
                      node_type=node_type, k=k + 1, team=job["team"], reason=str(e))  # fmt: skip
            continue
        _record_launch(conn, node_id, node_type, job, now)
        return node_id, node_type
    return None, None


def _record_launch(conn, node_id: str, node_type: str, job, now: int) -> None:
    # The broker's row goes in the same transaction as the provider's, so a
    # crash can't leave a billed node the broker doesn't know about. The price
    # is snapshotted now, so later price changes never rewrite history.
    conn.execute(
        """INSERT INTO nodes (id, node_type, gpus_total, interruptible,
               price_per_hour_micros, team, state, launched_at)
           VALUES (?, ?, ?, 0, ?, ?, 'launching', ?)""",
        (
            node_id,
            node_type,
            GPUS[node_type],
            PRICE_MICROS[node_type],
            job["team"],
            now,
        ),
    )
    log_event(conn, now, "node", node_id, "node_launched",
              node_type=node_type, team=job["team"], for_job=job["id"])  # fmt: skip


# --- the caps (decision 12) --------------------------------------------------------


def _live_node_count(conn, team: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM nodes WHERE state IN ('launching', 'ready')"
    if team is None:
        return conn.execute(sql).fetchone()[0]
    return conn.execute(sql + " AND team = ?", (team,)).fetchone()[0]


def _over_fair_cap(conn, team: str, cfg: Config) -> bool:
    """'fair' only: while other teams wait, a team gets ceil(max/active) nodes."""
    if cfg.policy != "fair":
        return False
    others_waiting = conn.execute(
        "SELECT 1 FROM jobs WHERE status = 'queued' AND team != ? LIMIT 1", (team,)
    ).fetchone()
    if others_waiting is None:
        return False
    active = conn.execute(
        "SELECT COUNT(DISTINCT team) FROM jobs WHERE status IN ('queued', 'running')"
    ).fetchone()[0]
    return _live_node_count(conn, team) >= cfg.fair_node_cap(active)


def _evict_for_room(conn, team: str, now: int, cfg: Config, provider) -> bool:
    """At the cap, terminate another team's longest-idle node. False if none.

    Never when cfg.evict_at_cap is off. Only ready, empty nodes that have
    lived min_node_lifetime_s (already paid for), and never one a queued job
    of its own team could use.
    """
    if not cfg.evict_at_cap:
        return False
    node = conn.execute(
        """SELECT n.id FROM nodes n
           WHERE n.state = 'ready' AND n.team != :team
             AND n.launched_at + :min_life <= :now
             AND NOT EXISTS (SELECT 1 FROM jobs j
                             WHERE j.node_id = n.id AND j.status = 'running')
             AND NOT EXISTS (SELECT 1 FROM jobs q
                             WHERE q.status = 'queued' AND q.team = n.team
                               AND q.gpus <= n.gpus_total)
           ORDER BY COALESCE(n.idle_since, n.ready_at), n.id
           LIMIT 1""",
        {"team": team, "min_life": cfg.min_node_lifetime_s, "now": now},
    ).fetchone()
    if node is None:
        return False
    terminate_node(conn, provider, node["id"], now, "evicted", "node_evicted",
                   for_team=team, reason="live-node cap reached")  # fmt: skip
    return True


# --- retry policies (decision 2), derived from events ---------------------------------


def _consecutive(
    conn, failure: str, reset: str, node_type: str
) -> tuple[int, int | None]:
    """(count, latest ts) of `failure` events for node_type since its last `reset`."""
    last_reset = conn.execute(
        """SELECT COALESCE(MAX(id), 0) FROM events
           WHERE event_type = ? AND json_extract(details, '$.node_type') = ?""",
        (reset, node_type),
    ).fetchone()[0]
    count, last_ts = conn.execute(
        """SELECT COUNT(*), MAX(ts) FROM events
           WHERE event_type = ? AND json_extract(details, '$.node_type') = ? AND id > ?""",
        (failure, node_type, last_reset),
    ).fetchone()
    return count, last_ts


def retry_at(conn, node_type: str, cfg: Config) -> int | None:
    """Earliest time this node type may be launched again, or None if now.

    CapacityUnavailable: 60, 120, 240, ... capped at 900 s after the k-th
    consecutive refusal (reset by a successful launch of that type).
    Never ready: 0, 60, 120, 240, 480 s after the k-th consecutive failure
    (reset when a node of that type becomes ready).
    """
    times = []
    k, last = _consecutive(conn, "capacity_unavailable", "node_launched", node_type)
    if k:
        delay = cfg.capacity_backoff_base_s * 2 ** (k - 1)
        times.append(last + min(delay, cfg.capacity_backoff_max_s))
    k, last = _consecutive(conn, "launch_failed", "node_ready", node_type)
    if k:
        delay = 0 if k == 1 else cfg.never_ready_backoff_base_s * 2 ** (k - 2)
        times.append(last + min(delay, cfg.never_ready_backoff_max_s))
    return max(times) if times else None


def backing_off(conn, node_type: str, now: int, cfg: Config) -> bool:
    t = retry_at(conn, node_type, cfg)
    return t is not None and now < t


def next_retry_time(conn, now: int, cfg: Config) -> int | None:
    """When a backing-off type becomes launchable again (engine wake-up)."""
    future = [
        t for nt in GPUS if (t := retry_at(conn, nt, cfg)) is not None and t > now
    ]
    return min(future) if future else None
