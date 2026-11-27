"""Tests for the database layer: schema constraints, init_db(), transaction().

Each test gets a fresh database file under pytest's tmp_path, so tests never
share state. No network, no real clock: every timestamp is a literal integer.
"""

import sqlite3
from pathlib import Path

import pytest

from broker.db import connect, init_db, transaction


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "broker.db"


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    init_db(c)
    yield c
    c.close()


# --- helpers -----------------------------------------------------------------
# Each helper inserts one *valid* row. A test overrides only the field it is
# checking, so a failure can only come from that field.


def insert_node(conn, **overrides):
    row = {
        "id": "node-aaa",
        "node_type": "gpu-m",
        "gpus_total": 4,
        "interruptible": 0,
        "price_per_hour_micros": 4_500_000,
        "team": "vision",
        "state": "ready",
        "launched_at": 100,
        "ready_at": 400,
        "idle_since": 400,
        "terminated_at": None,
        "end_reason": None,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO nodes ({cols}) VALUES ({marks})", list(row.values()))


def insert_job(conn, **overrides):
    row = {
        "id": "job-aaa",
        "team": "vision",
        "idempotency_key": None,
        "request_fingerprint": "fp",
        "gpus": 1,
        "estimated_minutes": 30,
        "priority": "normal",
        "status": "queued",
        "node_id": None,
        "submitted_at": 100,
        "started_at": None,
        "ended_at": None,
        "end_reason": None,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", list(row.values()))


def insert_provider_node(conn, **overrides):
    row = {
        "id": "node-aaa",
        "node_type": "gpu-s",
        "interruptible": 0,
        "requested_at": 100,
        "ready_at_planned": 400,
        "fails_at_planned": None,
        "reclaim_at": None,
        "state": "pending",
        "terminated_at": None,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO provider_nodes ({cols}) VALUES ({marks})", list(row.values())
    )


# --- init_db -----------------------------------------------------------------


def test_init_db_twice_keeps_exactly_one_clock_row(conn):
    init_db(conn)
    count = conn.execute("SELECT COUNT(*) FROM sim_clock").fetchone()[0]
    assert count == 1


def test_init_db_start_ts_is_set_once_and_not_overwritten(db_path):
    c = connect(db_path)
    init_db(c, start_ts=500)
    init_db(c, start_ts=999)
    now_ts = c.execute("SELECT now_ts FROM sim_clock WHERE id = 1").fetchone()[0]
    c.close()
    assert now_ts == 500


def test_user_version_is_2_after_init_db(conn):
    # Schema v2 (SPEC_DECISIONS.md #6, #11): jobs.created_seq, new end_reasons.
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_init_db_refuses_a_newer_schema(db_path):
    c = connect(db_path)
    c.execute("PRAGMA user_version = 3")
    with pytest.raises(RuntimeError, match="newer"):
        init_db(c)
    c.close()


# --- created_seq (decision 6) -------------------------------------------------


def test_created_seq_numbers_jobs_in_insert_order(conn):
    with transaction(conn):
        for job_id in ("job-zzz", "job-aaa", "job-mmm"):
            insert_job(conn, id=job_id)
    rows = conn.execute(
        "SELECT id, created_seq FROM jobs ORDER BY created_seq"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("job-zzz", 1), ("job-aaa", 2), ("job-mmm", 3)]


def test_created_seq_is_unique(conn):
    with transaction(conn):
        insert_job(conn, id="job-1")
    with pytest.raises(sqlite3.IntegrityError):
        insert_job(conn, id="job-2", created_seq=1)


def test_created_seq_continues_after_reopen(db_path, conn):
    with transaction(conn):
        insert_job(conn, id="job-1")
    conn.close()
    again = connect(db_path)
    init_db(again)
    with transaction(again):
        insert_job(again, id="job-2")
    assert (
        again.execute("SELECT created_seq FROM jobs WHERE id = 'job-2'").fetchone()[0]
        == 2
    )
    again.close()


@pytest.mark.parametrize("reason", ["idle_timeout", "manual", "provider_reclaimed",
                                    "provider_reconciled", "launch_failed", "evicted"])  # fmt: skip
def test_every_v2_end_reason_is_accepted(conn, reason):
    state = "failed" if reason == "launch_failed" else "terminated"
    insert_node(conn, state=state, terminated_at=500, end_reason=reason)


@pytest.mark.parametrize("reason", ["idle_reaped", "interrupted", "bogus"])
def test_v1_and_unknown_end_reasons_are_rejected(conn, reason):
    with pytest.raises(sqlite3.IntegrityError):
        insert_node(conn, state="terminated", terminated_at=500, end_reason=reason)


# --- migration v1 -> v2 --------------------------------------------------------

V1_SCHEMA = Path(__file__).parent / "fixtures" / "schema_v1.sql"


def make_v1_database(path):
    """A v1 database with data, built from the frozen v1 schema."""
    c = connect(path)
    c.executescript(V1_SCHEMA.read_text())
    c.execute("PRAGMA user_version = 1")
    with transaction(c):
        c.execute("INSERT INTO sim_clock (id, now_ts) VALUES (1, 5000)")
        insert_node(c, id="node-reaped", state="terminated", terminated_at=900,
                    end_reason="idle_reaped")  # fmt: skip
        insert_node(c, id="node-intr", state="terminated", terminated_at=900,
                    end_reason="interrupted")  # fmt: skip
        insert_node(c, id="node-live")
        insert_job(c, id="job-ccc", submitted_at=100)
        insert_job(c, id="job-aaa", submitted_at=100, status="running",
                   node_id="node-live", started_at=400)  # fmt: skip
        insert_job(c, id="job-bbb", submitted_at=100)
    return c


def test_migration_from_v1_keeps_data_and_maps_reasons(db_path):
    make_v1_database(db_path).close()
    c = connect(db_path)
    init_db(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    assert c.execute("SELECT now_ts FROM sim_clock").fetchone()[0] == 5000
    reasons = dict(c.execute("SELECT id, end_reason FROM nodes").fetchall())
    assert reasons == {"node-reaped": "idle_timeout", "node-intr": "provider_reclaimed",
                       "node-live": None}  # fmt: skip
    # Existing jobs are numbered in arrival (rowid) order, not id order.
    seq = [r[0] for r in c.execute("SELECT id FROM jobs ORDER BY created_seq")]
    assert seq == ["job-ccc", "job-aaa", "job-bbb"]
    assert (
        c.execute("SELECT node_id FROM jobs WHERE id = 'job-aaa'").fetchone()[0]
        == "node-live"
    )
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with transaction(c):
        insert_job(c, id="job-new")
    assert (
        c.execute("SELECT created_seq FROM jobs WHERE id = 'job-new'").fetchone()[0]
        == 4
    )
    c.close()


def test_migrated_database_accepts_the_new_reasons(db_path):
    make_v1_database(db_path).close()
    c = connect(db_path)
    init_db(c)
    insert_node(
        c, id="node-ev", state="terminated", terminated_at=900, end_reason="evicted"
    )
    with pytest.raises(sqlite3.IntegrityError):
        insert_node(c, id="node-old", state="terminated", terminated_at=900,
                    end_reason="idle_reaped")  # fmt: skip
    c.close()


def test_migration_runs_once(db_path):
    make_v1_database(db_path).close()
    for _ in range(2):
        c = connect(db_path)
        init_db(c)
        c.close()
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    assert c.execute("SELECT MAX(created_seq) FROM jobs").fetchone()[0] == 3
    c.close()


# --- sim_clock ---------------------------------------------------------------


def test_second_sim_clock_row_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sim_clock (id, now_ts) VALUES (2, 0)")


# --- jobs --------------------------------------------------------------------


@pytest.mark.parametrize("gpus", [3, 16])
def test_job_with_invalid_gpu_count_is_rejected(conn, gpus):
    with pytest.raises(sqlite3.IntegrityError):
        insert_job(conn, gpus=gpus)


def test_same_team_and_idempotency_key_twice_is_rejected(conn):
    insert_job(conn, id="job-1", idempotency_key="k1")
    with pytest.raises(sqlite3.IntegrityError):
        insert_job(conn, id="job-2", idempotency_key="k1")


def test_two_jobs_with_null_idempotency_key_are_allowed(conn):
    insert_job(conn, id="job-1", idempotency_key=None)
    insert_job(conn, id="job-2", idempotency_key=None)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_same_idempotency_key_under_two_teams_is_allowed(conn):
    insert_job(conn, id="job-1", team="vision", idempotency_key="k1")
    insert_job(conn, id="job-2", team="speech", idempotency_key="k1")
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_running_job_without_node_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        insert_job(conn, status="running", node_id=None, started_at=200)


def test_queued_job_with_node_is_rejected(conn):
    insert_node(conn, id="node-1")
    with pytest.raises(sqlite3.IntegrityError):
        insert_job(conn, status="queued", node_id="node-1")


def test_job_on_nonexistent_node_is_rejected(conn):
    # 'running' because a queued job may not have a node_id at all.
    with pytest.raises(sqlite3.IntegrityError):
        insert_job(conn, status="running", node_id="node-missing", started_at=200)


# --- nodes -------------------------------------------------------------------


def test_gpu_s_node_with_8_gpus_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        insert_node(conn, node_type="gpu-s", gpus_total=8)


def test_terminated_node_without_terminated_at_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        insert_node(conn, state="terminated", terminated_at=None)


# --- provider_nodes ----------------------------------------------------------


@pytest.mark.parametrize(
    "ready_at_planned, fails_at_planned",
    [(400, 1000), (None, None)],
    ids=["both_set", "neither_set"],
)
def test_provider_node_needs_exactly_one_planned_outcome(
    conn, ready_at_planned, fails_at_planned
):
    with pytest.raises(sqlite3.IntegrityError):
        insert_provider_node(
            conn, ready_at_planned=ready_at_planned, fails_at_planned=fails_at_planned
        )


# --- transaction() -----------------------------------------------------------


def test_transaction_rolls_back_all_rows_and_reraises(conn):
    class Boom(Exception):
        pass

    # Outer context checks the error reaches us; inner one must roll back first.
    with pytest.raises(Boom), transaction(conn):
        insert_node(conn, id="node-1")
        insert_job(conn, id="job-1", status="running", node_id="node-1", started_at=200)
        raise Boom()

    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# --- restart safety ----------------------------------------------------------


def test_data_survives_close_and_reopen(db_path):
    c = connect(db_path)
    init_db(c, start_ts=500)
    with transaction(c):
        insert_job(c, id="job-1")
        c.execute("UPDATE sim_clock SET now_ts = 750 WHERE id = 1")
    c.close()

    c = connect(db_path)
    init_db(c)  # a restarting service calls init_db() again; must not reset time
    job = c.execute("SELECT id, status FROM jobs").fetchone()
    now_ts = c.execute("SELECT now_ts FROM sim_clock").fetchone()[0]
    c.close()
    assert (job["id"], job["status"]) == ("job-1", "queued")
    assert now_ts == 750
