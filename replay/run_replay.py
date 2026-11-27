"""Replay a cleaned trace through the real HTTP API on the simulated clock.

    python -m replay.run_replay --trace 01a_trace.csv [--policies fair,fifo]
        [--idle-minutes 10] [--sweep-idle 2,10,30] [--seeds 42,43,44,45,46]
        [--burst-window 2025-03-05T05:00Z,2025-03-05T08:00Z]
        [--keep-duplicates] [--check-invariants] [--out-dir reports]
        [--analysis reports/replay_analysis.md]

Writes data_quality.md, replay_results.md and replay_results.json. The
markdown's Analysis section is copied from reports/replay_analysis.md (an
authored file) when it exists, so a rerun never overwrites it.

replay_scenario() drives the app with FastAPI's TestClient: the real routes,
validation, idempotency and scheduling passes, in process, with no server
and no real sleeps. For each accepted row, in time order:

  1. POST /sim/advance {to_ts: row time}
  2. POST /jobs with the row's request fields and its idempotency key
  3. 201 (a new job): attach the row's hidden plan with service.attach_plan,
     on this module's own connection. 200 (the API recognised the key): the
     row repeats an earlier one, so it is counted, not submitted twice.

Replay rule: every job, failed and cancelled rows included, runs for
actual_minutes after it STARTS, then ends with the row's outcome. A cancelled
row means "the user cancelled after actual_minutes of runtime", so it counts
for both cost and wait time.

After the last row, time moves on in 60-minute steps until nothing is queued
or running and no node is alive; after 14 extra days it fails with what is
stuck, rather than reporting numbers for an unfinished week.
"""

import argparse
import json
import re
import sqlite3
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from fastapi.testclient import TestClient

from broker import costs, service
from broker.api import create_app
from broker.config import Config
from broker.db import connect, init_db
from broker.state import BROKER_STATE_FOR_PROVIDER
from broker.trace import (
    DEFAULT_BURST_WINDOW,
    TraceRow,
    load_trace,
    parse_ts,
    parse_window,
    to_iso,
)
from replay.metrics import (
    BURST_METHOD,
    PERCENTILE_METHOD,
    aggregate,
    burst_sample_times,
    scenario_metrics,
)

DRAIN_STEP_MINUTES = 60
# The Analysis section is authored, not generated: run_all copies this file
# under "## Analysis" (tests/test_replay_analysis.py). Without it, the four
# headings are printed as TODO placeholders.
DEFAULT_ANALYSIS = Path("reports/replay_analysis.md")
ANALYSIS_HEADINGS = (
    "fair vs fifo",
    "the burst window",
    "idle timeout: cost vs wait",
    "what we would change",
)
DRAIN_LIMIT_S = 14 * 24 * 3600


# SQL for "the broker's state is the one paired with the provider's".
_PAIRS = " OR ".join(
    f"(p.state = '{p}' AND n.state = '{b}')"
    for p, b in BROKER_STATE_FOR_PROVIDER.items()
)


class InvariantViolation(AssertionError):
    pass


