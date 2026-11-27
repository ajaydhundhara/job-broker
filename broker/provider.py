"""The fake cloud provider: launch_node(type) and terminate_node(node_id).

It keeps its own state in `provider_nodes`, apart from the broker's `nodes`,
because the provider is the source of truth for what exists and bills. It
lives in the same SQLite file, so it survives restarts like everything else.

Behaviour (brief): ready 4-8 minutes after launch; ~3% never become ready and
are reported failed after 15 minutes; CapacityUnavailable comes in bursts.

Determinism (SPEC_DECISIONS.md #10): every random draw comes from
rng(purpose, key), a fresh generator per decision, by default
random.Random(f"{seed}:{purpose}:{key}"). The keys (launch number, outage
bucket) are persistent, so the same draws happen after a restart.

The provider never opens a transaction: the caller holds one, so a launch and
the broker's record of it commit together, and the "count + 1" id below is
taken under the write lock.
"""

import random
import sqlite3
from collections.abc import Callable

from broker.config import GPUS, Config

Rng = Callable[[str, str], random.Random]


class CapacityUnavailable(Exception):
    """The provider has no capacity of this node type right now."""


class UnknownNode(Exception):
    """terminate_node() was called with an id the provider never issued."""


def seeded_rng(seed: int) -> Rng:
    """The default generator factory: one seeded generator per decision."""

    def rng(purpose: str, key: str) -> random.Random:
        return random.Random(f"{seed}:{purpose}:{key}")

    return rng


class FakeProvider:
    def __init__(self, cfg: Config, rng: Rng | None = None):
        self.cfg = cfg
        self.rng = rng or seeded_rng(cfg.seed)

    # --- the provider's API -------------------------------------------------

    def launch_node(self, conn: sqlite3.Connection, node_type: str, now: int) -> str:
        """Start a node; billing starts now. Raises CapacityUnavailable."""
        if node_type not in GPUS:
            raise ValueError(f"unknown node type {node_type}")
        if self.in_outage(node_type, now):
            raise CapacityUnavailable(f"{node_type} unavailable at {now}")
        n = conn.execute("SELECT COUNT(*) FROM provider_nodes").fetchone()[0] + 1
        node_id = f"node-{n:05d}"  # sequential: reproducible (decision 14)
        ready_at, fails_at = self._plan(n, node_id, now)
        conn.execute(
            """INSERT INTO provider_nodes (id, node_type, interruptible, requested_at,
                   ready_at_planned, fails_at_planned, state)
               VALUES (?, ?, 0, ?, ?, ?, 'pending')""",
            (node_id, node_type, now, ready_at, fails_at),
        )
        return node_id

    def terminate_node(self, conn: sqlite3.Connection, node_id: str, now: int) -> None:
        """Give a node back; billing ends now. A no-op for ended nodes."""
        node = self.get(conn, node_id)
        if node is None:
            raise UnknownNode(node_id)
        if node["state"] in ("terminated", "failed"):
            return
        conn.execute(
            "UPDATE provider_nodes SET state = 'terminated', terminated_at = ? WHERE id = ?",
            (now, node_id),
        )

    def get(self, conn, node_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM provider_nodes WHERE id = ?", (node_id,)
        ).fetchone()

    # --- outages (decision 4) ----------------------------------------------

    def in_outage(self, node_type: str, now: int) -> bool:
        """True if launches of this type are refused at `now`. Stateless."""
        for window_type, start, end in self.cfg.unavailable_windows:
            if window_type == node_type and start <= now < end:
                return True
        return self._in_random_burst(node_type, now)

    def _in_random_burst(self, node_type: str, now: int) -> bool:
        # A burst that started in any of the last max_len buckets may still be
        # running. Each start is decided on its own, from its own generator.
        cfg = self.cfg
        b = now // cfg.outage_bucket_s
        for s in range(b - cfg.outage_len_buckets[1] + 1, b + 1):
            g = self.rng("outage", f"{node_type}:{s}")
            if g.random() < cfg.outage_start_prob:
                length = g.randint(*cfg.outage_len_buckets)  # second draw, same g
                if s <= b <= s + length - 1:
                    return True
        return False

    # --- what the engine needs ------------------------------------------------

    def due_transitions(self, conn, now: int) -> list[sqlite3.Row]:
        """Pending nodes whose ready or failure time has come, oldest first."""
        return conn.execute(
            """SELECT *, COALESCE(ready_at_planned, fails_at_planned) AS due_at
               FROM provider_nodes
               WHERE state = 'pending' AND COALESCE(ready_at_planned, fails_at_planned) <= ?
               ORDER BY due_at, id""",
            (now,),
        ).fetchall()

    def apply_transition(self, conn, row: sqlite3.Row) -> str:
        """Make a due pending node ready or failed. Returns which."""
        if row["ready_at_planned"] is not None:
            conn.execute(
                "UPDATE provider_nodes SET state = 'ready' WHERE id = ?", (row["id"],)
            )
            return "ready"
        conn.execute(
            "UPDATE provider_nodes SET state = 'failed', terminated_at = ? WHERE id = ?",
            (row["fails_at_planned"], row["id"]),
        )
        return "failed"

    def next_transition_time(self, conn, now: int) -> int | None:
        return conn.execute(
            """SELECT MIN(COALESCE(ready_at_planned, fails_at_planned))
               FROM provider_nodes WHERE state = 'pending'
                 AND COALESCE(ready_at_planned, fails_at_planned) > ?""",
            (now,),
        ).fetchone()[0]

    # --- internals -----------------------------------------------------------

    def _plan(self, n: int, node_id: str, now: int) -> tuple[int | None, int | None]:
        """(ready_at, None) for a good launch, (None, fails_at) for a bad one."""
        cfg = self.cfg
        r = self.rng("launch", node_id)
        never_ready = r.random() < cfg.never_ready_prob
        if never_ready or n in cfg.never_ready_launches:
            return None, now + cfg.never_ready_after_s
        return now + r.randint(cfg.boot_min_s, cfg.boot_max_s), None
