"""Runs one YAML test case (tests/cases/*.yaml) against the Job Broker API.

The YAML is a specification written by hand; this module is the interpreter.
It knows the case format (given / when / then, plus the table short form) and
nothing about which answers are "right": every expected value comes from the
YAML file.

Two targets:
  * in-process (default): a fresh temp database and a FastAPI TestClient per case.
  * live server: set BROKER_BASE_URL (e.g. http://127.0.0.1:8765) and BROKER_DB
    (the file that server uses). Each case wipes the tables first. Restart
    cases are skipped there, because the runner can't restart that process.

Fixture rules (where the YAML leaves a choice, documented here once):
  * given.now_ts sets the clock directly in the DB (default 0). It is setup,
    not an API call, so it writes no event.
  * given.nodes are inserted with SQL. gpus_total and the price come from the
    node type (gpu-s 1/1.00, gpu-m 4/4.50, gpu-l 8/8.50 per hour).
  * given.jobs with `sql: true` are inserted with SQL and get deterministic ids
    in the order listed (job-000000000001, job-000000000002, ...), so ties in
    queue order are reproducible. Other jobs are created with POST /jobs at
    their submitted_at (default now_ts), then the clock is set to now_ts.
  * A job that names a node the case did not define gets a placeholder node
    (gpu-l, owned by the job's team, ready since 0), because the schema
    requires every node_id to exist.
  * Every given node also gets a matching provider_nodes row (the provider's
    own record), so the engine and the provider agree on what exists. A
    launching node becomes ready at its given ready_at (default: launched_at
    + 240 s); a failed one failed at its terminated_at.
  * given.provider and given.config both set fields of the one Config
    (SPEC_DECISIONS.md #4, #7); the YAML names map as in PROVIDER_KEYS and
    CONFIG_KEYS below. deterministic_launch_delay_seconds fixes the boot
    delay (boot_min_s = boot_max_s). Unless a case asks, the provider has no
    random never-ready launches and no random outages, so a case only sees a
    failure it asks for. Everything else is the Config default (fair policy,
    10-minute reaper, 10 live nodes, seed 42), never the BROKER_* environment,
    so results don't depend on the shell the tests run in.
  * Against a live server, given.provider and given.config are skipped: the
    runner can't change that process's settings.
"""

import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import httpx
import pytest
from fastapi.testclient import TestClient

from broker.api import create_app
from broker.config import Config
from broker.db import connect, init_db, transaction
from broker.service import fingerprint

NODE_TYPES = {  # type -> (gpus_total, price_per_hour_micros)
    "gpu-s": (1, 1_000_000),
    "gpu-m": (4, 4_500_000),
    "gpu-l": (8, 8_500_000),
}
MONEY_TOLERANCE = 0.005  # "~4.5" means within half a cent
# YAML given.config key -> Config field
CONFIG_KEYS = {"idle_timeout_minutes": "idle_minutes", "max_live_nodes": "max_nodes"}
# YAML given.provider keys (each converted in config() below)
PROVIDER_KEYS = {
    "deterministic_launch_delay_seconds",
    "launch_failure_probability",
    "unavailable_windows",
    "never_ready_launches",
}
DEFAULTS = Config()
# broker node state -> provider state
PROVIDER_STATE = {"launching": "pending", "ready": "ready",
                  "terminated": "terminated", "failed": "failed"}  # fmt: skip


def config(given: dict) -> Config:
    """The Config a case runs with: given.config + given.provider over defaults."""
    settings = given.get("config") or {}
    provider = given.get("provider") or {}
    for name, part, known in (("config", settings, CONFIG_KEYS),
                              ("provider", provider, PROVIDER_KEYS)):  # fmt: skip
        unknown = set(part) - set(known)
        if unknown:
            raise AssertionError(f"setup: unknown given.{name} keys {sorted(unknown)}")
    # Defaults first, so a case's own setting wins.
    fields = {"never_ready_prob": 0.0, "outage_start_prob": 0.0}
    fields.update({CONFIG_KEYS[k]: v for k, v in settings.items()})
    if "deterministic_launch_delay_seconds" in provider:
        delay = provider["deterministic_launch_delay_seconds"]
        fields.update(boot_min_s=delay, boot_max_s=delay)
    if "launch_failure_probability" in provider:
        fields["never_ready_prob"] = provider["launch_failure_probability"]
    if "unavailable_windows" in provider:
        fields["unavailable_windows"] = tuple(
            (node_type, w["from_ts"], w["to_ts"])
            for node_type, windows in provider["unavailable_windows"].items()
            for w in windows
        )
    if "never_ready_launches" in provider:
        fields["never_ready_launches"] = tuple(provider["never_ready_launches"])
    return Config(**fields)


