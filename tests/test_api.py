"""HTTP API tests (sub-step A: jobs, lifecycle, queue, sim clock, health).

Each test gets a fresh database file under tmp_path. TestClient is used as a
context manager so the app's startup hook (init_db) runs.
"""

import pytest
from fastapi.testclient import TestClient

from broker.api import create_app
from broker.config import Config
from broker.db import connect, transaction

# These tests check the API contract on a fixed world: no node is launched
# or reaped unless a test does it itself. Launching and reaping are tested in
# test_scheduler.py, test_engine.py, test_reaper.py and the YAML cases.
FIXED_NODES = Config(scheduler_enabled=False, reaper_enabled=False)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "broker.db"


@pytest.fixture
def client(db_path):
    with TestClient(create_app(db_path, config=FIXED_NODES)) as c:
        yield c


def submit(client, **overrides):
    body = {"team": "vision", "gpus": 1, "estimated_minutes": 30}
    body.update(overrides)
    return client.post("/jobs", json=body)


# --- SQL helpers: the scheduler doesn't exist yet, so tests place jobs by hand.


def add_node(db_path, node_id="node-1", team="vision"):
    conn = connect(db_path)
    with transaction(conn):
        conn.execute(
            """INSERT INTO nodes (id, node_type, gpus_total, price_per_hour_micros,
                                  team, state, launched_at, ready_at, idle_since)
               VALUES (?, 'gpu-m', 4, 4500000, ?, 'ready', 0, 0, 0)""",
            (node_id, team),
        )
    conn.close()


def start_job_on_node(db_path, job_id, node_id="node-1"):
    conn = connect(db_path)
    with transaction(conn):
        conn.execute(
            """UPDATE jobs SET status = 'running', node_id = ?, started_at = submitted_at
               WHERE id = ?""",
            (node_id, job_id),
        )
        conn.execute("UPDATE nodes SET idle_since = NULL WHERE id = ?", (node_id,))
    conn.close()


def node_idle_since(db_path, node_id="node-1"):
    conn = connect(db_path)
    value = conn.execute(
        "SELECT idle_since FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()[0]
    conn.close()
    return value


def event_count(db_path):
    conn = connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    return n


# --- health and submit -------------------------------------------------------


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_submit_then_get_returns_same_queued_job(client):
    r = submit(client)
    assert r.status_code == 201
    job = r.json()
    assert job["status"] == "queued"
    assert job["id"].startswith("job-")

    got = client.get(f"/jobs/{job['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == job["id"]
    assert got.json()["status"] == "queued"


def test_submit_strips_team_whitespace(client):
    assert submit(client, team="  vision ").json()["team"] == "vision"


def test_submit_writes_an_event(client, db_path):
    submit(client)
    assert event_count(db_path) == 1


# --- idempotency -------------------------------------------------------------


def test_same_key_same_body_twice_returns_one_job(client):
    first = submit(client, idempotency_key="k1")
    second = submit(client, idempotency_key="k1")
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert len(client.get("/queue").json()) == 1


def test_omitted_priority_equals_normal_for_idempotency(client):
    first = submit(client, idempotency_key="k1")
    second = submit(client, idempotency_key="k1", priority="normal")
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_same_key_different_body_is_409(client):
    submit(client, idempotency_key="k1", gpus=1)
    r = submit(client, idempotency_key="k1", gpus=2)
    assert r.status_code == 409
    assert r.json()["detail"] == "idempotency key reused with different request"


def test_same_key_under_another_team_creates_new_job(client):
    a = submit(client, team="vision", idempotency_key="k1")
    b = submit(client, team="speech", idempotency_key="k1")
    assert b.status_code == 201
    assert b.json()["id"] != a.json()["id"]


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"gpus": 3},
        {"priority": "urgent"},
        {"team": "  "},
        {"gpus": "4"},  # strict: no string-to-int coercion
        {"gpus": True},  # strict: a bool is not a GPU count
        {"estimated_minutes": 0},
        {"idempotency_key": " "},
        {"prioirty": "high"},  # unknown field: a typo must not silently default
        {"estimated_minutes": 10081},  # more than 7 days
        {"team": "x" * 129},  # longer than 128 characters
    ],
    ids=[
        "gpus_3",
        "priority_urgent",
        "blank_team",
        "gpus_string",
        "gpus_bool",
        "minutes_0",
        "blank_key",
        "unknown_field",
        "minutes_over_7_days",
        "team_129_chars",
    ],
)
def test_invalid_submit_is_422(client, overrides):
    assert submit(client, **overrides).status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [{"estimated_minutes": 10080}, {"team": "x" * 128}],
    ids=["minutes_exactly_7_days", "team_exactly_128_chars"],
)
def test_submit_at_the_limits_is_accepted(client, overrides):
    assert submit(client, **overrides).status_code == 201


