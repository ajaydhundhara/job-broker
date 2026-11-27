"""The fake cloud provider: deterministic launches, boot delays, failures, outages.

SPEC_DECISIONS.md #4 (outages), #10 (seeded generators), #14 (node ids).
"""

import random

import pytest

from broker.config import Config
from broker.db import connect, init_db
from broker.provider import CapacityUnavailable, FakeProvider, UnknownNode, seeded_rng
from broker.scheduler import _smallest_fitting


@pytest.fixture
def make_conn(tmp_path):
    conns = []

    def make(name="broker.db"):
        conn = connect(tmp_path / name)
        init_db(conn)
        conns.append(conn)
        return conn

    yield make
    for c in conns:
        c.close()


def reliable(**overrides) -> Config:
    """No random never-ready launches or outages, unless overridden."""
    return Config(**{"never_ready_prob": 0.0, "outage_start_prob": 0.0, **overrides})


def launch_many(provider, conn, n, now=0, node_type="gpu-s"):
    ids = [provider.launch_node(conn, node_type, now) for _ in range(n)]
    return [provider.get(conn, i) for i in ids]


# --- node types --------------------------------------------------------------


@pytest.mark.parametrize(
    "gpus, node_type", [(1, "gpu-s"), (2, "gpu-m"), (4, "gpu-m"), (8, "gpu-l")]
)
def test_smallest_node_type_that_fits(gpus, node_type):
    assert _smallest_fitting(gpus) == node_type


def test_unknown_node_type_is_rejected(make_conn):
    with pytest.raises(ValueError):
        FakeProvider(reliable()).launch_node(make_conn(), "gpu-xl", 0)


# --- determinism (decision 10) and ids (decision 14) ------------------------------


def test_same_seed_gives_same_ids_and_timings(make_conn):
    a = launch_many(FakeProvider(Config(seed=7)), make_conn("a.db"), 30)
    b = launch_many(FakeProvider(Config(seed=7)), make_conn("b.db"), 30)
    assert [dict(r) for r in a] == [dict(r) for r in b]


def test_different_seed_gives_different_timings(make_conn):
    a = launch_many(FakeProvider(reliable(seed=1)), make_conn("a.db"), 10)
    b = launch_many(FakeProvider(reliable(seed=2)), make_conn("b.db"), 10)
    assert [r["id"] for r in a] == [r["id"] for r in b]  # ids are sequential
    assert [r["ready_at_planned"] for r in a] != [r["ready_at_planned"] for r in b]


def test_node_ids_are_sequential_and_zero_padded(make_conn):
    rows = launch_many(FakeProvider(reliable()), make_conn(), 3)
    assert [r["id"] for r in rows] == ["node-00001", "node-00002", "node-00003"]


def test_a_new_provider_object_continues_the_same_sequence(make_conn):
    # A restart builds a new FakeProvider: no in-memory state may matter.
    uninterrupted = launch_many(FakeProvider(Config(seed=3)), make_conn("a.db"), 6)
    conn = make_conn("b.db")
    first = launch_many(FakeProvider(Config(seed=3)), conn, 3)
    rest = launch_many(FakeProvider(Config(seed=3)), conn, 3)
    assert [dict(r) for r in first + rest] == [dict(r) for r in uninterrupted]


def test_the_injected_generator_factory_is_used(make_conn):
    seen = []

    def rng(purpose, key):
        seen.append((purpose, key))
        return random.Random(0)

    FakeProvider(reliable(), rng=rng).launch_node(make_conn(), "gpu-s", 0)
    assert ("launch", "node-00001") in seen


def test_seeded_rng_gives_the_same_draw_for_the_same_key():
    a, b = seeded_rng(5), seeded_rng(5)
    assert a("launch", "node-00001").random() == b("launch", "node-00001").random()
    assert a("launch", "node-00001").random() != a("launch", "node-00002").random()


# --- boot delay and never-ready failures --------------------------------------


def test_ready_delay_is_between_4_and_8_minutes(make_conn):
    rows = launch_many(FakeProvider(reliable()), make_conn(), 300, now=1000)
    delays = {r["ready_at_planned"] - 1000 for r in rows}
    assert min(delays) >= 240 and max(delays) <= 480
    assert len(delays) > 50  # actually spread out, not one fixed value


def test_about_3_percent_of_launches_never_become_ready(make_conn):
    provider = FakeProvider(Config(outage_start_prob=0.0))
    rows = launch_many(provider, make_conn(), 2000, now=500)
    failing = [r for r in rows if r["fails_at_planned"] is not None]
    assert 0.015 <= len(failing) / len(rows) <= 0.045
    for r in failing:
        assert r["ready_at_planned"] is None
        assert r["fails_at_planned"] == 500 + 900


def test_fixed_delay_override(make_conn):
    provider = FakeProvider(reliable(boot_min_s=240, boot_max_s=240))
    conn = make_conn()
    node_id = provider.launch_node(conn, "gpu-l", 100)
    assert provider.get(conn, node_id)["ready_at_planned"] == 340


def test_failure_probability_1_always_fails(make_conn):
    rows = launch_many(FakeProvider(reliable(never_ready_prob=1.0)), make_conn(), 20)
    assert all(
        r["fails_at_planned"] == 900 and r["ready_at_planned"] is None for r in rows
    )


