"""Scheduler: placement, node launches, caps, fallback, retry policies.

Direct calls to scheduler.schedule() on a temp database; no HTTP. Nodes and
jobs are inserted with SQL so each test states exactly the situation it needs.
Rules: SPEC_DECISIONS.md #2, #3, #6, #12, #13.
"""

from pathlib import Path

import pytest

from broker import scheduler
from broker.config import GPUS, PRICE_MICROS, Config
from broker.db import connect, init_db, transaction
from broker.provider import FakeProvider
from broker.service import fingerprint


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "broker.db")
    init_db(c)
    yield c
    c.close()


def config(**overrides) -> Config:
    """Reliable provider with a fixed 300 s boot, unless overridden."""
    return Config(**{"never_ready_prob": 0.0, "outage_start_prob": 0.0,
                     "boot_min_s": 300, "boot_max_s": 300, **overrides})  # fmt: skip


def add_node(conn, node_id, team="vision", node_type="gpu-m", state="ready", **f):
    row = {
        "id": node_id,
        "node_type": node_type,
        "gpus_total": GPUS[node_type],
        "price_per_hour_micros": PRICE_MICROS[node_type],
        "team": team,
        "state": state,
        "launched_at": 0,
        "ready_at": 0 if state == "ready" else None,
        "idle_since": None,
        **f,
    }
    cols, marks = ", ".join(row), ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO nodes ({cols}) VALUES ({marks})", list(row.values()))


_job_counter = iter(range(1, 100_000))


def add_job(conn, team="vision", gpus=1, priority="normal", submitted_at=0, **f):
    job_id = f"job-{next(_job_counter):012x}"
    row = {
        "id": job_id,
        "team": team,
        "request_fingerprint": fingerprint(team, gpus, 30, priority),
        "gpus": gpus,
        "estimated_minutes": 30,
        "priority": priority,
        "status": "queued",
        "submitted_at": submitted_at,
        **f,
    }
    cols, marks = ", ".join(row), ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", list(row.values()))
    return job_id


def busy_node(conn, node_id, team="vision", node_type="gpu-s"):
    add_node(conn, node_id, team=team, node_type=node_type)
    add_job(conn, team=team, gpus=GPUS[node_type], status="running",
            node_id=node_id, started_at=0)  # fmt: skip


def run(conn, now=0, cfg=None):
    cfg = cfg or config()
    with transaction(conn):
        return scheduler.schedule(conn, now, cfg, FakeProvider(cfg))


