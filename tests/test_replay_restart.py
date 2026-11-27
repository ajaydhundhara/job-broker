"""P0-06: a replay is a pure function of its inputs, also across restarts.

Inputs: the trace rows (broker.trace.load_trace), the Config (seed, policy,
idle minutes, provider parameters), a new database file whose clock starts at
the first row, and nothing else. The comparison is on the raw database: every
row of jobs, nodes, provider_nodes, events (ids, timestamps, details),
sim_job_plans and sim_clock, plus the cost report. Nothing is renamed or
ignored, so a random id anywhere is a difference.

The restart closes the app and its connection after row k and builds a new
app (new provider, new connection) on the same file, as a process restart
would. Each cut point is chosen from the uninterrupted run and then checked
live, so it really lands mid-boot, mid-failing-launch, mid-backoff or before
the drain.
"""

import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from broker import costs, scheduler, service, trace
from broker.api import create_app
from broker.config import GPUS, Config
from broker.db import connect, init_db
from broker.models import JobSubmit
from replay.run_replay import _Run, replay_scenario

ROOT = Path(__file__).resolve().parent.parent
HEADER = "submitted_at,team,gpus,estimated_minutes,actual_minutes,priority,outcome\n"
START = trace.parse_ts("2025-03-03T00:00:00Z")
# Provider randomness well above the brief's rates, so failed launches and
# capacity outages happen in a short scenario. The seed only fixes the fixture;
# the coverage test below checks it still covers every case.
CFG = Config(seed=7, never_ready_prob=0.3, outage_start_prob=0.2, idle_minutes=10)
TABLES = {  # table -> its order (a total order on every table)
    "jobs": "created_seq",
    "nodes": "id",
    "provider_nodes": "id",
    "events": "id",
    "sim_job_plans": "job_id",
    "sim_clock": "id",
}


def write_trace(path: Path) -> Path:
    teams, gpus, outcomes = ("vision", "nlp", "speech"), (1, 2, 4, 8), (
        "succeeded", "succeeded", "failed", "cancelled")  # fmt: skip
    lines = []
    for i in range(36):
        at = trace.to_iso(START + i * 7 * 60)
        lines.append(f"{at},{teams[i % 3]},{gpus[i % 4]},30,{5 + (i * 11) % 50},"
                     f"{('normal', 'high', 'low')[i % 3]},{outcomes[i % 4]}")  # fmt: skip
        if i % 9 == 4:
            lines.append(lines[-1])  # an exact duplicate row: one job, counted
    path.write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def rows(tmp_path_factory):
    path = write_trace(tmp_path_factory.mktemp("trace") / "trace.csv")
    return trace.load_trace(path)[0]


def snapshot(db_path) -> dict:
    conn = connect(db_path)
    try:
        out = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY {o}")]
               for t, o in TABLES.items()}  # fmt: skip
        now = service.get_now(conn)
        first = conn.execute("SELECT MIN(submitted_at) FROM jobs").fetchone()[0]
        report = costs.cost_by_team(conn, first, now + 1, now=now)
        out["cost"] = {
            "teams": {t: asdict(c) for t, c in sorted(report.teams.items())},
            "provider_overhead": report.provider_overhead,
            "grand_total": report.grand_total,
        }
        return out
    finally:
        conn.close()


def in_flight(conn, cfg) -> set[str]:
    """What is going on at this moment, read from the live database."""
    now = service.get_now(conn)
    one = lambda sql: conn.execute(sql).fetchone()[0] > 0
    found = set()
    if one("""SELECT COUNT(*) FROM provider_nodes
              WHERE state = 'pending' AND ready_at_planned IS NOT NULL"""):
        found.add("booting")
    if one("""SELECT COUNT(*) FROM provider_nodes
              WHERE state = 'pending' AND fails_at_planned IS NOT NULL"""):
        found.add("failing launch")
    if any(scheduler.backing_off(conn, nt, now, cfg) for nt in GPUS):
        found.add("backoff")
    if one("SELECT COUNT(*) FROM jobs WHERE status = 'running'") and one(
        "SELECT COUNT(*) FROM jobs WHERE status = 'queued'"
    ):
        found.add("running and queued")
    return found


