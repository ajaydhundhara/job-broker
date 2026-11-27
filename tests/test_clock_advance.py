"""P0-01: clock advances are serialized; relative ones ({"minutes": n}) add up.

Threads each use their own connection and call service.sim_advance. The
interleaving is forced with events, not sleeps: the first advance is held
inside advance_to() (its target already computed) until every other thread
has either computed its own target too (no lock) or is about to wait for
service.ADVANCE_LOCK. The timeout is only a hang guard; it never decides the
outcome.
"""

import threading

import pytest

from broker import engine, service
from broker.config import Config
from broker.db import connect, init_db
from broker.models import JobSubmit, SimAdvance
from broker.provider import FakeProvider

HANG_GUARD_S = 10


def make_db(path):
    conn = connect(path)
    init_db(conn)
    conn.close()
    return path


def clock(path):
    conn = connect(path)
    now = conn.execute("SELECT now_ts FROM sim_clock").fetchone()[0]
    rows = conn.execute(
        """SELECT json_extract(details, '$.from_ts'), json_extract(details, '$.to_ts')
           FROM events WHERE event_type = 'clock_advanced' ORDER BY id"""
    ).fetchall()
    conn.close()
    return now, [tuple(r) for r in rows]


def run_held(path, monkeypatch, requests, cfg=None):
    """Run requests[0] first and hold it inside advance_to() until every
    other request has reached the lock (or, without one, computed its target).
    Returns ({request index: response or exception}, targets entered)."""
    cfg = cfg or Config()
    provider = FakeProvider(cfg)
    others = len(requests) - 1
    first_inside = threading.Event()
    all_arrived = threading.Event()
    arrived = []
    entered = []
    mutex = threading.Lock()
    real_advance_to = engine.advance_to

    def arrive():
        with mutex:
            arrived.append(1)
            if len(arrived) == others:
                all_arrived.set()

    def held_advance_to(conn, target_ts, cfg, provider=None):
        with mutex:
            entered.append(target_ts)
            first = len(entered) == 1
        if first:
            first_inside.set()
            assert all_arrived.wait(HANG_GUARD_S), "the others never arrived"
        elif not getattr(threading.current_thread(), "counted", False):
            arrive()
        return real_advance_to(conn, target_ts, cfg, provider)

    class SignallingLock:
        def __init__(self):
            self._lock = threading.Lock()

        def __enter__(self):
            if first_inside.is_set():
                threading.current_thread().counted = True
                arrive()  # about to wait for the held advance
            self._lock.acquire()

        def __exit__(self, *exc):
            self._lock.release()

    monkeypatch.setattr(engine, "advance_to", held_advance_to)
    monkeypatch.setattr(service, "ADVANCE_LOCK", SignallingLock())

    out = {}

    def call(i):
        conn = connect(path)
        try:
            out[i] = service.sim_advance(conn, requests[i], cfg, provider)
        except service.Conflict as e:  # the expected 409; anything else fails below
            out[i] = e
        finally:
            conn.close()

    threads = [threading.Thread(target=call, args=(i,)) for i in range(len(requests))]
    threads[0].start()
    assert first_inside.wait(HANG_GUARD_S), "the first advance never started"
    for t in threads[1:]:
        t.start()
    for t in threads:
        t.join(HANG_GUARD_S)
    assert not any(t.is_alive() for t in threads), "a thread hung"
    assert sorted(out) == list(range(len(requests))), "a thread raised"
    return out, entered


def plus(minutes):
    return SimAdvance(minutes=minutes)


def to(ts):
    return SimAdvance(to_ts=ts)


def test_two_concurrent_plus_10_minutes_advance_20_minutes(tmp_path, monkeypatch):
    path = make_db(tmp_path / "broker.db")
    out, entered = run_held(path, monkeypatch, [plus(10), plus(10)])
    assert not [r for r in out.values() if isinstance(r, Exception)]
    assert entered == [600, 1200]  # the second target is computed after the first
    assert clock(path) == (1200, [(0, 600), (600, 1200)])
    # Each response reports the clock after its own advance.
    assert sorted(r["now_ts"] for r in out.values()) == [600, 1200]


