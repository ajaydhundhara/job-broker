"""Direct tests of broker/costs.py, checking exact integer micro-dollar values.

Prices used: gpu-s 1.00/h = 1_000_000 micros, gpu-m 4.50/h = 4_500_000 micros.
"""

import pytest

from broker import costs
from broker.db import connect, init_db, transaction


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "broker.db")
    init_db(c)
    yield c
    c.close()


def add_node(conn, node_id="node-1", node_type="gpu-m", team="vision", **fields):
    gpus, price = {"gpu-s": (1, 1_000_000), "gpu-m": (4, 4_500_000)}[node_type]
    row = {
        "id": node_id,
        "node_type": node_type,
        "gpus_total": gpus,
        "price_per_hour_micros": price,
        "team": team,
        "state": "ready",
        "launched_at": 0,
        "terminated_at": None,
    }
    row.update(fields)
    cols, marks = ", ".join(row), ", ".join("?" for _ in row)
    with transaction(conn):
        conn.execute(f"INSERT INTO nodes ({cols}) VALUES ({marks})", list(row.values()))
    return conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()


def add_job(conn, job_id, node_id, gpus, started_at, ended_at, team="vision"):
    status = "running" if ended_at is None else "succeeded"
    with transaction(conn):
        conn.execute(
            """INSERT INTO jobs (id, team, request_fingerprint, gpus, estimated_minutes,
                                 priority, status, node_id, submitted_at, started_at,
                                 ended_at)
               VALUES (?, ?, 'fp', ?, 30, 'normal', ?, ?, ?, ?, ?)""",
            (job_id, team, gpus, status, node_id, started_at, started_at, ended_at),
        )


# --- overlap -----------------------------------------------------------------


@pytest.mark.parametrize(
    "a, window, expected",
    [
        ((0, 100), (0, 100), 100),
        ((0, 100), (50, 200), 50),
        ((0, 100), (100, 200), 0),  # half-open: touching is not overlapping
        ((200, 300), (0, 100), 0),
    ],
)
def test_overlap(a, window, expected):
    assert costs.overlap(*a, *window) == expected


# --- node billing ------------------------------------------------------------


def test_spec_example_one_hour_gpu_m_with_one_2_gpu_job_for_30_min(conn):
    node = add_node(conn)  # gpu-m, 4.50/h, launched at 0, still alive
    add_job(conn, "job-1", "node-1", gpus=2, started_at=0, ended_at=1800)

    cost = costs.node_cost(conn, node, from_ts=0, to_ts=3600, now=3600)

    assert cost.billed_seconds == 3600
    assert cost.gpu_seconds == 3600  # 2 GPUs x 1800 s = 1.0 GPU-hour
    assert cost.total == 4_500_000
    assert cost.busy == 1_125_000  # 3600 GPU-s x 4.5M / 4 GPUs / 3600
    assert cost.idle == 3_375_000


def test_terminated_node_alive_120s_is_billed_600s(conn):
    node = add_node(conn, node_type="gpu-s", state="terminated", terminated_at=120)

    cost = costs.node_cost(conn, node, from_ts=0, to_ts=1000, now=1000)

    assert cost.billed_seconds == 600
    # 600 x 1_000_000 / 3600 = 166_666.67 -> floor
    assert cost.total == 166_666
    assert cost.idle == 166_666


def test_minimum_top_up_is_counted_once_across_adjacent_windows(conn):
    node = add_node(conn, node_type="gpu-s", state="terminated", terminated_at=120)

    # Terminated exactly at the end of the first window: its last billed
    # second (119) is inside [0, 120), so the top-up lands there.
    first = costs.node_billed_seconds(node, from_ts=0, to_ts=120, now=1000)
    second = costs.node_billed_seconds(node, from_ts=120, to_ts=1000, now=1000)

    assert (first, second) == (600, 0)


def test_alive_node_has_no_minimum_yet(conn):
    node = add_node(conn, node_type="gpu-s")
    assert costs.node_billed_seconds(node, from_ts=0, to_ts=120, now=120) == 120


def test_node_spanning_window_edges_is_billed_only_for_overlap(conn):
    node = add_node(conn, state="terminated", terminated_at=7200)

    cost = costs.node_cost(conn, node, from_ts=1800, to_ts=5400, now=9000)

    assert cost.billed_seconds == 3600
    assert cost.total == 4_500_000


