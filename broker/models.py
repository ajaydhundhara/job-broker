"""Pydantic v2 request and response models for the HTTP API.

Request models are strict on purpose:
  * StrictInt: Pydantic would otherwise turn "4", 4.0 or true into an int
    (true would even pass as gpus=1).
  * extra="forbid": a typo such as "prioirty" must be a 422, not a job that
    silently got the default priority.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

Priority = Literal["low", "normal", "high"]

# Limits the exercise leaves open; chosen here and documented in the README.
# 128: long enough for any real team name, bounded for logs and reports.
MAX_TEAM_NAME_LENGTH = 128
MAX_ESTIMATED_MINUTES = 10_080  # 7 days: the brief says jobs run "minutes to days"
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --- requests ----------------------------------------------------------------


class JobSubmit(StrictRequest):
    # Whitespace is stripped before min_length is checked, so "  " is rejected.
    team: Annotated[str, Field(min_length=1, max_length=MAX_TEAM_NAME_LENGTH)]
    gpus: Literal[1, 2, 4, 8]
    estimated_minutes: Annotated[StrictInt, Field(gt=0, le=MAX_ESTIMATED_MINUTES)]
    priority: Priority = "normal"
    # An empty key would be one key shared by every caller of the team.
    idempotency_key: Annotated[str, Field(min_length=1)] | None = None

    @model_validator(mode="before")
    @classmethod
    def gpus_must_be_a_real_int(cls, data):
        # Literal[1, 2, 4, 8] accepts True (because True == 1 in Python) and
        # floats such as 4.0 (checked with Pydantic 2.14). Reject anything that
        # isn't a plain int.
        if isinstance(data, dict) and "gpus" in data:
            gpus = data["gpus"]
            if isinstance(gpus, bool) or not isinstance(gpus, int):
                raise ValueError("gpus must be an integer: 1, 2, 4 or 8")
        return data


class JobComplete(StrictRequest):
    outcome: Literal["succeeded", "failed"]


class SimAdvance(StrictRequest):
    # 0 is allowed and is a no-op, like to_ts == now.
    minutes: Annotated[StrictInt, Field(ge=0)] | None = None
    to_ts: StrictInt | None = None

    @model_validator(mode="after")
    def exactly_one_field(self):
        if (self.minutes is None) == (self.to_ts is None):
            raise ValueError("give exactly one of 'minutes' or 'to_ts'")
        return self


# --- responses ---------------------------------------------------------------


class JobOut(BaseModel):
    id: str
    team: str
    idempotency_key: str | None
    request_fingerprint: str
    gpus: int
    estimated_minutes: int
    priority: Priority
    status: JobStatus
    node_id: str | None
    submitted_at: int
    started_at: int | None
    ended_at: int | None
    end_reason: str | None
    requeue_count: int
    # Started: started_at - submitted_at. Queued: now - submitted_at.
    # Cancelled before starting: ended_at - submitted_at (it waited until cancelled).
    wait_seconds: int | None


class QueueEntry(JobOut):
    position: int  # 1-based place in queue_order()


class SimNow(BaseModel):
    now_ts: int
    now_iso: str  # UTC, ISO 8601 with "Z"


class Health(BaseModel):
    status: Literal["ok"]


# Money in responses is dollars rounded to cents, converted from integer
# micro-dollars only at this edge (costs.micros_to_dollars).


class NodeOut(BaseModel):
    id: str
    type: str
    team: str
    state: str
    gpus_total: int
    gpus_used: int  # derived: SUM(gpus) of running jobs, never stored
    gpus_free: int
    running_job_ids: list[str]
    launched_at: int
    ready_at: int | None
    idle_since: int | None
    terminated_at: int | None
    cost_so_far: float  # dollars, window [0, now)


class TeamCost(BaseModel):
    team: str
    gpu_hours: float  # GPUs requested x hours run, 3 decimals
    busy_cost: float
    idle_cost: float
    total_cost: float


class CostReport(BaseModel):
    from_ts: int
    to_ts: int
    teams: list[TeamCost]
    provider_overhead: float  # failed launches, not charged to any team
    grand_total: float
