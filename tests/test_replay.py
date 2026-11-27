"""Replay metrics, the policy comparison and its reports (replay/).

The successors of the earlier broker/replay.py tests are named in
tests/TEST_MIGRATION_MAP.md ("Post-baseline tests replaced").
"""

import json
import time
from pathlib import Path

import pytest

from broker import trace
from broker.config import Config
from replay import metrics
from replay.run_replay import replay_scenario, run_all, scenarios

TRACE = Path(__file__).resolve().parent.parent / "01a_trace.csv"
HEADER = "submitted_at,team,gpus,estimated_minutes,actual_minutes,priority,outcome\n"
START = trace.parse_ts("2025-03-03T00:00:00Z")


# --- nearest-rank percentile -----------------------------------------------------


@pytest.mark.parametrize("values, p, expected", [
    (list(range(1, 11)), 90, 9),  # rank ceil(0.9 * 10) = 9
    (list(range(1, 11)), 50, 5),
    (list(range(1, 11)), 99, 10),  # rank ceil(9.9) = 10
    (list(range(1, 11)), 100, 10),
    ([], 50, None), ([5], 99, 5), ([1, 2, 3, 4], 50, 2), ([1, 2, 3, 4], 90, 4),
    (list(range(1, 101)), 99, 99), ([30, 10, 20], 50, 20),  # sorted first
])  # fmt: skip
def test_nearest_rank_percentile(values, p, expected):
    assert metrics.percentile(values, p) == expected


def test_peak_counts_half_open_intervals():
    # [0, 10) and [10, 20) never overlap; [5, 15) overlaps both.
    assert metrics.peak([(0, 10), (10, 20)]) == 1
    assert metrics.peak([(0, 10), (10, 20), (5, 15)]) == 2
    assert metrics.peak([]) == 0
    assert metrics.peak([(3, 3)]) == 0  # an empty interval is never "in"


# --- metrics of one scenario, on a hand-made result ------------------------------


def fake_result():
    m = 60
    job = lambda team, sub, start: {"team": team, "gpus": 1, "submitted_at": sub,
                                    "started_at": start, "ended_at": start + 10 * m}  # fmt: skip
    return {
        "jobs": [job("a", 0, 5 * m), job("a", 0, 30 * m), job("b", 100 * m, 100 * m)],
        "nodes": [
            {"team": "a", "state": "terminated", "launched_at": 0, "terminated_at": 50 * m},
            {"team": "a", "state": "failed", "launched_at": 0, "terminated_at": 15 * m},
            {"team": "b", "state": "terminated", "launched_at": 90 * m, "terminated_at": 120 * m},
        ],
        "events": [{"event_type": "capacity_unavailable", "details": '{"team": "a"}'}],
        "duplicates": 1,
        "final_now": 120 * m,
        "cost": {
            "teams": {"a": {"gpu_seconds": 1200, "busy": 300, "idle": 700, "total": 1000},
                      "b": {"gpu_seconds": 600, "busy": 400, "idle": 100, "total": 500}},
            "provider_overhead": 250, "grand_total": 1750},
    }  # fmt: skip


def test_scenario_metrics_per_team_and_overall():
    burst = (0, 60 * 60)  # team a's jobs only
    out = metrics.scenario_metrics(fake_result(), burst)
    a, b, total = out["teams"]["a"], out["teams"]["b"], out["overall"]
    assert (a["jobs"], a["wait_s_p50"], a["wait_s_max"]) == (2, 300, 1800)
    assert (b["jobs"], b["wait_s_p90"]) == (1, 0)
    assert a["started_within_10_min"] == 0.5 and b["started_within_10_min"] == 1.0
    assert (a["nodes_launched"], a["never_ready_nodes"], a["capacity_unavailable"]) == (
        2,
        1,
        1,
    )
    assert a["idle_share"] == 0.7
    assert (a["burst_jobs"], a["burst_wait_s_p90"]) == (2, 1800)
    assert (b["burst_jobs"], b["burst_wait_s_p90"]) == (0, None)
    assert total["jobs"] == 3 and total["wait_s_p90"] == 1800
    assert (total["grand_total"], total["provider_overhead"]) == (1750, 250)
    assert total["idle_share"] == round(800 / 1750, 6)  # team idle / grand total
    assert total["peak_queue"] == 2  # both of a's jobs wait from 0 to 5 min
    assert total["peak_alive_nodes"] == 2
    assert total["duplicates"] == 1


