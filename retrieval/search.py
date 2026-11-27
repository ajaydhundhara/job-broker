"""Query pipeline: text -> cleaned query -> hybrid scores -> group ranking.

1. redact; a pasted log is reduced to its signal lines (corpus noise set);
   a question is expanded with the glossary
2. per log: hybrid of dense cosine and squashed BM25
3. per group: G_MEMBER * best member + (1 - G_MEMBER) * group document
4. best group below TAU -> no results ("No similar past failure found")
5. output group by group, members by their own score; a further group only
   if it also reaches TAU. Never padded with weak matches.

Why group-first: the label is "shares the cause". Logs with one cause are
worded differently (NCCL "unhandled system error" vs "communicator was
aborted"), so one strong lexical hit on a member should bring in its siblings
even when their own wording scores low. The group document (all members'
lines + the fix) also lets a question match a cause no single log spells out.
"""

from dataclasses import dataclass

from retrieval import clean, glossary
from retrieval.index import Index, Params, hybrid

NO_MATCH = "No similar past failure found"
CONFLICT_NOTE = "conflicting resolutions recorded for an identical log"


@dataclass(frozen=True)
class SearchResult:
    input_type: str  # "log" | "question"
    backend: str
    no_match: bool
    message: str | None
    results: list[dict]
    query: str  # the cleaned, redacted query actually scored (not returned by the API)

    def response(self) -> dict:
        return {
            "input_type": self.input_type,
            "backend": self.backend,
            "no_match": self.no_match,
            "message": self.message,
            "results": self.results,
        }


def prepare_query(
    index: Index, text: str, use_glossary: bool = True
) -> tuple[str, str]:
    """(input type, the text to score), always redacted.

    clean.classify decides: a log is reduced to its signal lines (never
    expanded, so a "?" in one of its URLs changes nothing); a question is
    expanded with the glossary. use_glossary=False is for the evaluation's
    ablation only.
    """
    q = clean.redact(text)
    if clean.classify(q) == "log":
        return "log", "\n".join(clean.signal_lines(q, index.noise))
    return "question", glossary.expand(q) if use_glossary else q


def _note(index: Index, log_id: str) -> str | None:
    notes = []
    status = index.status[log_id]
    if status == "inferred":
        notes.append(
            "no resolution was recorded for this log; resolution inferred from its "
            f"most similar cause group (similarity {index.inferred[log_id]:.3f})"
        )
    elif status == "none":
        notes.append("no resolution recorded for this failure")
    if any(log_id in c.ids for c in index.conflicts):
        notes.append(CONFLICT_NOTE)
    return "; ".join(notes) or None


def resolution(index: Index, log_id: str) -> str | None:
    """The fix to show: recorded, else the inferred group's, else None."""
    status = index.status[log_id]
    if status == "recorded":
        return index.by_id[log_id].resolution
    if status == "inferred":
        return index.group(log_id).resolution
    return None


def hit(index: Index, log_id: str, score: float) -> dict:
    record = index.by_id[log_id]
    return {
        "id": log_id,
        "team": record.team,
        "cause_group": index.group_of[log_id],
        "error_lines": index.signal[log_id],  # redacted signal lines
        "resolution": resolution(index, log_id),
        "resolution_status": index.status[log_id],
        "score": round(float(score), 6),
        "note": _note(index, log_id),
    }


def _score(x) -> float:
    # One rounding for ranking, the TAU comparison and the output, so a score
    # shown as 0.2 is never treated as below a TAU of 0.2.
    return round(float(x), 6)


def search(
    index: Index,
    text: str,
    k: int = 5,
    *,
    params: Params | None = None,
    use_groups: bool = True,
    use_glossary: bool = True,
) -> SearchResult:
    """The top-k past failures for a log or a question.

    params overrides index.params (the evaluation's grid); use_groups and
    use_glossary exist for its ablations. The API always uses the defaults.
    """
    p = params or index.params
    input_type, query = prepare_query(index, text, use_glossary)
    vector = index.embedder.embed([query])[0]
    tokens = clean.tokenize(query)
    per_log = hybrid(index.log_vecs @ vector, index.log_bm25.scores(tokens), p)

    if use_groups:
        per_group_doc = hybrid(
            index.group_vecs @ vector, index.group_bm25.scores(tokens), p
        )
        ranked = []
        for i, g in enumerate(index.groups):
            best_member = max(per_log[index.position[m]] for m in g.members)
            score = p.g_member * best_member + (1 - p.g_member) * per_group_doc[i]
            members = sorted(g.members, key=lambda m: (-per_log[index.position[m]], m))
            ranked.append((-_score(score), g.id, members))
    else:  # ablation: every log on its own, ranked by its own score
        ranked = [
            (-_score(per_log[i]), r.id, [r.id]) for i, r in enumerate(index.records)
        ]
    ranked.sort()  # score descending, ties by group (or log) id: deterministic

    if not ranked or -ranked[0][0] < p.tau:
        return SearchResult(input_type, index.backend, True, NO_MATCH, [], query)
    results = []
    for neg_score, _, members in ranked:
        if -neg_score < p.tau or len(results) >= k:
            break  # never pad below TAU
        for m in members[: k - len(results)]:
            results.append(hit(index, m, -neg_score))
    return SearchResult(input_type, index.backend, False, None, results, query)


def get_failure(index: Index, log_id: str) -> dict:
    """One stored record (already redacted) with its signal lines and group."""
    record = index.by_id[log_id]  # KeyError -> 404 in the API
    return {
        "id": record.id,
        "team": record.team,
        "log": record.log,
        "signal_lines": index.signal[log_id],
        "cause_group": index.group_of[log_id],
        "resolution": resolution(index, log_id),
        "resolution_status": index.status[log_id],
        "note": _note(index, log_id),
    }
