"""P0-05: idempotent submits hold across separate connections.

The database constraint UNIQUE (team, idempotency_key) is what prevents a
duplicate, not only the lookup in service.submit_job. Every test uses its own
connection per caller, as each HTTP request does.

Interleavings are forced with events, never sleeps:
- A is held inside its transaction, after its INSERT and before its COMMIT
  (it holds the write lock), until B has arrived at its own transaction.
- B may additionally be made to miss its lookup, as a lookup that ran too
  early would. Then only the UNIQUE constraint can stop the duplicate.
B cannot begin until A commits, whichever thread the OS runs first, so the
outcome never depends on timing. HANG_GUARD_S only turns a hang into a failure.
"""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from broker import service
from broker.api import create_app
from broker.db import connect, init_db
from broker.models import JobSubmit

HANG_GUARD_S = 10
BODY = {"team": "vision", "gpus": 2, "estimated_minutes": 30, "idempotency_key": "k1"}
CHANGES = {  # one field changed: a different request under the same key
    "gpus": {"gpus": 4},
    "estimated_minutes": {"estimated_minutes": 31},
    "priority": {"priority": "high"},
}


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "broker.db"
    conn = connect(p)
    init_db(conn)
    conn.close()
    return p


def jobs(path):
    conn = connect(path)
    rows = conn.execute(
        "SELECT id, team, idempotency_key FROM jobs ORDER BY created_seq"
    ).fetchall()
    conn.close()
    return [tuple(r) for r in rows]