def test_job_gpu_seconds_are_clipped_to_the_window(conn):
    node = add_node(conn)
    add_job(conn, "job-1", "node-1", gpus=4, started_at=0, ended_at=3600)

    cost = costs.node_cost(conn, node, from_ts=1800, to_ts=3600, now=3600)

    assert cost.gpu_seconds == 4 * 1800
    assert cost.idle == 0  # all 4 GPUs busy for the whole window


def test_job_still_running_on_ended_node_never_makes_idle_negative(conn):
    # Inconsistent state the schema can't forbid: a job marked running on a
    # node that already terminated. Its time is clipped to the node's life.
    node = add_node(conn, state="terminated", terminated_at=3600)
    add_job(conn, "job-1", "node-1", gpus=4, started_at=0, ended_at=None)

    cost = costs.node_cost(conn, node, from_ts=0, to_ts=7200, now=7200)

    assert cost.gpu_seconds == 4 * 3600
    assert cost.idle == 0


@pytest.mark.parametrize(
    "node_fields",
    [
        # gpu-s: 1_000_000 / 3600 = 277.7... micros per second, so every
        # window boundary has a fraction to round (gpu-m's 1250/s has none).
        {"node_type": "gpu-s", "state": "terminated", "terminated_at": 1000},
        {"node_type": "gpu-s", "state": "terminated", "terminated_at": 121},  # top-up
        {"node_type": "gpu-s"},  # still alive at now
    ],
    ids=["ended", "short-lived", "alive"],
)
def test_adjacent_windows_add_up_to_the_micro_dollar(conn, node_fields):
    # Each window used to floor on its own, so [0, cut) + [cut, end) could
    # differ from [0, end) by 1 micro-dollar. Now every figure is the running
    # total at the window's end minus the running total at its start.
    node = add_node(conn, **node_fields)
    add_job(conn, "job-1", "node-1", gpus=1, started_at=37, ended_at=777)
    end = 1000

    whole = costs.node_cost(conn, node, 0, end, now=end)
    for cut in range(end + 1):
        left = costs.node_cost(conn, node, 0, cut, now=end)
        right = costs.node_cost(conn, node, cut, end, now=end)
        for field in ("billed_seconds", "gpu_seconds", "total", "busy", "idle"):
            assert getattr(left, field) + getattr(right, field) == getattr(
                whole, field
            ), (
                field,
                cut,
            )


def test_window_rounding_never_changes_the_whole_bill(conn):
    # 777 s of gpu-s: 215_833.33... micros, floored once to 215_833, whether
    # it is asked for in one window or in eight.
    node = add_node(conn, node_type="gpu-s", state="terminated", terminated_at=777)
    assert costs.node_cost(conn, node, 0, 2000, now=2000).total == 215_833
    pieces = [
        costs.node_cost(conn, node, a, a + 100, now=2000).total
        for a in range(0, 800, 100)
    ]
    assert sum(pieces) == 215_833


# --- report ------------------------------------------------------------------


def test_failed_node_goes_to_provider_overhead_not_team(conn):
    add_node(conn, node_id="node-ok", team="vision")
    add_node(conn, node_id="node-bad", team="speech", state="failed", terminated_at=900)

    report = costs.cost_by_team(conn, from_ts=0, to_ts=3600, now=3600)

    assert set(report.teams) == {"vision"}
    assert report.provider_overhead == 900 * 4_500_000 // 3600  # 1_125_000
    assert report.grand_total == 4_500_000 + 1_125_000


def test_report_sums_nodes_per_team(conn):
    add_node(conn, node_id="node-1", node_type="gpu-s", team="vision")
    add_node(conn, node_id="node-2", node_type="gpu-s", team="vision")
    add_job(conn, "job-1", "node-1", gpus=1, started_at=0, ended_at=3600)

    vision = costs.cost_by_team(conn, from_ts=0, to_ts=3600, now=3600).teams["vision"]

    assert vision.total == 2_000_000
    assert vision.busy == 1_000_000
    assert vision.idle == 1_000_000
    assert vision.gpu_seconds == 3600


# --- dollars -----------------------------------------------------------------


@pytest.mark.parametrize(
    "micros, dollars",
    [
        (4_500_000, 4.5),
        (1_125_000, 1.13),  # half-up; float round() would give 1.12
        (3_375_000, 3.38),
        (4_999, 0.0),
        (5_000, 0.01),
        (0, 0.0),
    ],
)
def test_micros_to_dollars_rounds_half_up(micros, dollars):
    assert costs.micros_to_dollars(micros) == dollars