# --- lifecycle ---------------------------------------------------------------


def test_cancel_queued_job_then_cancel_again_is_noop(client, db_path):
    job_id = submit(client).json()["id"]

    r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert r.json()["end_reason"] == "user_cancel"
    events_after_first_cancel = event_count(db_path)

    again = client.post(f"/jobs/{job_id}/cancel")
    assert again.status_code == 200
    assert again.json() == r.json()
    assert event_count(db_path) == events_after_first_cancel  # no-op: no event


def test_complete_queued_job_is_409(client):
    job_id = submit(client).json()["id"]
    r = client.post(f"/jobs/{job_id}/complete", json={"outcome": "succeeded"})
    assert r.status_code == 409


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("get", "/jobs/job-missing", None),
        ("post", "/jobs/job-missing/cancel", None),
        ("post", "/jobs/job-missing/complete", {"outcome": "succeeded"}),
    ],
    ids=["get", "cancel", "complete"],
)
def test_unknown_job_is_404(client, method, path, body):
    r = client.request(method.upper(), path, json=body)
    assert r.status_code == 404


def test_complete_running_job_succeeds_and_node_becomes_idle(client, db_path):
    add_node(db_path)
    job_id = submit(client).json()["id"]
    start_job_on_node(db_path, job_id)
    client.post("/sim/advance", json={"minutes": 5})

    r = client.post(f"/jobs/{job_id}/complete", json={"outcome": "succeeded"})
    assert r.status_code == 200
    assert r.json()["status"] == "succeeded"
    assert r.json()["ended_at"] == 300
    assert node_idle_since(db_path) == 300


def test_cancel_one_of_two_running_jobs_keeps_node_busy(client, db_path):
    add_node(db_path)
    a = submit(client).json()["id"]
    b = submit(client).json()["id"]
    start_job_on_node(db_path, a)
    start_job_on_node(db_path, b)

    r = client.post(f"/jobs/{a}/cancel")
    assert r.json()["status"] == "cancelled"
    assert node_idle_since(db_path) is None


def test_complete_finished_job_again_is_409(client, db_path):
    add_node(db_path)
    job_id = submit(client).json()["id"]
    start_job_on_node(db_path, job_id)
    client.post(f"/jobs/{job_id}/complete", json={"outcome": "failed"})

    assert client.post(f"/jobs/{job_id}/cancel").status_code == 409
    again = client.post(f"/jobs/{job_id}/complete", json={"outcome": "succeeded"})
    assert again.status_code == 409


# --- wait_seconds ------------------------------------------------------------


def test_wait_seconds_grows_while_queued(client):
    job_id = submit(client).json()["id"]
    client.post("/sim/advance", json={"minutes": 10})
    assert client.get(f"/jobs/{job_id}").json()["wait_seconds"] == 600


def test_wait_seconds_when_cancelled_before_start_is_time_until_cancel(client):
    # Rule changed by the user: a job cancelled while queued waited from
    # submission until cancellation (was: null).
    job_id = submit(client).json()["id"]
    client.post("/sim/advance", json={"minutes": 10})
    client.post(f"/jobs/{job_id}/cancel")
    client.post("/sim/advance", json={"minutes": 5})  # later time must not count
    assert client.get(f"/jobs/{job_id}").json()["wait_seconds"] == 600


# --- queue -------------------------------------------------------------------


def test_queue_orders_by_priority_then_submit_time(client):
    a = submit(client, priority="normal").json()["id"]  # t=0
    client.post("/sim/advance", json={"minutes": 1})
    b = submit(client, priority="low").json()["id"]  # t=60
    c = submit(client, priority="high").json()["id"]  # t=60
    client.post("/sim/advance", json={"minutes": 1})
    d = submit(client, priority="normal").json()["id"]  # t=120

    queue = client.get("/queue").json()
    assert [j["id"] for j in queue] == [c, a, d, b]
    assert [j["position"] for j in queue] == [1, 2, 3, 4]
    assert queue[1]["wait_seconds"] == 120  # job a, submitted at 0, now 120


