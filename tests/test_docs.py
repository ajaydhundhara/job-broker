"""P0-08: the reviewer-facing docs agree with the code.

Checked mechanically, so they can't drift again: the README's API table lists
exactly the app's routes; the retrieval parameters the README quotes are the
ones in retrieval/params.json; no reviewer-facing doc holds a TODO (the
replay report's Analysis comes from reports/replay_analysis.md).
"""

import json
import re
from pathlib import Path

from broker.api import create_app

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
REVIEWER_DOCS = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "SPEC_DECISIONS.md",
    ROOT / "requirements.txt",
    ROOT / "requirements-embeddings.txt",
    ROOT / "tests" / "TEST_MIGRATION_MAP.md",
    *sorted((ROOT / "docs").glob("*.md")),
    *sorted((ROOT / "reports").glob("*.md")),
]
# A TODO marker; AGENTS.md's rule "write TODO and say so" is not one.
TODO = re.compile(r"\bTODO\b(?! and say so)")
DOC_ROUTES = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")


def app_routes() -> set[tuple[str, str]]:
    app = create_app(ROOT / "unused.db")  # routes only: no lifespan, no file
    return {(m, r.path) for r in app.routes for m in getattr(r, "methods", ())
            if r.path not in DOC_ROUTES and m != "HEAD"}  # fmt: skip


def readme_api_table() -> set[tuple[str, str]]:
    text = README.read_text(encoding="utf-8")
    table = text.split("## API", 1)[1].split("\n\n", 2)[1]
    rows = re.findall(
        r"^\| (GET|POST|PUT|PATCH|DELETE) \| `([^`]+)` \|", table, re.MULTILINE
    )
    return set(rows)


def test_the_readme_api_table_lists_exactly_the_apps_routes():
    documented, served = readme_api_table(), app_routes()
    assert served - documented == set(), "served but not in the README table"
    assert documented - served == set(), "in the README table but not served"


def test_the_readme_quotes_the_saved_retrieval_parameters():
    saved = json.loads((ROOT / "retrieval" / "params.json").read_text())["tfidf"]
    text = README.read_text(encoding="utf-8")
    quoted = re.search(r"Chosen: `([^`]+)`", text)
    assert quoted, "the README no longer quotes the chosen parameters"
    values = dict(kv.split("=") for kv in quoted.group(1).replace(" ", "").split(","))
    assert set(values) == {"w_dense", "g_member", "lex_k", "tau"}
    for name, value in values.items():
        assert abs(float(value) - saved[name]) < 5e-4, (name, value, saved[name])


def test_no_reviewer_facing_doc_holds_a_todo():
    found = []
    for path in REVIEWER_DOCS:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if TODO.search(line):
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert found == []


def test_agents_md_states_the_scope():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    scope = next(x for x in text.splitlines() if x.startswith("- Scope"))
    assert "TODO" not in scope
