"""Load, clean and validate a job trace CSV, and report its data quality.

    python -m broker.trace 01a_trace.csv [--out reports/data_quality.md]
        [--burst-window 2025-03-05T05:00Z,2025-03-05T08:00Z] [--keep-duplicates]

The trace is data: parsed, never executed, never edited. The rules:

* Timestamps are converted to UTC epoch seconds. A non-UTC offset is fine
  (converted, counted); a timestamp with no offset is rejected, because its
  zone can't be guessed.
* Invalid rows are REJECTED with their line number and reason, never
  repaired. A row goes through the same rules as POST /jobs (the API's own
  JobSubmit model), so gpus=3 is rejected here as it would be there.
* Rows are sorted by (UTC time, line number).
* Duplicates are NOT dropped here. Each row gets an idempotency key (sha256
  of the normalised row), so identical rows collapse into one job through
  the API's own idempotency, the way a client's retry would.
  keep_duplicates=True adds the line number to the key, so every row becomes
  its own job (some duplicates sit inside a real-looking burst).
"""

import argparse
import csv
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from broker.models import JobSubmit

COLUMNS = ("submitted_at", "team", "gpus", "estimated_minutes", "actual_minutes",
           "priority", "outcome")  # fmt: skip
GPU_SIZES = (1, 2, 4, 8)
PRIORITIES = ("low", "normal", "high")
OUTCOMES = ("succeeded", "failed", "cancelled")
# The scheduler only sees estimates; a job that runs far longer than its
# estimate is worth knowing about when reading wait times.
OVERRUN_FACTOR = 4
_INT = re.compile(r"-?\d+")


def _parse(text: str) -> datetime:
    # datetime.fromisoformat accepts 'Z' only from Python 3.11; replacing it
    # keeps the parser explicit about what it accepts.
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)  # ValueError on anything else
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no UTC offset; its zone can't be guessed")
    return parsed


def parse_ts(text: str) -> int:
    """ISO 8601 with 'Z' or an offset -> UTC epoch seconds. Never reads the clock."""
    return int(_parse(text).timestamp())


def to_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


DEFAULT_BURST_WINDOW = (
    parse_ts("2025-03-05T05:00:00Z"),
    parse_ts("2025-03-05T08:00:00Z"),
)


@dataclass(frozen=True)
class TraceRow:
    line: int  # line number in the file; the header is line 1
    utc_ts: int
    team: str
    gpus: int
    estimated_minutes: int
    actual_minutes: int
    priority: str
    outcome: str
    idempotency_key: str


@dataclass(frozen=True)
class Rejected:
    line: int
    reason: str  # the rule broken, without the value, so reasons group
    value: str  # the offending value as written


