"""Final validation of cost accounting (FINAL_ASSESSMENT_VALIDATION_TASK §2).

tests/test_costs.py checks single nodes against hand-computed micro-dollars.
This file adds what it does not cover: report-level additivity over many cuts
with several teams and node types, events exactly on window edges, the
minimum-bill edges (599 / 600 / 900 s), idle bounds, agreement between
/nodes and /reports/cost, repeated requests, and a real lifecycle run through
the API with the scheduler and reaper on.

Policy under test (README "Billing", "10-minute minimum", "Cost attribution",
"Money"; SPEC_DECISIONS #1): windows are half-open [from_ts, to_ts); a node
is billed from launch to termination (or now); a node that ended under 600 s
is topped up to 600 s, counted in the window holding terminated_at - 1; all
node cost goes to the owning team except never-ready (failed) launches,
which go to provider_overhead.
"""

import itertools

import pytest
from fastapi.testclient import TestClient

from broker import costs, service
from broker.api import create_app
from broker.config import GPUS, PRICE_MICROS, Config
from broker.db import connect, init_db, transaction


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "broker.db")
    init_db(c)
    yield c
    c.close()


def add_node(conn, node_id, node_type, team, launched_at, terminated_at=None,
             state=None):  # fmt: skip
    state = state or ("ready" if terminated_at is None else "terminated")
    with transaction(conn):
        conn.execute(
            """INSERT INTO nodes (id, node_type, gpus_total, price_per_hour_micros,
                                  team, state, launched_at, terminated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, node_type, GPUS[node_type], PRICE_MICROS[node_type], team,
             state, launched_at, terminated_at),
        )  # fmt: skip
    return conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()


def add_job(conn, job_id, node_id, team, gpus, started_at, ended_at):
    status = "running" if ended_at is None else "succeeded"
    with transaction(conn):
        conn.execute(
            """INSERT INTO jobs (id, team, request_fingerprint, gpus, estimated_minutes,
                                 priority, status, node_id, submitted_at, started_at,
                                 ended_at)
               VALUES (?, ?, 'fp', ?, 30, 'normal', ?, ?, ?, ?, ?)""",
            (job_id, team, gpus, status, node_id, started_at, started_at, ended_at),
        )


NOW = 5000


def mixed_fleet(conn):
    """Three teams, every node type, a failed launch, a short-lived node, a
    reconciled-style node and a live one; prices with fractional micros per second."""
    add_node(conn, "n-l", "gpu-l", "vision", 0, 4000)
    add_job(conn, "j1", "n-l", "vision", 1, 100, 2500)
    add_job(conn, "j2", "n-l", "vision", 8, 2600, 3999)
    add_job(conn, "j3", "n-l", "vision", 2, 777, 1234)
    add_node(conn, "n-m", "gpu-m", "speech", 311, None)  # alive at NOW
    add_job(conn, "j4", "n-m", "speech", 4, 700, None)
    add_node(conn, "n-s", "gpu-s", "nlp", 1000, 1121)  # 121 s: topped up to 600
    add_job(conn, "j5", "n-s", "nlp", 1, 1050, 1121)
    add_node(conn, "n-f", "gpu-m", "nlp", 2000, 2900, state="failed")  # 900 s
    add_node(conn, "n-r", "gpu-s", "speech", 3000, 3456)  # provider_reconciled
    add_job(conn, "j6", "n-r", "speech", 1, 3100, 3456)


def report_tuple(r: costs.CostReport):
    teams = {
        t: (c.gpu_seconds, c.busy, c.idle, c.total) for t, c in sorted(r.teams.items())
    }
    return teams, r.provider_overhead, r.grand_total


def add_reports(a, b):
    teams = {}
    for t in set(a[0]) | set(b[0]):
        x, y = a[0].get(t, (0, 0, 0, 0)), b[0].get(t, (0, 0, 0, 0))
        teams[t] = tuple(i + j for i, j in zip(x, y, strict=True))
    return dict(sorted(teams.items())), a[1] + b[1], a[2] + b[2]


# --- additivity and internal consistency --------------------------------------------


@pytest.mark.parametrize("step", [37, 600, 1000])
def test_any_split_of_time_adds_up_to_the_whole_report(conn, step):
    mixed_fleet(conn)
    whole = report_tuple(costs.cost_by_team(conn, 0, NOW, NOW))
    cuts = [*range(0, NOW, step), NOW]
    total = ({}, 0, 0)
    for a, b in itertools.pairwise(cuts):
        total = add_reports(total, report_tuple(costs.cost_by_team(conn, a, b, NOW)))
    # Teams with no cost in a window are absent; compare with zeros dropped.
    total = ({t: v for t, v in total[0].items() if any(v)}, total[1], total[2])
    assert total == whole


def test_every_window_is_internally_consistent(conn):
    mixed_fleet(conn)
    for a in range(0, NOW, 250):
        for b in (a + 1, a + 250, NOW):
            r = costs.cost_by_team(conn, a, min(b, NOW), NOW)
            assert r.grand_total == sum(t.total for t in r.teams.values()) + (
                r.provider_overhead
            )
            for team in r.teams.values():
                assert team.busy + team.idle == team.total


def test_whole_report_has_exact_expected_values(conn):
    mixed_fleet(conn)
    r = costs.cost_by_team(conn, 0, NOW, NOW)
    # Hand-computed: seconds x price // 3600 per node, floored once.
    vision = 4000 * 8_500_000 // 3600  # 9_444_444
    # n-r lived 456 s, so it is topped up to 600 s too.
    speech = (NOW - 311) * 4_500_000 // 3600 + 600 * 1_000_000 // 3600
    nlp = 600 * 1_000_000 // 3600  # 121 s topped up to 600 s
    failed = 900 * 4_500_000 // 3600  # never ready: no top-up (900 > 600)
    assert r.teams["vision"].total == vision == 9_444_444
    assert r.teams["speech"].total == speech == 5_861_250 + 166_666
    assert r.teams["nlp"].total == nlp == 166_666
    assert r.provider_overhead == failed == 1_125_000
    assert r.grand_total == vision + speech + nlp + failed
    # GPU-hours are the requested GPUs x run time (brief, Clarifications).
    assert r.teams["vision"].gpu_seconds == 1 * 2400 + 8 * 1399 + 2 * 457
    assert r.teams["speech"].gpu_seconds == 4 * (NOW - 700) + 1 * 356


# --- window edges ------------------------------------------------------------------------


def test_a_node_launched_exactly_at_to_ts_is_not_in_the_window(conn):
    add_node(conn, "n", "gpu-s", "vision", 1000, None)
    r = costs.cost_by_team(conn, 0, 1000, NOW)
    assert r.teams == {} and r.grand_total == 0
    after = costs.cost_by_team(conn, 1000, 1001, NOW)
    assert (
        costs.node_billed_seconds(
            conn.execute("SELECT * FROM nodes").fetchone(), 1000, 1001, NOW
        )
        == 1
    )  # its first second belongs to the window that starts at launch
    assert after.teams["vision"].total == 1_000_000 // 3600  # 277


def test_a_node_that_ended_exactly_at_from_ts_is_not_in_the_window(conn):
    add_node(conn, "n", "gpu-s", "vision", 0, 120)  # top-up of 480 s
    later = costs.cost_by_team(conn, 120, 2000, NOW)
    assert later.teams == {} and later.grand_total == 0
    earlier = costs.cost_by_team(conn, 0, 120, NOW)
    assert earlier.teams["vision"].total == 600 * 1_000_000 // 3600


def test_jobs_touching_the_window_edges_count_zero_gpu_seconds(conn):
    node = add_node(conn, "n", "gpu-m", "vision", 0, None)
    add_job(conn, "before", "n", "vision", 4, 0, 1000)  # ends at from_ts
    add_job(conn, "after", "n", "vision", 4, 2000, None)  # starts at to_ts
    cost = costs.node_cost(conn, node, 1000, 2000, NOW)
    assert cost.gpu_seconds == 0
    assert cost.busy == 0 and cost.idle == cost.total == 1000 * 1250


# --- minimum billing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "lifetime, billed", [(1, 600), (599, 600), (600, 600), (601, 601), (900, 900)]
)
def test_minimum_bill_is_applied_once_and_only_below_600_s(conn, lifetime, billed):
    node = add_node(conn, "n", "gpu-s", "vision", 100, 100 + lifetime)
    assert costs.node_billed_seconds(node, 0, NOW, NOW) == billed
    split = sum(costs.node_billed_seconds(node, a, a + 1, NOW) for a in range(NOW))
    assert split == billed  # one-second windows: the top-up lands exactly once


def test_a_live_node_shows_its_minimum_only_once_it_ends(conn):
    node = add_node(conn, "n", "gpu-s", "vision", 0, None)
    assert costs.node_billed_seconds(node, 0, 120, now=120) == 120
    with transaction(conn):
        conn.execute("UPDATE nodes SET state = 'terminated', terminated_at = 120")
    ended = conn.execute("SELECT * FROM nodes").fetchone()
    assert costs.node_billed_seconds(ended, 0, 120, now=120) == 600


# --- idle bounds and rounding ------------------------------------------------------------


def test_idle_is_never_negative_from_launch_and_at_most_minus_one_later(conn):
    """costs.py documents that a window starting after launch can show idle of
    -1 micro-dollar (busy crosses a micro boundary that total does not). The
    README's "idle cost never goes negative" holds for windows from launch.
    This pins both: >= 0 from launch, >= -1 in any one-second window."""
    mixed_fleet(conn)
    nodes = conn.execute("SELECT * FROM nodes").fetchall()
    worst = 0
    for node in nodes:
        for end in range(0, NOW + 1, 50):
            assert costs.node_cost(conn, node, 0, end, NOW).idle >= 0
        for a in range(NOW):
            idle = costs.node_cost(conn, node, a, a + 1, NOW).idle
            worst = min(worst, idle)
    assert worst >= -1


# --- agreement and determinism -----------------------------------------------------------


def test_node_cost_so_far_agrees_with_the_cost_report_in_micros(conn):
    mixed_fleet(conn)
    nodes = conn.execute("SELECT * FROM nodes").fetchall()
    per_node = sum(costs.node_cost(conn, n, 0, NOW, NOW).total for n in nodes)
    assert per_node == costs.cost_by_team(conn, 0, NOW, NOW).grand_total


def test_repeated_reports_are_identical(conn):
    mixed_fleet(conn)
    with transaction(conn):
        conn.execute("UPDATE sim_clock SET now_ts = ?", (NOW,))
    first = [service.cost_report(conn, a, NOW) for a in (0, 1000, 4999)]
    again = [service.cost_report(conn, a, NOW) for a in (0, 1000, 4999)]
    assert first == again


def test_a_real_lifecycle_run_is_consistent_through_the_api(tmp_path):
    """Scheduler and reaper on, seeded provider: submit, cancel, complete,
    advance, then check the report against /nodes and against itself."""
    cfg = Config(seed=7, idle_minutes=2)
    with TestClient(create_app(tmp_path / "b.db", config=cfg)) as c:
        ids = []
        for team, gpus in [("vision", 1), ("vision", 2), ("speech", 4), ("nlp", 8)]:
            r = c.post("/jobs", json={"team": team, "gpus": gpus,
                                      "estimated_minutes": 20})  # fmt: skip
            assert r.status_code == 201
            ids.append(r.json()["id"])
        c.post("/sim/advance", json={"minutes": 10})
        c.post(f"/jobs/{ids[0]}/complete", json={"outcome": "succeeded"})
        c.post(f"/jobs/{ids[1]}/cancel")
        c.post("/sim/advance", json={"minutes": 60})
        now = c.get("/sim/now").json()["now_ts"]

        whole = c.get("/reports/cost", params={"to_ts": now}).json()
        assert whole == c.get("/reports/cost", params={"to_ts": now}).json()
        nodes = c.get("/nodes", params={"include_terminated": True}).json()
        assert nodes, "the scheduler launched nothing"
        assert all(n["state"] in ("terminated", "failed", "ready") for n in nodes)
        cents = round(sum(n["cost_so_far"] for n in nodes) * 100)
        # Each figure is rounded to cents on its own: at most 0.5 cent each.
        assert abs(cents - round(whole["grand_total"] * 100)) <= len(nodes)
        for team in whole["teams"]:
            for k in ("busy_cost", "idle_cost", "total_cost", "gpu_hours"):
                assert team[k] >= 0
            assert abs(team["busy_cost"] + team["idle_cost"] - team["total_cost"]) <= (
                0.011
            )
