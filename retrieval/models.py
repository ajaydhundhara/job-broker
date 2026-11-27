"""Request and response models for /failures (Pydantic v2).

The request is strict like the broker's: StrictInt (no "5" -> 5) and
extra="forbid" (a misspelt field is a 422, not silently ignored).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

MAX_TEXT = 20_000  # a 70-line log is ~5 KB; leaves room for long pastes

ResolutionStatus = Literal["recorded", "inferred", "none"]


class FailureSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # Stripped before min_length is checked, so a blank text is rejected.
    text: Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]
    k: Annotated[StrictInt, Field(ge=1, le=20)] = 5


class FailureHit(BaseModel):
    id: str
    team: str
    cause_group: str
    error_lines: list[str]  # redacted signal lines
    resolution: str | None  # None when nothing was recorded or inferred
    resolution_status: ResolutionStatus
    score: float  # the cause group's score (compared with TAU)
    note: str | None


class FailureSearchResponse(BaseModel):
    input_type: Literal["log", "question"]
    backend: str
    no_match: bool
    message: str | None
    results: list[FailureHit]


class FailureRecord(BaseModel):
    id: str
    team: str
    log: str  # redacted
    signal_lines: list[str]
    cause_group: str
    resolution: str | None
    resolution_status: ResolutionStatus
    note: str | None