@dataclass
class DataQualityReport:
    burst_window: tuple[int, int]
    keep_duplicates: bool
    total_rows: int = 0
    accepted: int = 0
    rejected: list[Rejected] = field(default_factory=list)
    non_utc_offsets: int = 0
    # Rows whose time is earlier than some row above them in the file.
    out_of_order: int = 0
    # Rows whose normalised content equals an earlier row's (the first
    # occurrence is not counted). Counted whether or not keep_duplicates is on.
    duplicate_rows: int = 0
    duplicate_lines: list[int] = field(default_factory=list)
    duplicates_only_after_utc: int = 0  # same row, different timestamp text
    time_range: tuple[int, int] | None = None
    rows_per_team: dict[str, int] = field(default_factory=dict)
    burst_rows_per_team: dict[str, int] = field(default_factory=dict)
    overrun_lines: list[int] = field(default_factory=list)

    @property
    def rejected_by_reason(self) -> dict[str, list[int]]:
        grouped: dict[str, list[int]] = {}
        for r in self.rejected:
            grouped.setdefault(r.reason, []).append(r.line)
        return grouped

    def summary(self) -> str:
        lines = [
            f"rows {self.total_rows}: {self.accepted} accepted, {len(self.rejected)} rejected",
            *(
                f"  rejected: {reason} (lines {', '.join(map(str, nums))})"
                for reason, nums in self.rejected_by_reason.items()
            ),
            f"non-UTC offsets converted: {self.non_utc_offsets}",
            f"out-of-order rows: {self.out_of_order}",
            f"duplicate rows: {self.duplicate_rows} "
            f"({self.duplicates_only_after_utc} only after UTC conversion); "
            + (
                "kept as separate jobs"
                if self.keep_duplicates
                else "collapse into one job via idempotency"
            ),
        ]
        if self.time_range:
            lines.append(
                f"time range: {to_iso(self.time_range[0])} .. {to_iso(self.time_range[1])}"
            )
        lines.append("rows per team: " + _counts(self.rows_per_team))
        lines.append(f"in burst window {_window(self.burst_window)}: "
                     + _counts(self.burst_rows_per_team))  # fmt: skip
        lines.append(
            f"actual > {OVERRUN_FACTOR} x estimate: {len(self.overrun_lines)} rows"
        )
        return "\n".join(lines)

    def to_markdown(self) -> str:
        md = ["# Data quality", "",
              ("Generated by `python -m broker.trace`. Invalid rows are rejected, "
               "not repaired; line numbers count the header as line 1."), "",
              "| | |", "|---|---|",
              f"| total rows | {self.total_rows} |", f"| accepted | {self.accepted} |",
              f"| rejected | {len(self.rejected)} |",
              f"| non-UTC offsets converted | {self.non_utc_offsets} |",
              f"| out-of-order rows | {self.out_of_order} |",
              f"| duplicate rows | {self.duplicate_rows} |",
              f"| of which identical only after UTC conversion | {self.duplicates_only_after_utc} |",
              f"| duplicate handling | {'keep (one job per row)' if self.keep_duplicates else 'collapse via idempotency'} |",
              f"| rows with actual > {OVERRUN_FACTOR} x estimate | {len(self.overrun_lines)} |"]  # fmt: skip
        if self.time_range:
            md.append(
                f"| time range (UTC) | {to_iso(self.time_range[0])} .. {to_iso(self.time_range[1])} |"
            )
        md += ["", "## Rejected rows", "", "| line | reason | value |", "|---|---|---|"]
        md += [f"| {r.line} | {r.reason} | `{r.value}` |" for r in self.rejected]
        teams = sorted(self.rows_per_team)
        md += ["", f"## Rows per team (burst window {_window(self.burst_window)})", "",
               "| team | accepted rows | in burst window |", "|---|---|---|"]  # fmt: skip
        md += [f"| {t} | {self.rows_per_team[t]} | {self.burst_rows_per_team.get(t, 0)} |"
               for t in teams]  # fmt: skip
        md += ["", "## Duplicate rows", "",
               "Lines repeating an earlier row: " + (", ".join(map(str, self.duplicate_lines)) or "none"),
               "", f"## Actual minutes > {OVERRUN_FACTOR} x estimate", "",
               "The scheduler only sees estimates. Lines: "
               + (", ".join(map(str, self.overrun_lines)) or "none"), ""]  # fmt: skip
        return "\n".join(md)


class _Reject(Exception):
    def __init__(self, reason: str, value: str):
        self.reason, self.value = reason, value


def _int(raw: dict, name: str) -> int:
    text = raw[name].strip()
    if not _INT.fullmatch(text):
        raise _Reject(f"{name} is not an integer", raw[name])
    return int(text)


def _validate(raw: dict) -> tuple[datetime, dict]:
    """The row's values, or _Reject. Checks in column order; the first fails."""
    missing = [c for c in COLUMNS if raw.get(c) is None]
    if missing or None in raw:  # DictReader: short row -> None, long row -> key None
        raise _Reject("missing field" if missing else "extra fields",
                      ",".join(missing) or str(raw[None]))  # fmt: skip
    try:
        ts = _parse(raw["submitted_at"])
    except ValueError as e:
        reason = (
            str(e) if "offset" in str(e) else "submitted_at is not an ISO 8601 time"
        )
        raise _Reject(reason, raw["submitted_at"]) from None
    v = {"team": raw["team"].strip()}
    if not v["team"]:
        raise _Reject("team is empty", raw["team"])
    v["gpus"] = _int(raw, "gpus")
    if v["gpus"] not in GPU_SIZES:
        raise _Reject("gpus not in 1, 2, 4, 8", raw["gpus"])
    v["estimated_minutes"] = _int(raw, "estimated_minutes")
    if v["estimated_minutes"] <= 0:
        raise _Reject("estimated_minutes must be > 0", raw["estimated_minutes"])
    v["actual_minutes"] = _int(raw, "actual_minutes")
    if v["actual_minutes"] < 0:
        raise _Reject("actual_minutes must be >= 0", raw["actual_minutes"])
    v["priority"] = raw["priority"].strip()
    if v["priority"] not in PRIORITIES:
        raise _Reject("priority not in low, normal, high", raw["priority"])
    v["outcome"] = raw["outcome"].strip()
    if v["outcome"] not in OUTCOMES:
        raise _Reject("outcome not in succeeded, failed, cancelled", raw["outcome"])
    try:
        # Whatever else POST /jobs would refuse (e.g. estimated_minutes over
        # the API's 7-day limit, a team name over 128 characters).
        JobSubmit(team=v["team"], gpus=v["gpus"], estimated_minutes=v["estimated_minutes"],
                  priority=v["priority"])  # fmt: skip
    except ValidationError as e:
        err = e.errors()[0]
        name = ".".join(map(str, err["loc"])) or "row"
        raise _Reject(
            f"{name} rejected by the API: {err['msg']}", str(err.get("input"))
        ) from None
    return ts, v


