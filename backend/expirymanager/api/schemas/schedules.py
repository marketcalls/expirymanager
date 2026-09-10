"""Request and response models for the schedules surface, API.md section 9.

These live here rather than inside `scheduler/service.py` for the same reason the download models
live outside the planner: the service produces them, a route renders them, and the tests assert
against exactly one definition of the shape.

Two rules are enforced here rather than at the route, because both of them are the difference
between a schedule that cannot fire and an error the user can read:

- A cron expression is validated with APScheduler's own `CronTrigger.from_crontab`. Writing a
  second parser would accept expressions the scheduler then rejects at `sync()`, which is a row
  in the table that silently never fires.
- A kind is validated against the registry in `scheduler/jobs_def.py`. A schedule whose kind has
  no action is a row that fires and does nothing, forever.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from expirymanager.api.schemas.common import ApiModel, RequestModel

__all__ = [
    "DEFAULT_TIMEZONE",
    "DEFAULT_MISFIRE_GRACE_SECONDS",
    "MAX_MISFIRE_GRACE_SECONDS",
    "DEFAULT_RUN_HISTORY",
    "MAX_RUN_HISTORY",
    "SCHEDULE_OUTCOMES",
    "CronError",
    "validate_cron",
    "ScheduleRow",
    "ScheduleCreate",
    "ScheduleUpdate",
    "ScheduleRunRow",
    "RunNowResult",
    "SchedulerSnapshot",
]

DEFAULT_TIMEZONE = "Asia/Kolkata"

# PIPELINE.md section 10: a laptop asleep at 18:00 runs the job once when it wakes rather than
# four times or not at all. One hour is the default grace; a day is the ceiling, past which a
# fire is not a late fire but a fire for a day that is over.
DEFAULT_MISFIRE_GRACE_SECONDS = 3600
MAX_MISFIRE_GRACE_SECONDS = 86400

DEFAULT_RUN_HISTORY = 50
MAX_RUN_HISTORY = 500

# Mirrors the CHECK on schedule_run after migration 0005.
SCHEDULE_OUTCOMES: tuple[str, ...] = (
    "enqueued",
    "completed",
    "skipped_disabled",
    "skipped_holiday",
    "skipped_needs_auth",
    "skipped_blocked",
    "skipped_budget",
    "error",
)


class CronError(ValueError):
    """A cron expression APScheduler will not accept. Surfaces as 400 invalid_cron."""


def validate_cron(expression: str, timezone: str = DEFAULT_TIMEZONE) -> str:
    """Return the normalised expression, or raise `CronError`.

    Validated by building the real trigger, so what passes here is exactly what `sync()` can
    install. The trigger is discarded: this is a check, not a construction site.
    """
    from apscheduler.triggers.cron import CronTrigger

    text = " ".join(str(expression).split())
    if len(text.split(" ")) != 5:
        raise CronError("a cron expression has five fields: minute hour day month day_of_week")
    try:
        CronTrigger.from_crontab(text, timezone=timezone)
    except Exception as exc:  # noqa: BLE001 - apscheduler raises bare ValueError here
        raise CronError(f"{text!r} is not a cron expression this scheduler can run") from exc
    return text


class ScheduleRow(ApiModel):
    """One row of `GET /api/v1/schedules`."""

    schedule_id: str
    name: str
    kind: str
    cron: str
    timezone: str = DEFAULT_TIMEZONE
    enabled: bool = True
    trading_days_only: bool = True
    misfire_grace_seconds: int = DEFAULT_MISFIRE_GRACE_SECONDS
    max_requests_per_run: int | None = None
    is_builtin: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    next_fire_at: str | None = None
    last_fired_at: str | None = None
    last_outcome: str | None = None
    last_job_id: str | None = None
    description: str | None = None


class ScheduleCreate(RequestModel):
    """`POST /api/v1/schedules`."""

    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=64)
    cron: str = Field(min_length=1, max_length=200)
    timezone: str = DEFAULT_TIMEZONE
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    trading_days_only: bool = True
    misfire_grace_seconds: int = Field(
        default=DEFAULT_MISFIRE_GRACE_SECONDS, ge=0, le=MAX_MISFIRE_GRACE_SECONDS
    )
    max_requests_per_run: int | None = Field(default=None, ge=1)

    @field_validator("cron")
    @classmethod
    def _cron_is_runnable(cls, value: str) -> str:
        return validate_cron(value)


class ScheduleUpdate(RequestModel):
    """`PATCH /api/v1/schedules/{id}`. Every field is optional; absent means unchanged.

    `kind` is absent on purpose. Re-kinding a schedule would keep its run history while changing
    what the history is a record of, and for a builtin it would orphan the action that drives it.
    A different kind is a different schedule.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    cron: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = None
    params: dict[str, Any] | None = None
    enabled: bool | None = None
    trading_days_only: bool | None = None
    misfire_grace_seconds: int | None = Field(
        default=None, ge=0, le=MAX_MISFIRE_GRACE_SECONDS
    )
    max_requests_per_run: int | None = Field(default=None, ge=1)

    @field_validator("cron")
    @classmethod
    def _cron_is_runnable(cls, value: str | None) -> str | None:
        return None if value is None else validate_cron(value)


class ScheduleRunRow(ApiModel):
    """One row of `GET /api/v1/schedules/{id}/runs`, newest first."""

    run_id: str
    schedule_id: str
    fired_at: str
    job_id: str | None = None
    outcome: str
    note: str | None = None


class RunNowResult(ApiModel):
    """`POST /api/v1/schedules/{id}/run-now`, and what every fire returns internally."""

    schedule_id: str
    run_id: str
    job_id: str | None = None
    outcome: str
    note: str | None = None

    @property
    def enqueued(self) -> bool:
        return self.outcome == "enqueued"

    @property
    def skipped(self) -> bool:
        return self.outcome.startswith("skipped_")


class SchedulerSnapshot(ApiModel):
    """Diagnostics for the system screen: what is actually installed in the running scheduler."""

    running: bool = False
    job_count: int = 0
    timezone: str = DEFAULT_TIMEZONE
    next_fire_at: str | None = None
    schedule_ids: list[str] = Field(default_factory=list)
