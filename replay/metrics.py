"""Metrics of a replay: wait percentiles, cost, nodes, queue, per team and overall.

All inputs are the raw results of run_replay.replay_scenario(). Money stays
in integer micro-dollars; times in integer seconds. Ratios are rounded to 6
decimals so the JSON is byte-identical across runs and machines.
"""

import json
import math
import statistics

PERCENTILE_METHOD = (
    "nearest-rank: sort the n values, take the value at rank ceil(p/100 * n) "
    "(1-based); no interpolation, so every percentile is an observed wait"
)
WITHIN_S = 10 * 60  # "started within 10 minutes", the ML engineers' wish


def percentile(values, p: float) -> int | None:
    """Nearest-rank percentile, by hand (no numpy). None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))  # p=0 -> the minimum
    return ordered[rank - 1]


def peak(intervals) -> int:
    """Most half-open [start, end) intervals open at one instant.

    At equal times an end is applied before a start, so [0, 10) and [10, 20)
    never count as overlapping.
    """
    points = sorted([(s, 1) for s, e in intervals if e > s] +
                    [(e, -1) for s, e in intervals if e > s])  # fmt: skip
    best = open_now = 0
    for _, step in points:  # (t, -1) sorts before (t, 1)
        open_now += step
        best = max(best, open_now)
    return best


def _ratio(part: float, whole: float) -> float | None:
    return round(part / whole, 6) if whole else None


def _group(jobs, nodes, events, burst) -> dict:
    """The metrics of one team, or of everyone (the same rules for both)."""
    waits = [j["started_at"] - j["submitted_at"] for j in jobs]
    start, end = burst
    burst_waits = [j["started_at"] - j["submitted_at"] for j in jobs
                   if start <= j["submitted_at"] < end]  # fmt: skip
    return {
        "jobs": len(jobs),
        "wait_s_p50": percentile(waits, 50),
        "wait_s_p90": percentile(waits, 90),
        "wait_s_p99": percentile(waits, 99),
        "wait_s_max": max(waits, default=None),
        "started_within_10_min": _ratio(sum(w <= WITHIN_S for w in waits), len(waits)),
        "burst_jobs": len(burst_waits),
        "burst_wait_s_p90": percentile(burst_waits, 90),
        "nodes_launched": len(nodes),
        "never_ready_nodes": sum(n["state"] == "failed" for n in nodes),
        "capacity_unavailable": len(events),
        # Waiting = submitted and not yet started; alive = launched, not ended.
        "peak_queue": peak([(j["submitted_at"], j["started_at"]) for j in jobs]),
        "peak_alive_nodes": peak(
            [(n["launched_at"], n["terminated_at"]) for n in nodes]
        ),
    }


def scenario_metrics(result: dict, burst_window: tuple[int, int]) -> dict:
    """{'overall': {...}, 'teams': {team: {...}}} for one replay result."""
    unstarted = [j for j in result["jobs"] if j["started_at"] is None]
    if unstarted:
        # A drained replay starts every job (cancels run actual_minutes first).
        raise ValueError(f"{len(unstarted)} jobs never started; wait time undefined")
    refusals = [
        e for e in result["events"] if e["event_type"] == "capacity_unavailable"
    ]
    refusal_team = [json.loads(e["details"]).get("team") for e in refusals]
    cost = result["cost"]
    teams = {}
    for team in sorted({j["team"] for j in result["jobs"]}):
        m = _group([j for j in result["jobs"] if j["team"] == team],
                   [n for n in result["nodes"] if n["team"] == team],
                   [r for r, t in zip(refusals, refusal_team, strict=True) if t == team],
                   burst_window)  # fmt: skip
        c = cost["teams"].get(
            team, {"gpu_seconds": 0, "busy": 0, "idle": 0, "total": 0}
        )
        m.update(gpu_seconds=c["gpu_seconds"], busy=c["busy"], idle=c["idle"],
                 total=c["total"], idle_share=_ratio(c["idle"], c["total"]))  # fmt: skip
        teams[team] = m
    overall = _group(result["jobs"], result["nodes"], refusals, burst_window)
    team_costs = cost["teams"].values()
    idle = sum(c["idle"] for c in team_costs)
    overall.update(
        gpu_seconds=sum(c["gpu_seconds"] for c in team_costs),
        busy=sum(c["busy"] for c in team_costs),
        idle=idle,
        total=sum(c["total"] for c in team_costs),  # what teams are charged
        provider_overhead=cost["provider_overhead"],  # failed launches
        grand_total=cost["grand_total"],  # total + provider_overhead
        # Idle share of everything spent. Provider overhead (never-ready
        # nodes) is waste too, but is reported on its own, not as idle.
        idle_share=_ratio(idle, cost["grand_total"]),
        duplicates=result["duplicates"],
        drained_at=result["final_now"],
    )
    return {"overall": overall, "teams": teams}


def aggregate(runs: list[dict]):
    """The same nested shape, each number -> {median, min, max} across runs.

    Median is the LOWER median (statistics.median_low): with an even number
    of seeds it is still an observed value, so micro-dollars stay integers.
    None (no data in that run, e.g. no burst jobs) is left out.
    """
    first = runs[0]
    if isinstance(first, dict):
        return {k: aggregate([r[k] for r in runs]) for k in first}
    values = [v for v in runs if v is not None]
    if not values:
        return None
    return {
        "median": statistics.median_low(values),
        "min": min(values),
        "max": max(values),
    }