def test_never_ready_launches_picks_launches_by_number(make_conn):
    rows = launch_many(
        FakeProvider(reliable(never_ready_launches=(2,))), make_conn(), 3
    )
    assert [r["fails_at_planned"] is not None for r in rows] == [False, True, False]


# --- outages (decision 4) -------------------------------------------------------


@pytest.mark.parametrize(
    "now, refused", [(99, False), (100, True), (299, True), (300, False)]
)
def test_unavailable_window_is_half_open(make_conn, now, refused):
    provider = FakeProvider(reliable(unavailable_windows=(("gpu-m", 100, 300),)))
    conn = make_conn()
    if refused:
        with pytest.raises(CapacityUnavailable):
            provider.launch_node(conn, "gpu-m", now)
        assert conn.execute("SELECT COUNT(*) FROM provider_nodes").fetchone()[0] == 0
    else:
        assert provider.launch_node(conn, "gpu-m", now) == "node-00001"


def test_unavailable_window_is_per_node_type(make_conn):
    provider = FakeProvider(reliable(unavailable_windows=(("gpu-m", 0, 600),)))
    assert provider.launch_node(make_conn(), "gpu-l", 0) == "node-00001"


def test_first_launch_succeeds_then_a_window_refuses_the_second(make_conn):
    provider = FakeProvider(reliable(unavailable_windows=(("gpu-s", 60, 120),)))
    conn = make_conn()
    assert provider.launch_node(conn, "gpu-s", 0) == "node-00001"
    with pytest.raises(CapacityUnavailable):
        provider.launch_node(conn, "gpu-s", 60)
    assert provider.launch_node(conn, "gpu-s", 120) == "node-00002"  # no id consumed


def test_random_outages_come_in_bursts_of_whole_buckets(make_conn):
    cfg = Config(never_ready_prob=0.0)
    provider = FakeProvider(cfg)
    bucket = cfg.outage_bucket_s
    states = []
    for b in range(400):
        inside = {
            provider.in_outage("gpu-s", b * bucket + off)
            for off in range(0, bucket, 100)
        }
        assert len(inside) == 1, f"bucket {b} mixed"  # all or nothing
        states.append(inside.pop())
    runs, run = [], 0
    for s in states + [False]:
        if s:
            run += 1
        elif run:
            runs.append(run)
            run = 0
    assert runs, "no outage at all in 400 buckets"
    assert min(runs) >= cfg.outage_len_buckets[0]  # a burst lasts >= 2 buckets
    assert 0.01 <= sum(states) / len(states) <= 0.25


def test_outages_are_stateless(make_conn):
    provider = FakeProvider(Config())
    times = list(range(0, 600 * 300, 600))
    forward = [provider.in_outage("gpu-m", t) for t in times]
    backward = [FakeProvider(Config()).in_outage("gpu-m", t) for t in reversed(times)]
    assert forward == backward[::-1]


# --- lifecycle ---------------------------------------------------------------


def test_due_transitions_happen_exactly_at_the_planned_time(make_conn):
    provider = FakeProvider(reliable(boot_min_s=240, boot_max_s=240))
    conn = make_conn()
    node_id = provider.launch_node(conn, "gpu-s", 0)

    assert provider.due_transitions(conn, 239) == []
    [row] = provider.due_transitions(conn, 240)
    assert (row["id"], row["due_at"]) == (node_id, 240)
    assert provider.apply_transition(conn, row) == "ready"
    assert provider.due_transitions(conn, 240) == []  # applied once
    assert provider.get(conn, node_id)["state"] == "ready"


def test_never_ready_node_fails_at_its_planned_time(make_conn):
    provider = FakeProvider(reliable(never_ready_prob=1.0))
    conn = make_conn()
    provider.launch_node(conn, "gpu-s", 0)
    [row] = provider.due_transitions(conn, 900)
    assert provider.apply_transition(conn, row) == "failed"
    state = provider.get(conn, row["id"])
    assert (state["state"], state["terminated_at"]) == ("failed", 900)


def test_next_transition_time_is_the_earliest_pending_one(make_conn):
    provider = FakeProvider(reliable(boot_min_s=300, boot_max_s=300))
    conn = make_conn()
    assert provider.next_transition_time(conn, 0) is None
    provider.launch_node(conn, "gpu-s", 0)
    provider.launch_node(conn, "gpu-s", 100)
    assert provider.next_transition_time(conn, 0) == 300
    assert provider.next_transition_time(conn, 300) == 400  # strictly after now


def test_terminate_marks_node_terminated(make_conn):
    provider = FakeProvider(reliable())
    conn = make_conn()
    node_id = provider.launch_node(conn, "gpu-s", 0)
    provider.terminate_node(conn, node_id, 900)
    state = provider.get(conn, node_id)
    assert (state["state"], state["terminated_at"]) == ("terminated", 900)


def test_terminate_an_ended_node_is_a_no_op(make_conn):
    provider = FakeProvider(reliable())
    conn = make_conn()
    node_id = provider.launch_node(conn, "gpu-s", 0)
    provider.terminate_node(conn, node_id, 900)
    provider.terminate_node(conn, node_id, 1200)
    assert provider.get(conn, node_id)["terminated_at"] == 900


def test_terminate_unknown_node_raises(make_conn):
    with pytest.raises(UnknownNode):
        FakeProvider(reliable()).terminate_node(make_conn(), "node-99999", 0)