def play(rows, cfg, db_path, cut=None):
    """Replay rows like replay_scenario; with cut=k, restart before row k
    (k = len(rows): before the drain). Returns (snapshot, in flight at the cut)."""
    conn = connect(db_path)
    init_db(conn, start_ts=rows[0].utc_ts)
    app = TestClient(create_app(db_path, cfg))
    run = _Run(app.__enter__(), conn, cfg, check=True)
    seen = None
    try:
        for i, row in enumerate([*rows, None]):
            if i == cut:
                seen = in_flight(conn, cfg)
                app.__exit__(None, None, None)
                conn.close()
                conn = connect(db_path)  # a new process: new app, provider, connection
                app = TestClient(create_app(db_path, cfg))
                old, run = run, _Run(app.__enter__(), conn, cfg, check=True)
                run.lines, run.duplicates = old.lines, old.duplicates
            if row is not None:
                run.submit(row)
        run.drain()
    finally:
        app.__exit__(None, None, None)
        conn.close()
    return snapshot(db_path), seen


@pytest.fixture(scope="module")
def straight(rows, tmp_path_factory):
    db = tmp_path_factory.mktemp("straight") / "broker.db"
    return play(rows, CFG, db)[0]


def cut_points(rows, straight) -> dict[str, int]:
    """The first row index k at which the uninterrupted run had each case going
    (from its final tables); every cut is re-checked live in the test."""

    def first(pred):
        return next(k for k in range(1, len(rows)) if pred(rows[k - 1].utc_ts))

    prov = straight["provider_nodes"]
    refusals = [e["ts"] for e in straight["events"]
                if e["event_type"] == "capacity_unavailable"]  # fmt: skip
    return {
        "booting": first(lambda t: any(
            p["ready_at_planned"] and p["requested_at"] <= t < p["ready_at_planned"]
            for p in prov)),
        "failing launch": first(lambda t: any(
            p["fails_at_planned"] and p["requested_at"] <= t < p["fails_at_planned"]
            for p in prov)),
        "backoff": first(lambda t: any(r <= t < r + CFG.capacity_backoff_base_s
                                       for r in refusals)),
        "running and queued": len(rows) // 2,
        "before the drain": len(rows),
    }  # fmt: skip


# --- the scenario covers what it is meant to ------------------------------------------


def test_the_scenario_covers_failures_outages_duplicates_and_reaping(straight):
    events = {e["event_type"] for e in straight["events"]}
    assert {"launch_failed", "capacity_unavailable", "node_reaped"} <= events
    assert {j["status"] for j in straight["jobs"]} == {
        "succeeded",
        "failed",
        "cancelled",
    }
    assert len(straight["jobs"]) < 36 + 4  # the duplicate rows made no job


# --- the same inputs, twice -----------------------------------------------------------


def test_the_same_scenario_twice_is_identical(rows, straight, tmp_path):
    again, _ = play(rows, CFG, tmp_path / "again.db")
    assert again == straight


def test_replay_scenario_twice_gives_identical_results(rows, tmp_path):
    a = replay_scenario(rows, CFG, tmp_path / "a.db", check_invariants=True)
    b = replay_scenario(rows, CFG, tmp_path / "b.db", check_invariants=True)
    assert a == b


def test_another_seed_gives_another_run(rows, straight, tmp_path):
    other, _ = play(rows, Config(**{**asdict(CFG), "seed": 8}), tmp_path / "s8.db")
    assert other["provider_nodes"] != straight["provider_nodes"]


