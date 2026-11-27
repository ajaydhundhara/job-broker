"""The ONE place that computes cost. /nodes and /reports/cost both call it,
so the two can never disagree.

All amounts are INTEGER micro-dollars (1 USD = 1_000_000). Dollars appear only
in micros_to_dollars(), used when building API responses.

Windows are half-open: [from_ts, to_ts) includes from_ts, excludes to_ts. Two
adjacent windows therefore never count the same second twice.
"""

import sqlite3
from dataclasses import dataclass, field

MIN_BILLED_SECONDS = 600  # provider bills at least 10 minutes per node
SECONDS_PER_HOUR = 3600


@dataclass(frozen=True)
class NodeCost:
    billed_seconds: int  # overlap with the window, plus any minimum top-up
    gpu_seconds: int  # sum of gpus x run time of its jobs, inside the window
    total: int  # micros
    busy: int  # micros: the share of the node used by jobs
    idle: int  # micros: total - busy (empty GPUs, boot, idle, minimum top-up)


@dataclass
class TeamCost:
    gpu_seconds: int = 0
    busy: int = 0
    idle: int = 0
    total: int = 0


@dataclass
class CostReport:
    teams: dict[str, TeamCost] = field(default_factory=dict)
    provider_overhead: int = 0  # failed launches: billed, but no team's fault
    grand_total: int = 0


def overlap(a_start: int, a_end: int, w_start: int, w_end: int) -> int:
    """Seconds shared by the half-open ranges [a_start, a_end) and [w_start, w_end)."""
    return max(0, min(a_end, w_end) - max(a_start, w_start))


def _node_end(node: sqlite3.Row, now: int) -> int:
    # A node still alive is billed up to "now".
    return node["terminated_at"] if node["terminated_at"] is not None else now


def node_billed_seconds(node: sqlite3.Row, from_ts: int, to_ts: int, now: int) -> int:
    """Seconds of this node billed inside [from_ts, to_ts).

    Billing runs per second from launch (boot time included) to termination.
    The 10-minute minimum applies only once a node has terminated: the missing
    seconds are charged at its last billed second, terminated_at - 1. So the
    top-up counts when from_ts <= terminated_at - 1 < to_ts, which is
    from_ts < terminated_at <= to_ts. That puts it in exactly one window of any
    split of time, and a node reaped exactly at "now" shows it at once.
    """
    seconds = overlap(node["launched_at"], _node_end(node, now), from_ts, to_ts)
    terminated_at = node["terminated_at"]
    if terminated_at is not None:
        lifetime = terminated_at - node["launched_at"]
        if lifetime < MIN_BILLED_SECONDS and from_ts < terminated_at <= to_ts:
            seconds += MIN_BILLED_SECONDS - lifetime
    return seconds


def node_gpu_seconds(
    conn: sqlite3.Connection, node: sqlite3.Row, from_ts: int, to_ts: int, now: int
) -> int:
    """GPU-seconds of all jobs on this node inside [from_ts, to_ts).

    Each job's run [started_at, ended_at or now) is first clipped to the node's
    life [launched_at, end). The schema can't stop a job being marked running
    on a node that already ended; without the clip that job would count time
    the node was never billed for, and idle cost would go negative.
    """
    node_start, node_end = node["launched_at"], _node_end(node, now)
    jobs = conn.execute(
        """SELECT gpus, started_at, ended_at FROM jobs
           WHERE node_id = ? AND started_at IS NOT NULL""",
        (node["id"],),
    ).fetchall()
    total = 0
    for job in jobs:
        job_end = job["ended_at"] if job["ended_at"] is not None else now
        start = max(job["started_at"], node_start)
        end = min(job_end, node_end)
        total += job["gpus"] * overlap(start, end, from_ts, to_ts)
    return total


def node_cost(
    conn: sqlite3.Connection, node: sqlite3.Row, from_ts: int, to_ts: int, now: int
) -> NodeCost:
    """Cost of one node in [from_ts, to_ts), split into busy and idle."""
    price = node["price_per_hour_micros"]
    billed = node_billed_seconds(node, from_ts, to_ts, now)
    gpu_seconds = node_gpu_seconds(conn, node, from_ts, to_ts, now)

    # Rounding rule: a window's cost is the node's running total at the
    # window's end minus its running total at the start, each floored once
    # (multiply first, divide last). Flooring each window on its own would
    # make [a, b) + [b, c) differ from [a, c) by up to 1 micro-dollar; this
    # way any split adds up exactly, and a node's whole bill is one floor.
    # The running total "before" counts billing in [launched_at, from_ts);
    # seconds are additive across windows, so "before + window" is the
    # running total at to_ts.
    before_billed = node_billed_seconds(node, node["launched_at"], from_ts, now)
    before_gpu = node_gpu_seconds(conn, node, node["launched_at"], from_ts, now)

    def bill(seconds: int) -> int:
        return seconds * price // SECONDS_PER_HOUR

    def busy_bill(gpu_secs: int) -> int:
        # A job pays for its share of the node: its GPUs / the node's GPUs.
        return gpu_secs * price // (node["gpus_total"] * SECONDS_PER_HOUR)

    total = bill(before_billed + billed) - bill(before_billed)
    busy = busy_bill(before_gpu + gpu_seconds) - busy_bill(before_gpu)
    # idle is the remainder, so busy + idle == total exactly. Over any window
    # that starts at launch it is >= 0; in a window that starts later it can
    # be -1 micro-dollar when busy crosses a micro-dollar boundary that total
    # doesn't (shown as $0.00). Accepted to keep total exact and additive.
    return NodeCost(billed, gpu_seconds, total, busy, total - busy)


def cost_by_team(
    conn: sqlite3.Connection, from_ts: int, to_ts: int, now: int
) -> CostReport:
    """Cost per team in [from_ts, to_ts).

    The WHOLE cost of a node goes to the team that owns it (only its jobs may
    run there), including idle time: the owner's demand caused the launch.
    Failed launches go to provider_overhead instead: no team got anything.
    """
    report = CostReport()
    nodes = conn.execute(
        "SELECT * FROM nodes WHERE launched_at < ? ORDER BY team, id", (to_ts,)
    ).fetchall()
    for node in nodes:
        cost = node_cost(conn, node, from_ts, to_ts, now)
        if cost.billed_seconds == 0:
            continue  # ended before the window started
        report.grand_total += cost.total
        if node["state"] == "failed":
            report.provider_overhead += cost.total
            continue
        team = report.teams.setdefault(node["team"], TeamCost())
        team.gpu_seconds += cost.gpu_seconds
        team.busy += cost.busy
        team.idle += cost.idle
        team.total += cost.total
    return report


def micros_to_dollars(micros: int) -> float:
    """Micro-dollars -> dollars rounded to cents, half-up, for API responses only.

    Rounding is done in integers: (micros + 5_000) // 10_000 is whole cents,
    rounded half-up. Python's round(1.125, 2) would give 1.12 (it rounds half
    to even), and the spec wants 1.13. The float is made only at the end.
    """
    cents = (micros + 5_000) // 10_000
    return cents / 100