def check_invariants(conn: sqlite3.Connection, cfg: Config) -> None:
    """Raise InvariantViolation if the broker's state breaks a brief rule."""
    alive = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE state IN ('launching', 'ready')"
    ).fetchone()[0]
    if alive > cfg.max_nodes:
        raise InvariantViolation(
            f"{alive} nodes alive, more than max_nodes={cfg.max_nodes}"
        )
    checks = [
        ("running GPUs exceed the node's gpus_total",
         """SELECT n.id, n.gpus_total, SUM(j.gpus) FROM nodes n
            JOIN jobs j ON j.node_id = n.id AND j.status = 'running'
            GROUP BY n.id HAVING SUM(j.gpus) > n.gpus_total"""),
        # A job on a node of another team. Since every job must match its
        # node's owner, this also rules out a node running two teams' jobs.
        ("a job runs on a node owned by another team",
         """SELECT j.id, j.team, n.id, n.team FROM jobs j JOIN nodes n ON n.id = j.node_id
            WHERE j.status = 'running' AND j.team != n.team"""),
        ("a running job is not on a ready node",
         """SELECT j.id, j.node_id, n.state FROM jobs j LEFT JOIN nodes n ON n.id = j.node_id
            WHERE j.status = 'running' AND (n.state IS NULL OR n.state != 'ready')"""),
        # Broker and provider agree on every node (state.BROKER_STATE_FOR_PROVIDER).
        # Last, so a broken job placement is reported under its own message.
        ("a node is unknown to the provider",
         """SELECT n.id, n.state FROM nodes n LEFT JOIN provider_nodes p ON p.id = n.id
            WHERE p.id IS NULL"""),
        ("broker and provider disagree on a node's state",
         f"""SELECT n.id, n.state, p.state FROM nodes n JOIN provider_nodes p ON p.id = n.id
             WHERE NOT ({_PAIRS})"""),
    ]  # fmt: skip
    for message, sql in checks:
        bad = conn.execute(sql).fetchall()
        if bad:
            raise InvariantViolation(f"{message}: {[tuple(r) for r in bad]}")


class _Run:
    """One replay's HTTP client, its own DB connection and its counters."""

    def __init__(self, client: TestClient, conn, cfg: Config, check: bool):
        self.client, self.conn, self.cfg, self.check = client, conn, cfg, check
        self.duplicates = 0
        self.invariant_checks = 0
        self.lines: dict[str, int] = {}  # job id -> trace line that created it

    def _post(self, path: str, body: dict, ok: tuple[int, ...]):
        r = self.client.post(path, json=body)
        if r.status_code not in ok:
            # The loader applies the API's rules, so any other status is a bug.
            raise RuntimeError(f"POST {path} {body} -> {r.status_code}: {r.text}")
        if self.check:
            check_invariants(self.conn, self.cfg)
            self.invariant_checks += 1
        return r

    def submit(self, row: TraceRow) -> None:
        self._post("/sim/advance", {"to_ts": row.utc_ts}, (200,))
        body = {"team": row.team, "gpus": row.gpus, "priority": row.priority,
                "estimated_minutes": row.estimated_minutes,
                "idempotency_key": row.idempotency_key}  # fmt: skip
        r = self._post("/jobs", body, (200, 201))
        if r.status_code == 200:
            self.duplicates += 1
            return
        job_id = r.json()["id"]
        # After the POST's own scheduling pass, so the job may already be
        # running. That is fine: its end is started_at + actual_minutes, and
        # the next advance first runs a group at the current time, so even a
        # 0-minute job ends at the moment it started.
        service.attach_plan(self.conn, job_id, row.actual_minutes, row.outcome)
        self.lines[job_id] = row.line

    def drain(self) -> None:
        deadline = service.get_now(self.conn) + DRAIN_LIMIT_S
        while self._busy():
            if service.get_now(self.conn) >= deadline:
                raise RuntimeError(self._stuck())
            self._post("/sim/advance", {"minutes": DRAIN_STEP_MINUTES}, (200,))

    def _busy(self) -> bool:
        return (
            self.conn.execute(
                """SELECT EXISTS (SELECT 1 FROM jobs WHERE status IN ('queued', 'running'))
                   OR EXISTS (SELECT 1 FROM nodes WHERE state IN ('launching', 'ready'))"""
            ).fetchone()[0]
            == 1
        )

    def _stuck(self) -> str:
        jobs = self.conn.execute(
            """SELECT id, status, team, gpus FROM jobs
               WHERE status IN ('queued', 'running') ORDER BY created_seq"""
        ).fetchall()
        nodes = self.conn.execute(
            "SELECT id, state, team FROM nodes WHERE state IN ('launching', 'ready') ORDER BY id"
        ).fetchall()
        return (
            f"replay did not drain within {DRAIN_LIMIT_S // 86400} extra days. "
            f"Stuck jobs: {[f'{j[0]} {j[1]} team={j[2]} gpus={j[3]}' for j in jobs]}. "
            f"Alive nodes: {[f'{n[0]} {n[1]} team={n[2]}' for n in nodes]}"
        )