SNAPSHOT_SCRIPT = """
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/tests")
import test_replay_restart as t
from broker import trace
rows = trace.load_trace(t.write_trace(Path(sys.argv[2]) / "trace.csv"))[0]
snap, _ = t.play(rows, t.CFG, Path(sys.argv[2]) / "broker.db")
print(hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest())
"""


def test_two_processes_with_different_hash_seeds_agree(tmp_path):
    # str hashing (set and dict-of-str order) differs per process unless
    # PYTHONHASHSEED is fixed; the replay must not depend on it.
    digests = []
    for hash_seed in ("1", "2"):
        work = tmp_path / hash_seed
        work.mkdir()
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "RETRIEVAL_BACKEND": "tfidf"}
        out = subprocess.run([sys.executable, "-c", SNAPSHOT_SCRIPT, str(ROOT), str(work)],
                             env=env, capture_output=True, text=True, check=True)  # fmt: skip
        digests.append(out.stdout.strip())
    assert digests[0] == digests[1]


# --- a restart in the middle ------------------------------------------------------------

CASES = [
    "booting",
    "failing launch",
    "backoff",
    "running and queued",
    "before the drain",
]


@pytest.mark.parametrize("case", CASES)
def test_a_restart_gives_the_uninterrupted_result(rows, straight, tmp_path, case):
    cut = cut_points(rows, straight)[case]
    restarted, seen = play(rows, CFG, tmp_path / "restarted.db", cut=cut)
    if case != "before the drain":
        assert case in seen, f"cut {cut} is not {case}: {seen}"
    for part in [*TABLES, "cost"]:  # one message per part, then the whole
        assert restarted[part] == straight[part], part
    assert restarted == straight


# --- job ids: the only randomness there was ---------------------------------------------


def submit(conn, team="vision", key=None):
    req = {"team": team, "gpus": 1, "estimated_minutes": 30, "idempotency_key": key}
    return service.submit_job(conn, JobSubmit(**req))


def test_job_ids_are_job_plus_the_creation_sequence_in_hex(tmp_path):
    conn = connect(tmp_path / "ids.db")
    init_db(conn)
    ids = [submit(conn)[0]["id"] for _ in range(17)]
    seqs = [
        r[0] for r in conn.execute("SELECT created_seq FROM jobs ORDER BY created_seq")
    ]
    conn.close()
    assert ids[:2] == ["job-000000000001", "job-000000000002"]
    assert ids[16] == "job-000000000011"  # 17 in hex
    assert ids == [f"job-{n:012x}" for n in seqs]


def test_a_repeated_key_reuses_the_id_and_takes_no_number(tmp_path):
    conn = connect(tmp_path / "ids.db")
    init_db(conn)
    first = submit(conn, key="k1")[0]["id"]
    again = submit(conn, key="k1")[0]["id"]
    after = submit(conn)[0]["id"]
    conn.close()
    assert (first, again, after) == ("job-000000000001",) * 2 + ("job-000000000002",)


def test_job_ids_continue_after_a_restart(tmp_path):
    db = tmp_path / "ids.db"
    with TestClient(create_app(db, CFG)) as client:
        body = {"team": "vision", "gpus": 1, "estimated_minutes": 30}
        assert client.post("/jobs", json=body).json()["id"] == "job-000000000001"
    with TestClient(create_app(db, CFG)) as client:
        assert client.post("/jobs", json=body).json()["id"] == "job-000000000002"


def test_job_ids_continue_after_migrated_v1_jobs(tmp_path):
    # v1 ids were random; migrated rows keep them and new ids follow the
    # sequence the migration assigned (created_seq = rowid).
    from test_schema import make_v1_database

    db = tmp_path / "v1.db"
    make_v1_database(db).close()
    conn = connect(db)
    init_db(conn)
    new = submit(conn)[0]["id"]
    ids = [r[0] for r in conn.execute("SELECT id FROM jobs ORDER BY created_seq")]
    conn.close()
    assert new == "job-000000000004"
    assert ids == ["job-ccc", "job-aaa", "job-bbb", new]
