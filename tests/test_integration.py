"""End to end: a mixed workload through the real engine (launches, launch
failures, planned job ends, the reaper), checked for restart recovery and
for billing that adds up.

Each test first asserts the workload really produced the events it is meant
to cover, so it can't pass on a world where nothing happened.
"""

import pytest
from fastapi.testclient import TestClient

from broker import costs
from broker.api import create_app
from broker.config import Config
from broker.db import connect, transaction

# 30% never-ready launches, so the run has failed launches and every job
# still finishes (checked by the first test). The seed only picks a fixture
# that covers failures; no assertion depends on its exact numbers.
CONFIG = Config(seed=0, never_ready_prob=0.3, outage_start_prob=0.0, idle_minutes=10)
END = 6 * 3600

WORKLOAD = [  # (submit at, team, gpus, priority, actual minutes)
    (0, "vision", 1, "normal", 20),
    (0, "vision", 2, "high", 45),
    (60, "nlp", 8, "normal", 30),
    (120, "speech", 4, "low", 10),
    (600, "vision", 1, "normal", 5),
    (900, "nlp", 2, "normal", 60),
    (3600, "speech", 1, "high", 15),
    (3700, "vision", 4, "normal", 25),
]


def app(path):
    return TestClient(create_app(path, config=CONFIG))


def submit_with_plan(client, path, team, gpus, priority, minutes):
    r = client.post("/jobs", json={"team": team, "gpus": gpus, "estimated_minutes": 30,
                                   "priority": priority})  # fmt: skip
    assert r.status_code == 201, r.text
    conn = connect(path)
    with transaction(conn):  # hidden trace truth: how long it really runs
        conn.execute(
            "INSERT INTO sim_job_plans (job_id, actual_minutes, outcome) VALUES (?, ?, 'succeeded')",
            (r.json()["id"], minutes),
        )
    conn.close()


def play(path, stop_at=None, restart=False):
    """Run WORKLOAD up to END; with restart, a new app takes over at stop_at."""
    stack = [app(path)]
    client = stack[0].__enter__()
    try:
        for at, team, gpus, priority, minutes in WORKLOAD:
            if restart and at >= stop_at and len(stack) == 1:
                client.post("/sim/advance", json={"to_ts": stop_at})
                stack[0].__exit__(None, None, None)
                stack.append(app(path))
                client = stack[1].__enter__()
            client.post("/sim/advance", json={"to_ts": at})
            submit_with_plan(client, path, team, gpus, priority, minutes)
        client.post("/sim/advance", json={"to_ts": END})
        return observe(client)
    finally:
        stack[-1].__exit__(None, None, None)


def observe(client):
    nodes = client.get("/nodes", params={"include_terminated": True}).json()
    jobs = [client.get(f"/jobs/{j}").json() for j in job_ids(client)]
    return {
        "nodes": nodes,
        # job ids are random per run; compare everything else, in arrival order
        "jobs": [{k: v for k, v in j.items() if k != "id"} for j in jobs],
        "cost": client.get("/reports/cost").json(),
    }


def job_ids(client):
    # No list-jobs endpoint: read ids in arrival order straight from the DB.
    conn = connect(client.app.state.db_path)
    ids = [r[0] for r in conn.execute("SELECT id FROM jobs ORDER BY created_seq")]
    conn.close()
    return ids


@pytest.fixture(scope="module")
def straight(tmp_path_factory):
    path = tmp_path_factory.mktemp("straight") / "broker.db"
    return path, play(path)


def test_workload_covers_launch_failures_reaping_and_planned_ends(straight):
    path, result = straight
    states = {n["state"] for n in result["nodes"]}
    assert {"failed", "terminated"} <= states
    conn = connect(path)
    reasons = {r[0] for r in conn.execute("SELECT end_reason FROM nodes")}
    statuses = {r[0] for r in conn.execute("SELECT status FROM jobs")}
    conn.close()
    assert {"launch_failed", "idle_timeout"} <= reasons
    assert statuses == {"succeeded"}  # every job ran to its planned end


@pytest.mark.parametrize("stop_at", [300, 1500, 3650])
def test_restart_mid_run_gives_the_same_result(straight, tmp_path, stop_at):
    _, expected = straight
    assert play(tmp_path / "broker.db", stop_at=stop_at, restart=True) == expected


# --- billing consistency (exact micro-dollars, no display rounding) -------------------


def report(path, from_ts, to_ts):
    conn = connect(path)
    try:
        return costs.cost_by_team(conn, from_ts, to_ts, now=END)
    finally:
        conn.close()


def flat(r):
    out = {"overhead": r.provider_overhead, "grand": r.grand_total}
    for team, t in r.teams.items():
        out.update(
            {f"{team}.busy": t.busy, f"{team}.idle": t.idle, f"{team}.total": t.total}
        )
    return out


def test_parts_add_up_to_the_totals(straight):
    path, _ = straight
    r = report(path, 0, END)
    assert r.provider_overhead > 0  # the failed launches are in there
    assert all(t.total == t.busy + t.idle for t in r.teams.values())
    assert r.grand_total == sum(t.total for t in r.teams.values()) + r.provider_overhead


@pytest.mark.parametrize("cut", [1, 299, 600, 601, 1500, 3600, 5000])
def test_cost_windows_split_exactly(straight, cut):
    # [0, END) must equal [0, cut) + [cut, END) to the micro-dollar, also for
    # the 10-minute minimum top-up, which must land in exactly one window.
    path, _ = straight
    whole, left, right = (
        report(path, 0, END),
        report(path, 0, cut),
        report(path, cut, END),
    )
    a, b = flat(left), flat(right)
    assert flat(whole) == {k: a.get(k, 0) + b.get(k, 0) for k in flat(whole)}


def test_nothing_is_billed_after_every_node_has_ended(straight):
    path, result = straight
    assert all(n["state"] in ("terminated", "failed") for n in result["nodes"])
    last_end = max(n["terminated_at"] for n in result["nodes"])
    assert report(path, last_end, END).grand_total == 0


def test_cost_report_is_the_same_after_a_restart(straight):
    path, result = straight
    with app(path) as again:
        assert again.get("/reports/cost").json() == result["cost"]