def replay_scenario(rows: list[TraceRow], cfg: Config, db_path: str | Path,
                    check_invariants: bool = False) -> dict:  # fmt: skip
    """Replay rows (sorted, from broker.trace.load_trace) on a NEW database file.

    Returns raw results: jobs, nodes, events, the duplicate count and the
    cost totals in integer micro-dollars over [first submission, final now + 1),
    so nodes terminated at the final moment are included with their minimum
    charge. Job ids are the creation sequence (SPEC_DECISIONS.md #16), so the
    same rows and config give the same ids; "line" maps each job to its row.
    """
    if not rows:
        raise ValueError("no accepted rows to replay")
    db_path = Path(db_path)
    if db_path.exists():
        # A fresh database per scenario: leftover jobs or nodes would mix runs.
        raise FileExistsError(f"replay database already exists: {db_path}")
    conn = connect(db_path)
    try:
        start = rows[0].utc_ts
        init_db(conn, start_ts=start)  # the app's own init_db then keeps this clock
        with TestClient(create_app(db_path, cfg)) as client:  # startup: reconcile
            run = _Run(client, conn, cfg, check_invariants)
            for row in rows:
                run.submit(row)
            run.drain()
        return _results(conn, start, run)
    finally:
        conn.close()


def _results(conn, start: int, run: _Run) -> dict:
    final_now = service.get_now(conn)
    window = (start, final_now + 1)
    report = costs.cost_by_team(conn, *window, now=final_now)
    jobs = conn.execute(
        """SELECT id, team, gpus, priority, estimated_minutes, status, end_reason,
                  node_id, submitted_at, started_at, ended_at
           FROM jobs ORDER BY created_seq"""
    ).fetchall()
    nodes = conn.execute(
        """SELECT id, node_type, team, state, launched_at, ready_at, terminated_at,
                  end_reason FROM nodes ORDER BY id"""
    ).fetchall()
    events = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    return {
        "jobs": [{**dict(j), "line": run.lines[j["id"]]} for j in jobs],
        "nodes": [dict(n) for n in nodes],
        "events": [dict(e) for e in events],
        "duplicates": run.duplicates,
        "invariant_checks": run.invariant_checks,
        "final_now": final_now,
        "cost_window": window,
        "cost": {
            "teams": {team: asdict(c) for team, c in sorted(report.teams.items())},
            "provider_overhead": report.provider_overhead,
            "grand_total": report.grand_total,
        },
    }


# --- many scenarios: the policy comparison (requirement 8) ---------------------------

METHOD = {
    "percentile": PERCENTILE_METHOD,
    "burst": BURST_METHOD,
    "wait": "started_at - submitted_at, in seconds",
    "money": "integer micro-dollars (1 USD = 1_000_000); dollars only in the markdown",
    "idle_share": "team: idle / team total; overall: teams' idle / grand total "
    "(provider overhead, i.e. never-ready nodes, is reported separately)",
    "across_seeds": "median = lower median (an observed value), with min and max",
    "seeds": "boot times and failure draws are keyed by node launch number, which "
    "differs between policies; a policy difference counts only if it holds in "
    "every seed",
    "replay_rule": "every job runs actual_minutes after it starts, then ends with "
    "the row's outcome; a cancel means cancelled after actual_minutes of runtime",
}


