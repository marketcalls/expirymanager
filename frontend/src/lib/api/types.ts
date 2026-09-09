// Hand-written mirrors of the response bodies in docs/API.md.
//
// These are hand-written rather than generated because the generator would have to run against
// a live backend, and a frontend that cannot be type-checked without the backend running is a
// frontend that blocks on it. The cost is that a backend schema change has to be mirrored here
// by hand, which is why every field below is named exactly as the wire names it: a rename shows
// up as a compile error at the call site instead of as undefined at runtime.
//
// Wire naming is snake_case throughout. It is not camelised on the way in. Renaming at the
// boundary means every field has two names and every bug report has to be translated.

// ---------------------------------------------------------------------------
// Shared primitives
// ---------------------------------------------------------------------------

/** ISO 8601 date, no time component. Example: 2025-03-27. */
export type IsoDate = string

/** ISO 8601 timestamp. IST offset when the backend renders a wall clock, naive when it renders
 *  a DuckDB timestamp. Never parsed blindly: use the helpers in lib/format.ts. */
export type IsoTimestamp = string

/** Seconds since the Unix epoch, UTC. The chart adapter consumes these directly. */
export type EpochSeconds = number

export interface Paged<TItem> {
  items: TItem[]
  next_cursor: string | null
}

export type TokenState = 'active' | 'expired' | 'revoked' | 'none'
export type PipelineMode = 'running' | 'paused' | 'stopped' | 'blocked_auth'
export type InstrumentKind = 'INDEX' | 'EQUITY'
export type ContractKind = 'FUT' | 'OPT' | 'SPOT'
export type OptionType = 'CE' | 'PE'
export type BrokerPlan = 'standard' | 'prime'

// ---------------------------------------------------------------------------
// 1. Bootstrap and authentication
// ---------------------------------------------------------------------------

/** GET /api/v1/bootstrap. The only fact the SPA reads before it picks a screen. */
export interface Bootstrap {
  provisioned: boolean
  has_user: boolean
  has_credentials: boolean
  broker_connected: boolean
  token_state: TokenState
  token_expires_at: IsoTimestamp | null
  needs_reauth: boolean
  data_dir: string
  app_version: string
  duckdb_version: string
}

export interface SetupRequest {
  username: string
  password: string
}

export interface SetupResponse {
  user_id: string
}

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  user_id: string
  username: string
}

export interface CurrentUser {
  user_id: string
  username: string
  session_expires_at: IsoTimestamp
}

export interface PasswordChangeRequest {
  current_password: string
  new_password: string
}

// ---------------------------------------------------------------------------
// 2. Broker credentials and OAuth
// ---------------------------------------------------------------------------

/** GET /api/v1/broker/fyers and every write that returns the same shape.
 *
 *  There is no secret and no mask here, by design: a mask is an oracle, and it tempts the
 *  frontend into round-tripping it back on save. Only the *_configured booleans cross. */
export interface BrokerStatus {
  credential_id: string | null
  label: string | null
  app_id: string | null
  redirect_uri: string
  plan: BrokerPlan
  app_secret_configured: boolean
  connected: boolean
  token_state: TokenState
  token_expires_at: IsoTimestamp | null
  token_fingerprint: string | null
  last_error: string | null
}

export interface BrokerCredentialsRequest {
  label: string
  app_id: string
  app_secret: string
  redirect_uri: string
  plan: BrokerPlan
}

export interface BrokerConnectResponse {
  authorize_url: string
  state_expires_at: IsoTimestamp
}

export interface BrokerManualCallbackRequest {
  redirected_url: string
}

export interface BrokerTestResponse {
  ok: boolean
  endpoint: string
  latency_ms: number
  requests_used_today: number
}

// ---------------------------------------------------------------------------
// 3. Underlyings
// ---------------------------------------------------------------------------

