"""Text cleaning shared by ingestion and queries.

- redact: secrets out, before anything is stored, indexed or returned.
- template: a line with its variable parts (numbers, ids, paths) masked, so
  "epoch 3 started" and "epoch 40 started" count as one line.
- learn_noise / signal_lines: boilerplate is learned from the corpus (lines
  in many logs), not listed by hand, so new boilerplate needs no code change.
- classify: a pasted log and a typed question are cleaned differently.
- tokenize: one tokenizer for logs and questions, so both sides match.
"""

import json
import re
from collections import Counter
from typing import Literal

DEFAULT_NOISE_DF = 0.15  # a template in more than 15% of logs is boilerplate
REDACTED = "[REDACTED]"

_SECRET_KEY = r"(?:password|passwd|secret|token|api[_-]key)"
# Each pattern replaces the WHOLE match, key included: "SERVICE_PASSWORD=[...]"
# would still say what was there. Order matters: the header first (it holds a
# Bearer token), then JSON pairs (before the bare key=value form eats half).
_SECRETS = [
    # Authorization / Proxy-Authorization header: the value runs to the line end.
    re.compile(r"(?i)\b(?:proxy-)?authorization\s*[:=][^\r\n]*"),
    # "key": "value" or "key": 123, where the key contains a secret word.
    re.compile(
        rf'(?i)"[^"\n]*{_SECRET_KEY}[^"\n]*"\s*:\s*(?:"(?:[^"\\\n]|\\.)*"|[^\s,}}]+)'
    ),
    # KEY=VALUE, KEY: VALUE, KEY = 'VALUE'.
    re.compile(
        rf"(?i)\b[\w.-]*{_SECRET_KEY}[\w.-]*\s*[:=]\s*"
        r"""(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)"""
    ),
    # "Bearer <token>": only a token-shaped value (8+ chars with a digit, an
    # upper-case letter or a symbol). "bearer token expired" is a real error
    # line and must stay readable.
    re.compile(r"(?i:\bbearer)\s+(?=[\w.~+/=-]*[A-Z0-9_.~+/=-])[\w.~+/=-]{8,}"),
    # JWT shape: three base64url parts joined by dots. 8+ chars a part, so
    # dotted names like torch.cuda.OutOfMemoryError are not taken for one.
    re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
]


def redact_count(text: str) -> tuple[str, int]:
    """(text with every secret replaced by [REDACTED], number replaced)."""
    total = 0
    for pattern in _SECRETS:
        text, n = pattern.subn(REDACTED, text)
        total += n
    return text, total


def redact(text: str) -> str:
    return redact_count(text)[0]


# --- templates ---------------------------------------------------------------

# Applied in this order: a quoted path becomes <str>, not <path>; hex before
# numbers, or "3f2a9c" would become "<num>f<num>a...".
_QUOTED = re.compile(r"""(?<![a-z0-9])(?:'[^'\n]*'|"[^"\n]*")""")
_PATH = re.compile(r"(?<![\w<>.])/[^\s:'\",;()\[\]{}]+")
# Hex: 0x..., or 6+ hex chars with at least one digit and one letter, so
# English words ("added", "decade") are not ids.
_HEX = re.compile(r"\b(?:0x[0-9a-f]+|(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{6,})\b")
# Digits anywhere, versions included (2.16.x, 7.5); "fp16" -> "fp<num>".
_NUM = re.compile(r"\d+(?:[.,]\d+)*(?:\.x\b)?")
_SPACES = re.compile(r"\s+")


def template(line: str) -> str:
    t = line.lower()
    t = _QUOTED.sub("<str>", t)
    t = _PATH.sub("<path>", t)
    t = _HEX.sub("<hex>", t)
    t = _NUM.sub("<num>", t)
    return _SPACES.sub(" ", t).strip()


# --- the learned noise filter ---------------------------------------------------


def _lines(log: str) -> list[str]:
    return [x.strip() for x in log.splitlines() if x.strip()]


def template_df(logs: list[str]) -> Counter:
    """template -> number of logs containing it (a log counts once)."""
    df = Counter()
    for log in logs:
        df.update({template(x) for x in _lines(log)})
    return df


def learn_noise(logs: list[str], noise_df: float = DEFAULT_NOISE_DF) -> dict[str, int]:
    """Templates in MORE than noise_df of the logs -> their document frequency.

    Boilerplate (heartbeats, epoch counters, deprecation warnings) repeats in
    most logs whatever the cause; the error line is shared only by the few
    logs with the same cause. Saved in the index and applied to query logs too.
    """
    df = template_df(logs)
    limit = noise_df * len(logs)
    return {t: n for t, n in sorted(df.items()) if n > limit}


_LOW_LEVEL = re.compile(r"^(?:INFO|DEBUG)\b")


def signal_lines(log: str, noise) -> list[str]:
    """The lines worth matching on, in log order.

    Fallbacks so a log is never empty: if every line is noise, keep the ones
    that are not INFO/DEBUG (a WARN may be the only clue); else the whole log.
    """
    lines = _lines(log)
    kept = [x for x in lines if template(x) not in noise]
    if not kept:
        kept = [x for x in lines if not _LOW_LEVEL.match(x)]
    return kept or lines


