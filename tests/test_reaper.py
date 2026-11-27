"""Idle reaper: terminate a ready, empty node once it is eligible.

SPEC_DECISIONS.md #1 and #5:

    eligible_at = max(launched_at + 600, idle_since + N*60 + 1)

"More than N minutes" in integer seconds: idle since t, the node is still
alive at t + N*60 and terminated at t + N*60 + 1, provided it has lived the
600 s minimum lifetime. Most tests here use nodes launched at 0 and idle from
600 or later, so only the idle rule decides; the lifetime tests say so.
"""

import pytest

from broker import costs, engine, reaper, service
from broker.config import GPUS, PRICE_MICROS, Config
from broker.db import connect, init_db, transaction
from broker.models import JobSubmit
from broker.provider import FakeProvider
from broker.service import fingerprint

N = 5  # minutes, as in the YAML reaper cases
T = N * 60


def config(**overrides) -> Config:
    return Config(**{"idle_minutes": N, "never_ready_prob": 0.0, "outage_start_prob": 0.0,
                     "boot_min_s": 300, "boot_max_s": 300, **overrides})  # fmt: skip


def advance(conn, ts, **overrides):
    engine.advance_to(conn, ts, config(**overrides))


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "broker.db")
    init_db(c)
    yield c
    c.close()


def add_node(conn, node_id="node-a", team="vision", node_type="gpu-s", state="ready",
             idle_since=600, launched_at=0, **f):  # fmt: skip
    row = {"id": node_id, "node_type": node_type, "gpus_total": GPUS[node_type],
           "price_per_hour_micros": PRICE_MICROS[node_type], "team": team, "state": state,
           "launched_at": launched_at, "ready_at": launched_at if state == "ready" else None,
           "idle_since": idle_since, **f}  # fmt: skip
    with transaction(conn):
        cols, marks = ", ".join(row), ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO nodes ({cols}) VALUES ({marks})", list(row.values()))


_ids = iter(range(1, 10_000))


def add_job(
    conn, team="vision", gpus=1, status="queued", node_id=None, started_at=None
):
    job_id = f"job-{next(_ids):012x}"
    with transaction(conn):
        conn.execute(
            """INSERT INTO jobs (id, team, request_fingerprint, gpus, estimated_minutes,
                   priority, status, node_id, submitted_at, started_at)
               VALUES (?, ?, ?, ?, 30, 'normal', ?, ?, 0, ?)""",
            (job_id, team, fingerprint(team, gpus, 30, "normal"), gpus, status,
             node_id, started_at),
        )  # fmt: skip
    return job_id


def reap_now(conn, now, **overrides):
    cfg = config(**overrides)
    with transaction(conn):
        return reaper.reap(conn, now, cfg, FakeProvider(cfg))


def node(conn, node_id="node-a"):
    return conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()


def events(conn, event_type):
    return conn.execute(
        "SELECT * FROM events WHERE event_type = ? ORDER BY id", (event_type,)
    ).fetchall()


# --- the timeout boundary --------------------------------------------------------


def test_idle_node_is_alive_at_exactly_the_timeout(conn):
    add_node(conn, idle_since=600)
    advance(conn, 600 + T)
    assert node(conn)["state"] == "ready"


def test_idle_node_is_reaped_one_second_after_the_timeout(conn):
    add_node(conn, idle_since=600)
    advance(conn, 600 + T + 1)
    n = node(conn)
    assert (n["state"], n["terminated_at"], n["end_reason"]) == (
        "terminated", 600 + T + 1, "idle_timeout")  # fmt: skip
    [event] = events(conn, "node_reaped")
    assert (event["ts"], event["entity_id"]) == (600 + T + 1, "node-a")


def test_reaped_at_its_exact_second_even_in_one_big_advance(conn):
    add_node(conn, idle_since=1000)
    advance(conn, 24 * 3600)
    assert node(conn)["terminated_at"] == 1000 + T + 1  # not the target time


def test_timeout_counts_from_idle_since_not_from_ready_at(conn):
    add_node(conn, idle_since=1000)  # ready since 0, idle only since 1000
    advance(conn, 1000 + T)
    assert node(conn)["state"] == "ready"
    advance(conn, 1000 + T + 1)
    assert node(conn)["state"] == "terminated"


# --- the minimum lifetime (decision 1) --------------------------------------------