export interface Underlying {
  underlying_id: number
  fyers_symbol: string
  root: string
  exchange: string
  segment: string
  instrument_kind: InstrumentKind
  display_name: string
  data_from: IsoDate
  default_resolutions: string[]
  include_oi: boolean
  is_builtin: boolean
  is_active: boolean
  expiry_count: number
  contract_count: number
  first_expiry: IsoDate | null
  last_expiry: IsoDate | null
  spot_bars: number
  spot_last_ts: IsoTimestamp | null
}

export interface UnderlyingCandidate {
  fyers_symbol: string
  root: string
  exchange: string
  segment: string
  instrument_kind: InstrumentKind
  under_fytoken: string
  fo_contract_count: number
  source: string
}

export interface UnderlyingResolveResponse {
  candidates: UnderlyingCandidate[]
  probe: {
    attempted: boolean
    root_echo: string | null
    expiry_count: number | null
  }
}

export interface UnderlyingCreateRequest {
  fyers_symbol: string
  display_name: string
  default_resolutions: string[]
  include_oi: boolean
  option_life_days: number
  future_life_days: number
}

export interface UnderlyingPatchRequest {
  display_name?: string
  default_resolutions?: string[]
  include_oi?: boolean
  option_life_days?: number
  future_life_days?: number
  is_active?: boolean
}

// ---------------------------------------------------------------------------
// 4. Expiries
// ---------------------------------------------------------------------------

/** The four counts CoverageBar renders. contracts_with_data is the numerator the screens show,
 *  the three chunk counts are the segments. */
export interface Coverage {
  contracts_with_data: number
  contracts_sealed: number
  chunks_ok: number
  chunks_empty: number
  chunks_missing: number
  rows: number
}

export interface Expiry {
  expiry_date: IsoDate
  expiry_dow: number
  has_futures: boolean
  has_options: boolean
  futures_count: number
  options_count: number
  contract_count: number
  expiry_cycle: string | null
  expiry_cycle_source: string | null
  contracts_discovered_at: IsoTimestamp | null
  contract_id_lo: number | null
  contract_id_hi: number | null
  min_strike: number | null
  max_strike: number | null
  strike_step: number | null
  coverage: Coverage | null
}

export interface ExpiryDiscoverRequest {
  range_from: IsoDate
  range_to: IsoDate
}

// ---------------------------------------------------------------------------
// 5. Contracts
// ---------------------------------------------------------------------------

export interface ContractResolutionBounds {
  res_id: number
  fyers_code: string
  rows: number
  first_ts: IsoTimestamp | null
  last_ts: IsoTimestamp | null
}

export interface Contract {
  contract_id: number
  fyers_symbol: string
  underlying_id: number
  kind: ContractKind
  instrument_class: string
  expiry_date: IsoDate | null
  strike: number | null
  strike_raw: string | null
  option_type: OptionType | null
  lot_size: number | null
  tick_size: number | null
  fytoken: string
  symbol_expiry_encoding: string | null
  expiry_cycle: string | null
  parse_confidence: string | null
  sealed_at: IsoTimestamp | null
  resolutions: ContractResolutionBounds[]
}

export interface ContractDetail extends Contract {
  coverage: Coverage | null
}

/** GET /api/v1/contracts/{id}/bounds. first_ts and last_ts are UTC seconds here, not ISO, so
 *  the chart window clamp needs no conversion. This is the lookup that stops every expired
 *  contract rendering as no bars. */
export interface ContractBoundsResolution {
  res_id: number
  fyers_code: string
  chart_interval: string
  first_ts: EpochSeconds
  last_ts: EpochSeconds
  rows: number
}

export interface ContractBounds {
  contract_id: number
  fyers_symbol: string
  resolutions: ContractBoundsResolution[]
}

// ---------------------------------------------------------------------------
// 6. Downloads and jobs
// ---------------------------------------------------------------------------

export type StrikeScope =
  | { mode: 'all' }
  | { mode: 'atm_band'; steps: number }
  | { mode: 'explicit'; strikes: number[] }

