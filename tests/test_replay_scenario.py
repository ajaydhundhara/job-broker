"""One replay scenario through the real HTTP API (replay/run_replay.py).

Each test writes a tiny CSV, loads it with broker.trace and replays it on a
fresh database through TestClient: no real server, no sleeps, simulated time.
"""

import pytest

from broker import service, trace
from broker.api import create_app
from broker.config import PRICE_MICROS, Config
from broker.db import connect, init_db, transaction
from replay.run_replay import InvariantViolation, check_invariants, replay_scenario

HEADER = "submitted_at,team,gpus,estimated_minutes,actual_minutes,priority,outcome\n"
T0 = "2025-03-03T00:00:00Z"
START = trace.parse_ts(T0)
# Boots in exactly 60 s, never fails, no outages: times in the tests are exact.
CFG = Config(never_ready_prob=0.0, outage_start_prob=0.0, boot_min_s=60, boot_max_s=60)


def at(minutes: int) -> str:
    return trace.to_iso(START + minutes * 60)


def rows_from(tmp_path, *lines, **kwargs):
    path = tmp_path / "trace.csv"
    path.write_text(HEADER + "".join(x + "\n" for x in lines), encoding="utf-8")
    rows, _ = trace.load_trace(path, **kwargs)
    return rows


def replay(tmp_path, rows, cfg=CFG, check=True):
    return replay_scenario(rows, cfg, tmp_path / "replay.db", check_invariants=check)


def independent_grand_total(db_path) -> int:
    """Every provider node's bill, from provider_nodes and the billing rules alone:
    per second from request to termination, at least 600 s, floored once."""
    conn = connect(db_path)
    try:
        total = 0
        for n in conn.execute("SELECT * FROM provider_nodes").fetchall():
            assert n["terminated_at"] is not None, n["id"]  # the replay drained
            seconds = max(n["terminated_at"] - n["requested_at"], 600)
            total += PRICE_MICROS[n["node_type"]] * seconds // 3600
        return total
    finally:
        conn.close()


# --- a whole small scenario ----------------------------------------------------------


def test_five_rows_all_end_and_every_node_is_gone(tmp_path):
    rows = rows_from(
        tmp_path,
        f"{at(0)},vision,1,30,10,normal,succeeded",
        f"{at(1)},nlp,2,30,20,high,succeeded",
        f"{at(2)},vision,4,60,30,low,failed",
        f"{at(3)},speech,8,60,5,normal,cancelled",
        f"{at(4)},vision,1,15,15,normal,succeeded",
    )
    result = replay(tmp_path, rows)
    assert len(result["jobs"]) == 5
    assert {j["status"] for j in result["jobs"]} <= {"succeeded", "failed", "cancelled"}
    assert [j["line"] for j in result["jobs"]] == [2, 3, 4, 5, 6]
    assert all(n["state"] in ("terminated", "failed") for n in result["nodes"])
    assert result["cost"]["grand_total"] > 0
    assert result["duplicates"] == 0


def test_every_job_runs_actual_minutes_after_it_starts(tmp_path):
    rows = rows_from(
        tmp_path,
        f"{at(0)},vision,1,30,10,normal,succeeded",
        f"{at(0)},nlp,1,30,7,normal,cancelled",  # a cancel after 7 minutes of runtime
    )
    jobs = {j["team"]: j for j in replay(tmp_path, rows)["jobs"]}
    for team, minutes, status in [("vision", 10, "succeeded"), ("nlp", 7, "cancelled")]:
        j = jobs[team]
        assert j["started_at"] == START + 60  # waited for the 60 s boot
        assert (j["ended_at"] - j["started_at"], j["status"]) == (minutes * 60, status)


# --- duplicates ----------------------------------------------------------------------


def test_duplicate_rows_become_one_job_and_are_counted(tmp_path):
    row = f"{at(0)},vision,1,30,10,normal,succeeded"
    result = replay(tmp_path, rows_from(tmp_path, row, row, row))
    assert len(result["jobs"]) == 1
    assert result["duplicates"] == 2


