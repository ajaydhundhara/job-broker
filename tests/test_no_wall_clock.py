"""The broker reads time only from sim_clock, never from the real clock, and
draws random numbers only from seeded generators (SPEC_DECISIONS.md #10).

Two checks, because each misses what the other catches:
  * static: scan broker/ source (Python and SQL) for wall-clock calls;
  * runtime: drive a full workload through the API with every wall-clock
    read from broker code made to fail (tests/wall_clock_guard.py). All YAML
    cases also run under this guard (tests/test_yaml_cases.py).
"""

import ast
import re
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from wall_clock_guard import WallClockRead, no_wall_clock

from broker.api import create_app
from broker.config import Config

BROKER = Path(__file__).resolve().parent.parent / "broker"
PY_FILES = sorted(BROKER.glob("*.py"))
SQL_FILES = sorted(BROKER.glob("*.sql"))

FORBIDDEN_IMPORTS = {"time"}
FORBIDDEN_CALLS = {"now", "utcnow", "today", "uuid1"}  # uuid1 embeds the clock
# From the random module only the generator class may be used, and only with a
# seed: random.random(), random.choice(), random.seed(), ... share one hidden
# global stream, and random.Random() with no seed is seeded from the OS.
ALLOWED_RANDOM = {"Random"}
# SQLite reads the real clock for these.
SQL_CLOCK = re.compile(r"CURRENT_(TIMESTAMP|DATE|TIME)|'now'", re.IGNORECASE)


def test_the_scan_finds_the_broker_sources():
    # Guards the guard: an empty glob would make every check below pass.
    assert {"api.py", "engine.py", "service.py", "scheduler.py", "reaper.py"} <= {
        p.name for p in PY_FILES
    }
    assert [p.name for p in SQL_FILES] == ["schema.sql"]


def scan(source: str) -> list[str]:
    """Wall-clock reads and uncontrolled randomness in Python source."""
    problems = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            problems += [a.name for a in node.names if a.name in FORBIDDEN_IMPORTS]
        elif isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_IMPORTS:
            problems.append(f"from {node.module} import ...")
        elif isinstance(node, ast.ImportFrom) and node.module == "random":
            problems += [f"from random import {a.name}" for a in node.names
                         if a.name not in ALLOWED_RANDOM]  # fmt: skip
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "random"
            and node.attr not in ALLOWED_RANDOM
        ):
            problems.append(f"random.{node.attr} at line {node.lineno}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if name in FORBIDDEN_CALLS:
                problems.append(f"{name}() at line {node.lineno}")
            if name == "Random" and not node.args and not node.keywords:
                problems.append(f"unseeded Random() at line {node.lineno}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and SQL_CLOCK.search(node.value)
        ):
            problems.append(f"SQL clock in string at line {node.lineno}")
    return problems


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_no_wall_clock_calls_in_python(path):
    assert scan(path.read_text()) == []


@pytest.mark.parametrize(
    "source",
    [
        "import random\nrandom.random()",
        "import random\nrandom.seed(1)",
        "import random\nx = random.choice([1, 2])",
        "from random import randint",
        "import random\nrandom.Random()",
        "import time\ntime.time()",
        "from datetime import datetime\ndatetime.now()",
    ],
)
def test_the_scan_rejects_clock_reads_and_global_random(source):
    # Guards the guard: each of these must be reported.
    assert scan(source) != []


def test_the_scan_allows_seeded_generators():
    source = 'import random\ng = random.Random(f"{seed}:launch:{key}")\ng.random()'
    assert scan(source) == []


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.name)
def test_no_wall_clock_in_sql(path):
    # Strip comments first: a comment may explain the rule.
    sql = "\n".join(line.split("--")[0] for line in path.read_text().splitlines())
    assert SQL_CLOCK.findall(sql) == []


# --- runtime ------------------------------------------------------------------------


def test_the_guard_catches_broker_code_reading_the_clock():
    # Proves the guard is live: broker.service's datetime is the guarded one.
    from broker import service

    with no_wall_clock() as calls, pytest.raises(WallClockRead):
        service.datetime.now()
    assert calls == ["datetime.now"]


def _reads_the_clock():
    return time.time()


def test_the_guard_catches_time_calls_from_broker_frames_only():
    # The same code, once with a broker.* module name (the application) and
    # once as this test module: only the first is refused.
    as_app = types.FunctionType(
        _reads_the_clock.__code__, {"__name__": "broker.fake", "time": time}
    )
    with no_wall_clock() as calls:
        with pytest.raises(WallClockRead):
            as_app()
        assert _reads_the_clock() > 0
    assert calls == ["time.time"]


def test_full_workload_never_reads_the_wall_clock(tmp_path):
    # Default Config: random never-ready launches and outages are on.
    app = create_app(tmp_path / "broker.db", config=Config())
    with no_wall_clock() as calls, TestClient(app) as client:
        client.post("/sim/advance", json={"to_ts": 1_000_000})
        ids = []
        for team, gpus in [("vision", 1), ("vision", 4), ("nlp", 8), ("speech", 2)]:
            r = client.post("/jobs", json={"team": team, "gpus": gpus,
                                           "estimated_minutes": 30, "idempotency_key": team + str(gpus)})  # fmt: skip
            assert r.status_code == 201
            ids.append(r.json()["id"])
        client.post("/sim/advance", json={"minutes": 10})  # nodes become ready
        client.post(f"/jobs/{ids[0]}/complete", json={"outcome": "succeeded"})
        client.post(f"/jobs/{ids[1]}/cancel")
        client.post("/sim/advance", json={"minutes": 60})  # idle nodes are reaped
        for path in ["/health", "/queue", "/nodes?include_terminated=true",
                     "/reports/cost", "/sim/now", f"/jobs/{ids[2]}"]:  # fmt: skip
            assert client.get(path).status_code == 200
        assert client.get("/sim/now").json()["now_ts"] == 1_000_000 + 70 * 60
    assert calls == []