# --- expanding the YAML into runnable cases ------------------------------------


def expand(cases: list[dict]) -> list[tuple[str, dict]]:
    """Turn the file into (test_id, case) pairs; a `table` becomes one per row."""
    out = []
    for case in cases:
        if "table" not in case:
            out.append((case["id"], case))
            continue
        for i, row in enumerate(case["table"], start=1):
            body = dict(case.get("base_body") or {})
            body.update(row.get("set") or {})
            for key in row.get("remove") or []:
                body.pop(key, None)
            label = ",".join(f"{k}={v}" for k, v in (row.get("set") or {}).items())
            label = label or "remove=" + ",".join(row.get("remove") or [])
            then = {"status": row["status"]}
            if "body" in row:
                then["body"] = row["body"]
            out.append(
                (
                    f"{case['id']}[{i}:{label}]",
                    {
                        **case,
                        "when": [{"request": case["endpoint"], "body": body}],
                        "then": then,
                    },
                )
            )
    return out


# --- matching expected against actual -------------------------------------------

ID_PATTERNS = {
    "ANY_JOB_ID": re.compile(r"^job-[0-9a-f]{12}$"),
    "ANY_NODE_ID": re.compile(r"^node-\d{5}$"),  # sequential (SPEC_DECISIONS.md #14)
}


def match(expected, actual, refs: dict, path: str, errors: list[str]) -> None:
    """Partial match: dicts check only the listed keys. Appends to errors."""
    if isinstance(expected, dict) and set(expected) == {"ref"}:
        expected = refs[expected["ref"]]
    if isinstance(expected, dict) and set(expected) == {"set"}:
        _match_unordered(expected["set"], actual, refs, path, errors)
        return
    if expected == "ANY":
        return
    if isinstance(expected, str) and expected in ID_PATTERNS:
        if not (isinstance(actual, str) and ID_PATTERNS[expected].match(actual)):
            errors.append(f"{path}: expected {expected}, got {actual!r}")
        return
    if isinstance(expected, str) and expected.startswith("~"):
        target = float(expected[1:])
        ok = isinstance(actual, int | float) and not isinstance(actual, bool)
        if not ok or abs(actual - target) > MONEY_TOLERANCE:
            errors.append(
                f"{path}: expected {expected} (±{MONEY_TOLERANCE}), got {actual!r}"
            )
        return
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            errors.append(f"{path}: expected an object, got {actual!r}")
            return
        for key, value in expected.items():
            if key not in actual:
                errors.append(
                    f"{path}.{key}: missing (response keys: {sorted(actual)})"
                )
            else:
                match(value, actual[key], refs, f"{path}.{key}", errors)
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            got = len(actual) if isinstance(actual, list) else type(actual).__name__
            errors.append(
                f"{path}: expected a list of {len(expected)}, got {got}: {actual!r}"
            )
            return
        for i, (e, a) in enumerate(zip(expected, actual, strict=True)):
            match(e, a, refs, f"{path}[{i}]", errors)
        return
    # Scalars. A bool never equals an int here (Python says True == 1).
    if isinstance(expected, bool) != isinstance(actual, bool) or expected != actual:
        errors.append(f"{path}: expected {expected!r}, got {actual!r}")


def _match_unordered(expected, actual, refs, path, errors):
    if not isinstance(actual, list) or len(actual) != len(expected):
        errors.append(f"{path}: expected a set of {len(expected)}, got {actual!r}")
        return
    remaining = list(actual)
    for e in expected:
        for i, a in enumerate(remaining):
            trial: list[str] = []
            match(e, a, refs, path, trial)
            if not trial:
                remaining.pop(i)
                break
        else:
            errors.append(f"{path}: no element matches {e!r} in {actual!r}")


# --- running one case -----------------------------------------------------------


