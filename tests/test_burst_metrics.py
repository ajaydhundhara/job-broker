"""P0-07: burst-window metrics (replay/metrics.py), computed after the run from
the jobs table. The scheduler is not involved.

Queue at instant t = jobs with submitted_at <= t < started_at (half-open, as
metrics.peak). Window [ws, we):
- burst_queue: the queue every BURST_SAMPLE_S from ws to we + BURST_TAIL_S;
- burst_peak_queue / burst_peak_after_s: the largest queue at any instant in
  [ws, we), and how long after ws it was first reached;
- burst_recovery_s: when the last job submitted in the window started, minus
  we (negative: before the window ended; None: no job submitted in it).
"""

import json

from test_replay import run_small

from replay import metrics

WINDOW = (0, 3600)


def job(team, sub, start):
    return {"team": team, "gpus": 1, "submitted_at": sub, "started_at": start,
            "ended_at": start + 60}  # fmt: skip


JOBS = [
    job("a", 0, 600),  # in the window, waits 10 min
    job("a", 300, 4500),  # in the window, starts 15 min after it ends
    job("b", 1200, 1200),  # in the window, never waits (never "in" the queue)
    job("a", 5000, 6000),  # after the window: in the samples, not a burst job
    job("b", -600, 900),  # before the window: queued at its start, not a burst job
]


def result(jobs):
    teams = sorted({j["team"] for j in jobs})
    zero = {"gpu_seconds": 0, "busy": 0, "idle": 0, "total": 0}
    return {"jobs": jobs, "nodes": [], "events": [], "duplicates": 0, "final_now": 9000,
            "cost": {"teams": {t: zero for t in teams}, "provider_overhead": 0,
                     "grand_total": 0}}  # fmt: skip


def test_the_sample_times_cover_the_window_and_the_tail():
    assert metrics.BURST_SAMPLE_S == 900 and metrics.BURST_TAIL_S == 7200
    assert metrics.burst_sample_times(WINDOW) == list(range(0, 10801, 900))


def test_the_queue_is_sampled_with_half_open_waits():
    out = metrics.scenario_metrics(result(JOBS), WINDOW)
    #                                0  900 1800 2700 3600 4500 5400 6300 ...
    assert out["overall"]["burst_queue"] == [2, 1, 1, 1, 1, 0, 1, 0, 0, 0, 0, 0, 0]
    assert out["teams"]["a"]["burst_queue"] == [1, 1, 1, 1, 1, 0, 1, 0, 0, 0, 0, 0, 0]
    assert out["teams"]["b"]["burst_queue"] == [1] + [0] * 12


def test_the_peak_is_the_largest_queue_at_any_instant_in_the_window():
    out = metrics.scenario_metrics(result(JOBS), WINDOW)
    # at 300: the first job (0-600), the second (300-4500) and the one from before
    # the window (-600-900); a sample every 15 min alone would only see 2.
    assert (
        out["overall"]["burst_peak_queue"],
        out["overall"]["burst_peak_after_s"],
    ) == (3, 300)
    assert (
        out["teams"]["a"]["burst_peak_queue"],
        out["teams"]["a"]["burst_peak_after_s"],
    ) == (2, 300)
    assert (
        out["teams"]["b"]["burst_peak_queue"],
        out["teams"]["b"]["burst_peak_after_s"],
    ) == (1, 0)


def test_recovery_is_the_last_burst_start_after_the_window_end():
    out = metrics.scenario_metrics(result(JOBS), WINDOW)
    assert out["overall"]["burst_recovery_s"] == 4500 - 3600
    assert out["teams"]["a"]["burst_recovery_s"] == 900
    assert out["teams"]["b"]["burst_recovery_s"] == 1200 - 3600  # done before the end


def test_a_window_with_no_jobs_has_no_recovery_and_an_empty_queue():
    out = metrics.scenario_metrics(result([job("a", 20000, 20100)]), WINDOW)
    o = out["overall"]
    assert o["burst_recovery_s"] is None
    assert (o["burst_peak_queue"], o["burst_peak_after_s"]) == (0, 0)
    assert o["burst_queue"] == [0] * 13


def test_aggregate_takes_a_list_element_by_element():
    runs = [{"q": [1, 2]}, {"q": [3, 0]}, {"q": [2, 1]}]
    assert metrics.aggregate(runs) == {"q": [{"median": 2, "min": 1, "max": 3},
                                             {"median": 1, "min": 0, "max": 2}]}  # fmt: skip


# --- the report ----------------------------------------------------------------------


def test_the_report_has_a_burst_section_before_the_analysis(tmp_path):
    run_small(tmp_path, "a", seeds=(1, 2))
    md = (tmp_path / "a" / "replay_results.md").read_text()
    assert "## 5. Burst window" in md
    section = md.split("## 5. Burst window", 1)[1].split("## Analysis", 1)[0]
    for label in ("peak queue in the window", "peak reached after (min)",
                  "recovery (min)", "| time (UTC) |"):  # fmt: skip
        assert label in section, label
    samples = [x for x in section.splitlines() if x.startswith("| 20")]
    assert len(samples) == 13  # the small trace's 1-hour window + 2 h, every 15 min
    data = json.loads((tmp_path / "a" / "replay_results.json").read_text())
    run = data["scenarios"][0]["runs"]["1"]["overall"]
    assert len(run["burst_queue"]) == 13
    assert data["method"]["burst"].startswith("queue at t = jobs with")
