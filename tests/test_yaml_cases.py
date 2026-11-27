"""Collects every case in tests/cases/*.yaml as one pytest test.

Run only these:   python -m pytest tests/test_yaml_cases.py -v
Against a live server:
    BROKER_BASE_URL=http://127.0.0.1:8765 BROKER_DB=<that server's db> \
        python -m pytest tests/test_yaml_cases.py -v
"""

import os
from pathlib import Path

import pytest
import yaml
from case_runner import CaseRun, expand
from wall_clock_guard import no_wall_clock

CASES_DIR = Path(__file__).parent / "cases"

CASES = [
    item
    for path in sorted(CASES_DIR.glob("*.yaml"))
    for item in expand(yaml.safe_load(path.read_text(encoding="utf-8")))
]


@pytest.mark.parametrize("case", [c for _, c in CASES], ids=[i for i, _ in CASES])
def test_case(case, tmp_path, record_property):
    # Recorded in the JUnit XML so the report can group results by these.
    record_property("category", case["category"])
    record_property("endpoint", case["endpoint"])
    record_property("title", case.get("title", ""))
    live = bool(os.environ.get("BROKER_BASE_URL"))
    db_path = os.environ.get("BROKER_DB") if live else None
    run = CaseRun(case, db_path or tmp_path / "broker.db")
    if live:
        run.run()  # the guard can't reach another process
        return
    # Every in-process case runs with wall-clock reads from broker code made
    # to fail (TIME-NO-REAL-CLOCK-READS asks for this; it is cheap for all).
    with no_wall_clock() as calls:
        run.run()
    assert calls == [], f"broker code read the wall clock: {calls}"