export interface DownloadPlanRequest {
  underlying_id: number
  expiry_dates: IsoDate[]
  resolutions: string[]
  instrument_class: 'FUT' | 'OPT' | 'BOTH'
  option_types: OptionType[]
  strike_scope: StrikeScope
  include_oi: boolean
  force_refresh: boolean
  range_from: IsoDate | null
  range_to: IsoDate | null
}

/** The estimate card that gates Start. Shape from PIPELINE.md section 1.1. */
export interface PlanPreview {
  contracts_matched: number
  contracts_skipped_sealed: number
  chunks_total: number
  requests_estimated: number
  discovery_tasks: number
  rows_estimated: number
  bytes_estimated: number
  budget_remaining: number
  exceeds_budget: boolean
  minutes_estimated: number
  warnings: string[]
}

export interface DownloadStartRequest extends DownloadPlanRequest {
  confirm_requests: number
  defer_to_tomorrow?: boolean
}

export interface DownloadStartResponse {
  job_id: string
  status: JobStatus
  total_tasks: number
  est_requests: number
  deferred: boolean
}

export type JobStatus =
  | 'queued'
  | 'running'
  | 'paused'
  | 'blocked_auth'
  | 'deferred_budget'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type JobKind =
  | 'expiry_discovery'
  | 'contract_discovery'
  | 'candle_download'
  | 'export'
  | 'maintenance'

export interface Job {
  job_id: string
  kind: JobKind
  status: JobStatus
  created_at: IsoTimestamp
  started_at: IsoTimestamp | null
  finished_at: IsoTimestamp | null
  total_tasks: number
  done_tasks: number
  empty_tasks: number
  failed_tasks: number
  skipped_tasks: number
  requests_used: number
  rows_written: number
  throughput_per_minute: number | null
  eta_seconds: number | null
  reason: string | null
  parent_job_id: string | null
  schedule_id: string | null
  params: Record<string, unknown> | null
}

export interface JobDetail extends Job {
  task_states: Record<string, number>
  child_job_ids: string[]
}

export type TaskState =
  | 'pending'
  | 'running'
  | 'ok'
  | 'empty'
  | 'failed'
  | 'skipped'
  | 'blocked_auth'

export interface JobTask {
  task_id: string
  job_id: string
  kind: string
  state: TaskState
  contract_id: number | null
  fyers_symbol: string | null
  res_id: number | null
  range_from: IsoTimestamp | null
  range_to: IsoTimestamp | null
  attempt: number
  row_count: number | null
  latency_ms: number | null
  http_status: number | null
  error_code: string | null
  error_message: string | null
  /** The path itself never crosses. A boolean is all the browser is allowed to know. */
  has_raw_body: boolean
  request_params_json: Record<string, unknown> | null
  updated_at: IsoTimestamp
}

export interface RetryFailedResponse {
  job_id: string
  parent_job_id: string
  total_tasks: number
}

export interface CoverageGridCell {
  expiry_date: IsoDate
  res_id: number
  contracts_total: number
  contracts_with_data: number
  chunks_ok: number
  chunks_empty: number
  chunks_missing: number
  rows: number
}

export interface CoverageGrid {
  underlying_id: number
  resolutions: Array<{ res_id: number; fyers_code: string }>
  cells: CoverageGridCell[]
}

export interface CoverageGap {
  contract_id: number
  fyers_symbol: string
  expiry_date: IsoDate | null
  res_id: number
  gap_from: IsoTimestamp
  gap_to: IsoTimestamp
  missing_chunks: number
}

// ---------------------------------------------------------------------------
// 7. Data and charts
// ---------------------------------------------------------------------------

/** The columnar bar payload. Rows are positional and must be read through `columns`, exactly as
 *  the ingest path does, so adding a column later is a non-event on both sides. */
export interface BarsResponse {
  contract_id: number | null
  symbol: string
  resolution: string
  columns: string[]
  candles: number[][]
}

export interface OpenInterestResponse {
  points: Array<[EpochSeconds, number]>
}

export interface ChainLeg {
  contract_id: number
  close: number | null
  volume: number | null
  oi: number | null
}

