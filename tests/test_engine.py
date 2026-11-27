"""Simulation engine: events happen at their exact simulated times, in a fixed
order, one transaction per timestamp group, and the result does not depend on
how time is advanced, on restarts or on crashes (SPEC_DECISIONS.md #5, #8, #9).
"""

import pytest

from broker import engine, scheduler, service
from broker.config import Config
from broker.db import connect, init_db, transaction
from broker.engine import SimulationStuck, TimeWentBackwards
from broker.models import JobSubmit
from broker.provider import FakeProvider


def config(**overrides) -> Config:
    return Config(**{"never_ready_prob": 0.0, "outage_start_prob": 0.0,
                     "boot_min_s": 300, "boot_max_s": 300, **overrides})  # fmt: skip


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "broker.db")
    init_db(c)
    yield c
    c.close()


def submit(conn, cfg, team="vision", gpus=1, actual_minutes=None, outcome="succeeded"):
    """Like POST /jobs: insert, then one pass at the current time."""
    job, _ = service.submit_job(
        conn, JobSubmit(team=team, gpus=gpus, estimated_minutes=30)
    )
    if actual_minutes is not None:
        # Hidden trace truth: the engine ends the job this long after it starts.
        with transaction(conn):
            conn.execute(
                "INSERT INTO sim_job_plans (job_id, actual_minutes, outcome) VALUES (?, ?, ?)",
                (job["id"], actual_minutes, outcome),
            )
    engine.run_pass(conn, cfg)
    return job["id"]


def row(conn, table, row_id):
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()


def add_ready_node(conn, node_id="node-a", node_type="gpu-s", gpus=1, team="vision"):
    with transaction(conn):
        conn.execute(
            """INSERT INTO nodes (id, node_type, gpus_total, price_per_hour_micros, team,
                                  state, launched_at, ready_at, idle_since)
               VALUES (?, ?, ?, 1000000, ?, 'ready', 0, 0, 0)""",
            (node_id, node_type, gpus, team),
        )


def event_types(conn, *types):
    marks = ", ".join("?" for _ in types)
    return [tuple(r) for r in conn.execute(
        f"SELECT ts, event_type, entity_id FROM events WHERE event_type IN ({marks}) ORDER BY id",
        types)]  # fmt: skip


# --- exact times -----------------------------------------------------------------


def test_queued_job_starts_exactly_when_its_node_becomes_ready(conn):
    cfg = config()
    job_id = submit(conn, cfg)
    engine.advance_to(conn, 3600, cfg)
    job = row(conn, "jobs", job_id)
    node = row(conn, "nodes", job["node_id"])
    assert (node["state"], node["ready_at"]) == ("ready", 300)
    assert (job["status"], job["started_at"]) == ("running", 300)


def test_planned_job_ends_exactly_actual_minutes_after_it_starts(conn):
    cfg = config()
    first = submit(conn, cfg, gpus=1, actual_minutes=10)
    second = submit(conn, cfg, gpus=1, actual_minutes=5)
    # Two 1-GPU jobs: each gets its own gpu-s, both ready at 300.
    engine.advance_to(conn, 4 * 3600, cfg)
    a, b = row(conn, "jobs", first), row(conn, "jobs", second)
    assert (a["status"], a["started_at"], a["ended_at"]) == ("succeeded", 300, 900)
    assert (b["status"], b["started_at"], b["ended_at"]) == ("succeeded", 300, 600)


def test_a_planned_cancel_ends_the_job_cancelled(conn):
    cfg = config()
    j = submit(conn, cfg, actual_minutes=10, outcome="cancelled")
    engine.advance_to(conn, 3600, cfg)
    job = row(conn, "jobs", j)
    assert (job["status"], job["ended_at"], job["end_reason"]) == (
        "cancelled",
        900,
        "replay_cancel",
    )


def test_job_waits_for_gpus_released_by_a_planned_end(conn):
    add_ready_node(conn)  # one 1-GPU node, already ready, owned by vision
    cfg = config(max_nodes=1)  # no room for a second node: the job must wait
    first = submit(conn, cfg, actual_minutes=10)
    second = submit(conn, cfg, actual_minutes=10)
    engine.advance_to(conn, 3600, cfg)
    a, b = row(conn, "jobs", first), row(conn, "jobs", second)
    assert (a["started_at"], a["ended_at"]) == (0, 600)
    assert (b["started_at"], b["ended_at"]) == (600, 1200)  # took the freed GPU at once


def test_never_ready_launch_fails_at_15_minutes_then_retries_with_backoff(conn):
    cfg = config(never_ready_prob=1.0)
    submit(conn, cfg)  # 1 GPU: gpu-s, no fallback type allowed
    engine.advance_to(conn, 2000, cfg)
    launched = conn.execute(
        "SELECT launched_at, state, terminated_at FROM nodes ORDER BY launched_at"
    ).fetchall()
    # 0 fails at 900 -> relaunch at once (k=1: 0 s); that fails at 1800 ->
    # relaunch 60 s later (k=2) at 1860.
    assert [tuple(r) for r in launched] == [
        (0, "failed", 900),
        (900, "failed", 1800),
        (1860, "launching", None),
    ]