class CaseRun:
    def __init__(self, case: dict, db_path):
        self.case = case
        self.db_path = db_path
        self.refs: dict[str, str] = {}
        self.live_url = os.environ.get("BROKER_BASE_URL")
        self.stack = ExitStack()
        self.client = None
        self.config = config(case.get("given") or {})

    # targets

    def _open_client(self):
        if self.live_url:
            self.client = self.stack.enter_context(
                httpx.Client(base_url=self.live_url, timeout=10)
            )
        else:
            app = create_app(self.db_path, config=self.config)
            self.client = self.stack.enter_context(TestClient(app))

    def _restart(self):
        if self.live_url:
            pytest.skip(
                "restart needs a process restart; not possible against a live server"
            )
        self.stack.close()  # app shutdown; the database file stays
        self.stack = ExitStack()
        self._open_client()

    def _db(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def _sql(self, sql, params=()):
        conn = self._db()
        try:
            with transaction(conn):
                conn.execute(sql, params)
        finally:
            conn.close()

    def _query(self, sql):
        conn = self._db()
        try:
            row = conn.execute(sql).fetchone()
        finally:
            conn.close()
        return None if row is None else row[0]

    # given

    def _reset_live_db(self):
        conn = self._db()
        init_db(conn)
        with transaction(conn):
            for table in ("sim_job_plans", "jobs", "nodes", "provider_nodes", "events"):
                conn.execute(f"DELETE FROM {table}")
        conn.close()

    def _set_clock(self, ts: int):
        self._sql("UPDATE sim_clock SET now_ts = ? WHERE id = 1", (ts,))

    def _insert_node(self, node: dict):
        gpus, price = NODE_TYPES[node["type"]]
        self._sql(
            """INSERT INTO nodes (id, node_type, gpus_total, interruptible,
                   price_per_hour_micros, team, state, launched_at, ready_at,
                   idle_since, terminated_at, end_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node["id"], node["type"], gpus, int(node.get("interruptible", 0)), price,
             node.get("team"), node["state"], node["launched_at"], node.get("ready_at"),
             node.get("idle_since"), node.get("terminated_at"), node.get("end_reason")),
        )  # fmt: skip
        self._insert_provider_node(node)

    def _insert_provider_node(self, node: dict):
        state = PROVIDER_STATE[node["state"]]
        launched = node["launched_at"]
        if state == "failed":
            ready_at = None
            fails_at = (
                node.get("terminated_at") or launched + DEFAULTS.never_ready_after_s
            )
        else:
            ready_at = node.get("ready_at")
            if ready_at is None:
                ready_at = launched + DEFAULTS.boot_min_s
            fails_at = None
        self._sql(
            """INSERT INTO provider_nodes (id, node_type, interruptible, requested_at,
                   ready_at_planned, fails_at_planned, state, terminated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (node["id"], node["type"], int(node.get("interruptible", 0)), launched,
             ready_at, fails_at, state, node.get("terminated_at")),
        )  # fmt: skip

    def _ensure_node(self, node_id: str, team: str):
        exists = self._query(f"SELECT COUNT(*) FROM nodes WHERE id = '{node_id}'")
        if not exists:
            self._insert_node({"id": node_id, "type": "gpu-l", "team": team,
                               "state": "ready", "launched_at": 0, "ready_at": 0})  # fmt: skip

    def _insert_job_sql(self, job: dict, n: int, now: int):
        job_id = f"job-{n:012x}"
        priority = job.get("priority", "normal")
        if job.get("node"):
            self._ensure_node(job["node"], job["team"])
        self._sql(
            """INSERT INTO jobs (id, team, idempotency_key, request_fingerprint, gpus,
                   estimated_minutes, priority, status, node_id, submitted_at,
                   started_at, ended_at, end_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, job["team"], job.get("idempotency_key"),
             fingerprint(job["team"], job["gpus"], job["estimated_minutes"], priority),
             job["gpus"], job["estimated_minutes"], priority,
             job.get("status", "queued"), job.get("node"),
             job.get("submitted_at", now), job.get("started_at"), job.get("ended_at"),
             job.get("end_reason")),
        )  # fmt: skip
        return job_id

    def _submit_job_api(self, job: dict, now: int):
        self._set_clock(job.get("submitted_at", now))
        body = {k: job[k] for k in ("team", "gpus", "estimated_minutes",
                                    "priority", "idempotency_key") if k in job}  # fmt: skip
        r = self.client.post("/jobs", json=body)
        if r.status_code not in (200, 201):
            raise AssertionError(f"setup: POST /jobs for ref {job.get('ref')} gave "
                                 f"{r.status_code} {r.text}")  # fmt: skip
        return r.json()["id"]

    def _apply_given(self):
        given = self.case.get("given") or {}
        for key in ("provider", "config"):
            if self.live_url and key in given:
                pytest.skip(f"given.{key} can't be set on a running server")
        if self.live_url:
            self._reset_live_db()
        now = given.get("now_ts", 0)
        self._set_clock(now)
        for node in given.get("nodes") or []:
            try:
                self._insert_node(node)
            except sqlite3.IntegrityError as e:
                raise AssertionError(f"setup: schema rejected node {node}: {e}") from e
        for n, job in enumerate(given.get("jobs") or [], start=1):
            if job.get("sql"):
                job_id = self._insert_job_sql(job, n, now)
            else:
                job_id = self._submit_job_api(job, now)
            if "ref" in job:
                self.refs[job["ref"]] = job_id
        self._set_clock(now)

    # when

    def _path(self, path: str) -> str:
        return re.sub(r"\{(\w+)\}", lambda m: self.refs.get(m[1], m[0]), path)

    def _send(self, step: dict) -> httpx.Response:
        method, path = step["request"].split(" ", 1)
        kwargs = {"params": step.get("query"), "headers": step.get("headers")}
        if "raw_body" in step:
            kwargs["content"] = step["raw_body"]
        elif "body" in step:
            kwargs["json"] = step["body"]
        return self.client.request(method, self._path(path), **kwargs)

    def _snapshot(self) -> dict:
        return {
            "events": self._query("SELECT COUNT(*) FROM events"),
            "sim_now": self.client.get("/sim/now").json(),
            "queue": self.client.get("/queue").json(),
        }

    def _run_steps(self):
        last, before = None, None
        for step in self.case.get("when") or []:
            if "restart" in step:
                self._restart()
            elif "advance" in step:
                before = self._snapshot()
                last = self.client.post("/sim/advance", json=step["advance"])
            elif "parallel_requests" in step:
                before = self._snapshot()
                with ThreadPoolExecutor(len(step["parallel_requests"])) as pool:
                    responses = list(pool.map(self._send, step["parallel_requests"]))
                # A crash in any one request is a failure even if the db
                # checks pass: that is exactly how the thread bug hid.
                crashed = [r.status_code for r in responses if r.status_code >= 500]
                if crashed:
                    raise AssertionError(f"parallel_requests: server errors {crashed}")
                last = None  # several responses; only db checks apply
            else:
                before = self._snapshot()
                last = self._send(step)
        return last, before

    # then

    def _check(self, last, before) -> list[str]:
        then = self.case.get("then") or {}
        errors: list[str] = []
        body = None
        if last is not None:
            try:
                body = last.json()
            except json.JSONDecodeError:
                body = last.text
        if "status" in then and (last is None or last.status_code != then["status"]):
            got = None if last is None else f"{last.status_code} {str(body)[:300]}"
            errors.append(f"status: expected {then['status']}, got {got}")
        if "body" in then:
            match(then["body"], body, self.refs, "body", errors)
        for i, check in enumerate(then.get("db") or []):
            match(check["equals"], self._query(check["sql"]), self.refs,
                  f"db[{i}] {check['sql']!r}", errors)  # fmt: skip
        if "events_added" in then:
            added = self._query("SELECT COUNT(*) FROM events") - before["events"]
            if added != then["events_added"]:
                errors.append(
                    f"events_added: expected {then['events_added']}, got {added}"
                )
        after = self._snapshot() if then.get("unchanged") else {}
        for name in then.get("unchanged") or []:
            if before[name] != after[name]:
                errors.append(
                    f"unchanged[{name}]: was {before[name]!r}, now {after[name]!r}"
                )
        return errors

    def run(self):
        with self.stack:
            self._open_client()
            self._apply_given()
            last, before = self._run_steps()
            errors = self._check(last, before)
        if errors:
            title = f"{self.case['id']}: {self.case.get('title', '')}"
            raise AssertionError(title + "\n  " + "\n  ".join(errors))