def test_keep_duplicates_makes_every_row_a_job(tmp_path):
    row = f"{at(0)},vision,1,30,10,normal,succeeded"
    result = replay(tmp_path, rows_from(tmp_path, row, row, keep_duplicates=True))
    assert len(result["jobs"]) == 2
    assert result["duplicates"] == 0


# --- failed, cancelled and zero-length jobs count for cost and wait ----------------


def test_a_failed_row_is_billed_and_counted_in_wait_time(tmp_path):
    result = replay(
        tmp_path, rows_from(tmp_path, f"{at(0)},vision,1,30,3,normal,failed")
    )
    [job] = result["jobs"]
    assert job["status"] == "failed"
    assert job["started_at"] - job["submitted_at"] == 60  # its wait: the boot
    assert job["ended_at"] - job["started_at"] == 180
    # gpu-s ($1.00/h) launched at 0, job 60..240, idle since 240, reaped at
    # 240 + 600 + 1 = 841 (> its 600 s minimum life): 841 billed seconds.
    vision = result["cost"]["teams"]["vision"]
    assert vision["total"] == PRICE_MICROS["gpu-s"] * 841 // 3600
    assert vision["busy"] == PRICE_MICROS["gpu-s"] * 180 // 3600


def test_a_zero_minute_job_placed_during_its_own_post_still_ends_at_its_start(tmp_path):
    # Row 1 leaves a ready, idle vision node. Row 2 arrives while that node is
    # idle, so POST /jobs starts it in its own scheduling pass, BEFORE the
    # replay attaches its plan (actual_minutes=0). It must still end at the
    # moment it started, not at some later group.
    rows = rows_from(
        tmp_path,
        f"{at(0)},vision,1,30,5,normal,succeeded",  # node ready at 1, job ends at 6
        f"{at(8)},vision,1,30,0,normal,succeeded",
    )
    second = replay(tmp_path, rows)["jobs"][1]
    assert second["started_at"] == START + 8 * 60  # placed in its own POST
    assert (second["status"], second["ended_at"]) == ("succeeded", START + 8 * 60)


# --- invariants --------------------------------------------------------------------


def busy_rows(tmp_path):
    """Several teams and sizes, more demand than a 3-node cap allows."""
    lines = []
    for i in range(30):
        team = ("vision", "nlp", "speech")[i % 3]
        gpus = (1, 2, 4, 8, 1)[i % 5]
        lines.append(
            f"{at(i // 2)},{team},{gpus},30,{5 + (i * 7) % 40},normal,succeeded"
        )
    return rows_from(tmp_path, *lines)


def test_invariants_hold_throughout_a_crowded_scenario(tmp_path):
    cfg = Config(max_nodes=3, never_ready_prob=0.3, outage_start_prob=0.2, seed=7)
    result = replay(tmp_path, busy_rows(tmp_path), cfg=cfg, check=True)
    assert result["invariant_checks"] > 30  # checked after every advance
    assert {j["status"] for j in result["jobs"]} == {"succeeded"}


@pytest.mark.parametrize(
    "corrupt, word",
    [
        # both 1-GPU jobs on the 1-GPU node n1
        ("UPDATE jobs SET status = 'running', node_id = 'n1', started_at = 0", "GPUs"),
        (
            "UPDATE jobs SET status = 'running', node_id = 'n2', started_at = 0 WHERE rowid = 1",
            "team",
        ),
        (
            "UPDATE jobs SET status = 'running', node_id = 'n3', started_at = 0 WHERE rowid = 1",
            "ready",
        ),
    ],
    ids=["overcommit", "other-team", "not-ready"],
)
def test_the_invariant_check_catches_a_broken_state(tmp_path, corrupt, word):
    # Guards the guard: a check that can't fail proves nothing.
    conn = connect(tmp_path / "broken.db")
    init_db(conn)
    with transaction(conn):
        for node_id, team, state in [("n1", "vision", "ready"), ("n2", "nlp", "ready"),
                                     ("n3", "vision", "launching")]:  # fmt: skip
            conn.execute(
                """INSERT INTO nodes (id, node_type, gpus_total, price_per_hour_micros,
                                      team, state, launched_at)
                   VALUES (?, 'gpu-s', 1, 1000000, ?, ?, 0)""",
                (node_id, team, state),
            )
        for _ in range(2):
            conn.execute(
                """INSERT INTO jobs (id, team, request_fingerprint, gpus, estimated_minutes,
                                     priority, status, submitted_at)
                   VALUES (?, 'vision', 'x', 1, 30, 'normal', 'queued', 0)""",
                (service.new_id("job"),),
            )
        conn.execute(corrupt)
    with pytest.raises(InvariantViolation, match=word):
        check_invariants(conn, Config())
    conn.close()