def read_analysis(path, required: bool = True) -> str | None:
    """The authored analysis, checked to hold the four headings in order.

    None for no path, or for a missing file when not required (the CLI's
    default path on a checkout without one).
    """
    if path is None:
        return None
    path = Path(path)
    if not path.exists():
        if required:
            raise FileNotFoundError(f"analysis file not found: {path}")
        return None
    text = path.read_text(encoding="utf-8")
    found = re.findall(r"^### (.+?)\s*$", text, flags=re.MULTILINE)
    missing = [h for h in ANALYSIS_HEADINGS if h not in found]
    if missing:
        raise ValueError(f"{path}: missing analysis heading(s): {missing}")
    if [h for h in found if h in ANALYSIS_HEADINGS] != list(ANALYSIS_HEADINGS):
        raise ValueError(f"{path}: headings must be in this order: {ANALYSIS_HEADINGS}")
    return text.rstrip("\n") + "\n"


def scenarios(policies, idle_minutes: int, sweep) -> list[tuple[str, int]]:
    """Unique (policy, idle_minutes) pairs: each policy at idle_minutes, then
    the first policy at each sweep value. fair@10 appears once, not twice."""
    out: list[tuple[str, int]] = []
    for combo in [(p, idle_minutes) for p in policies] + [
        (policies[0], s) for s in sweep
    ]:
        if combo not in out:
            out.append(combo)
    return out


def run_all(trace_path, *, policies=("fair", "fifo"), idle_minutes=10,
            sweep=(2, 10, 30), seeds=(42, 43, 44, 45, 46),
            burst_window=DEFAULT_BURST_WINDOW, keep_duplicates=False,
            check_invariants=False, out_dir="reports",
            analysis=None) -> dict:  # fmt: skip
    """Run every scenario once per seed; write the three reports; return the JSON.

    analysis: an authored analysis file (read_analysis), copied into the
    markdown under "## Analysis"; None prints the TODO headings.
    """
    analysis_text = read_analysis(analysis)  # fail before a long run, not after
    rows, quality = load_trace(trace_path, keep_duplicates, burst_window)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data_quality.md").write_text(quality.to_markdown(), encoding="utf-8")
    base = {k: v for k, v in asdict(Config()).items()
            if k not in ("seed", "policy", "idle_minutes")}  # fmt: skip
    results = {
        "method": METHOD,
        # The file name, not its path: the JSON must not depend on where it ran.
        "trace": {
            "file": Path(trace_path).name,
            "total_rows": quality.total_rows,
            "accepted": quality.accepted,
            "duplicate_rows": quality.duplicate_rows,
            "rejected": [[r.line, r.reason] for r in quality.rejected],
        },
        "options": {
            "policies": list(policies),
            "idle_minutes": idle_minutes,
            "sweep_idle": list(sweep),
            "seeds": list(seeds),
            "burst_window": [to_iso(t) for t in burst_window],
            "keep_duplicates": keep_duplicates,
            "check_invariants": check_invariants,
            "config": base,
        },
        "scenarios": [],
    }
    with tempfile.TemporaryDirectory() as tmp:  # one fresh database per run
        for policy, idle in scenarios(policies, idle_minutes, sweep):
            runs = {}
            for seed in seeds:
                cfg = Config(seed=seed, policy=policy, idle_minutes=idle)
                db = Path(tmp) / f"{policy}-{idle}-{seed}.db"
                raw = replay_scenario(rows, cfg, db, check_invariants)
                runs[str(seed)] = scenario_metrics(raw, burst_window)
            results["scenarios"].append({
                "name": f"{policy}@{idle}", "policy": policy, "idle_minutes": idle,
                "runs": runs, "summary": aggregate(list(runs.values())),
            })  # fmt: skip
    text = json.dumps(results, indent=2) + "\n"
    (out_dir / "replay_results.json").write_text(text, encoding="utf-8")
    md = render_markdown(results, quality.summary(), analysis_text)
    (out_dir / "replay_results.md").write_text(md, encoding="utf-8")
    return results


# --- markdown ------------------------------------------------------------------------


