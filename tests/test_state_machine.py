"""P0-04: invalid transitions are rejected and broker / provider stay in step.

Job transitions are guarded by state.ALLOWED_TRANSITIONS. These tests cover
the paths that are not: the helpers that end a node or put a job on one, and
reconciliation of broker vs provider node states after a restart.
"""

import threading

import pytest
from fastapi.testclient import TestClient

from broker import engine, service, state
from broker.api import create_app
from broker.config import Config
from broker.db import connect, init_db, transaction
from broker.models import JobSubmit
from broker.provider import FakeProvider
from broker.state import Conflict
from replay.run_replay import InvariantViolation, check_invariants

CFG = Config()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "broker.db")
    init_db(c)
    yield c
    c.close()


def add_node(conn, node_id, *, state_="ready", provider_state="ready", team="vision",
             launched_at=0, terminated_at=None, gpus=1, node_type="gpu-s"):  # fmt: skip
    """A broker node and its provider twin (None: no provider row)."""
    ended = state_ in ("terminated", "failed")
    conn.execute(
        """INSERT INTO nodes (id, node_type, gpus_total, price_per_hour_micros, team,
               state, launched_at, ready_at, idle_since, terminated_at, end_reason)
           VALUES (?, ?, ?, 1000000, ?, ?, ?, ?, ?, ?, ?)""",
        (node_id, node_type, gpus, team, state_, launched_at,
         None if state_ == "launching" else launched_at + 300,
         None if state_ == "launching" else launched_at + 300,
         terminated_at if ended else None,
         ("idle_timeout" if state_ == "terminated" else "launch_failed") if ended else None),
    )  # fmt: skip
    if provider_state is not None:
        failed = provider_state == "failed"
        conn.execute(
            """INSERT INTO provider_nodes (id, node_type, requested_at, ready_at_planned,
                   fails_at_planned, state, terminated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (node_id, node_type, launched_at,
             None if failed else launched_at + 300,
             launched_at + 900 if failed else None,
             provider_state,
             terminated_at if provider_state in ("terminated", "failed") else None),
        )  # fmt: skip


def add_job(conn, job_id, *, team="vision", gpus=1, status="queued", node_id=None):
    conn.execute(
        """INSERT INTO jobs (id, team, request_fingerprint, gpus, estimated_minutes,
               priority, status, node_id, submitted_at, started_at)
           VALUES (?, ?, 'x', ?, 30, 'normal', ?, ?, 0, ?)""",
        (job_id, team, gpus, status, node_id, 0 if status == "running" else None),
    )


def job(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def node(conn, node_id):
    return conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()


def provider_node(conn, node_id):
    return conn.execute(
        "SELECT * FROM provider_nodes WHERE id = ?", (node_id,)
    ).fetchone()


def event_count(conn):
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def set_now(conn, ts):
    conn.execute("UPDATE sim_clock SET now_ts = ?", (ts,))


# --- ending a node -----------------------------------------------------------------


def test_ending_an_already_ended_node_is_rejected_and_changes_nothing(conn):
    with transaction(conn):
        add_node(conn, "n1", state_="terminated", provider_state="terminated",
                 terminated_at=1000)  # fmt: skip
    before = (tuple(node(conn, "n1")), event_count(conn))
    with pytest.raises(Conflict), transaction(conn):
        state.terminate_node(conn, FakeProvider(CFG), "n1", 5000,
                             "idle_timeout", "node_reaped")  # fmt: skip
    assert (tuple(node(conn, "n1")), event_count(conn)) == before  # bill end kept


def test_ending_a_node_with_a_running_job_is_rejected(conn):
    with transaction(conn):
        add_node(conn, "n1")
        add_job(conn, "j1", status="running", node_id="n1")
    with pytest.raises(Conflict), transaction(conn):
        state.terminate_node(conn, FakeProvider(CFG), "n1", 5000,
                             "idle_timeout", "node_reaped")  # fmt: skip
    assert node(conn, "n1")["state"] == "ready"
    assert provider_node(conn, "n1")["state"] == "ready"
    assert job(conn, "j1")["status"] == "running"


# --- putting a job on a node ---------------------------------------------------------


@pytest.mark.parametrize("setup, why", [
    ({"state_": "terminated", "provider_state": "terminated", "terminated_at": 100}, "ended node"),
    ({"state_": "launching", "provider_state": "pending"}, "node not ready"),
    ({"team": "nlp"}, "other team's node"),
])  # fmt: skip
def test_starting_a_job_on_an_unusable_node_is_rejected(conn, setup, why):
    with transaction(conn):
        add_node(conn, "n1", **setup)
        add_job(conn, "j1")
    with pytest.raises(Conflict), transaction(conn):
        state.start_job(conn, job(conn, "j1"), "n1", 400)
    assert job(conn, "j1")["status"] == "queued", why


def test_starting_a_job_that_does_not_fit_is_rejected(conn):
    with transaction(conn):
        add_node(conn, "n1", gpus=4, node_type="gpu-m")
        add_job(conn, "j1", gpus=4, status="running", node_id="n1")
        add_job(conn, "j2", gpus=1)
    with pytest.raises(Conflict), transaction(conn):
        state.start_job(conn, job(conn, "j2"), "n1", 400)
    assert job(conn, "j2")["status"] == "queued"


# --- restart reconcile with intermediate states ---------------------------------------


def test_reconcile_stops_the_bill_of_a_node_the_broker_already_ended(conn):
    # Broker says terminated, provider still bills it (a crash between a real
    # provider call and our commit, or a hand edit).
    with transaction(conn):
        add_node(conn, "n1", state_="terminated", provider_state="ready",
                 terminated_at=1000)  # fmt: skip
        set_now(conn, 2000)
    engine.reconcile_now(conn, CFG, FakeProvider(CFG))
    assert provider_node(conn, "n1")["state"] == "terminated"


def test_reconcile_catches_up_a_node_the_provider_already_made_ready(conn):
    # Broker still says launching, provider has made it ready.
    with transaction(conn):
        add_node(conn, "n1", state_="launching", provider_state="ready")
        set_now(conn, 2000)
    engine.reconcile_now(conn, CFG, FakeProvider(CFG))
    assert node(conn, "n1")["state"] == "ready"


# --- the invariant check sees broker / provider disagreement ---------------------------


@pytest.mark.parametrize("broker_state, provider_state", [
    ("ready", "terminated"),
    ("launching", "ready"),
    ("terminated", "ready"),
    ("ready", None),  # the provider never heard of it
])  # fmt: skip
def test_the_invariant_check_catches_broker_provider_disagreement(
    conn, broker_state, provider_state
):
    with transaction(conn):
        add_node(conn, "n1", state_=broker_state, provider_state=provider_state,
                 terminated_at=1000)  # fmt: skip
    with pytest.raises(InvariantViolation, match="provider"):
        check_invariants(conn, CFG)


# --- lifecycles through the real code paths ---------------------------------------------

RELIABLE = Config(never_ready_prob=0.0, outage_start_prob=0.0)


def submit(conn, cfg=RELIABLE, gpus=1, team="vision"):
    created, _ = service.submit_job(
        conn, JobSubmit(team=team, gpus=gpus, estimated_minutes=30)
    )
    engine.run_pass(conn, cfg, FakeProvider(cfg))
    return created["id"]


def events_for(conn, entity_id):
    return [
        r[0]
        for r in conn.execute(
            "SELECT event_type FROM events WHERE entity_id = ? ORDER BY id",
            (entity_id,),
        )
    ]


def only_node(conn):
    return conn.execute("SELECT * FROM nodes ORDER BY id").fetchone()


def test_normal_lifecycle_submit_boot_run_succeed_reap(conn):
    cfg, provider = RELIABLE, FakeProvider(RELIABLE)
    jid = submit(conn)
    n = only_node(conn)
    assert (job(conn, jid)["status"], n["state"]) == ("queued", "launching")
    assert provider_node(conn, n["id"])["state"] == "pending"
    check_invariants(conn, cfg)

    engine.advance_to(conn, cfg.boot_max_s, cfg, provider)
    assert job(conn, jid)["status"] == "running"
    assert node(conn, n["id"])["state"] == "ready"
    check_invariants(conn, cfg)

    service.complete_job(conn, jid, "succeeded")
    engine.advance_to(conn, 3 * 3600, cfg, provider)
    assert job(conn, jid)["status"] == "succeeded"
    assert node(conn, n["id"])["state"] == "terminated"
    assert provider_node(conn, n["id"])["state"] == "terminated"
    check_invariants(conn, cfg)
    assert events_for(conn, jid) == ["job_submitted", "job_started", "job_succeeded"]
    assert events_for(conn, n["id"]) == ["node_launched", "node_ready", "node_reaped"]


def test_a_finished_job_frees_its_gpus_but_keeps_its_node_for_cost(conn):
    cfg = RELIABLE
    jid = submit(conn, gpus=4)
    engine.advance_to(conn, cfg.boot_max_s, cfg, FakeProvider(cfg))
    nid = job(conn, jid)["node_id"]
    assert state.used_gpus(conn, nid) == 4
    service.complete_job(conn, jid, "failed")
    assert state.used_gpus(conn, nid) == 0
    assert job(conn, jid)["node_id"] == nid  # history: cost attribution uses it
    assert node(conn, nid)["idle_since"] is not None


def test_cancel_before_start(conn):
    jid = submit(conn)
    out = service.cancel_job(conn, jid)
    assert out["status"] == "cancelled"
    row = job(conn, jid)
    assert row["node_id"] is None and row["started_at"] is None
    engine.advance_to(conn, RELIABLE.boot_max_s, RELIABLE, FakeProvider(RELIABLE))
    assert job(conn, jid)["status"] == "cancelled"  # never rescheduled
    assert events_for(conn, jid) == ["job_submitted", "job_cancelled"]


def started_job(conn):
    jid = submit(conn)
    engine.advance_to(conn, RELIABLE.boot_max_s, RELIABLE, FakeProvider(RELIABLE))
    assert job(conn, jid)["status"] == "running"
    return jid


def test_duplicate_complete_is_rejected_and_writes_no_event(conn):
    jid = started_job(conn)
    service.complete_job(conn, jid, "succeeded")
    n = event_count(conn)
    for outcome in ("succeeded", "failed"):
        with pytest.raises(Conflict):
            service.complete_job(conn, jid, outcome)
    assert event_count(conn) == n
    assert job(conn, jid)["status"] == "succeeded"


def test_duplicate_cancel_is_a_no_op_and_writes_no_event(conn):
    jid = started_job(conn)
    service.cancel_job(conn, jid)
    n = event_count(conn)
    assert service.cancel_job(conn, jid)["status"] == "cancelled"
    assert event_count(conn) == n


@pytest.mark.parametrize(
    "first, second", [("complete", "cancel"), ("cancel", "complete")]
)
def test_complete_and_cancel_in_either_order_end_the_job_once(conn, first, second):
    jid = started_job(conn)
    calls = {"complete": lambda: service.complete_job(conn, jid, "succeeded"),
             "cancel": lambda: service.cancel_job(conn, jid)}  # fmt: skip
    calls[first]()
    with pytest.raises(Conflict):
        calls[second]()
    ends = [e for e in events_for(conn, jid) if e in ("job_succeeded", "job_cancelled")]
    assert ends == [{"complete": "job_succeeded", "cancel": "job_cancelled"}[first]]


def test_concurrent_complete_and_cancel_end_the_job_exactly_once(tmp_path):
    # Two connections. The main thread holds the write lock while both
    # requests are started, so both are queued behind it (BEGIN IMMEDIATE +
    # busy_timeout); which one SQLite lets in first is not ours to fix, so the
    # assertions hold for either order.
    path = tmp_path / "race.db"
    c = connect(path)
    init_db(c)
    jid = started_job(c)
    results = {}

    def run(name, fn):
        conn = connect(path)
        try:
            results[name] = fn(conn)["status"]
        except Conflict:
            results[name] = "conflict"
        finally:
            conn.close()

    lock = connect(path)
    lock.execute("BEGIN IMMEDIATE")
    threads = [
        threading.Thread(target=run, args=("complete",
                         lambda k: service.complete_job(k, jid, "succeeded"))),
        threading.Thread(target=run, args=("cancel", lambda k: service.cancel_job(k, jid))),
    ]  # fmt: skip
    for t in threads:
        t.start()
    lock.execute("COMMIT")
    lock.close()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads)

    final = job(c, jid)["status"]
    ends = [e for e in events_for(c, jid) if e in ("job_succeeded", "job_cancelled")]
    assert final in ("succeeded", "cancelled") and ends == [f"job_{final}"]
    winner = "complete" if final == "succeeded" else "cancel"
    loser = "cancel" if winner == "complete" else "complete"
    assert results == {winner: final, loser: "conflict"}
    c.close()


def test_invalid_transitions_are_rejected_and_not_saved(conn):
    jid = submit(conn)
    before = (tuple(job(conn, jid)), event_count(conn))
    with pytest.raises(Conflict):
        service.complete_job(conn, jid, "succeeded")  # queued -> succeeded
    assert (tuple(job(conn, jid)), event_count(conn)) == before
    service.cancel_job(conn, jid)
    with pytest.raises(Conflict), transaction(conn):
        state.start_job(conn, job(conn, jid), only_node(conn)["id"], 0)  # terminal
    assert job(conn, jid)["status"] == "cancelled"


def test_launch_failure_keeps_the_job_queued_and_launches_again(conn):
    cfg = Config(never_ready_prob=0.0, outage_start_prob=0.0, never_ready_launches=(1,))
    jid = submit(conn, cfg)
    first = only_node(conn)["id"]
    engine.advance_to(conn, cfg.never_ready_after_s, cfg, FakeProvider(cfg))
    assert node(conn, first)["state"] == "failed"
    assert provider_node(conn, first)["state"] == "failed"
    assert node(conn, first)["end_reason"] == "launch_failed"
    assert job(conn, jid)["status"] == "queued"  # never placed on the failed node
    engine.advance_to(conn, 2 * 3600, cfg, FakeProvider(cfg))
    row = job(conn, jid)
    assert row["status"] == "running" and row["node_id"] != first
    check_invariants(conn, cfg)


def test_restart_mid_boot_reconciles_nothing_and_resumes(tmp_path):
    path = tmp_path / "restart.db"
    with TestClient(create_app(path, config=RELIABLE)) as client:
        jid = client.post(
            "/jobs", json={"team": "vision", "gpus": 1, "estimated_minutes": 30}
        ).json()["id"]
        client.post("/sim/advance", json={"minutes": 2})  # node still booting
    c = connect(path)
    before = event_count(c)
    assert only_node(c)["state"] == "launching"
    c.close()

    with TestClient(create_app(path, config=RELIABLE)) as client:  # restart
        c = connect(path)
        assert event_count(c) == before  # consistent: startup reconcile did nothing
        check_invariants(c, RELIABLE)
        client.post("/sim/advance", json={"minutes": 10})
        assert job(c, jid)["status"] == "running"
        check_invariants(c, RELIABLE)
        c.close()
