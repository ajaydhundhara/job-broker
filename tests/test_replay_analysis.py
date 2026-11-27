"""The replay report's Analysis section comes from an authored file.

reports/replay_results.md is generated, so text typed into it would be lost
on the next replay. The analysis is written in reports/replay_analysis.md
under four fixed headings; run_replay copies it under "## Analysis". With no
analysis file (a test's temporary folder, a new trace) the section keeps its
TODO headings, as tests/test_replay.py checks.

The analysis may quote only figures the generated tables print (whole, not
inside a longer number) or configured constants (node prices, the fallback
limit), so a rerun that changes a number fails here instead of leaving the
text stale.
"""

import re
from pathlib import Path

import pytest
from test_replay import START, small_trace

from broker.config import PRICE_MICROS, Config
from replay.run_replay import (
    ANALYSIS_HEADINGS,
    DEFAULT_ANALYSIS,
    read_analysis,
    run_all,
)

REPORTS = Path(__file__).resolve().parent.parent / "reports"
ANALYSIS = REPORTS / "replay_analysis.md"
FIGURE = re.compile(r"[-+]?\$?\d[\d,]*\.\d+%?")
SEED_COUNT = re.compile(r"\d+ / \d+ / \d+")


def authored(*headings) -> str:
    return "".join(f"### {h}\n\nText about {h}.\n\n" for h in headings)


def run_small(tmp_path, analysis):
    return run_all(small_trace(tmp_path), policies=["fair", "fifo"], idle_minutes=10,
                   sweep=[2], seeds=[1], burst_window=(START, START + 3600),
                   out_dir=tmp_path / "out", analysis=analysis)  # fmt: skip


def test_the_four_headings_are_the_reports_analysis_topics():
    assert ANALYSIS_HEADINGS == (
        "fair vs fifo",
        "the burst window",
        "idle timeout: cost vs wait",
        "what we would change",
    )
    assert Path("reports/replay_analysis.md") == DEFAULT_ANALYSIS


def test_an_authored_analysis_replaces_the_todo_headings(tmp_path):
    path = tmp_path / "analysis.md"
    path.write_text(authored(*ANALYSIS_HEADINGS), encoding="utf-8")
    run_small(tmp_path, path)
    md = (tmp_path / "out" / "replay_results.md").read_text(encoding="utf-8")
    section = md.split("## Analysis\n\n", 1)[1]
    assert section == authored(*ANALYSIS_HEADINGS).rstrip("\n") + "\n"
    assert "TODO" not in md


def test_an_analysis_missing_a_heading_is_refused(tmp_path):
    path = tmp_path / "analysis.md"
    path.write_text(authored(*ANALYSIS_HEADINGS[:3]), encoding="utf-8")
    with pytest.raises(ValueError, match="what we would change"):
        read_analysis(path)


def test_an_analysis_with_its_headings_out_of_order_is_refused(tmp_path):
    path = tmp_path / "analysis.md"
    path.write_text(authored(*reversed(ANALYSIS_HEADINGS)), encoding="utf-8")
    with pytest.raises(ValueError, match="order"):
        read_analysis(path)


def test_no_analysis_file_means_no_analysis(tmp_path):
    assert read_analysis(None) is None
    assert read_analysis(tmp_path / "absent.md", required=False) is None
    with pytest.raises(FileNotFoundError):
        read_analysis(tmp_path / "absent.md")


# --- the committed report ---------------------------------------------------------------


def report_parts() -> tuple[str, str]:
    md = (REPORTS / "replay_results.md").read_text(encoding="utf-8")
    tables, analysis = md.split("## Analysis\n\n", 1)
    return tables, analysis


def test_the_committed_report_carries_the_authored_analysis():
    _, analysis = report_parts()
    assert analysis == ANALYSIS.read_text(encoding="utf-8").rstrip("\n") + "\n"
    assert "TODO" not in analysis
    for heading in ANALYSIS_HEADINGS:
        body = analysis.split(f"### {heading}\n", 1)[1].split("\n### ", 1)[0]
        assert len(body.split()) > 40, f"{heading}: no real analysis"


def configured_figures() -> set[str]:
    """Node prices and the fallback limit: config values, not replay output."""
    prices = {f"${micros / 1e6:.2f}" for micros in PRICE_MICROS.values()}
    return prices | {str(Config().max_fallback_price_ratio)}


def printed(figure: str, text: str) -> bool:
    """`figure` appears as a whole number in `text`, not inside a longer one."""
    pattern = rf"(?<![\d,.]){re.escape(figure)}(?![\d.]|,\d)"
    return re.search(pattern, text) is not None


def test_every_figure_the_analysis_quotes_is_in_the_generated_tables():
    tables, analysis = report_parts()  # the tables only, not the analysis itself
    figures = FIGURE.findall(analysis)
    assert len(figures) > 30  # the check is not vacuous
    missing = [f for f in figures
               if f not in configured_figures() and not printed(f, tables)]  # fmt: skip
    seeds = SEED_COUNT.findall(analysis)
    assert len(seeds) > 5
    missing += [s for s in seeds if f"| {s} |" not in tables]
    assert missing == []