def _usd(micros) -> str:
    return f"${costs.micros_to_dollars(micros):,.2f}"


def _min(seconds) -> str:
    return f"{seconds / 60:.1f}"


def _pct(ratio) -> str:
    return f"{ratio * 100:.1f}%"


def _gpu_h(seconds) -> str:
    return f"{seconds / 3600:,.1f}"


def _cell(stat, fmt, with_range=True) -> str:
    """'median (min–max)', or just the value when every seed agrees."""
    if stat is None:
        return "–"
    if not with_range or stat["min"] == stat["max"]:
        return fmt(stat["median"])
    return f"{fmt(stat['median'])} ({fmt(stat['min'])}–{fmt(stat['max'])})"


def _per_seed(a: dict, b: dict, path) -> str:
    """Seeds where a < b / a = b / a > b, comparing the same seed's runs."""
    lt = eq = gt = 0
    for seed in sorted(set(a["runs"]) & set(b["runs"]), key=int):
        x, y = a["runs"][seed], b["runs"][seed]
        for key in path:
            x, y = x[key], y[key]
        if x is None or y is None:
            continue
        lt, eq, gt = lt + (x < y), eq + (x == y), gt + (x > y)
    return f"{lt} / {eq} / {gt}"


def render_markdown(
    results: dict, quality_summary: str, analysis: str | None = None
) -> str:
    opts = results["options"]
    by_name = {s["name"]: s for s in results["scenarios"]}
    main = [
        s for s in results["scenarios"] if s["idle_minutes"] == opts["idle_minutes"]
    ]
    n = len(opts["seeds"])
    md = ["# Replay results", "",
          (f"Trace `{results['trace']['file']}`, replayed through the HTTP API on the "
          f"simulated clock. Seeds: {', '.join(map(str, opts['seeds']))}. Burst window: "
          f"[{opts['burst_window'][0]}, {opts['burst_window'][1]})."), "",
          (f"Method: percentiles are {PERCENTILE_METHOD}. Wait = started_at − "
          "submitted_at. Each cell is the median across seeds (the lower median for an "
          "even count) with the min–max range in brackets when the seeds differ. "
          "Every number is in `replay_results.json` (money in micro-dollars)."), "",
          "## 1. Data quality", "", "Full report: [data_quality.md](data_quality.md).",
          "", "```text", quality_summary, "```", ""]  # fmt: skip

    md += [
        "## 2. Per team (median across seeds)",
        "",
        # Medians are taken per cell, so they may come from different seeds.
        (
            "Each cell is that number's median across seeds, taken on its own, so "
            "team rows need not add up to the **all** row. Per-seed values are in "
            "`replay_results.json`."
        ),
        "",
    ]
    cols = ("team", "jobs", "wait p50 (min)", "p90", "p99", "max", "started ≤ 10 min",
            "GPU-h", "busy", "idle", "total", "idle share", "nodes", "never ready",
            "capacity refusals", "peak queue", "peak nodes")  # fmt: skip
    fields = (("jobs", str), ("wait_s_p50", _min), ("wait_s_p90", _min),
              ("wait_s_p99", _min), ("wait_s_max", _min), ("started_within_10_min", _pct),
              ("gpu_seconds", _gpu_h), ("busy", _usd), ("idle", _usd), ("total", _usd),
              ("idle_share", _pct), ("nodes_launched", str), ("never_ready_nodes", str),
              ("capacity_unavailable", str), ("peak_queue", str),
              ("peak_alive_nodes", str))  # fmt: skip
    for s in main:
        md += [f"### {s['name']}", "", "| " + " | ".join(cols) + " |",
               "|" + "---|" * len(cols)]  # fmt: skip
        groups = [*s["summary"]["teams"].items(), ("**all**", s["summary"]["overall"])]
        for team, m in groups:
            md.append(f"| {team} | " + " | ".join(
                _cell(m[f], fmt, with_range=False) for f, fmt in fields) + " |")  # fmt: skip
        o = s["summary"]["overall"]
        md += ["", (f"Provider overhead (never-ready nodes): "
               f"{_cell(o['provider_overhead'], _usd, False)}. Grand total: "
               f"{_cell(o['grand_total'], _usd, False)}."), ""]  # fmt: skip

    fair = by_name.get(f"fair@{opts['idle_minutes']}")
    fifo = by_name.get(f"fifo@{opts['idle_minutes']}")
    md += ["## 3. Fair vs fifo", ""]
    if fair and fifo:
        md += [(f"Both at idle timeout {opts['idle_minutes']} min. Last column: in how "
               f"many of the {n} seeds fair is lower / equal / higher than fifo, "
               "comparing the same seed."), "",
               "| metric | fair | fifo | seeds fair < / = / > fifo |", "|---|---|---|---|"]  # fmt: skip

        def row(label, path, fmt):
            a, b = fair["summary"], fifo["summary"]
            for key in path:
                a, b = a[key], b[key]
            md.append(f"| {label} | {_cell(a, fmt)} | {_cell(b, fmt)} | "
                      f"{_per_seed(fair, fifo, path)} |")  # fmt: skip

        row("grand total", ("overall", "grand_total"), _usd)
        row("idle share", ("overall", "idle_share"), _pct)
        row("started ≤ 10 min, all", ("overall", "started_within_10_min"), _pct)
        row("wait p90 (min), all", ("overall", "wait_s_p90"), _min)
        teams = list(fair["summary"]["teams"])
        for team in teams:
            row(f"wait p90 (min), {team}", ("teams", team, "wait_s_p90"), _min)
        for team in teams:
            if fair["summary"]["teams"][team]["burst_wait_s_p90"] is not None:
                row(f"burst wait p90 (min), {team}",
                    ("teams", team, "burst_wait_s_p90"), _min)  # fmt: skip
        for team in teams:
            row(
                f"started ≤ 10 min, {team}",
                ("teams", team, "started_within_10_min"),
                _pct,
            )
    else:
        md.append("Not run: needs both policies (`--policies fair,fifo`).")
    md.append("")

    first = opts["policies"][0]
    sweep = sorted((s for s in results["scenarios"] if s["policy"] == first),
                   key=lambda s: s["idle_minutes"])  # fmt: skip
    md += ["## 4. Idle timeout sweep", "", f"Policy {first}.", "",
           "| idle minutes | grand total | idle share | wait p90 (min), all | nodes launched |",
           "|---|---|---|---|---|"]  # fmt: skip
    for s in sweep:
        o = s["summary"]["overall"]
        md.append(f"| {s['idle_minutes']} | {_cell(o['grand_total'], _usd)} | "
                  f"{_cell(o['idle_share'], _pct)} | {_cell(o['wait_s_p90'], _min)} | "
                  f"{_cell(o['nodes_launched'], str)} |")  # fmt: skip
    md += [""] + _burst_section(results, main, fair, fifo)
    md += ["## Analysis", ""]
    if analysis is None:
        return "\n".join(md + [f"- TODO: {h}" for h in ANALYSIS_HEADINGS] + [""])
    return "\n".join(md) + "\n" + analysis  # a blank line after the heading


