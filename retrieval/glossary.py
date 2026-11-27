"""A small ML-ops glossary: precise everyday phrases -> terms used in logs.

The glossary expands natural-language QUESTIONS before semantic/lexical
retrieval. It does not classify a pasted log and should not be used to rewrite
one. Call ``expand_for_retrieval(text, is_log=...)`` from the retrieval layer:
question-shaped text takes precedence over a coarse log detector, so a query
such as "what does exit code 137 mean?" is still expanded.

Entries are based on general ML-ops terminology and the failure vocabulary in
the supplied historical logs, not on the labelled evaluation queries. Each
entry explains its mapping. Expansion is additive: the user's original text
is always preserved.
"""

from dataclasses import dataclass

from retrieval.clean import tokenize


@dataclass(frozen=True)
class Entry:
    phrases: tuple[str, ...]  # specific user phrases; matched as whole tokens
    terms: tuple[str, ...]  # log vocabulary appended when a phrase matches
    why: str  # why the mapping holds generally


# Only low-risk grammatical words may appear between words in a multi-token
# phrase. This accommodates wording such as "disk is completely full" without
# turning single common words (e.g. "disk", "driver", "permission") into
# triggers. Keep the maximum gap small to avoid broad semantic matching.
_BRIDGE_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "has",
        "have",
        "had",
        "seems",
        "seem",
        "looks",
        "look",
        "currently",
        "now",
        "almost",
        "nearly",
        "completely",
        "totally",
        "really",
        "very",
        "still",
        "my",
        "our",
        "your",
        "its",
        "of",
        "on",
        "in",
        "to",
        "for",
        "from",
        "with",
    }
)
_MAX_BRIDGE_WORDS = 2