def test_timeout_before_lifetime_waits_for_the_lifetime(conn):
    add_node(conn, idle_since=0)  # idle-eligible at 301, lifetime at 600
    advance(conn, 599)
    assert node(conn)["state"] == "ready"
    advance(conn, 3600)
    assert node(conn)["terminated_at"] == 600


def test_lifetime_before_timeout_waits_for_the_timeout(conn):
    add_node(conn, idle_since=500)  # lifetime at 600, idle-eligible at 801
    advance(conn, 800)
    assert node(conn)["state"] == "ready"
    advance(conn, 3600)
    assert node(conn)["terminated_at"] == 801


def test_both_rules_due_at_the_same_second(conn):
    add_node(conn, idle_since=299)  # 299 + 301 = 600 = launched_at + 600
    advance(conn, 599)
    assert node(conn)["state"] == "ready"
    advance(conn, 600)
    assert node(conn)["terminated_at"] == 600


def test_lifetime_counts_from_launch_not_from_ready(conn):
    add_node(conn, idle_since=900, launched_at=500)  # ready at 500 here
    advance(conn, 3600)
    assert node(conn)["terminated_at"] == 900 + T + 1  # 1201 > 500 + 600


def test_minimum_lifetime_is_configurable(conn):
    add_node(conn, idle_since=0)
    advance(conn, 3600, min_node_lifetime_s=0)
    assert node(conn)["terminated_at"] == T + 1


@pytest.mark.parametrize("now, eligible", [(599, False), (600, True)])
def test_eligible_at_formula(conn, now, eligible):
    add_node(conn, idle_since=0)
    assert reap_now(conn, now) is eligible


# --- what is never reaped --------------------------------------------------------


def test_busy_node_is_never_reaped(conn):
    add_node(conn, node_type="gpu-m", idle_since=None)
    add_job(conn, gpus=2, status="running", node_id="node-a", started_at=0)
    advance(conn, 24 * 3600)
    assert node(conn)["state"] == "ready"
    assert events(conn, "node_reaped") == []


def test_launching_node_is_never_reaped(conn):
    add_node(conn, state="launching", idle_since=None)
    assert reap_now(conn, 24 * 3600) is False
    assert node(conn)["state"] == "launching"


def test_not_reaped_while_a_queued_same_team_job_fits_on_it(conn):
    # Normally the scheduler places such a job first. The reaper checks too,
    # so it can never take a node a waiting job is about to get (decision 5).
    add_node(conn, idle_since=0)
    add_job(conn, team="vision", gpus=1)
    assert reap_now(conn, 3600) is False
    assert node(conn)["state"] == "ready"


@pytest.mark.parametrize(
    "team, gpus", [("nlp", 1), ("vision", 4)], ids=["other-team", "too-big"]
)
def test_reaped_when_the_queued_job_could_not_use_it(conn, team, gpus):
    add_node(conn, idle_since=0)  # gpu-s, vision
    add_job(conn, team=team, gpus=gpus)
    assert reap_now(conn, 3600) is True
    assert node(conn)["state"] == "terminated"


def test_a_protected_node_gets_the_job_in_the_same_group(conn):
    # Reaper then scheduler, in one timestamp group: the job starts, not a reap.
    # The node is already past eligible_at (901) when the group runs.
    add_node(conn, idle_since=600)
    j = add_job(conn, team="vision", gpus=1)
    with transaction(conn):
        conn.execute("UPDATE sim_clock SET now_ts = 2000")
    engine.run_pass(conn, config())
    assert node(conn)["state"] == "ready"
    started = conn.execute(
        "SELECT node_id, started_at FROM jobs WHERE id = ?", (j,)
    ).fetchone()
    assert tuple(started) == ("node-a", 2000)
    assert events(conn, "node_reaped") == []


def test_reaper_off_never_reaps(conn):
    add_node(conn, idle_since=0)
    advance(conn, 24 * 3600, reaper_enabled=False)
    assert node(conn)["state"] == "ready"


# --- idle_since is kept right by the job lifecycle ------------------------------


def test_job_start_stops_the_idle_clock_and_its_end_restarts_it(conn):
    add_node(conn, idle_since=0)
    cfg = config()
    job, _ = service.submit_job(
        conn, JobSubmit(team="vision", gpus=1, estimated_minutes=30)
    )
    with transaction(conn):  # plan: the job runs 10 minutes from its start at 0
        conn.execute(
            "INSERT INTO sim_job_plans (job_id, actual_minutes, outcome) VALUES (?, 10, 'succeeded')",
            (job["id"],),
        )
    engine.run_pass(conn, cfg)
    engine.advance_to(conn, 24 * 3600, cfg)
    assert node(conn)["terminated_at"] == 600 + T + 1


