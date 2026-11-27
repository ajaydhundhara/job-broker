"""P0-07: the hand-written candidate observations cite only numbers that the
generated report prints, so a rerun that changes a number makes this fail
instead of leaving the candidates silently stale.

Checked: every money, percentage or decimal figure, and every "a / b / c"
seed count, must appear verbatim in reports/replay_results.md. Plain integers
(5 seeds, 10 nodes, ...) are too common to check this way.
"""

import re
from pathlib import Path

REPORTS = Path(__file__).resolve().parent.parent / "reports"
CANDIDATES = REPORTS / "replay_analysis_candidates.md"
HEADINGS = (
    "fair vs fifo",
    "the burst window",
    "idle timeout: cost vs wait",
    "what we would change",
)
FIGURE = re.compile(r"[-+]?\$?\d[\d,]*\.\d+%?")
SEED_COUNT = re.compile(r"\d+ / \d+ / \d+")


def sections() -> dict[str, str]:
    text = CANDIDATES.read_text(encoding="utf-8")
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_every_analysis_heading_has_candidates_marked_for_author_review():
    found = sections()
    assert list(found) == list(HEADINGS)
    for heading, body in found.items():
        assert "for author review" in body.split("\n- ", 1)[0], heading
        assert "\n- " in body, f"{heading}: no candidate observation"


def test_the_report_keeps_its_analysis_headings_as_todo():
    md = (REPORTS / "replay_results.md").read_text(encoding="utf-8")
    analysis = md.split("## Analysis", 1)[1]
    assert [f"- TODO: {h}" for h in HEADINGS] == [
        x for x in analysis.splitlines() if x.startswith("- ")
    ]


def test_every_cited_figure_is_in_the_generated_report():
    md = (REPORTS / "replay_results.md").read_text(encoding="utf-8")
    text = CANDIDATES.read_text(encoding="utf-8")
    figures = FIGURE.findall(text)
    assert len(figures) > 40  # the check is not vacuous
    missing = [f for f in figures if f not in md]
    seeds = SEED_COUNT.findall(text)
    assert len(seeds) > 10
    missing += [s for s in seeds if f"| {s} |" not in md]
    assert missing == []
