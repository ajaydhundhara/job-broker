"""Text cleaning shared by ingestion and queries.

- redact: secrets out, before anything is stored, indexed or returned.
- template: a line with its variable parts (numbers, ids, paths) masked, so
  "epoch 3 started" and "epoch 40 started" count as one line.
- learn_noise / signal_lines: boilerplate is learned from the corpus (lines
  in many logs), not listed by hand, so new boilerplate needs no code change.
- is_log: a pasted log and a typed question are cleaned differently.
- tokenize: one tokenizer for logs and questions, so both sides match.
"""

import re
from collections import Counter

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

_LEVEL = re.compile(r"\b(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\b")
_EXC_CLASS = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:Error|Exception)\b")
_EXIT = re.compile(r"(?i)\bexit code\b|\berrno\b")


def is_log(text: str) -> bool:
    """A pasted log rather than a typed question.

    Signs: more than one line, an upper-case log level, an exception class
    name (RuntimeError, but not a plain "Error"), "exit code" or "errno".
    """
    t = text.strip()
    return bool(
        "\n" in t or _LEVEL.search(t) or _EXC_CLASS.search(t) or _EXIT.search(t)
    )


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