GLOSSARY: tuple[Entry, ...] = (
    # --- GPU memory ---------------------------------------------------------
    Entry(
        (
            "out of memory",
            "oom",
            "cuda oom",
            "ran out of memory",
            "not enough memory",
            "not enough gpu memory",
            "insufficient gpu memory",
            "vram exhausted",
            "memory allocation failed",
        ),
        ("oom", "cuda out of memory", "outofmemoryerror", "allocate"),
        "GPU memory exhaustion is commonly reported as CUDA out of memory, "
        "OutOfMemoryError, or a failed allocation.",
    ),
    Entry(
        (
            "exit code 137",
            "code 137",
            "exit status 137",
            "status 137",
            "oomkilled",
            "sigkill",
            "sig kill",
        ),
        ("137", "sigkill", "oomkilled", "oom"),
        "Exit code 137 indicates termination by SIGKILL (128 + signal 9); "
        "containers often report OOMKilled, although SIGKILL is not proof "
        "of an out-of-memory cause by itself.",
    ),
    Entry(
        (
            "batch size",
            "batch too big",
            "batch too large",
            "batch size too large",
            "large batch size",
            "reduce the batch size",
        ),
        ("oom", "cuda out of memory", "allocate", "batch size"),
        "A larger batch generally increases activation memory, so an "
        "oversized batch can trigger a GPU out-of-memory failure.",
    ),
    # --- disk and checkpoint writes ----------------------------------------
    Entry(
        (
            "disk full",
            "full disk",
            "disk is full",
            "disk space full",
            "disk space is full",
            "disk completely full",
            "no space",
            "out of disk space",
            "out of disk",
            "storage full",
            "storage is full",
            "scratch disk full",
            "scratch is full",
            "filesystem full",
            "file system full",
            "disk ran out of space",
            "storage ran out of space",
        ),
        ("no space left on device", "enospc", "disk usage", "scratch disk"),
        "A full filesystem commonly produces ENOSPC (errno 28), shown as "
        "'No space left on device'.",
    ),
    Entry(
        (
            "checkpoint save",
            "save checkpoint",
            "saving checkpoint",
            "checkpoint save failed",
            "failed to save checkpoint",
            "checkpoint write failed",
            "checkpoint could not be saved",
        ),
        ("checkpoint", "checkpoint save", "write", "checkpoint error"),
        "Checkpoint-save failures can result from storage, permissions, "
        "serialization, or I/O problems; the phrase alone does not prove "
        "that the disk is full.",
    ),
    # --- numerical instability ---------------------------------------------
    Entry(
        (
            "nan",
            "not a number",
            "infinite gradient",
            "infinity",
            "blew up",
            "blow up",
            "training diverged",
            "loss diverged",
            "exploding gradients",
            "loss spike",
        ),
        ("nan", "inf", "divergence", "gradient", "loss"),
        "Training instability can appear as NaN or infinite loss/gradients "
        "and is often described as divergence or exploding gradients.",
    ),
    Entry(
        (
            "learning rate too high",
            "learning rate is too high",
            "unstable training",
            "loss went up",
            "loss keeps increasing",
            "training loss increasing",
        ),
        ("divergence", "loss", "nan", "gradient", "learning rate"),
        "An excessively high learning rate can cause training divergence "
        "and an increasing or unstable loss.",
    ),
    # --- filesystem access and missing files -------------------------------
    Entry(
        (
            "permission denied",
            "file permission denied",
            "file access denied",
            "cannot read file",
            "cant read file",
            "can't read file",
            "cannot access file",
            "not allowed to read file",
            "permission to read file",
            "file is not readable",
            "no read permission",
        ),
        ("permission denied", "permissionerror", "errno 13", "eacces"),
        "Filesystem access failures commonly appear as EACCES (errno 13) or "
        "PermissionError; HTTP authentication codes are handled separately.",
    ),
    Entry(
        (
            "file not found",
            "missing file",
            "file is missing",
            "wrong file path",
            "path is wrong",
            "cannot find file",
            "file does not exist",
            "no such file or directory",
            "missing path",
        ),
        ("no such file or directory", "filenotfounderror", "errno 2", "enoent"),
        "ENOENT (errno 2) is 'No such file or directory'; Python commonly "
        "raises FileNotFoundError for this condition.",
    ),
    Entry(
        (
            "missing dataset file",
            "dataset not found",
            "dataset path",
            "dataset mount",
            "dataset permission",
            "cannot read dataset",
            "cannot access dataset",
            "cannot load dataset",
            "training data missing",
            "missing training data",
            "training data path",
            "data path missing",
            "data file not found",
            "dataset is unavailable",
        ),
        (
            "no such file or directory",
            "filenotfounderror",
            "errno 2",
            "permission denied",
            "permissionerror",
            "errno 13",
            "mount",
            "dataset path",
        ),
        "Dataset-read failures commonly involve a missing file/path or a "
        "dataset mount that does not grant the job read access.",
    ),
    # --- distributed training and communication ----------------------------
    Entry(
        (
            "multi gpu",
            "multigpu",
            "multi node",
            "distributed training",
            "all reduce",
            "allreduce",
            "collective operation",
            "collective failure",
            "communicator failure",
            "gpu communication",
            "gpus communicating",
            "gpus cannot communicate",
            "communication between gpus",
        ),
        ("nccl", "allreduce", "collective", "communicator", "rank", "timeout"),
        "Distributed multi-GPU training commonly uses NCCL collectives such "
        "as all-reduce; failures often mention a rank, communicator, or timeout.",
    ),
    Entry(
        (
            "job hangs",
            "job is hanging",
            "training hangs",
            "training is hanging",
            "process hangs",
            "process is frozen",
            "training is frozen",
            "no progress in training",
            "training stopped making progress",
            "collective hangs",
            "collective is hanging",
            "not making progress",
        ),
        ("timeout", "timed out", "watchdog", "heartbeat"),
        "A genuinely non-progressing job may be detected through a watchdog "
        "timeout, a request timeout, or missed heartbeats.",
    ),
    Entry(
        (
            "interconnect failure",
            "infiniband failure",
            "network between nodes",
            "node to node network failure",
            "distributed network failure",
        ),
        ("nccl", "unhandled system error", "timeout", "interconnect"),
        "Distributed-training interconnect faults can surface as NCCL "
        "'unhandled system error' messages or collective timeouts.",
    ),
    # --- authentication and authorization ----------------------------------
    Entry(
        (
            "http 401",
            "401 unauthorized",
            "403",
            "http 403",
            "403 forbidden",
            "unauthorized",
            "authentication failed",
            "authentication error",
            "credentials rejected",
            "login failed",
            "cannot authenticate",
            "access token expired",
            "token expired",
            "expired token",
            "session expired",
            "jwt expired",
            "api token expired",
        ),
        (
            "401",
            "403",
            "unauthorized",
            "authentication required",
            "token expired",
            "jwt expired",
        ),
        "Authentication or authorization failures commonly appear as HTTP "
        "401/403 responses, rejected credentials, or expired tokens.",
    ),
    # --- container image and registry --------------------------------------
    Entry(
        (
            "image pull",
            "pull image",
            "docker pull",
            "container image",
            "imagepullbackoff",
            "image pull backoff",
            "pull access denied",
            "cannot pull image",
            "cant pull image",
            "can't pull image",
            "failed to pull image",
            "container image download",
            "registry login",
            "registry credentials",
        ),
        (
            "image pull",
            "pull access denied",
            "registry",
            "docker",
            "unauthorized",
            "401",
            "backing off",
            "imagepullbackoff",
        ),
        "Container runtimes report image-fetch failures as pull-access or "
        "registry errors; Kubernetes may retry them as ImagePullBackOff.",
    ),
    # --- CUDA drivers and shared libraries ---------------------------------
    Entry(
        (
            "cuda driver",
            "nvidia driver",
            "driver is too old",
            "outdated driver",
            "outdated nvidia driver",
            "cuda version mismatch",
            "torch cuda mismatch",
            "torch requires cuda",
            "cuda runtime mismatch",
            "incompatible cuda runtime",
            "driver version incompatible",
            "cuda compatibility issue",
            "cuda compatibility problem",
            "torch needs newer cuda",
            "torch needs a newer cuda",
            "driver does not support cuda",
        ),
        ("driver", "cuda", "torch", "version", "requires", "too old"),
        "A PyTorch/CUDA build requires compatible runtime and driver versions; "
        "mismatch errors often name both versions or say the driver is too old.",
    ),
    Entry(
        (
            "missing library",
            "shared library missing",
            "import error",
            "missing so file",
            "cannot open shared object file",
            "undefined symbol",
            "cublas missing",
        ),
        (
            "importerror",
            "cannot open shared object file",
            "undefined symbol",
            "libcudnn",
            "cublas",
        ),
        "A binary built against incompatible CUDA libraries can fail at load "
        "time with a missing shared object or an undefined symbol.",
    ),
    Entry(
        (
            "no gpu detected",
            "gpu not found",
            "cuda not available",
            "gpu not detected",
            "no cuda device",
            "cuda device not found",
            "gpu unavailable",
        ),
        ("driver", "cuda", "nvidia", "device not found"),
        "When CUDA cannot see a device, common causes include an unavailable "
        "device, missing driver, or runtime configuration problem.",
    ),
    Entry(
        ("segfault", "segmentation fault", "core dumped", "exit code 139"),
        ("sigsegv", "139", "segmentation fault", "undefined symbol"),
        "Exit code 139 = 128 + 11 (SIGSEGV); native-library incompatibility "
        "is one possible cause in ML environments, not the only cause.",
    ),
    # --- node lifecycle and preemption -------------------------------------
    Entry(
        (
            "preempted",
            "preemption",
            "spot instance",
            "spot interruption",
            "interruptible instance",
            "preemptible node",
            "node reclaimed",
            "instance reclaimed",
            "node was reclaimed",
            "node went away",
            "node disappeared",
            "node terminated unexpectedly",
            "node was evicted",
            "evicted node",
        ),
        (
            "termination notice",
            "reclaim",
            "interruption",
            "node lost",
            "heartbeat missed",
            "terminated",
        ),
        "Spot or interruptible capacity may be reclaimed by the provider, "
        "often with a termination notice followed by lost heartbeats.",
    ),
    Entry(
        (
            "node died",
            "node lost",
            "lost node",
            "node crashed",
            "machine died",
            "machine disappeared",
            "node heartbeat missed",
            "heartbeat missed on node",
        ),
        ("heartbeat missed", "node lost", "terminated", "node marked lost"),
        "A scheduler may identify an unavailable node through missed "
        "heartbeats or an explicit node-lost event.",
    ),
    # --- service/network failures ------------------------------------------
    Entry(
        (
            "service timed out",
            "service request timed out",
            "api request timed out",
            "request timed out",
            "waiting for response",
            "timed out waiting for response",
            "dependency not responding",
            "service not responding",
            "operation timed out",
            "read timeout",
            "connection timeout",
        ),
        ("timed out", "timeout", "waiting for response", "retry"),
        "A slow or unavailable dependency can surface as a request timeout "
        "or a client waiting too long for a response.",
    ),
    Entry(
        ("rate limit", "rate limited", "throttled", "too many requests"),
        ("429", "too many requests", "retry", "backoff"),
        "HTTP 429 Too Many Requests is the standard throttling response; "
        "clients are expected to back off and retry.",
    ),
    Entry(
        (
            "connection refused",
            "service unreachable",
            "cannot connect to service",
            "network connection error",
            "connection failed",
        ),
        ("connection refused", "timeout", "timed out", "unreachable"),
        "A down service may refuse connections; an overloaded or partitioned "
        "service may instead time out or become unreachable.",
    ),
    Entry(
        (
            "data loader",
            "dataloader",
            "loader workers",
            "input pipeline",
            "data loading worker failed",
            "dataloader worker crashed",
        ),
        ("dataloader", "workers", "loader", "timed out"),
        "PyTorch input pipelines use DataLoader worker processes; their "
        "failures commonly mention the loader or its workers.",
    ),
)