def submitted_events(path):
    conn = connect(path)
    rows = conn.execute(
        """SELECT entity_id FROM events WHERE event_type = 'job_submitted'
           ORDER BY id"""
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


class Interleave:
    """Hooks into service.submit_job for a forced A-before-B interleaving.

    A is the first caller to write its job_submitted event; it waits there
    (inside its transaction) until another thread enters service.transaction.
    With b_misses, that other thread's first lookup returns nothing.
    """

    def __init__(self, monkeypatch, b_misses=False):
        self.a_inside = threading.Event()
        self.b_arrived = threading.Event()
        self.a_ident = None
        self.b_missed = False
        self.b_misses = b_misses
        real_log, real_tx, real_find = (
            service.log_event,
            service.transaction,
            service._find_by_key,
        )

        def log_event(conn, ts, kind, entity_id, event_type, **details):
            if event_type == "job_submitted" and self.a_ident is None:
                self.a_ident = threading.get_ident()
                self.a_inside.set()
                assert self.b_arrived.wait(HANG_GUARD_S), "B never arrived"
            return real_log(conn, ts, kind, entity_id, event_type, **details)

        def transaction(conn):
            if self.is_b():
                self.b_arrived.set()
            return real_tx(conn)

        def find(conn, team, key):
            if self.b_misses and self.is_b() and not self.b_missed:
                self.b_missed = True
                return None  # as if the lookup ran before A committed
            return real_find(conn, team, key)

        monkeypatch.setattr(service, "log_event", log_event)
        monkeypatch.setattr(service, "transaction", transaction)
        monkeypatch.setattr(service, "_find_by_key", find)

    def is_b(self):
        return self.a_inside.is_set() and threading.get_ident() != self.a_ident


def submit_on_own_connection(path, body):
    conn = connect(path)
    try:
        return service.submit_job(conn, JobSubmit(**body))
    except service.Conflict as e:
        return e
    finally:
        conn.close()


def run_pair(path, hooks, body_a, body_b):
    """A starts and is held mid-transaction; then B starts. Returns (A, B)."""
    out = {}

    def run(name, body):
        out[name] = submit_on_own_connection(path, body)

    a = threading.Thread(target=run, args=("A", body_a))
    a.start()
    assert hooks.a_inside.wait(HANG_GUARD_S), "A never reached its INSERT"
    b = threading.Thread(target=run, args=("B", body_b))
    b.start()
    for t in (a, b):
        t.join(HANG_GUARD_S)
        assert not t.is_alive(), "a submit hung"
    assert set(out) == {"A", "B"}
    return out["A"], out["B"]


MODES = {"lookup": False, "constraint": True}  # b_misses


# --- the constraint itself -------------------------------------------------------


def insert(conn, job_id, team, key):
    conn.execute(
        """INSERT INTO jobs (id, team, idempotency_key, request_fingerprint, gpus,
                             estimated_minutes, priority, status, submitted_at)
           VALUES (?, ?, ?, 'fp', 1, 30, 'normal', 'queued', 0)""",
        (job_id, team, key),
    )
    conn.commit()


def test_the_unique_constraint_rejects_a_duplicate_from_another_connection(path):
    a, b = connect(path), connect(path)
    insert(a, "job-a", "vision", "k1")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(b, "job-b", "vision", "k1")
    insert(b, "job-c", "speech", "k1")  # another team: allowed
    insert(a, "job-d", "vision", None)  # no key: allowed many times
    insert(b, "job-e", "vision", None)
    a.close(), b.close()
    assert [j[0] for j in jobs(path)] == ["job-a", "job-c", "job-d", "job-e"]


# --- a lookup that misses: only the constraint stops the duplicate ----------------


def test_a_missed_lookup_still_returns_the_existing_job(path, monkeypatch):
    first, created = submit_on_own_connection(path, BODY)
    assert created
    Interleave(monkeypatch, b_misses=True).a_inside.set()  # every caller is "B"
    again, created_again = submit_on_own_connection(path, BODY)
    assert (again["id"], created_again) == (first["id"], False)
    assert jobs(path) == [(first["id"], "vision", "k1")]
    assert submitted_events(path) == [first["id"]]


@pytest.mark.parametrize("change", CHANGES)
def test_a_missed_lookup_with_another_payload_is_a_conflict(path, monkeypatch, change):
    first, _ = submit_on_own_connection(path, BODY)
    Interleave(monkeypatch, b_misses=True).a_inside.set()
    out = submit_on_own_connection(path, {**BODY, **CHANGES[change]})
    assert isinstance(out, service.Conflict)
    assert str(out) == "idempotency key reused with different request"
    assert jobs(path) == [(first["id"], "vision", "k1")]
    assert submitted_events(path) == [first["id"]]


# --- concurrent submits on two connections ---------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_concurrent_identical_submits_make_one_job(path, monkeypatch, mode):
    hooks = Interleave(monkeypatch, b_misses=MODES[mode])
    (job_a, created_a), (job_b, created_b) = run_pair(path, hooks, BODY, BODY)
    assert (created_a, created_b) == (True, False)
    assert job_a["id"] == job_b["id"]
    assert jobs(path) == [(job_a["id"], "vision", "k1")]
    assert submitted_events(path) == [job_a["id"]]
    assert hooks.b_missed is MODES[mode]


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("mode", MODES)
def test_concurrent_different_payload_is_a_conflict(path, monkeypatch, mode, change):
    hooks = Interleave(monkeypatch, b_misses=MODES[mode])
    (job_a, created_a), out_b = run_pair(path, hooks, BODY, {**BODY, **CHANGES[change]})
    assert created_a
    assert isinstance(out_b, service.Conflict)
    assert jobs(path) == [(job_a["id"], "vision", "k1")]
    assert submitted_events(path) == [job_a["id"]]


@pytest.mark.parametrize("mode", MODES)
def test_a_different_payload_is_a_conflict_every_time(path, monkeypatch, mode):
    first, _ = submit_on_own_connection(path, BODY)
    for change in [*CHANGES.values(), *CHANGES.values()]:
        if MODES[mode]:
            monkeypatch.undo()
            Interleave(monkeypatch, b_misses=True).a_inside.set()
        out = submit_on_own_connection(path, {**BODY, **change})
        assert isinstance(out, service.Conflict), change
    again, created = submit_on_own_connection(path, BODY)
    assert (again["id"], created) == (first["id"], False)
    assert jobs(path) == [(first["id"], "vision", "k1")]
    assert submitted_events(path) == [first["id"]]


@pytest.mark.parametrize("mode", MODES)
def test_concurrent_same_key_under_two_teams_makes_two_jobs(path, monkeypatch, mode):
    hooks = Interleave(monkeypatch, b_misses=MODES[mode])
    other = {**BODY, "team": "speech"}
    (job_a, created_a), (job_b, created_b) = run_pair(path, hooks, BODY, other)
    assert (created_a, created_b) == (True, True)
    assert job_a["id"] != job_b["id"]
    assert jobs(path) == [(job_a["id"], "vision", "k1"), (job_b["id"], "speech", "k1")]
    assert submitted_events(path) == [job_a["id"], job_b["id"]]


# --- the same through HTTP: one connection per request ---------------------------


@pytest.mark.parametrize("mode", MODES)
def test_concurrent_identical_http_submits_return_the_same_id(
    tmp_path, monkeypatch, mode
):
    db_path = tmp_path / "broker.db"
    with TestClient(create_app(db_path)) as client:
        hooks = Interleave(monkeypatch, b_misses=MODES[mode])
        out = {}

        def post(name):
            out[name] = client.post("/jobs", json=BODY)

        a = threading.Thread(target=post, args=("A",))
        a.start()
        assert hooks.a_inside.wait(HANG_GUARD_S)
        b = threading.Thread(target=post, args=("B",))
        b.start()
        for t in (a, b):
            t.join(HANG_GUARD_S)
            assert not t.is_alive()
    assert (out["A"].status_code, out["B"].status_code) == (201, 200)
    assert out["A"].json()["id"] == out["B"].json()["id"]
    assert jobs(db_path) == [(out["A"].json()["id"], "vision", "k1")]
    assert submitted_events(db_path) == [out["A"].json()["id"]]
