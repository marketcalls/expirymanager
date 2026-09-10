"""Request and response models for the plan and commit surface.

These live here rather than inside the planner because three consumers share them: the planner
produces a `PlanPreview`, the job service consumes a `DownloadRequest`, and the routes render
both. One definition means the number the user was shown and the number the commit gate compares
against are literally the same field, which is the entire point of `confirm_requests`.

Two rules the models enforce so no caller has to remember them:

- A plan request forbids unknown fields, inherited from `RequestModel`. A mistyped `include_oi`
  must be a 422 and never a download that quietly omits open interest.
- A strike scope is validated as a whole, not field by field. `{"mode": "atm_band"}` without
  `steps`, or `{"mode": "explicit"}` without strikes, is a malformed scope and is rejected at the
  edge rather than silently planning every strike in the chain, which on a NIFTY weekly is the
  difference between 20 requests and 482.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from expirymanager.api.schemas.common import ApiModel, RequestModel

__all__ = [
    "INSTRUMENT_CLASSES",
    "OPTION_TYPES",
    "STRIKE_SCOPE_MODES",
    "MAX_EXPIRIES_PER_PLAN",
    "MAX_RESOLUTIONS_PER_PLAN",
    "StrikeScope",
    "PlanRequest",
    "PlanWarning",
    "PlanPreview",
    "DownloadRequest",
    "DownloadAccepted",
    "JobActionResult",
    "RetryFailedAccepted",
]

INSTRUMENT_CLASSES = ("FUT", "OPT", "BOTH")
OPTION_TYPES = ("CE", "PE")
STRIKE_SCOPE_MODES = ("all", "atm_band", "explicit")

# A plan is answered from local state and costs zero Fyers requests, but it still walks every
# contract of every selected expiry at every selected resolution. These two ceilings keep one
# request off the event loop for longer than a user will wait, and they are far above any real
# sheet: eight expiries and two resolutions is the documented worked example.
MAX_EXPIRIES_PER_PLAN = 200
MAX_RESOLUTIONS_PER_PLAN = 20


class StrikeScope(RequestModel):
    """Which strikes of a chain the sheet selected.

    `atm_band` is expressed in strike steps each side of the money rather than in rupees, because
    a step is 50 on NIFTY and 100 on BANKNIFTY and the user is thinking in strikes.
    """

    mode: Literal["all", "atm_band", "explicit"] = "all"
    steps: int | None = Field(default=None, ge=1, le=200)
    strikes: list[float] | None = None

    @model_validator(mode="after")
    def _coherent(self) -> "StrikeScope":
        if self.mode == "atm_band" and self.steps is None:
            raise ValueError("an atm_band strike scope needs steps")
        if self.mode == "explicit" and not self.strikes:
            raise ValueError("an explicit strike scope needs at least one strike")
        if self.mode != "atm_band" and self.steps is not None:
            raise ValueError("steps is only meaningful for an atm_band strike scope")
        if self.mode != "explicit" and self.strikes is not None:
            raise ValueError("strikes is only meaningful for an explicit strike scope")
        return self


class PlanRequest(RequestModel):
    """The download sheet, exactly as API.md section 6 documents it."""

    underlying_id: int = Field(ge=1)
    expiry_dates: Annotated[list[date], Field(min_length=1)]
    resolutions: Annotated[list[str], Field(min_length=1)]
    instrument_class: Literal["FUT", "OPT", "BOTH"] = "OPT"
    option_types: list[Literal["CE", "PE"]] = Field(default_factory=lambda: ["CE", "PE"])
    strike_scope: StrikeScope = Field(default_factory=StrikeScope)
    include_oi: bool = True
    force_refresh: bool = False
    range_from: date | None = None
    range_to: date | None = None
    # Spot history is off by default because a download sheet is about contracts. The
    # underlying_history schedule turns it on, and an atm_band scope needs the spot series to
    # exist already rather than to be planned alongside the thing that depends on it.
    include_spot: bool = False

    @field_validator("expiry_dates")
    @classmethod
    def _unique_expiries(cls, value: list[date]) -> list[date]:
        if len(value) > MAX_EXPIRIES_PER_PLAN:
            raise ValueError(f"at most {MAX_EXPIRIES_PER_PLAN} expiries can be planned at once")
        return sorted(set(value))

    @field_validator("resolutions")
    @classmethod
    def _unique_resolutions(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value:
            code = item.strip().upper()
            if not code:
                raise ValueError("a resolution code cannot be blank")
            if code not in seen:
                seen.append(code)
        if len(seen) > MAX_RESOLUTIONS_PER_PLAN:
            raise ValueError(
                f"at most {MAX_RESOLUTIONS_PER_PLAN} resolutions can be planned at once"
            )
        return seen

    @field_validator("option_types")
    @classmethod
    def _unique_option_types(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return seen

    @model_validator(mode="after")
    def _ordered_range(self) -> "PlanRequest":
        if self.range_from and self.range_to and self.range_to < self.range_from:
            raise ValueError("range_to precedes range_from")
        if self.instrument_class == "OPT" and not self.option_types:
            raise ValueError("an options plan needs at least one option type")
        return self

    def scope_key(self) -> dict[str, Any]:
        """The identity of the sheet, for the job's params_json and for a plan comparison."""
        return self.model_dump(mode="json")


