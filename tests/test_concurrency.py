"""Concurrent requests: no HTTP 500s and no duplicate jobs.

The thread-affinity bug only shows when two requests land on different worker
threads, which is timing-dependent, so each scenario runs many rounds. Those
repeated stress tests carry the `slow` marker; the deterministic, forced
interleavings for idempotency are in tests/test_idempotency.py.
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from broker.api import create_app
from broker.config import Config
from broker.db import connect, init_db, transaction
from broker.provider import FakeProvider

ROUNDS = 50
PARALLEL = 8
BODY = {"team": "vision", "gpus": 2, "estimated_minutes": 30}


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "broker.db"


@pytest.fixture
def client(db_path):
    with TestClient(create_app(db_path)) as c:
        yield c


def post_in_parallel(client, bodies):
    with ThreadPoolExecutor(len(bodies)) as pool:
        return list(pool.map(lambda b: client.post("/jobs", json=b), bodies))


def count(db_path, sql, params=()):
    conn = connect(db_path)
    n = conn.execute(sql, params).fetchone()[0]
    conn.close()
    return n


@pytest.mark.slow
def test_parallel_same_key_same_body_creates_one_job_every_round(client, db_path):
    for i in range(ROUNDS):
        key = f"round-{i}"
        responses = post_in_parallel(
            client, [{**BODY, "idempotency_key": key}] * PARALLEL
        )

        codes = sorted(r.status_code for r in responses)
        assert codes == [200] * (PARALLEL - 1) + [201], f"round {i}: {codes}"
        assert len({r.json()["id"] for r in responses}) == 1, f"round {i}"
        assert (
            count(
                db_path, "SELECT COUNT(*) FROM jobs WHERE idempotency_key = ?", (key,)
            )
            == 1
        )
        assert (
            count(
                db_path,
                """SELECT COUNT(*) FROM events WHERE event_type = 'job_submitted'
                   AND entity_id = ?""",
                (responses[0].json()["id"],),
            )
            == 1
        ), f"round {i}"


@pytest.mark.slow
def test_parallel_same_key_different_bodies_one_wins_rest_conflict(client, db_path):
    for i in range(ROUNDS):
        key = f"round-{i}"
        bodies = [
            {**BODY, "estimated_minutes": 30 + n, "idempotency_key": key}
            for n in range(PARALLEL)
        ]
        codes = sorted(r.status_code for r in post_in_parallel(client, bodies))
        assert codes == [201] + [409] * (PARALLEL - 1), f"round {i}: {codes}"
        assert (
            count(
                db_path, "SELECT COUNT(*) FROM jobs WHERE idempotency_key = ?", (key,)
            )
            == 1
        )


@pytest.mark.slow
def test_parallel_submits_without_key_all_succeed(client, db_path):
    for i in range(ROUNDS):
        codes = [r.status_code for r in post_in_parallel(client, [BODY] * PARALLEL)]
        assert codes == [201] * PARALLEL, f"round {i}: {codes}"
    assert count(db_path, "SELECT COUNT(*) FROM jobs") == ROUNDS * PARALLEL


# --- node ids and caps under concurrency (SPEC_DECISIONS.md #14, #13) ---------------


def test_parallel_launches_get_unique_sequential_node_ids(db_path):
    # Each thread has its own connection, as each request does. The id is
    # COUNT + 1, read and written inside one BEGIN IMMEDIATE, so two
    # launches can never get the same number.
    conn = connect(db_path)
    init_db(conn)
    conn.close()
    provider = FakeProvider(Config(never_ready_prob=0.0, outage_start_prob=0.0))
    launches = 40

    def launch(_):
        c = connect(db_path)
        try:
            with transaction(c):
                return provider.launch_node(c, "gpu-s", 0)
        finally:
            c.close()

    with ThreadPoolExecutor(PARALLEL) as pool:
        ids = list(pool.map(launch, range(launches)))
    assert sorted(ids) == [f"node-{n:05d}" for n in range(1, launches + 1)]


@pytest.mark.slow
def test_parallel_submits_from_many_teams_keep_every_invariant(client, db_path):
    teams = [f"team-{n}" for n in range(PARALLEL)]
    for i in range(10):
        bodies = [{"team": t, "gpus": 1 + (i + n) % 2, "estimated_minutes": 30}
                  for n, t in enumerate(teams)]  # fmt: skip
        codes = [r.status_code for r in post_in_parallel(client, bodies)]
        assert codes == [201] * PARALLEL, f"round {i}: {codes}"
        assert client.post("/sim/advance", json={"minutes": 3}).status_code == 200
    live = count(
        db_path, "SELECT COUNT(*) FROM nodes WHERE state IN ('launching', 'ready')"
    )
    assert 0 < live <= 10
    overcommitted = count(
        db_path,
        """SELECT COUNT(*) FROM (
               SELECT n.id FROM nodes n JOIN jobs j ON j.node_id = n.id
               WHERE j.status = 'running' GROUP BY n.id
               HAVING SUM(j.gpus) > MAX(n.gpus_total))""",
    )
    assert overcommitted == 0
    shared = count(
        db_path,
        """SELECT COUNT(*) FROM jobs j JOIN nodes n ON n.id = j.node_id
           WHERE j.team != n.team""",
    )
    assert shared == 0
    assert count(db_path, "SELECT COUNT(*) FROM jobs WHERE status = 'running'") > 0