def test_queue_excludes_cancelled_jobs(client):
    job_id = submit(client).json()["id"]
    client.post(f"/jobs/{job_id}/cancel")
    assert client.get("/queue").json() == []


# --- sim clock ---------------------------------------------------------------


def test_sim_now_starts_at_epoch_zero(client):
    assert client.get("/sim/now").json() == {
        "now_ts": 0,
        "now_iso": "1970-01-01T00:00:00Z",
    }


def test_sim_advance_by_minutes(client):
    r = client.post("/sim/advance", json={"minutes": 5})
    assert r.status_code == 200
    assert r.json()["now_ts"] == 300


def test_sim_advance_to_ts_in_the_past_is_409(client):
    client.post("/sim/advance", json={"to_ts": 600})
    r = client.post("/sim/advance", json={"to_ts": 100})
    assert r.status_code == 409
    assert client.get("/sim/now").json()["now_ts"] == 600


def test_sim_advance_to_now_is_noop(client, db_path):
    client.post("/sim/advance", json={"to_ts": 600})
    events_before = event_count(db_path)
    r = client.post("/sim/advance", json={"to_ts": 600})
    assert r.status_code == 200
    assert r.json()["now_ts"] == 600
    assert event_count(db_path) == events_before


@pytest.mark.parametrize(
    "body",
    [{"minutes": 5, "to_ts": 600}, {}, {"minutes": -1}],
    ids=["both", "neither", "minutes_negative"],
)
def test_sim_advance_invalid_body_is_422(client, body):
    assert client.post("/sim/advance", json=body).status_code == 422


def test_sim_advance_zero_minutes_is_noop(client, db_path):
    # Rule changed by the user: {"minutes": 0} is a 200 no-op (was: 422).
    client.post("/sim/advance", json={"to_ts": 600})
    events_before = event_count(db_path)
    r = client.post("/sim/advance", json={"minutes": 0})
    assert r.status_code == 200
    assert r.json()["now_ts"] == 600
    assert event_count(db_path) == events_before


# --- restart safety ----------------------------------------------------------


def test_new_app_on_same_db_keeps_jobs_and_time(db_path):
    with TestClient(create_app(db_path, config=FIXED_NODES)) as first:
        a = submit(first).json()
        b = submit(first, team="speech", gpus=4).json()
        first.post("/sim/advance", json={"minutes": 15})

    with TestClient(create_app(db_path, config=FIXED_NODES)) as second:
        assert second.get("/sim/now").json()["now_ts"] == 900
        assert second.get(f"/jobs/{a['id']}").json()["team"] == "vision"
        assert second.get(f"/jobs/{b['id']}").json()["gpus"] == 4
        # A set, not a list: ordering has its own test.
        assert {j["id"] for j in second.get("/queue").json()} == {a["id"], b["id"]}


# --- sub-step B: nodes view and cost report ----------------------------------


def add_failed_node(db_path, node_id="node-bad", team="speech"):
    conn = connect(db_path)
    with transaction(conn):
        conn.execute(
            """INSERT INTO nodes (id, node_type, gpus_total, price_per_hour_micros,
                                  team, state, launched_at, terminated_at, end_reason)
               VALUES (?, 'gpu-s', 1, 1000000, ?, 'failed', 0, 900, 'launch_failed')""",
            (node_id, team),
        )
    conn.close()


def run_spec_example(client, db_path):
    """gpu-m at 4.50/h alive 1 hour; one 2-GPU job runs for its first 30 min."""
    add_node(db_path)
    job_id = submit(client, gpus=2).json()["id"]
    start_job_on_node(db_path, job_id)
    client.post("/sim/advance", json={"minutes": 30})
    client.post(f"/jobs/{job_id}/complete", json={"outcome": "succeeded"})
    client.post("/sim/advance", json={"minutes": 30})