class PlanWarning(ApiModel):
    """One machine readable warning, so the UI can render a remedy rather than a sentence."""

    code: str
    message: str
    detail: Any | None = None


class PlanPreview(ApiModel):
    """The PIPELINE.md section 1.1 preview.

    Every field is derived from local state. Asking for a plan costs zero Fyers requests, which is
    what lets the user price a 62,000 request backfill before committing to it.
    """

    tasks_total: int = 0
    requests_estimated: int = 0
    chunks_skipped_covered: int = 0
    contracts_sealed_skipped: int = 0
    rows_estimated: int = 0
    bytes_estimated: int = 0
    eta_seconds: int = 0
    budget_used_today: int = 0
    budget_remaining_today: int = 0
    budget_after: int = 0
    warnings: list[str] = Field(default_factory=list)

    # Beyond the documented shape, and additive: the UI needs to explain the number, and the
    # commit gate needs to know whether the reserve was the binding constraint.
    expiries_planned: int = 0
    contracts_planned: int = 0
    discovery_tasks: int = 0
    estimated_downstream_requests: int = 0
    spot_tasks: int = 0
    budget_allowance: int = 0
    reserve_applied: bool = False
    exceeds_budget: bool = False
    warning_details: list[PlanWarning] = Field(default_factory=list)


class DownloadRequest(PlanRequest):
    """The plan request plus the commit gate.

    `confirm_requests` must equal the `requests_estimated` of the plan the user was actually
    shown. A mismatch is a 409, which is what stops a stale preview committing a job nobody
    priced. It is deliberately optional: a schedule fire and an internal caller never saw a
    preview, and an estimate the user did not see is not a gate.
    """

    confirm_requests: int | None = Field(default=None, ge=0)
    defer_to_tomorrow: bool = False
    priority: int | None = Field(default=None, ge=1, le=1000)


class DownloadAccepted(ApiModel):
    """The 202 body of POST /api/v1/downloads."""

    job_id: str
    status: str
    total_tasks: int
    est_requests: int
    deferred: bool = False
    preview: PlanPreview | None = None


class JobActionResult(ApiModel):
    """The 200 body of pause, resume and cancel."""

    job_id: str
    status: str
    cancel_requested: bool = False
    tasks_cancelled: int = 0


class RetryFailedAccepted(ApiModel):
    """The 202 body of POST /api/v1/jobs/{job_id}/retry-failed."""

    job_id: str
    parent_job_id: str
    total_tasks: int