def test_several_concurrent_relative_advances_all_add_up(tmp_path, monkeypatch):
    path = make_db(tmp_path / "broker.db")
    out, _ = run_held(path, monkeypatch, [plus(10), plus(5), plus(1), plus(30)])
    assert not [r for r in out.values() if isinstance(r, Exception)]
    now, events = clock(path)
    assert now == (10 + 5 + 1 + 30) * 60
    # Back to back, no gap and no overlap; the order after the first is
    # whichever thread got the lock next (threading.Lock is not FIFO).
    assert events[0] == (0, 600)
    assert [e[0] for e in events[1:]] == [e[1] for e in events[:-1]]
    assert events[-1][1] == now


def test_relative_waits_for_an_absolute_advance_and_adds_to_it(tmp_path, monkeypatch):
    # Without the lock the relative one reads now=0, targets 600 < 900 and
    # can fail with "time never goes backwards" (409) for a "+10 min" request.
    path = make_db(tmp_path / "broker.db")
    out, entered = run_held(path, monkeypatch, [to(900), plus(10)])
    assert not [r for r in out.values() if isinstance(r, Exception)]
    assert entered == [900, 1500]
    assert clock(path) == (1500, [(0, 900), (900, 1500)])


def test_absolute_waits_for_a_relative_advance(tmp_path, monkeypatch):
    path = make_db(tmp_path / "broker.db")
    out, _ = run_held(path, monkeypatch, [plus(10), to(900)])
    assert not [r for r in out.values() if isinstance(r, Exception)]
    assert clock(path) == (900, [(0, 600), (600, 900)])


def test_absolute_into_the_past_is_still_a_conflict_after_waiting(
    tmp_path, monkeypatch
):
    path = make_db(tmp_path / "broker.db")
    out, _ = run_held(path, monkeypatch, [plus(10), to(300)])
    assert out[0]["now_ts"] == 600
    assert isinstance(out[1], service.Conflict)
    assert clock(path) == (600, [(0, 600)])


def test_lock_is_released_after_a_rejected_advance(tmp_path):
    path = make_db(tmp_path / "broker.db")
    cfg = Config()
    conn = connect(path)
    service.sim_advance(conn, plus(10), cfg, FakeProvider(cfg))
    with pytest.raises(service.Conflict):
        service.sim_advance(conn, to(0), cfg, FakeProvider(cfg))
    assert service.ADVANCE_LOCK.acquire(blocking=False)
    service.ADVANCE_LOCK.release()
    conn.close()


# --- no event processed twice or skipped ----------------------------------------


def seed_jobs(path, cfg):
    conn = connect(path)
    provider = FakeProvider(cfg)
    for team, gpus in (("vision", 2), ("nlp", 4), ("vision", 1), ("speech", 8)):
        service.submit_job(conn, JobSubmit(team=team, gpus=gpus, estimated_minutes=30))
        engine.run_pass(conn, cfg, provider)
    conn.close()


def history(path):
    """Everything that happened, with random job ids replaced by arrival order."""
    conn = connect(path)
    order = {
        r["id"]: n
        for n, r in enumerate(conn.execute("SELECT id FROM jobs ORDER BY created_seq"))
    }
    events = [
        (
            r["ts"],
            r["entity_type"],
            order.get(r["entity_id"], r["entity_id"]),
            r["event_type"],
        )
        for r in conn.execute("SELECT * FROM events ORDER BY id")
    ]
    jobs = [
        (order[r["id"]], r["status"], r["node_id"], r["started_at"])
        for r in conn.execute("SELECT * FROM jobs ORDER BY created_seq")
    ]
    nodes = [tuple(r) for r in conn.execute("SELECT * FROM nodes ORDER BY id")]
    now = conn.execute("SELECT now_ts FROM sim_clock").fetchone()[0]
    conn.close()
    return {"events": events, "jobs": jobs, "nodes": nodes, "now": now}


def test_concurrent_advances_process_each_event_once(tmp_path, monkeypatch):
    cfg = Config()
    concurrent = make_db(tmp_path / "concurrent.db")
    sequential = make_db(tmp_path / "sequential.db")
    seed_jobs(concurrent, cfg)
    seed_jobs(sequential, cfg)

    conn = connect(sequential)
    service.sim_advance(conn, plus(10), cfg, FakeProvider(cfg))
    service.sim_advance(conn, plus(10), cfg, FakeProvider(cfg))
    conn.close()
    out, _ = run_held(concurrent, monkeypatch, [plus(10), plus(10)], cfg)
    assert not [r for r in out.values() if isinstance(r, Exception)]

    got, want = history(concurrent), history(sequential)
    assert want["now"] == 1200
    assert any(e[3] == "job_started" for e in want["events"])  # the run did something
    assert got == want