def _key(utc_ts: int, v: dict) -> str:
    normalised = (utc_ts, v["team"], v["gpus"], v["estimated_minutes"],
                  v["actual_minutes"], v["priority"], v["outcome"])  # fmt: skip
    return hashlib.sha256("|".join(map(str, normalised)).encode()).hexdigest()


def load_trace(path: str | Path, keep_duplicates: bool = False,
               burst_window: tuple[int, int] = DEFAULT_BURST_WINDOW,
               ) -> tuple[list[TraceRow], DataQualityReport]:  # fmt: skip
    """Accepted rows sorted by (utc_ts, line), and the data-quality report."""
    report = DataQualityReport(
        burst_window=burst_window, keep_duplicates=keep_duplicates
    )
    rows: list[TraceRow] = []
    first_text: dict[str, str] = {}  # base key -> timestamp text of its first row
    latest = None  # latest time seen so far, in file order
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            # A wrong header is a wrong file, not 500 bad rows.
            raise ValueError(f"expected columns {COLUMNS}, got {reader.fieldnames}")
        for line, raw in enumerate(reader, start=2):
            report.total_rows += 1
            try:
                ts, v = _validate(raw)
            except _Reject as r:
                report.rejected.append(Rejected(line, r.reason, r.value))
                continue
            utc_ts = int(ts.timestamp())
            report.non_utc_offsets += ts.utcoffset().total_seconds() != 0
            if latest is not None and utc_ts < latest:
                report.out_of_order += 1
            latest = utc_ts if latest is None else max(latest, utc_ts)
            key = _key(utc_ts, v)
            if key in first_text:
                report.duplicate_rows += 1
                report.duplicate_lines.append(line)
                report.duplicates_only_after_utc += (
                    first_text[key] != raw["submitted_at"]
                )
            else:
                first_text[key] = raw["submitted_at"]
            if keep_duplicates:
                # Still deterministic, but unique per row: no two rows collapse.
                key = hashlib.sha256(f"{key}|line={line}".encode()).hexdigest()
            rows.append(TraceRow(line=line, utc_ts=utc_ts, idempotency_key=key, **v))
    rows.sort(key=lambda r: (r.utc_ts, r.line))
    report.accepted = len(rows)
    if rows:
        report.time_range = (rows[0].utc_ts, rows[-1].utc_ts)
    report.rows_per_team = dict(sorted(Counter(r.team for r in rows).items()))
    start, end = burst_window
    report.burst_rows_per_team = dict(
        sorted(Counter(r.team for r in rows if start <= r.utc_ts < end).items())
    )
    report.overrun_lines = sorted(
        r.line for r in rows if r.actual_minutes > OVERRUN_FACTOR * r.estimated_minutes
    )
    return rows, report


def _counts(by_team: dict[str, int]) -> str:
    return ", ".join(f"{t} {n}" for t, n in by_team.items()) or "none"


def _window(window: tuple[int, int]) -> str:
    return f"[{to_iso(window[0])}, {to_iso(window[1])})"


def parse_window(text: str) -> tuple[int, int]:
    """'FROM,TO' (ISO 8601 with offset) -> (from_ts, to_ts), half-open."""
    start, end = (parse_ts(part) for part in text.split(","))
    if end <= start:
        raise ValueError("burst window must end after it starts")
    return start, end


def main(argv=None) -> None:
    p = argparse.ArgumentParser(
        description="Check a job trace and write its data-quality report."
    )
    p.add_argument("trace")
    p.add_argument("--out", default="reports/data_quality.md")
    p.add_argument("--burst-window", type=parse_window, default=DEFAULT_BURST_WINDOW)
    p.add_argument("--keep-duplicates", action="store_true")
    args = p.parse_args(argv)
    _, report = load_trace(args.trace, args.keep_duplicates, args.burst_window)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_markdown(), encoding="utf-8")
    print(report.summary())
    print(f"written: {out}")


if __name__ == "__main__":
    main()
