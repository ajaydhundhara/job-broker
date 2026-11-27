"""All settings in one frozen dataclass, passed explicitly to the engine,
scheduler, reaper and provider. Only Config.from_env() reads the environment,
so the rest of the code is a pure function of (database, config).

See SPEC_DECISIONS.md for why each value is what it is.
"""

import math
import os
from dataclasses import dataclass

# node type -> GPUs and price, from the brief.
GPUS = {"gpu-s": 1, "gpu-m": 4, "gpu-l": 8}
PRICE_MICROS = {"gpu-s": 1_000_000, "gpu-m": 4_500_000, "gpu-l": 8_500_000}
NODE_TYPES_BY_SIZE = ("gpu-s", "gpu-m", "gpu-l")
POLICIES = ("fair", "fifo")


@dataclass(frozen=True)
class Config:
    # --- broker policy ---------------------------------------------------------
    max_nodes: int = 10  # [brief] live = launching or ready
    policy: str = "fair"  # queue order: 'fair' | 'fifo' (decision 6)
    idle_minutes: int = 10  # reaper: idle MORE than this (decision 1)
    min_node_lifetime_s: int = 600  # [policy] keep a ready node this long
    # A fallback to a larger node type may cost at most this many times the
    # preferred type's hourly price (decisions 3, 13).
    max_fallback_price_ratio: float = 2.0
    # [policy] at the node cap, evict another team's idle node to make room (#12)
    evict_at_cap: bool = True

    # --- retry policies (decision 2), per node type ------------------------------
    capacity_backoff_base_s: int = 60
    capacity_backoff_max_s: int = 900
    never_ready_backoff_base_s: int = 60  # 1st failure 0 s, then 60, 120, ...
    never_ready_backoff_max_s: int = 480

    # --- test-only switches (decision 7) -------------------------------------
    scheduler_enabled: bool = True
    reaper_enabled: bool = True

    # --- fake provider ---------------------------------------------------------
    seed: int = 42
    boot_min_s: int = 240  # [brief] ready 4-8 minutes after launch
    boot_max_s: int = 480
    never_ready_prob: float = 0.03  # [brief] ~3% never become ready ...
    never_ready_after_s: int = 900  # ... reported failed after 15 minutes
    outage_bucket_s: int = 600  # [brief] CapacityUnavailable comes in bursts
    outage_start_prob: float = 0.02
    outage_len_buckets: tuple[int, int] = (2, 6)
    # [test] explicit outages: (node_type, from_ts, to_ts), half-open (decision 4)
    unavailable_windows: tuple[tuple[str, int, int], ...] = ()
    # [test] launch numbers (1 = first launch ever) that never become ready
    never_ready_launches: tuple[int, ...] = ()

    def __post_init__(self):
        # Fail at startup, not in the middle of a scheduling pass.
        checks = [
            (self.max_nodes >= 1, "max_nodes must be at least 1"),
            (self.policy in POLICIES, f"policy must be one of {POLICIES}"),
            (self.idle_minutes >= 0, "idle_minutes must be 0 or more"),
            (self.min_node_lifetime_s >= 0, "min_node_lifetime_s must be 0 or more"),
            (
                self.max_fallback_price_ratio >= 1,
                "max_fallback_price_ratio must be >= 1",
            ),
            (
                0 < self.boot_min_s <= self.boot_max_s,
                "need 0 < boot_min_s <= boot_max_s",
            ),
            (0 <= self.never_ready_prob <= 1, "never_ready_prob must be in [0, 1]"),
            (0 <= self.outage_start_prob <= 1, "outage_start_prob must be in [0, 1]"),
            (
                1 <= self.outage_len_buckets[0] <= self.outage_len_buckets[1],
                "outage_len_buckets must be (min, max) with 1 <= min <= max",
            ),
        ]
        for node_type, start, end in self.unavailable_windows:
            checks.append((node_type in GPUS, f"unknown node type {node_type!r}"))
            checks.append((start < end, f"window {start}..{end} is empty"))
        for ok, message in checks:
            if not ok:
                raise ValueError(message)

    @property
    def idle_timeout_s(self) -> int:
        return self.idle_minutes * 60

    def fair_node_cap(self, active_teams: int) -> int:
        """Most live nodes one team may hold while others wait (decision 12)."""
        return math.ceil(self.max_nodes / max(active_teams, 1))

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        env = os.environ

        def first(*names: str, default: str) -> str:
            # The older names (BROKER_IDLE_TIMEOUT_MINUTES, BROKER_MAX_LIVE_NODES)
            # still work, so existing setups don't silently change.
            return next((env[n] for n in names if n in env), default)

        values = {
            "idle_minutes": int(
                first(
                    "BROKER_IDLE_MINUTES", "BROKER_IDLE_TIMEOUT_MINUTES", default="10"
                )
            ),
            "max_nodes": int(
                first("BROKER_MAX_NODES", "BROKER_MAX_LIVE_NODES", default="10")
            ),
            "policy": env.get("BROKER_POLICY", "fair"),
            "seed": int(env.get("BROKER_SEED", "42")),
        }
        values.update(overrides)
        return cls(**values)