export interface ChainRow {
  strike: number
  lot_size: number | null
  ce: ChainLeg | null
  pe: ChainLeg | null
}

export interface ChainResponse {
  underlying_id: number
  expiry_date: IsoDate
  ts: EpochSeconds
  spot: number | null
  atm_strike: number | null
  rows: ChainRow[]
}

export interface ChainAtmResponse {
  spot: number | null
  atm_strike: number | null
  ce_contract_id: number | null
  pe_contract_id: number | null
}

// ---------------------------------------------------------------------------
// 8. Exports
// ---------------------------------------------------------------------------

export type ExportFormat = 'parquet' | 'csv'
export type ExportLayout = 'single' | 'hive'
export type ExportStatus = 'queued' | 'running' | 'ready' | 'failed' | 'deleted'

export interface ExportScope {
  underlying_id: number
  expiry_from: IsoDate | null
  expiry_to: IsoDate | null
  resolutions: string[]
  kind: 'FUT' | 'OPT' | 'BOTH'
  include_catalog: boolean
}

export interface ExportCreateRequest {
  format: ExportFormat
  layout: ExportLayout
  compression: string
  scope: ExportScope
  denormalise: boolean
}

export interface ExportCreateResponse {
  export_id: string
  job_id: string
  status: ExportStatus
}

export interface ExportJob {
  export_id: string
  job_id: string | null
  status: ExportStatus
  format: ExportFormat
  layout: ExportLayout
  compression: string
  row_count: number | null
  byte_size: number | null
  sha256: string | null
  created_at: IsoTimestamp
  finished_at: IsoTimestamp | null
  error_message: string | null
}

// ---------------------------------------------------------------------------
// 9. Schedules
// ---------------------------------------------------------------------------

export type ScheduleKind =
  | 'rolling_backfill'
  | 'expiry_discovery'
  | 'contract_discovery'
  | 'maintenance'

export interface Schedule {
  schedule_id: string
  name: string
  kind: ScheduleKind
  cron: string
  timezone: string
  enabled: boolean
  trading_days_only: boolean
  misfire_grace_seconds: number
  max_requests_per_run: number
  is_builtin: boolean
  params: Record<string, unknown>
  next_fire_at: IsoTimestamp | null
  last_fired_at: IsoTimestamp | null
  last_outcome: string | null
  last_job_id: string | null
}

export interface ScheduleWriteRequest {
  name: string
  kind: ScheduleKind
  cron: string
  timezone: string
  params: Record<string, unknown>
  enabled: boolean
  trading_days_only: boolean
  misfire_grace_seconds: number
  max_requests_per_run: number
}

export interface ScheduleRunNowResponse {
  job_id: string | null
  outcome: string
}

export interface ScheduleRun {
  run_id: string
  schedule_id: string
  fired_at: IsoTimestamp
  outcome: string
  job_id: string | null
  detail: string | null
}

// ---------------------------------------------------------------------------
// 10. System
// ---------------------------------------------------------------------------

/** GET /api/v1/system/budget and the `budget` SSE frame carry this identical body, which is why
 *  the stream can write straight into the cache slot the REST query owns. */
export interface Budget {
  ist_date: IsoDate
  plan: BrokerPlan
  requests_used: number
  plan_limit_day: number
  remaining: number
  minute_headroom: number
  minute_violations: number
  strikes_remaining: number
  blocked_until: IsoTimestamp | null
  pipeline_mode: PipelineMode
  pipeline_reason: string | null
  sweep_reserve_fraction: number
}

export interface Storage {
  duckdb_bytes: number
  duckdb_wal_bytes: number
  sqlite_bytes: number
  exports_bytes: number
  raw_payload_bytes: number
  candle_rows: number
  bytes_per_row: number
  modelled_bytes: number
  bloat_ratio: number
  compaction_suggested: boolean
  free_disk_bytes: number
}

export interface HealthRow {
  check_name: string
  status: string
  detail: string | null
  observed_at: IsoTimestamp | null
}