def tokens(text: str) -> tuple[str, ...]:
    """Tokenize using the same normalization as the retrieval pipeline."""
    return tuple(tokenize(text))


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    """Return true when needle occurs as a contiguous whole-token phrase."""
    n = len(needle)
    return bool(n) and any(
        haystack[i : i + n] == needle for i in range(len(haystack) - n + 1)
    )


def _contains_with_bridges(
    haystack: tuple[str, ...],
    needle: tuple[str, ...],
    *,
    max_bridge_words: int = _MAX_BRIDGE_WORDS,
) -> bool:
    """Match a multi-token phrase across a few harmless grammatical words.

    Examples: "disk is full" matches "disk full" and "disk is completely
    full" matches "disk full". Single-token glossary phrases remain exact,
    which prevents broad triggers such as a standalone "driver" or "slow".
    """
    if not needle:
        return False
    if _contains(haystack, needle):
        return True
    if len(needle) == 1:
        return False

    for start, token in enumerate(haystack):
        if token != needle[0]:
            continue
        position = start
        matched = True
        for expected in needle[1:]:
            found_at = None
            # Search forward over zero to max_bridge_words permitted words.
            for gap in range(max_bridge_words + 1):
                candidate = position + gap + 1
                if candidate >= len(haystack):
                    break
                skipped = haystack[position + 1 : candidate]
                if any(word not in _BRIDGE_WORDS for word in skipped):
                    break
                if haystack[candidate] == expected:
                    found_at = candidate
                    break
            if found_at is None:
                matched = False
                break
            position = found_at
        if matched:
            return True
    return False