def test_node_ready_with_no_job_is_idle_from_ready_at(conn):
    # A job triggers a launch, then is cancelled while the node boots: the
    # node becomes ready at 300 with nothing to run, so it is idle from 300.
    cfg = config()
    job, _ = service.submit_job(
        conn, JobSubmit(team="vision", gpus=1, estimated_minutes=30)
    )
    engine.run_pass(conn, cfg)
    service.cancel_job(conn, job["id"])
    engine.advance_to(conn, 24 * 3600, cfg)
    [n] = conn.execute("SELECT * FROM nodes").fetchall()
    assert (n["ready_at"], n["idle_since"]) == (300, 300)
    assert (n["state"], n["terminated_at"]) == ("terminated", 300 + T + 1)  # 601 > 600


def test_reaping_survives_a_restart(tmp_path):
    path = tmp_path / "broker.db"
    conn = connect(path)
    init_db(conn)
    add_node(conn, idle_since=600)
    advance(conn, 700)
    conn.close()
    conn = connect(path)  # new connection, new provider: idle_since is in the DB
    advance(conn, 3600)
    assert node(conn)["terminated_at"] == 600 + T + 1
    conn.close()


# --- billing ------------------------------------------------------------------------


def test_billing_stops_when_the_node_is_reaped(conn):
    add_node(conn, idle_since=0)  # gpu-s, $1.00/hour, launched at 0
    advance(conn, 2 * 3600, idle_minutes=10)
    reaped_at = 10 * 60 + 1
    assert node(conn)["terminated_at"] == reaped_at
    report = costs.cost_by_team(conn, 0, 2 * 3600, now=2 * 3600)
    # 601 billed seconds (over the 10-minute minimum), all idle, nothing after.
    assert report.teams["vision"].total == 1_000_000 * reaped_at // 3600
    assert report.teams["vision"].idle == report.teams["vision"].total
    later = costs.cost_by_team(conn, reaped_at, 2 * 3600, now=2 * 3600)
    assert later.grand_total == 0


def test_idle_time_before_the_reap_is_billed_to_the_team(conn):
    add_node(conn, idle_since=None)  # gpu-s, $1.00/hour, launched 0
    j = add_job(conn, gpus=1, status="running", node_id="node-a", started_at=0)
    with transaction(conn):  # the job runs 0 .. 1200
        conn.execute(
            "INSERT INTO sim_job_plans (job_id, actual_minutes, outcome) VALUES (?, 20, 'succeeded')",
            (j,),
        )
    advance(conn, 2 * 3600)
    assert node(conn)["terminated_at"] == 1200 + T + 1  # idle 1200 .. 1501
    vision = costs.cost_by_team(conn, 0, 2 * 3600, now=2 * 3600).teams["vision"]
    assert vision.total == 1_000_000 * 1501 // 3600
    assert abs(vision.busy - 1_000_000 * 1200 / 3600) <= 1
    assert abs(vision.idle - 1_000_000 * 301 / 3600) <= 1  # the idle tail is charged


def test_reaping_writes_reason_and_event_details(conn):
    add_node(conn, idle_since=600)
    advance(conn, 3600)
    import json

    [event] = events(conn, "node_reaped")
    assert json.loads(event["details"])["idle_minutes"] == N
    assert node(conn)["end_reason"] == "idle_timeout"


# --- configuration ------------------------------------------------------------------


def test_default_idle_timeout_is_10_minutes():
    assert Config().idle_minutes == 10
    assert Config().min_node_lifetime_s == 600
    assert Config().reaper_enabled is True


@pytest.mark.parametrize("name", ["BROKER_IDLE_MINUTES", "BROKER_IDLE_TIMEOUT_MINUTES"])
def test_idle_timeout_from_environment(monkeypatch, name):
    monkeypatch.setenv(name, "3")
    assert Config.from_env().idle_minutes == 3


@pytest.mark.parametrize("bad", [{"idle_minutes": -1}, {"max_nodes": 0},
                                 {"min_node_lifetime_s": -1}, {"policy": "random"}])  # fmt: skip
def test_invalid_config_is_rejected(bad):
    with pytest.raises(ValueError):
        Config(**bad)