def _signed_min(seconds) -> str:
    return f"{seconds / 60:+.1f}"


def _burst_section(results, main, fair, fifo) -> list[str]:
    """## 5: queue over time, peak and recovery in the burst window."""
    opts = results["options"]
    window = tuple(parse_ts(t) for t in opts["burst_window"])
    md = ["## 5. Burst window", "",
          (f"Window [{opts['burst_window'][0]}, {opts['burst_window'][1]}), idle timeout "
           f"{opts['idle_minutes']} min. Queue = jobs submitted and not yet started, at "
           "that instant. Peak: the largest queue at any instant in the window, and how "
           "long after its start it was first reached. Recovery: when the last job "
           "submitted in the window started, minus the window end (negative: before it "
           "ended). Median (min–max) across seeds."), ""]  # fmt: skip
    if not main:
        return md + ["Not run.", ""]
    both = fair is not None and fifo is not None
    head = ["metric", *(s["name"] for s in main)] + (
        ["seeds fair < / = / > fifo"] * both
    )
    md += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]

    def row(label, path, fmt):
        cells = []
        for s in main:
            x = s["summary"]
            for key in path:
                x = x[key]
            cells.append(_cell(x, fmt))
        if both:
            cells.append(_per_seed(fair, fifo, path))
        md.append(f"| {label} | " + " | ".join(cells) + " |")

    row("jobs submitted in the window", ("overall", "burst_jobs"), str)
    row("peak queue in the window", ("overall", "burst_peak_queue"), str)
    row("peak reached after (min)", ("overall", "burst_peak_after_s"), _min)
    row("recovery (min)", ("overall", "burst_recovery_s"), _signed_min)
    for team, m in main[0]["summary"]["teams"].items():
        if m["burst_recovery_s"] is not None:
            row(f"recovery (min), {team}", ("teams", team, "burst_recovery_s"),
                _signed_min)  # fmt: skip
    md += ["", "Queue length, all teams (per team in `replay_results.json`):", "",
           "| time (UTC) | " + " | ".join(s["name"] for s in main) + " |",
           "|" + "---|" * (len(main) + 1)]  # fmt: skip
    for i, t in enumerate(burst_sample_times(window)):
        cells = [_cell(s["summary"]["overall"]["burst_queue"][i], str) for s in main]
        md.append(f"| {to_iso(t)[:16]}Z | " + " | ".join(cells) + " |")
    return md + [""]