def test_capacity_outage_delays_the_launch_until_the_retry(conn):
    cfg = config(unavailable_windows=(("gpu-s", 0, 60),))
    job_id = submit(conn, cfg)
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
    engine.advance_to(conn, 3600, cfg)
    assert row(conn, "jobs", job_id)["started_at"] == 60 + 300  # retried at 60


def test_scheduler_disabled_never_places_or_launches(conn):
    add_ready_node(conn)
    cfg = config(scheduler_enabled=False)
    j = submit(conn, cfg)
    engine.advance_to(conn, 3600, cfg)
    assert row(conn, "jobs", j)["status"] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1


# --- the clock ---------------------------------------------------------------------


def test_one_clock_event_per_advance(conn):
    cfg = config()
    submit(conn, cfg)
    engine.advance_to(conn, 3600, cfg)
    [(ts, _kind, _)] = event_types(conn, "clock_advanced")
    assert ts == 3600


def test_advance_to_the_past_raises_and_changes_nothing(conn):
    cfg = config()
    engine.advance_to(conn, 600, cfg)
    events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with pytest.raises(TimeWentBackwards):
        engine.advance_to(conn, 599, cfg)
    assert service.get_now(conn) == 600
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before


# --- zero-minute advance (decision 8) ---------------------------------------------


def test_zero_minute_advance_starts_an_eligible_job(conn):
    add_ready_node(conn)
    j, _ = service.submit_job(
        conn, JobSubmit(team="vision", gpus=1, estimated_minutes=30)
    )
    assert engine.advance_to(conn, 0, config()) == 0
    assert row(conn, "jobs", j["id"])["status"] == "running"
    assert event_types(conn, "clock_advanced") == []
    assert service.get_now(conn) == 0


def test_zero_minute_advance_with_nothing_to_do_writes_nothing(conn):
    engine.advance_to(conn, 0, config())
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_zero_minute_advance_does_not_repeat_a_transition(conn):
    cfg = config()
    submit(conn, cfg)
    engine.advance_to(conn, 300, cfg)  # node ready at 300
    engine.advance_to(conn, 300, cfg)
    engine.advance_to(conn, 300, cfg)
    assert len(event_types(conn, "node_ready")) == 1


# --- ties at one timestamp (decision 9) --------------------------------------------


def test_identical_timestamps_are_processed_in_a_fixed_order(conn):
    cfg = config()
    jobs = [submit(conn, cfg, team=t, actual_minutes=5) for t in ("c", "a", "b")]
    engine.advance_to(conn, 3600, cfg)
    # All three nodes are ready at 300 and all three jobs end at 600.
    assert event_types(conn, "node_ready") == [
        (300, "node_ready", "node-00001"), (300, "node_ready", "node-00002"),
        (300, "node_ready", "node-00003")]  # fmt: skip
    ended = [e[2] for e in event_types(conn, "job_succeeded")]
    assert ended == jobs  # creation order, not id order


# --- crash and resume (decision 9) -------------------------------------------------


def scenario(conn, cfg):
    """A small mixed workload: several teams, sizes and planned durations."""
    for team, gpus, minutes in [("vision", 1, 20), ("vision", 2, 45), ("speech", 8, 30),
                                ("nlp", 4, 10), ("vision", 1, 5), ("nlp", 2, 60)]:  # fmt: skip
        submit(conn, cfg, team=team, gpus=gpus, actual_minutes=minutes)


def snapshot(conn):
    # Compare what happened, by creation order (ids are sequential since D16).
    jobs = conn.execute(
        """SELECT created_seq, team, gpus, status, node_id, started_at, ended_at
           FROM jobs ORDER BY created_seq"""
    ).fetchall()
    nodes = conn.execute(
        """SELECT id, node_type, team, state, launched_at, ready_at, terminated_at, end_reason
           FROM nodes ORDER BY id"""
    ).fetchall()
    return [tuple(r) for r in jobs], [tuple(r) for r in nodes]


