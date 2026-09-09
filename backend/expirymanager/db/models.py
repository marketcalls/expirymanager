"""SQLAlchemy 2.0 declarative models for every SQLite table.

The schema itself is created by the plain SQL migrations, not by `metadata.create_all`. These
models exist so the rest of the backend has typed, named access to the same tables, and so a
column rename in a migration fails a test instead of failing a query at runtime:
tests/test_db_schema.py compares this metadata against the migrated database.

Value conventions that are not visible in the column types:

- Every timestamp column is TEXT holding an ISO 8601 UTC string, except the columns whose name
  says otherwise (`ist_date`, `holiday_date`, `data_from`, `expiry_date`, `range_from`,
  `range_to`), which hold a yyyy-mm-dd date in Asia/Kolkata.
- Every `*_json` column is TEXT holding JSON.
- Every boolean is INTEGER 0 or 1, which is what SQLite stores anyway.
- Every `*_enc` column is a BLOB holding an EM1 envelope, never plaintext.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "metadata",
    "SchemaVersion",
    "Setting",
    "CryptoKey",
    "AppUser",
    "AppSession",
    "BrokerCredential",
    "BrokerToken",
    "OAuthState",
    "UnderlyingRegistry",
    "Schedule",
    "Job",
    "Task",
    "ScheduleRun",
    "ApiBudget",
    "RateEvent",
    "PipelineState",
    "MarketHoliday",
    "ExportJob",
    "Notification",
    "AuditLog",
    "RefExchange",
    "RefSegment",
    "RefInstrumentType",
    "RefResolution",
]


class Base(DeclarativeBase):
    # Explicit index naming, because every index name in DATA-MODEL.md is fixed and the tests
    # assert on those exact names.
    metadata = MetaData()


metadata = Base.metadata


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class CryptoKey(Base):
    __tablename__ = "crypto_key"
    __table_args__ = (
        CheckConstraint(
            "kek_provider IN ('keyfile','keyring','passphrase')", name="ck_crypto_key_provider"
        ),
        CheckConstraint("state IN ('active','retiring','retired')", name="ck_crypto_key_state"),
    )

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kek_provider: Mapped[str] = mapped_column(Text, nullable=False)
    kdf_params: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    retired_at: Mapped[str | None] = mapped_column(Text)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class AppUser(Base):
    __tablename__ = "app_user"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_phc: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_login_at: Mapped[str | None] = mapped_column(Text)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[str | None] = mapped_column(Text)


class AppSession(Base):
    """The `session` table. Named AppSession so it never shadows sqlalchemy.orm.Session."""

    __tablename__ = "session"
    __table_args__ = (
        Index("idx_session_user", "user_id"),
        Index("idx_session_expiry", "absolute_expires_at"),
    )

    id_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("app_user.user_id", ondelete="CASCADE"), nullable=False
    )
    csrf_token: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_seen_at: Mapped[str] = mapped_column(Text, nullable=False)
    idle_expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    absolute_expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text)
    client_ip: Mapped[str | None] = mapped_column(Text)


class BrokerCredential(Base):
    """Fyers app registration. There is no PIN column.

    SEBI discontinued the refresh token flow from 1 April 2026, so unattended refresh is not
    possible and a stored PIN would be a fourth secret that buys nothing.
    """

    __tablename__ = "broker_credential"
    __table_args__ = (
        CheckConstraint("plan IN ('standard','prime')", name="ck_broker_credential_plan"),
    )

    credential_id: Mapped[str] = mapped_column(Text, primary_key=True)
    broker: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'fyers'"))
    label: Mapped[str] = mapped_column(Text, nullable=False)
    app_id: Mapped[str] = mapped_column(Text, nullable=False)
    app_secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'standard'"))
    key_ver: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class BrokerToken(Base):
    __tablename__ = "broker_token"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active','expiring','expired','needs_reauth','revoked')",
            name="ck_broker_token_state",
        ),
        Index("idx_token_credential", "credential_id", "state"),
    )

    token_id: Mapped[str] = mapped_column(Text, primary_key=True)
    credential_id: Mapped[str] = mapped_column(
        Text, ForeignKey("broker_credential.credential_id", ondelete="CASCADE"), nullable=False
    )
    access_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_ver: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    token_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[str] = mapped_column(Text, nullable=False)
    access_expires_at: Mapped[str | None] = mapped_column(Text)
    refresh_expires_at: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[str | None] = mapped_column(Text)


class OAuthState(Base):
    __tablename__ = "oauth_state"

    state_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    session_id_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    credential_id: Mapped[str] = mapped_column(
        Text, ForeignKey("broker_credential.credential_id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    used_at: Mapped[str | None] = mapped_column(Text)


class UnderlyingRegistry(Base):
    __tablename__ = "underlying_registry"
    __table_args__ = (
        CheckConstraint("exchange IN ('NSE','BSE','MCX')", name="ck_underlying_exchange"),
        CheckConstraint("segment IN ('CM','FO','CD','COM')", name="ck_underlying_segment"),
        CheckConstraint(
            "instrument_kind IN ('INDEX','EQUITY','COMMODITY','CURRENCY')",
            name="ck_underlying_kind",
        ),
    )

    underlying_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fyers_symbol: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    root: Mapped[str] = mapped_column(Text, nullable=False)
    exchange: Mapped[str] = mapped_column(Text, nullable=False)
    segment: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_kind: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    data_from: Mapped[str] = mapped_column(Text, nullable=False)
    default_resolutions: Mapped[str] = mapped_column(Text, nullable=False)
    include_oi: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    option_life_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("200")
    )
    future_life_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("400")
    )
    spot_contract_id: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_root_echo: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[str | None] = mapped_column(Text)
    is_builtin: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class Schedule(Base):
    __tablename__ = "schedule"

    schedule_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    cron: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'Asia/Kolkata'")
    )
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    trading_days_only: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    misfire_grace_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3600")
    )
    max_requests_per_run: Mapped[int | None] = mapped_column(Integer)
    is_builtin: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_fired_at: Mapped[str | None] = mapped_column(Text)
    next_fire_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('expiry_discovery','contract_discovery','candle_backfill',"
            "'underlying_history','symbol_master','seconds_capture','chain_snapshot',"
            "'gap_repair','export')",
            name="ck_job_kind",
        ),
        CheckConstraint(
            "status IN ('draft','queued','running','paused','blocked_auth','blocked_rate',"
            "'deferred_budget','completed','completed_with_errors','cancelled','failed')",
            name="ck_job_status",
        ),
        Index("idx_job_status", "status", text("created_at DESC")),
    )

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    parent_job_id: Mapped[str | None] = mapped_column(Text, ForeignKey("job.job_id"))
    schedule_id: Mapped[str | None] = mapped_column(Text, ForeignKey("schedule.schedule_id"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    est_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_tasks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    done_tasks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    empty_tasks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failed_tasks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    skipped_tasks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    requests_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rows_written: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    bytes_downloaded: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cancel_requested: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    block_reason: Mapped[str | None] = mapped_column(Text)
    error_text: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)


class Task(Base):
    """One outbound Fyers request: work queue entry, retry record and provenance record at once.

    idx_task_dispatch is the index the lease statement in PIPELINE.md section 3.1 rides. It is
    partial on state = 'pending' and ordered (priority, job_id, seq), so the single
    UPDATE ... RETURNING both filters and orders without a sort.
    """

    __tablename__ = "task"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('expiry_dates','underlying_symbols','candle_chunk','spot_chunk',"
            "'symbol_master','chain_snapshot')",
            name="ck_task_kind",
        ),
        CheckConstraint(
            "state IN ('pending','leased','done','empty','failed','skipped','cancelled')",
            name="ck_task_state",
        ),
        Index(
            "idx_task_dispatch",
            "priority",
            "job_id",
            "seq",
            sqlite_where=text("state = 'pending'"),
        ),
        Index("idx_task_ready", "not_before", sqlite_where=text("state = 'pending'")),
        Index("idx_task_lease", "lease_expires_at", sqlite_where=text("state = 'leased'")),
        Index("idx_task_job", "job_id", "state"),
        Index("idx_task_contract", "contract_id", "resolution", "range_from"),
    )

    task_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        Text, ForeignKey("job.job_id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))

    underlying_id: Mapped[int | None] = mapped_column(Integer)
    contract_id: Mapped[int | None] = mapped_column(Integer)
    fyers_symbol: Mapped[str | None] = mapped_column(Text)
    expiry_date: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
    range_from: Mapped[str | None] = mapped_column(Text)
    range_to: Mapped[str | None] = mapped_column(Text)
    include_oi: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    request_params_json: Mapped[str | None] = mapped_column(Text)

    parent_task_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("task.task_id"))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("4"))
    not_before: Mapped[str] = mapped_column(Text, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[str | None] = mapped_column(Text)

    http_status: Mapped[int | None] = mapped_column(Integer)
    fyers_s: Mapped[str | None] = mapped_column(Text)
    fyers_code: Mapped[int | None] = mapped_column(Integer)
    last_error_text: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    response_bytes: Mapped[int | None] = mapped_column(Integer)
    row_count: Mapped[int | None] = mapped_column(Integer)
    first_ts: Mapped[str | None] = mapped_column(Text)
    last_ts: Mapped[str | None] = mapped_column(Text)
    columns_json: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[int | None] = mapped_column(Integer)
    payload_sha256: Mapped[str | None] = mapped_column(Text)
    raw_body_path: Mapped[str | None] = mapped_column(Text)
    token_fingerprint: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class ScheduleRun(Base):
    __tablename__ = "schedule_run"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('enqueued','skipped_disabled','skipped_holiday','skipped_needs_auth',"
            "'skipped_blocked','skipped_budget','error')",
            name="ck_schedule_run_outcome",
        ),
        Index("idx_schedule_run", "schedule_id", text("fired_at DESC")),
    )

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    schedule_id: Mapped[str] = mapped_column(
        Text, ForeignKey("schedule.schedule_id", ondelete="CASCADE"), nullable=False
    )
    fired_at: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[str | None] = mapped_column(Text, ForeignKey("job.job_id"))
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


class ApiBudget(Base):
    """One row per IST date. The durable half of the governor's counters."""

    __tablename__ = "api_budget"

    ist_date: Mapped[str] = mapped_column(Text, primary_key=True)
    plan: Mapped[str] = mapped_column(Text, nullable=False)
    plan_limit_day: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_limit_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    requests_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    minute_violations: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_429_at: Mapped[str | None] = mapped_column(Text)
    blocked_until: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class RateEvent(Base):
    __tablename__ = "rate_event"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('throttle_wait','http_429','fyers_429','breaker_open','breaker_close',"
            "'budget_warning','budget_exhausted')",
            name="ck_rate_event_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)


class PipelineState(Base):
    """Exactly one row, id 1. The CHECK is what enforces the singleton."""

    __tablename__ = "pipeline_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_pipeline_state_singleton"),
        CheckConstraint(
            "mode IN ('running','paused_auth','paused_rate','paused_user',"
            "'stopped_budget','stopped_fatal')",
            name="ck_pipeline_state_mode",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by: Mapped[str | None] = mapped_column(Text)


class MarketHoliday(Base):
    __tablename__ = "market_holiday"

    exchange: Mapped[str] = mapped_column(Text, primary_key=True)
    holiday_date: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'seed'"))


class ExportJob(Base):
    __tablename__ = "export_job"
    __table_args__ = (
        CheckConstraint("format IN ('parquet','csv')", name="ck_export_job_format"),
        CheckConstraint("layout IN ('single','hive')", name="ck_export_job_layout"),
        CheckConstraint(
            "status IN ('queued','running','ready','failed','deleted')", name="ck_export_job_status"
        ),
    )

    export_id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str | None] = mapped_column(Text, ForeignKey("job.job_id"))
    format: Mapped[str] = mapped_column(Text, nullable=False)
    layout: Mapped[str] = mapped_column(Text, nullable=False)
    compression: Mapped[str | None] = mapped_column(Text)
    scope_json: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str | None] = mapped_column(Text)
    manifest_path: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int | None] = mapped_column(Integer)
    byte_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[str | None] = mapped_column(Text)