def test_cost_report_spec_example(client, db_path):
    run_spec_example(client, db_path)

    r = client.get("/reports/cost")
    assert r.status_code == 200
    report = r.json()
    assert report["teams"] == [
        {
            "team": "vision",
            "gpu_hours": 1.0,
            "busy_cost": 1.13,
            "idle_cost": 3.38,
            "total_cost": 4.5,
        }
    ]
    assert report["provider_overhead"] == 0.0
    assert report["grand_total"] == 4.5
    assert (report["from_ts"], report["to_ts"]) == (0, 3600)


def test_cost_report_failed_node_is_provider_overhead(client, db_path):
    add_failed_node(db_path)
    client.post("/sim/advance", json={"minutes": 60})

    report = client.get("/reports/cost").json()
    assert report["teams"] == []
    assert report["provider_overhead"] == 0.25  # 900 s at 1.00/h
    assert report["grand_total"] == 0.25


@pytest.mark.parametrize("query", ["from_ts=100&to_ts=100", "from_ts=200&to_ts=100"])
def test_cost_report_empty_or_reversed_window_is_422(client, query):
    assert client.get(f"/reports/cost?{query}").status_code == 422


def test_nodes_shows_derived_gpu_usage(client, db_path):
    add_node(db_path)
    a = submit(client, gpus=2).json()["id"]
    b = submit(client, gpus=1).json()["id"]
    start_job_on_node(db_path, a)
    start_job_on_node(db_path, b)

    [node] = client.get("/nodes").json()
    assert node["type"] == "gpu-m"
    assert (node["gpus_total"], node["gpus_used"], node["gpus_free"]) == (4, 3, 1)
    assert node["running_job_ids"] == sorted([a, b])
    assert node["idle_since"] is None


def test_nodes_hides_ended_nodes_unless_asked(client, db_path):
    add_node(db_path)
    add_failed_node(db_path)
    client.post("/sim/advance", json={"minutes": 60})

    assert [n["id"] for n in client.get("/nodes").json()] == ["node-1"]
    shown = client.get("/nodes?include_terminated=true").json()
    assert {n["id"] for n in shown} == {"node-1", "node-bad"}


def test_nodes_cost_so_far_equals_cost_report(client, db_path):
    run_spec_example(client, db_path)

    [node] = client.get("/nodes").json()
    [team] = client.get("/reports/cost").json()["teams"]
    assert node["cost_so_far"] == team["total_cost"] == 4.5


# --- a failing scheduling pass after the commit -----------------------------------
#
# POST /jobs, cancel and complete commit their change first, then run one
# engine pass. If that pass fails, its own transaction rolls back and the
# committed change stands. The client must be told about the change it made
# (not a 500), or a retry without an idempotency key would submit twice. The
# skipped pass is not lost: the next request or /sim/advance runs it again.

LIVE = Config(never_ready_prob=0.0, outage_start_prob=0.0)


@pytest.fixture
def live_client(db_path):
    with TestClient(create_app(db_path, config=LIVE)) as c:
        yield c


@pytest.fixture
def failing_pass(monkeypatch):
    monkeypatch.setattr("broker.engine.run_pass", _raise)
    return monkeypatch


def test_submit_returns_the_saved_job_when_the_pass_fails(
    live_client, db_path, failing_pass, caplog
):
    r = submit(live_client)
    assert r.status_code == 201
    assert r.json()["status"] == "queued"
    assert "simulated pass failure" in caplog.text  # logged, not hidden
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
    conn.close()

    failing_pass.undo()  # the next pass schedules the job
    live_client.post("/sim/advance", json={"minutes": 0})
    assert len(live_client.get("/nodes").json()) == 1


def test_cancel_returns_the_cancelled_job_when_the_pass_fails(live_client, monkeypatch):
    job_id = submit(live_client).json()["id"]
    monkeypatch.setattr("broker.engine.run_pass", _raise)
    r = live_client.post(f"/jobs/{job_id}/cancel")
    assert (r.status_code, r.json()["status"]) == (200, "cancelled")


def test_complete_returns_the_finished_job_when_the_pass_fails(
    client, db_path, monkeypatch
):
    add_node(db_path)
    job_id = submit(client).json()["id"]
    start_job_on_node(db_path, job_id)
    monkeypatch.setattr("broker.engine.run_pass", _raise)
    r = client.post(f"/jobs/{job_id}/complete", json={"outcome": "succeeded"})
    assert (r.status_code, r.json()["status"]) == (200, "succeeded")


def _raise(*args, **kwargs):
    raise RuntimeError("simulated pass failure")