# --- CLI -----------------------------------------------------------------------------


def _ints(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Replay a job trace and compare policies.")
    p.add_argument("--trace", default="01a_trace.csv")
    p.add_argument("--policies", default="fair,fifo", type=lambda t: t.split(","))
    p.add_argument("--idle-minutes", type=int, default=10)
    p.add_argument("--sweep-idle", type=_ints, default=[2, 10, 30])
    p.add_argument("--seeds", type=_ints, default=[42, 43, 44, 45, 46])
    p.add_argument("--burst-window", type=parse_window, default=DEFAULT_BURST_WINDOW)
    p.add_argument("--keep-duplicates", action="store_true")
    p.add_argument("--check-invariants", action="store_true")
    p.add_argument("--out-dir", default="reports")
    p.add_argument("--analysis", type=Path, default=None,
                   help=f"authored Analysis section (default: {DEFAULT_ANALYSIS} if it exists)")  # fmt: skip
    args = p.parse_args(argv)
    # perf_counter only measures this run for the console; it never reaches
    # the reports or the simulation, which run on the simulated clock.
    started = time.perf_counter()
    analysis = args.analysis
    if analysis is None and read_analysis(DEFAULT_ANALYSIS, required=False) is not None:
        analysis = DEFAULT_ANALYSIS
    results = run_all(args.trace, policies=args.policies, idle_minutes=args.idle_minutes,
                      sweep=args.sweep_idle, seeds=args.seeds,
                      burst_window=args.burst_window, keep_duplicates=args.keep_duplicates,
                      check_invariants=args.check_invariants, out_dir=args.out_dir,
                      analysis=analysis)  # fmt: skip
    for s in results["scenarios"]:
        o = s["summary"]["overall"]
        print(f"{s['name']:>8}: grand total {_cell(o['grand_total'], _usd)}, "
              f"wait p90 {_cell(o['wait_s_p90'], _min)} min")  # fmt: skip
    print(
        f"written: {args.out_dir}/replay_results.md, replay_results.json, data_quality.md"
    )
    print(f"runtime: {time.perf_counter() - started:.1f} s")


if __name__ == "__main__":
    main()