# --- log or question? ------------------------------------------------------------
#
# One classifier, in tiers; the first that applies decides:
#   1. structural log evidence -> log: several non-blank lines, a stack trace,
#      a structured record (JSON object, 3+ key=value fields), or a line that
#      starts like a log record (timestamp, level, "SomeError:",
#      "Error response from <component>:")
#   2. a question cue -> question: starts with a question / request word, or
#      has a "?" ending a sentence ("?" inside a URL does not count)
#   3. log words inside one line -> log: exception class, exit code, errno,
#      an upper-case level
#   4. anything else (a short symptom) -> question
# Tier 2 before 3 keeps "what does exit code 137 mean?" a question, while a
# pasted record ("ERROR fetch https://h/x?rev=3 failed") stays a log.

_LEVELS = r"(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)"
_LEVEL = re.compile(rf"\b{_LEVELS}\b")
_EXC_CLASS = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:Error|Exception)\b")
# "exit code", "errno", and an exit code with its number in any common form:
# "exited with code 1", "exit status 2", "exit code: 137".
_EXIT = re.compile(
    r"(?i)\bexit code\b|\berrno\b"
    r"|\bexit(?:ed)?\s+(?:with\s+)?(?:code|status)\s*[:=]?\s*-?\d+"
)
_STACK = re.compile(
    r"^\s*Traceback \(most recent call last\)"  # Python
    r'|^\s*File "[^"\n]+", line \d+'  # Python frame
    r"|^\s*at [\w$.<>]+\([^)\n]*\)\s*$",  # JVM / JS frame
    re.MULTILINE,
)
_RECORD_START = re.compile(
    r"^\s*(?:"
    r"\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"  # ISO timestamp
    rf"|\[?{_LEVELS}\]?(?=[\s:\]])"  # upper-case level
    rf"|(?i:\[{_LEVELS}\]|{_LEVELS}:)"  # [error] / error: in any case
    r"|(?:[a-z_]\w*\.)*[A-Z]\w*(?:Error|Exception)\s*:"  # SomeError: / a.b.SomeError:
    # "Error response from <component>:" (docker, containerd, a registry), in
    # any case, optionally after a "tool: " prefix. The component and its
    # colon are required, so "Error when pulling the image" stays a symptom.
    r"|(?:[\w.-]+:\s+)?(?i:error response from)\s+[\w.-]+:"
    r")",
    re.MULTILINE,
)
_KV_FIELD = re.compile(r"(?:^|\s)[A-Za-z_][\w.]*=[^\s=]")  # space-separated only
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+")
_QUESTION_MARK = re.compile(r"\?(?=\s|$)")
_QUESTION_WORDS = frozenset(
    [
        "what",
        "why",
        "how",
        "who",
        "when",
        "where",
        "which",
        "does",
        "do",
        "did",
        "is",
        "are",
        "can",
        "could",
        "should",
        "would",
        "explain",
        "describe",
        "define",
    ]
)
_FIRST_WORD = re.compile(r"[a-z]+")


def _structured(text: str) -> bool:
    if text.startswith("{") and text.endswith("}"):
        try:
            if isinstance(json.loads(text), dict):
                return True
        except ValueError:
            pass
    return any(len(_KV_FIELD.findall(line)) >= 3 for line in text.splitlines())


def _question_cue(text: str) -> bool:
    prose = _URL.sub(" ", text)
    first = _FIRST_WORD.match(prose.lower().lstrip())
    return bool(first and first.group() in _QUESTION_WORDS) or bool(
        _QUESTION_MARK.search(prose)
    )


def classify(text: str) -> Literal["log", "question"]:
    """Is this a pasted log or a typed question? See the tiers above.

    Deterministic syntax only, no wording of particular queries. Run it on
    redacted text. Blank text is a "question" (the API rejects it anyway).
    """
    t = text.strip()
    lines = [x for x in t.splitlines() if x.strip()]
    if len(lines) > 1 or _STACK.search(t) or _structured(t) or _RECORD_START.search(t):
        return "log"
    if _question_cue(t):
        return "question"
    if _LEVEL.search(t) or _EXC_CLASS.search(t) or _EXIT.search(t):
        return "log"
    return "question"


# --- tokens --------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _stem(word: str) -> str:
    """Light suffix stripping, so "timeouts" meets "timeout".

    Only alphabetic words longer than 4 letters; "es" only where English adds
    it (crashes, boxes), and never an "s" after an "s" (loss, process).
    """
    if len(word) <= 4 or not word.isalpha():
        return word
    if word.endswith("ing"):
        return word[:-3]
    if word.endswith("ed"):
        return word[:-2]
    if word.endswith("es") and word[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    """Lower-case word and number tokens.

    A hyphenated word gives its joined form and its parts (all-reduce ->
    allreduce, all, reduce), so "allreduce" and "all reduce" both match it.
    Numbers stay: 137, 401 and 403 carry meaning in this domain.
    """
    out = []
    for match in _WORD.finditer(text.lower()):
        word = match.group()
        if "-" in word:
            parts = word.split("-")
            out.append(_stem("".join(parts)))
            out.extend(_stem(p) for p in parts)
        else:
            out.append(_stem(word))
    return out