def test_crash_between_groups_keeps_committed_groups_and_resumes(tmp_path, monkeypatch):
    cfg = config()
    straight = connect(tmp_path / "straight.db")
    init_db(straight)
    scenario(straight, cfg)
    engine.advance_to(straight, 7200, cfg)

    conn = connect(tmp_path / "crashed.db")
    init_db(conn)
    scenario(conn, cfg)
    real = scheduler.schedule

    def crash_at_900(c, now, *args):
        if now == 900:
            raise RuntimeError("simulated crash")
        return real(c, now, *args)

    monkeypatch.setattr(scheduler, "schedule", crash_at_900)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.advance_to(conn, 7200, cfg)
    # Groups before 900 are committed (nodes ready at 300); the 900 group is not.
    assert service.get_now(conn) == 600
    assert (
        conn.execute("SELECT COUNT(*) FROM nodes WHERE state = 'ready'").fetchone()[0]
        > 0
    )
    assert event_types(conn, "clock_advanced") == []  # no event for the partial move

    monkeypatch.setattr(scheduler, "schedule", real)
    engine.advance_to(conn, 7200, cfg)  # resume
    assert snapshot(conn) == snapshot(straight)
    assert [e[0] for e in event_types(conn, "clock_advanced")] == [7200]
    # Nothing was done twice.
    for kind in ("node_ready", "job_started", "node_launched", "job_succeeded"):
        assert len(event_types(conn, kind)) == len(event_types(straight, kind)), kind


def test_a_group_that_never_settles_raises_instead_of_hanging(conn, monkeypatch):
    monkeypatch.setattr(engine, "MAX_ROUNDS_PER_GROUP", 5)
    monkeypatch.setattr(scheduler, "schedule", lambda *a: True)  # "always changed"
    with pytest.raises(SimulationStuck):
        engine.run_pass(conn, config())
    assert service.get_now(conn) == 0


# --- the result must not depend on how time is advanced ------------------------


def test_many_small_advances_equal_one_big_advance(tmp_path):
    cfg = config()
    results = []
    for name, steps in [("big", [7200]), ("small", list(range(60, 7201, 60)))]:
        conn = connect(tmp_path / f"{name}.db")
        init_db(conn)
        scenario(conn, cfg)
        for t in steps:
            engine.advance_to(conn, t, cfg)
        results.append(snapshot(conn))
        conn.close()
    assert results[0] == results[1]


def test_restart_mid_simulation_gives_the_same_result(tmp_path):
    cfg = config()
    straight = connect(tmp_path / "straight.db")
    init_db(straight)
    scenario(straight, cfg)
    engine.advance_to(straight, 7200, cfg)

    path = tmp_path / "restarted.db"
    conn = connect(path)
    init_db(conn)
    scenario(conn, cfg)
    engine.advance_to(conn, 1500, cfg)
    conn.close()
    conn = connect(path)  # "restart": new connection, new provider, same file
    init_db(conn)
    engine.advance_to(conn, 7200, cfg, FakeProvider(cfg))
    assert snapshot(straight) == snapshot(conn)


def test_same_seed_gives_the_same_simulation_with_random_failures(tmp_path):
    # Default provider randomness on (never-ready, outages), high failure rate
    # so randomness really matters.
    results = []
    for name, seed in [("a", 9), ("b", 9), ("c", 10)]:
        cfg = Config(seed=seed, never_ready_prob=0.3, outage_start_prob=0.2)
        conn = connect(tmp_path / f"{name}.db")
        init_db(conn)
        scenario(conn, cfg)
        engine.advance_to(conn, 6 * 3600, cfg)
        results.append(snapshot(conn))
        conn.close()
    assert results[0] == results[1]
    assert results[0] != results[2]


# --- reconciliation (decision 11) ---------------------------------------------------


def test_orphan_provider_node_is_terminated(conn):
    cfg = config()
    FakeProvider(cfg).launch_node(conn, "gpu-s", 0)  # billed, but no broker row
    engine.reconcile_now(conn, cfg)
    assert FakeProvider(cfg).get(conn, "node-00001")["state"] == "terminated"
    assert len(event_types(conn, "orphan_terminated")) == 1


def test_node_the_provider_terminated_is_reconciled_and_its_job_fails(conn):
    cfg = config()
    j = submit(conn, cfg)
    engine.advance_to(conn, 400, cfg)  # running on node-00001 since 300
    with transaction(conn):  # the provider ends it behind our back
        conn.execute(
            "UPDATE provider_nodes SET state = 'terminated', terminated_at = 350"
        )
    engine.reconcile_now(conn, cfg)
    node = row(conn, "nodes", "node-00001")
    assert (node["state"], node["terminated_at"], node["end_reason"]) == (
        "terminated", 350, "provider_reconciled")  # fmt: skip
    job = row(conn, "jobs", j)
    assert (job["status"], job["ended_at"], job["end_reason"]) == (
        "failed",
        350,
        "node_lost",
    )


def test_node_the_provider_failed_is_marked_failed(conn):
    cfg = config()
    submit(conn, cfg)
    with transaction(conn):
        conn.execute("UPDATE provider_nodes SET state = 'failed', terminated_at = 100")
    engine.reconcile_now(conn, cfg)
    node = row(conn, "nodes", "node-00001")
    assert (node["state"], node["end_reason"]) == ("failed", "launch_failed")


def test_reconcile_with_nothing_wrong_changes_nothing(conn):
    cfg = config()
    submit(conn, cfg)
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    engine.reconcile_now(conn, cfg)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