export interface Health {
  rows: HealthRow[]
  last_maintenance: {
    ran_at: IsoTimestamp
    outcome: string
    detail: string | null
  } | null
}

export interface CheckpointResponse {
  wal_bytes_before: number
  wal_bytes_after: number
}

export interface Notification {
  notification_id: string
  level: 'info' | 'warning' | 'error'
  title: string
  body: string | null
  created_at: IsoTimestamp
  read_at: IsoTimestamp | null
  dismissed_at: IsoTimestamp | null
  job_id: string | null
}

/** One typed setting from sqlite.settings. This is the replacement for a .env file, so the
 *  spec travels with the value and the Settings screen renders the control from it. */
export interface SettingDescriptor {
  key: string
  value: string | number | boolean | null
  value_type: 'str' | 'int' | 'bool' | 'float'
  default: string | number | boolean | null
  description: string
  minimum: number | null
  maximum: number | null
  choices: string[] | null
  requires_restart: boolean
}

export interface RequestLogRow {
  task_id: string
  job_id: string | null
  endpoint: string
  outcome: string
  http_status: number | null
  latency_ms: number | null
  requested_at: IsoTimestamp
  fyers_symbol: string | null
  error_code: string | null
  params: Record<string, unknown> | null
}

// ---------------------------------------------------------------------------
// 11. Events
// ---------------------------------------------------------------------------

export interface JobProgressFrame {
  job_id: string
  status: JobStatus
  total: number
  done: number
  empty: number
  failed: number
  skipped: number
  requests_used: number
  rows_written: number
  eta_seconds: number | null
}

export interface JobLifecycleFrame {
  job_id: string
  status: JobStatus
  reason: string | null
}

export interface TaskCompletedFrame {
  job_id: string
  task_id: string
  kind: string
  state: TaskState
  fyers_symbol: string | null
  row_count: number | null
  latency_ms: number | null
}

export interface AuthRequiredFrame {
  token_state: TokenState
  reason: string
  parked_jobs: number
  parked_tasks: number
}

export interface RateLimitedFrame {
  strikes_used: number
  strikes_remaining: number
  blocked_until: IsoTimestamp | null
}

export interface PipelineModeFrame {
  mode: PipelineMode
  reason: string | null
}

export interface ScheduleFiredFrame {
  schedule_id: string
  job_id: string | null
  outcome: string
}

export interface ExportReadyFrame {
  export_id: string
  byte_size: number
  row_count: number
}

/** The discriminated union of everything /events/stream can deliver. The stream is a refresh
 *  accelerator and never the source of truth: every frame has a REST equivalent. */
export type EventFrame =
  | { event: 'job_progress'; data: JobProgressFrame }
  | { event: 'job_started'; data: JobLifecycleFrame }
  | { event: 'job_finished'; data: JobLifecycleFrame }
  | { event: 'job_blocked'; data: JobLifecycleFrame }
  | { event: 'task_completed'; data: TaskCompletedFrame }
  | { event: 'budget'; data: Budget }
  | { event: 'auth_required'; data: AuthRequiredFrame }
  | { event: 'rate_limited'; data: RateLimitedFrame }
  | { event: 'pipeline_mode'; data: PipelineModeFrame }
  | { event: 'schedule_fired'; data: ScheduleFiredFrame }
  | { event: 'export_ready'; data: ExportReadyFrame }
  | { event: 'notification'; data: Notification }

export type EventName = EventFrame['event']

/** Listed once so the hook subscribes to exactly the frames the cache knows how to patch, and
 *  so an unknown frame name is a compile error here rather than a silent no-op at runtime. */
export const EVENT_NAMES: readonly EventName[] = [
  'job_progress',
  'job_started',
  'job_finished',
  'job_blocked',
  'task_completed',
  'budget',
  'auth_required',
  'rate_limited',
  'pipeline_mode',
  'schedule_fired',
  'export_ready',
  'notification',
]