def matched_entries(
    question: str,
    entries: tuple[Entry, ...] = GLOSSARY,
) -> tuple[Entry, ...]:
    """Return matching entries, primarily for tests and retrieval diagnostics."""
    have = tokens(question)
    matches: list[Entry] = []
    for entry in entries:
        if any(
            _contains_with_bridges(have, tokens(phrase)) for phrase in entry.phrases
        ):
            matches.append(entry)
    return tuple(matches)


def expand(question: str, entries: tuple[Entry, ...] = GLOSSARY) -> str:
    """Append log vocabulary for matched phrases, preserving the question.

    Matching is whole-token and case/punctuation normalization is delegated
    to ``retrieval.clean.tokenize``. Multi-word phrases tolerate up to two
    harmless bridge words. Single-word entries still require an exact token.
    Each appended term is included at most once, in glossary order.
    """
    matches = matched_entries(question, entries)
    added: list[str] = []
    for entry in matches:
        for term in entry.terms:
            if term not in added:
                added.append(term)
    return f"{question} {' '.join(added)}" if added else question


def is_question_like(text: str) -> bool:
    """Recognize obvious natural-language questions before a coarse log check.

    This intentionally catches questions that contain technical tokens, e.g.
    "what does exit code 137 mean?". It is a small precedence helper, not a
    general-purpose log classifier.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if "?" in stripped:
        return True
    first = tokens(stripped)[:2]
    question_starts = {
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
    }
    return bool(first and first[0] in question_starts)


def expand_for_retrieval(
    text: str,
    *,
    is_log: bool = False,
    entries: tuple[Entry, ...] = GLOSSARY,
) -> str:
    """Expand question text while leaving pasted logs unchanged.

    If an upstream detector labels text as a log, a clear natural-language
    question takes precedence. This fixes the case where "what does exit code
    137 mean?" is incorrectly classified as a log merely because it contains
    an error-code phrase. Pass the detector's result through ``is_log``.
    """
    if is_log and not is_question_like(text):
        return text
    return expand(text, entries)


def to_markdown(entries: tuple[Entry, ...] = GLOSSARY) -> str:
    """Render the glossary for documentation/review."""
    rows = ["| phrases | terms appended | why |", "|---|---|---|"]
    rows += [
        f"| {', '.join(entry.phrases)} | {', '.join(entry.terms)} | {entry.why} |"
        for entry in entries
    ]
    return "\n".join(rows) + "\n"


if __name__ == "__main__":
    print(to_markdown(), end="")