def test_aggregate_gives_median_and_range_across_seeds():
    runs = [
        {"x": 3, "t": {"y": None}},
        {"x": 1, "t": {"y": 4}},
        {"x": 2, "t": {"y": 6}},
    ]
    assert metrics.aggregate(runs) == {
        "x": {"median": 2, "min": 1, "max": 3},
        # None (no data) is left out. Two values left: the LOWER median, an
        # observed value, so micro-dollars stay integers.
        "t": {"y": {"median": 4, "min": 4, "max": 6}},
    }


# --- scenarios -----------------------------------------------------------------------


def test_default_scenarios_are_unique_and_ordered():
    assert scenarios(["fair", "fifo"], 10, [2, 10, 30]) == [
        ("fair", 10), ("fifo", 10), ("fair", 2), ("fair", 30)]  # fmt: skip


# --- reports: reproducible, numbers only -------------------------------------------


def small_trace(tmp_path) -> Path:
    path = tmp_path / "small.csv"
    rows = [f"{trace.to_iso(START + i * 120)},{('vision', 'nlp')[i % 2]},"
            f"{(1, 2, 4)[i % 3]},30,{10 + i},normal,succeeded" for i in range(8)]  # fmt: skip
    path.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def run_small(tmp_path, out, seeds=(1, 2)):
    window = (START, START + 3600)
    return run_all(small_trace(tmp_path), policies=["fair", "fifo"], idle_minutes=10,
                   sweep=[2], seeds=list(seeds), burst_window=window,
                   out_dir=tmp_path / out, check_invariants=True)  # fmt: skip


def test_the_same_seeds_give_byte_identical_reports_and_other_seeds_differ(tmp_path):
    run_small(tmp_path, "a")
    run_small(tmp_path, "b")
    for name in ("replay_results.json", "replay_results.md", "data_quality.md"):
        assert (tmp_path / "a" / name).read_bytes() == (
            tmp_path / "b" / name
        ).read_bytes()
    runs = json.loads((tmp_path / "a" / "replay_results.json").read_text())[
        "scenarios"
    ][0]["runs"]
    assert runs["1"] != runs["2"]  # boot times depend on the seed


def test_the_json_holds_no_runtime_or_run_timestamp(tmp_path):
    run_small(tmp_path, "a", seeds=(1,))
    data = json.loads((tmp_path / "a" / "replay_results.json").read_text())

    def keys(x):
        if isinstance(x, dict):
            for k, v in x.items():
                yield k
                yield from keys(v)
        elif isinstance(x, list):
            for v in x:
                yield from keys(v)

    # No field records how long the run took or when it ran.
    bad = [
        k
        for k in keys(data)
        if any(w in k for w in ("runtime", "elapsed", "generated"))
    ]
    assert bad == []
    assert [s["name"] for s in data["scenarios"]] == ["fair@10", "fifo@10", "fair@2"]
    money = data["scenarios"][0]["runs"]["1"]["overall"]["grand_total"]
    assert isinstance(money, int)  # micro-dollars, never floats
    assert "nearest-rank" in data["method"]["percentile"]


def test_the_markdown_has_every_section_and_leaves_the_analysis_to_the_reader(tmp_path):
    run_small(tmp_path, "a", seeds=(1,))
    md = (tmp_path / "a" / "replay_results.md").read_text()
    for heading in ("## 1. Data quality", "## 2. Per team", "## 3. Fair vs fifo",
                    "## 4. Idle timeout sweep", "## Analysis"):  # fmt: skip
        assert heading in md, heading
    assert "(data_quality.md)" in md
    assert "nearest-rank" in md
    analysis = md.split("## Analysis", 1)[1]
    bullets = [x for x in analysis.splitlines() if x.startswith("- ")]
    assert bullets and all("TODO" in x for x in bullets)  # placeholders only


# --- the full trace ----------------------------------------------------------------------


@pytest.mark.slow
def test_full_trace_smoke(tmp_path):
    rows, report = trace.load_trace(TRACE)
    started = time.perf_counter()
    result = replay_scenario(
        rows, Config(seed=42), tmp_path / "full.db", check_invariants=True
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 60, elapsed
    assert {j["status"] for j in result["jobs"]} <= {"succeeded", "failed", "cancelled"}
    # Every row is accounted for: a job, a duplicate of one, or rejected.
    assert (
        len(result["jobs"]) + result["duplicates"] + len(report.rejected)
        == report.total_rows
    )
    out = metrics.scenario_metrics(result, trace.DEFAULT_BURST_WINDOW)["overall"]
    assert out["peak_alive_nodes"] <= 10
    assert out["grand_total"] == sum(t["total"] for t in result["cost"]["teams"].values()) + (
        result["cost"]["provider_overhead"])  # fmt: skip