def test_the_invariant_check_catches_too_many_nodes(tmp_path):
    conn = connect(tmp_path / "broken.db")
    init_db(conn)
    with transaction(conn):
        for i in range(3):
            conn.execute(
                """INSERT INTO nodes (id, node_type, gpus_total, price_per_hour_micros,
                                      team, state, launched_at)
                   VALUES (?, 'gpu-s', 1, 1000000, 'vision', 'ready', 0)""",
                (f"n{i}",),
            )
    with pytest.raises(InvariantViolation, match="alive"):
        check_invariants(conn, Config(max_nodes=2))
    conn.close()


# --- cost ----------------------------------------------------------------------------


def test_the_grand_total_equals_every_node_billed_independently(tmp_path):
    # Random never-ready launches on, so failed nodes (provider overhead) are in it.
    cfg = Config(max_nodes=3, never_ready_prob=0.3, outage_start_prob=0.2, seed=7)
    result = replay(tmp_path, busy_rows(tmp_path), cfg=cfg)
    assert any(n["state"] == "failed" for n in result["nodes"])  # overhead exercised
    cost = result["cost"]
    assert cost["grand_total"] == independent_grand_total(tmp_path / "replay.db")
    assert cost["grand_total"] == (
        sum(t["total"] for t in cost["teams"].values()) + cost["provider_overhead"]
    )
    assert all(isinstance(v, int) for t in cost["teams"].values() for v in t.values())


def test_the_cost_window_holds_every_node_and_its_minimum_charge(tmp_path):
    # No minimum life and no idle grace, so the node ends well under 600 s:
    # launched 0, ready 60, job 60..120, reaped at 121, billed 600 s.
    cfg = Config(never_ready_prob=0.0, outage_start_prob=0.0, boot_min_s=60,
                 boot_max_s=60, min_node_lifetime_s=0, idle_minutes=0)  # fmt: skip
    result = replay(tmp_path, rows_from(tmp_path, f"{at(0)},vision,1,30,1,normal,succeeded"),
                    cfg=cfg)  # fmt: skip
    [node] = result["nodes"]
    assert node["terminated_at"] - node["launched_at"] == 121
    assert result["cost_window"] == (START, result["final_now"] + 1)
    assert result["cost"]["grand_total"] == PRICE_MICROS["gpu-s"] * 600 // 3600


# --- guards ------------------------------------------------------------------------------


def test_an_existing_database_is_refused(tmp_path):
    (tmp_path / "replay.db").write_text("")
    with pytest.raises(FileExistsError):
        replay(tmp_path, rows_from(tmp_path, f"{at(0)},vision,1,30,3,normal,failed"))


def test_a_replay_that_never_drains_fails_loudly_with_what_is_stuck(tmp_path):
    cfg = Config(scheduler_enabled=False)  # nothing is ever placed
    rows = rows_from(tmp_path, f"{at(0)},vision,1,30,3,normal,succeeded")
    with pytest.raises(RuntimeError, match=r"14 extra days.*queued.*vision"):
        replay(tmp_path, rows, cfg=cfg)


def test_the_hidden_plan_cannot_be_set_over_http():
    # attach_plan is a service function for the simulator, never a route.
    paths = {route.path for route in create_app(":memory:", CFG).routes}
    assert not any("plan" in p for p in paths)
    assert {"/jobs", "/sim/advance"} <= paths