class Notification(Base):
    __tablename__ = "notification"
    __table_args__ = (
        CheckConstraint("level IN ('info','warning','error')", name="ck_notification_level"),
    )

    notification_id: Mapped[str] = mapped_column(Text, primary_key=True)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    read_at: Mapped[str | None] = mapped_column(Text)
    dismissed_at: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(Text)
    detail_json: Mapped[str | None] = mapped_column(Text)


class RefExchange(Base):
    __tablename__ = "ref_exchange"

    code: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    data_available_from: Mapped[str] = mapped_column(Text, nullable=False)


class RefSegment(Base):
    __tablename__ = "ref_segment"

    code: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class RefInstrumentType(Base):
    """Fyers instrument type integers are reused across segments, so the key is the pair."""

    __tablename__ = "ref_instrument_type"

    segment_code: Mapped[int] = mapped_column(
        Integer, ForeignKey("ref_segment.code"), primary_key=True
    )
    type_code: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class RefResolution(Base):
    __tablename__ = "ref_resolution"

    fyers_code: Mapped[str] = mapped_column(Text, primary_key=True)
    res_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    chart_interval: Mapped[str] = mapped_column(Text, nullable=False)
    max_days_per_request: Mapped[int] = mapped_column(Integer, nullable=False)
    availability_window_days: Mapped[int | None] = mapped_column(Integer)
    is_intraday: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