def job(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def nodes(conn, state=None):
    sql = "SELECT * FROM nodes" + (" WHERE state = ?" if state else "")
    return conn.execute(
        sql + " ORDER BY launched_at, id", (state,) if state else ()
    ).fetchall()


def events(conn, event_type):
    return conn.execute(
        "SELECT * FROM events WHERE event_type = ? ORDER BY id", (event_type,)
    ).fetchall()


def add_event(conn, ts, event_type, node_type):
    conn.execute(
        """INSERT INTO events (ts, entity_type, entity_id, event_type, details)
           VALUES (?, 'provider', NULL, ?, json_object('node_type', ?))""",
        (ts, event_type, node_type),
    )


# --- placement (decision 13.1) ---------------------------------------------------


def test_places_job_on_same_team_ready_node_with_room(conn):
    add_node(conn, "node-a", team="vision", idle_since=0)
    j = add_job(conn, team="vision", gpus=2)
    run(conn, now=50)
    placed = job(conn, j)
    assert (placed["status"], placed["node_id"], placed["started_at"]) == (
        "running",
        "node-a",
        50,
    )
    assert (
        conn.execute("SELECT idle_since FROM nodes WHERE id='node-a'").fetchone()[0]
        is None
    )
    assert len(events(conn, "job_started")) == 1


def test_never_places_a_job_on_another_teams_node(conn):
    add_node(conn, "node-a", team="vision")
    j = add_job(conn, team="nlp", gpus=1)
    run(conn)
    assert job(conn, j)["status"] == "queued"
    [launched] = nodes(conn, "launching")
    assert launched["team"] == "nlp"


def test_best_fit_picks_the_node_left_with_fewest_free_gpus(conn):
    add_node(conn, "node-big", node_type="gpu-l")  # 8 free
    add_node(conn, "node-mid", node_type="gpu-m")  # 4 free
    j = add_job(conn, gpus=4)
    run(conn)
    assert job(conn, j)["node_id"] == "node-mid"


def test_never_overcommits_a_node(conn):
    add_node(conn, "node-a")  # 4 GPUs
    add_job(conn, gpus=2, status="running", node_id="node-a", started_at=0)
    add_job(conn, gpus=1, status="running", node_id="node-a", started_at=0)
    j = add_job(conn, gpus=2)
    run(conn)
    assert job(conn, j)["status"] == "queued"
    used = conn.execute(
        "SELECT SUM(gpus) FROM jobs WHERE node_id='node-a' AND status='running'"
    ).fetchone()[0]
    assert used == 3


def test_does_not_place_on_a_launching_node(conn):
    add_node(conn, "node-a", state="launching")
    j = add_job(conn, gpus=1)
    run(conn)
    assert job(conn, j)["status"] == "queued"
    assert len(nodes(conn)) == 1  # and the launching node covers it: no new launch


def test_queue_order_decides_who_gets_scarce_capacity(conn):
    add_node(conn, "node-a")  # 4 GPUs
    early_normal = add_job(conn, gpus=4, priority="normal", submitted_at=0)
    late_high = add_job(conn, gpus=4, priority="high", submitted_at=60)
    run(conn, now=60)
    assert job(conn, late_high)["status"] == "running"
    assert job(conn, early_normal)["status"] == "queued"


def test_smaller_job_behind_a_blocked_one_can_still_start(conn):
    add_node(conn, "node-a", node_type="gpu-s")  # 1 GPU
    big = add_job(conn, gpus=8, submitted_at=0)
    small = add_job(conn, gpus=1, submitted_at=10)
    run(conn, now=10)
    assert (job(conn, big)["status"], job(conn, small)["status"]) == (
        "queued",
        "running",
    )


def test_a_running_job_is_never_preempted_by_priority(conn):
    add_node(conn, "node-a", node_type="gpu-s")
    low = add_job(
        conn, gpus=1, priority="low", status="running", node_id="node-a", started_at=0
    )
    high = add_job(conn, gpus=1, priority="high", submitted_at=10)
    run(conn, now=10)
    assert job(conn, low)["status"] == "running"
    assert job(conn, high)["status"] == "queued"


# --- queue order (decision 6) -------------------------------------------------------


def test_fifo_ties_are_broken_by_creation_order_not_id(conn):
    # ids sort the other way round from creation order
    conn.execute("""INSERT INTO jobs (id, team, request_fingerprint, gpus, estimated_minutes,
                        priority, status, submitted_at)
                    VALUES ('job-ffffffffffff', 'vision', 'fp', 1, 30, 'normal', 'queued', 0),
                           ('job-000000000000', 'vision', 'fp', 1, 30, 'normal', 'queued', 0)""")  # fmt: skip
    order = [j["id"] for j in scheduler.queue_order(conn, 0, "fifo")]
    assert order == ["job-ffffffffffff", "job-000000000000"]


def test_fair_order_puts_the_team_that_used_less_first(conn):
    add_node(conn, "node-x", team="heavy", node_type="gpu-l")
    add_job(conn, team="heavy", gpus=8, status="succeeded", node_id="node-x",
            started_at=0, ended_at=3600, submitted_at=0)  # fmt: skip
    heavy = add_job(conn, team="heavy", gpus=1, submitted_at=100)
    light = add_job(conn, team="light", gpus=1, submitted_at=200)
    assert [j["id"] for j in scheduler.queue_order(conn, 3600, "fair")] == [
        light,
        heavy,
    ]
    assert [j["id"] for j in scheduler.queue_order(conn, 3600, "fifo")] == [
        heavy,
        light,
    ]


def test_fair_usage_only_counts_the_last_24_hours(conn):
    add_node(conn, "node-x", team="heavy", node_type="gpu-l")
    add_job(conn, team="heavy", gpus=8, status="succeeded", node_id="node-x",
            started_at=0, ended_at=3600, submitted_at=0)  # fmt: skip
    heavy = add_job(conn, team="heavy", gpus=1, submitted_at=100)
    light = add_job(conn, team="light", gpus=1, submitted_at=200)
    day_later = 3600 + 24 * 3600
    assert [j["id"] for j in scheduler.queue_order(conn, day_later, "fair")] == [
        heavy,
        light,
    ]


def test_queue_order_survives_a_restart(tmp_path):
    path = tmp_path / "broker.db"
    c = connect(path)
    init_db(c)
    with transaction(c):
        ids = [add_job(c, team=t, gpus=1, submitted_at=0) for t in ("b", "a", "c", "a")]
    before = [j["id"] for j in scheduler.queue_order(c, 0, "fair")]
    c.close()
    c = connect(path)
    init_db(c)
    assert [j["id"] for j in scheduler.queue_order(c, 0, "fair")] == before == ids
    c.close()


# --- launching (decision 13) ---------------------------------------------------------


@pytest.mark.parametrize(
    "gpus, node_type", [(1, "gpu-s"), (2, "gpu-m"), (4, "gpu-m"), (8, "gpu-l")]
)
def test_launches_the_cheapest_node_type_that_fits(conn, gpus, node_type):
    add_job(conn, gpus=gpus)
    run(conn)
    [launched] = nodes(conn)
    assert (launched["node_type"], launched["state"], launched["team"]) == (
        node_type, "launching", "vision")  # fmt: skip
    assert len(events(conn, "node_launched")) == 1


def test_five_one_gpu_jobs_with_free_slots_get_five_small_nodes(conn):
    # Five gpu-s cost $5.00/h; one gpu-l would cost $8.50/h (decision 13.4).
    for _ in range(5):
        add_job(conn, gpus=1)
    run(conn)
    assert [n["node_type"] for n in nodes(conn)] == ["gpu-s"] * 5


def test_at_the_cap_waiting_jobs_are_packed_onto_one_larger_node(conn):
    for i in range(8):
        busy_node(conn, f"node-busy-{i}")
    for _ in range(5):
        add_job(conn, gpus=1)  # 5 jobs, 2 free slots
    run(conn)
    launched = nodes(conn, "launching")
    assert [n["node_type"] for n in launched] == ["gpu-l"]


def test_queued_jobs_share_one_launching_node(conn):
    add_job(conn, gpus=2)
    add_job(conn, gpus=2)
    run(conn)
    assert len(nodes(conn)) == 1  # one gpu-m covers both


def test_no_second_launch_while_a_launching_node_covers_the_job(conn):
    add_job(conn, gpus=1)
    run(conn, now=0)
    assert run(conn, now=60) is False
    assert len(nodes(conn)) == 1


def test_never_more_than_10_live_nodes(conn):
    for i in range(12):
        add_job(conn, team=f"team-{i}", gpus=1)
    run(conn)
    assert len(nodes(conn)) == 10
    assert (
        conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
        == 12
    )


def test_ended_nodes_do_not_count_toward_the_cap(conn):
    for i in range(10):
        add_node(conn, f"node-{i:02}", state="terminated", terminated_at=10)
    add_job(conn, gpus=1)
    run(conn, now=20)
    assert len(nodes(conn, "launching")) == 1


def test_the_scheduler_never_reads_actual_durations():
    source = (Path(scheduler.__file__)).read_text()
    assert "sim_job_plans" not in source.split('"""', 2)[2]  # outside the docstring
    assert "actual_minutes" not in source


# --- fair-share cap and eviction (decision 12) -----------------------------------------


def test_fair_cap_stops_a_team_launching_while_others_wait(conn):
    for i in range(5):
        busy_node(conn, f"node-a{i}", team="a")
    a = add_job(conn, team="a", gpus=1)
    b = add_job(conn, team="b", gpus=1)
    run(conn)  # 2 active teams -> cap ceil(10/2) = 5 each
    assert [n["team"] for n in nodes(conn, "launching")] == ["b"]
    assert (job(conn, a)["status"], job(conn, b)["status"]) == ("queued", "queued")


def test_fair_cap_does_not_apply_when_no_other_team_waits(conn):
    for i in range(5):
        busy_node(conn, f"node-a{i}", team="a")
    add_job(conn, team="a", gpus=1)
    run(conn)
    assert [n["team"] for n in nodes(conn, "launching")] == ["a"]


def test_fifo_has_no_fair_cap(conn):
    for i in range(5):
        busy_node(conn, f"node-a{i}", team="a")
    add_job(conn, team="a", gpus=1)
    add_job(conn, team="b", gpus=1)
    run(conn, cfg=config(policy="fifo"))
    assert sorted(n["team"] for n in nodes(conn, "launching")) == ["a", "b"]


def _a_at_cap_b_idle(conn):
    """10 live nodes: team a 6 busy, team b 4 idle and old enough to evict."""
    for i in range(6):
        busy_node(conn, f"node-a{i}", team="a")
    for i in range(4):
        add_node(conn, f"node-b{i}", team="b", node_type="gpu-s", idle_since=0)
    add_job(conn, team="b", gpus=8)  # b waits too, and doesn't fit its own nodes
    return add_job(conn, team="a", gpus=1)


def test_a_team_at_its_fair_cap_does_not_evict_another_teams_node(conn):
    a = _a_at_cap_b_idle(conn)
    run(conn, now=1000)
    assert nodes(conn, "terminated") == []
    assert job(conn, a)["status"] == "queued"


def test_without_the_fair_cap_the_same_job_would_evict(conn):
    # Control for the test above: what stops the eviction is the cap.
    _a_at_cap_b_idle(conn)
    run(conn, now=1000, cfg=config(policy="fifo"))
    [evicted] = nodes(conn, "terminated")
    assert evicted["team"] == "b"


def _at_cap_with_one_idle(conn, idle_launched_at=0, idle_since=100):
    for i in range(9):
        busy_node(conn, f"node-busy-{i}", team="vision")
    add_node(conn, "node-idle", team="vision", node_type="gpu-s",
             launched_at=idle_launched_at, idle_since=idle_since)  # fmt: skip
    return add_job(conn, team="nlp", gpus=1)


def test_at_cap_the_longest_idle_node_is_evicted_to_make_room(conn):
    waiting = _at_cap_with_one_idle(conn)
    run(conn, now=600)
    evicted = conn.execute("SELECT * FROM nodes WHERE id = 'node-idle'").fetchone()
    assert (evicted["state"], evicted["terminated_at"], evicted["end_reason"]) == (
        "terminated", 600, "evicted")  # fmt: skip
    assert len(events(conn, "node_evicted")) == 1
    [launched] = nodes(conn, "launching")
    assert launched["team"] == "nlp"
    assert job(conn, waiting)["status"] == "queued"  # until the new node is ready


def test_a_node_younger_than_the_minimum_lifetime_is_not_evicted(conn):
    _at_cap_with_one_idle(conn)
    run(conn, now=599)
    assert nodes(conn, "terminated") == []
    assert nodes(conn, "launching") == []


def test_a_protected_node_is_not_evicted(conn):
    _at_cap_with_one_idle(conn)
    # A queued vision job that fits node-idle protects it (decision 5), and
    # then also starts on it.
    mine = add_job(conn, team="vision", gpus=1)
    run(conn, now=600)
    assert nodes(conn, "terminated") == []
    assert job(conn, mine)["node_id"] == "node-idle"


def test_at_cap_with_no_idle_node_the_job_waits(conn):
    for i in range(10):
        busy_node(conn, f"node-{i:02}")
    j = add_job(conn, team="nlp", gpus=1)
    run(conn, now=1000)
    assert job(conn, j)["status"] == "queued"
    assert len(nodes(conn)) == 10


def test_eviction_can_be_turned_off(conn):
    for i in range(10):
        add_node(conn, f"node-{i:02}", node_type="gpu-s", idle_since=0)
    add_job(conn, team="nlp", gpus=1)
    run(conn, now=1000, cfg=config(evict_at_cap=False))
    assert nodes(conn, "terminated") == []
    assert nodes(conn, "launching") == []


# --- capacity unavailable and fallback (decisions 2, 3) ---------------------------------


def test_capacity_unavailable_keeps_job_queued_and_logs_it(conn):
    j = add_job(conn, gpus=1)
    run(conn, cfg=config(unavailable_windows=(("gpu-s", 0, 60),)))
    assert job(conn, j)["status"] == "queued"
    assert nodes(conn) == []
    [refused] = events(conn, "capacity_unavailable")
    assert refused["entity_type"] == "provider"


def test_fallback_to_the_next_larger_type_in_the_same_pass(conn):
    add_job(conn, gpus=2)
    run(conn, cfg=config(unavailable_windows=(("gpu-m", 0, 60),)))
    assert [n["node_type"] for n in nodes(conn)] == ["gpu-l"]
    kinds = [e["event_type"] for e in conn.execute("SELECT * FROM events ORDER BY id")]
    assert kinds == ["capacity_unavailable", "node_launched"]


def test_no_fallback_beyond_twice_the_price(conn):
    add_job(conn, gpus=1)
    run(conn, cfg=config(unavailable_windows=(("gpu-s", 0, 60),)))
    assert nodes(conn) == []  # gpu-m is 4.5x gpu-s


def test_a_higher_price_ratio_allows_the_expensive_fallback(conn):
    add_job(conn, gpus=1)
    run(
        conn,
        cfg=config(unavailable_windows=(("gpu-s", 0, 60),), max_fallback_price_ratio=5),
    )
    assert [n["node_type"] for n in nodes(conn)] == ["gpu-m"]


def test_all_acceptable_types_unavailable(conn):
    j = add_job(conn, gpus=4)
    run(conn, cfg=config(unavailable_windows=(("gpu-m", 0, 60), ("gpu-l", 0, 60))))
    assert job(conn, j)["status"] == "queued"
    assert nodes(conn) == []
    assert [json_type(e) for e in events(conn, "capacity_unavailable")] == [
        "gpu-m",
        "gpu-l",
    ]


def json_type(event):
    import json

    return json.loads(event["details"])["node_type"]


def test_one_refusal_stops_further_tries_of_that_type_in_the_same_pass(conn):
    # Failures come in bursts, so hammering the provider is pointless.
    add_job(conn, team="a", gpus=1)
    add_job(conn, team="b", gpus=1)
    run(conn, cfg=config(unavailable_windows=(("gpu-s", 0, 60),)))
    assert len(events(conn, "capacity_unavailable")) == 1
    assert nodes(conn) == []


@pytest.mark.parametrize(
    "k, delay", [(1, 60), (2, 120), (3, 240), (4, 480), (5, 900), (9, 900)]
)
def test_capacity_backoff_doubles_and_is_capped_at_15_minutes(conn, k, delay):
    for _ in range(k):
        add_event(conn, 1000, "capacity_unavailable", "gpu-s")
    assert scheduler.retry_at(conn, "gpu-s", config()) == 1000 + delay


@pytest.mark.parametrize(
    "k, delay", [(1, 0), (2, 60), (3, 120), (4, 240), (5, 480), (8, 480)]
)
def test_never_ready_backoff(conn, k, delay):
    for _ in range(k):
        add_event(conn, 1000, "launch_failed", "gpu-s")
    assert scheduler.retry_at(conn, "gpu-s", config()) == 1000 + delay


def test_backoff_is_per_node_type(conn):
    add_event(conn, 1000, "capacity_unavailable", "gpu-s")
    assert scheduler.retry_at(conn, "gpu-m", config()) is None


def test_capacity_backoff_resets_after_a_successful_launch(conn):
    add_event(conn, 0, "capacity_unavailable", "gpu-s")
    add_event(conn, 60, "node_launched", "gpu-s")
    assert scheduler.retry_at(conn, "gpu-s", config()) is None
    add_event(conn, 100, "capacity_unavailable", "gpu-s")
    assert scheduler.retry_at(conn, "gpu-s", config()) == 160  # k = 1 again


def test_never_ready_backoff_resets_when_a_node_becomes_ready(conn):
    add_event(conn, 900, "launch_failed", "gpu-s")
    add_event(conn, 1800, "launch_failed", "gpu-s")
    add_event(conn, 2000, "node_ready", "gpu-s")
    assert scheduler.retry_at(conn, "gpu-s", config()) is None


def test_launch_retries_wait_for_the_backoff(conn):
    cfg = config(unavailable_windows=(("gpu-s", 0, 420),))
    add_job(conn, gpus=1)

    run(conn, now=0, cfg=cfg)  # refusal 1 -> retry at 60
    run(conn, now=59, cfg=cfg)
    assert len(events(conn, "capacity_unavailable")) == 1
    assert scheduler.next_retry_time(conn, 0, cfg) == 60

    run(conn, now=60, cfg=cfg)  # refusal 2 -> retry at 180
    assert len(events(conn, "capacity_unavailable")) == 2
    assert scheduler.next_retry_time(conn, 60, cfg) == 180

    run(conn, now=180, cfg=cfg)  # refusal 3 -> retry at 420
    run(conn, now=420, cfg=cfg)  # window over: launch succeeds
    assert len(events(conn, "capacity_unavailable")) == 3
    assert len(nodes(conn, "launching")) == 1
