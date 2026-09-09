"""Typed accessors over the `settings` table. The replacement for a .env file.

This project deliberately has no environment configuration and no .env. Everything a user can
tune is a row in `sqlite.settings`, editable through `PATCH /api/v1/system/settings`, which means
one place to look, one place to validate, and a change that survives a restart without anyone
editing a file.

Every key carries a code default, so the store answers correctly before the first migration has
run and on a database whose `settings` table is empty. A missing row is never an error.

The store speaks plain SQL through a SQLAlchemy connectable rather than the ORM models, so it
does not depend on the declarative layer and can be used during bootstrap, before models are
importable.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "SettingSpec",
    "SETTINGS",
    "SettingsError",
    "UnknownSettingError",
    "SettingOutOfRangeError",
    "SettingsStore",
    "defaults",
]

# Fyers resolution codes from DATA-MODEL.md section 1.9. `100` is the derived daily series and is
# never requested from the broker, so it is not selectable here.
VALID_RESOLUTIONS: frozenset[str] = frozenset(
    {"5S", "1", "2", "3", "5", "10", "15", "20", "30", "45", "60", "120", "180", "240"}
)


class SettingsError(Exception):
    """Base class for settings failures. `code` is the API error code the handler returns."""

    code = "setting_invalid"


class UnknownSettingError(SettingsError):
    code = "unknown_setting"


class SettingOutOfRangeError(SettingsError):
    code = "setting_out_of_range"


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """One tunable: its Python type, its code default and its validator."""

    key: str
    type: type
    default: Any
    description: str
    validator: Callable[[Any], None] | None = None

    def coerce(self, value: Any) -> Any:
        """Coerce a JSON-decoded or user-supplied value into the declared type."""
        if self.type is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            if isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower() == "true"
            raise SettingOutOfRangeError(f"{self.key} must be a boolean")
        if self.type is int:
            # bool is a subclass of int, and accepting True for worker_count is nonsense.
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise SettingOutOfRangeError(f"{self.key} must be an integer")
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise SettingOutOfRangeError(f"{self.key} must be an integer") from exc
        if self.type is float:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise SettingOutOfRangeError(f"{self.key} must be a number")
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise SettingOutOfRangeError(f"{self.key} must be a number") from exc
        if self.type is str:
            if not isinstance(value, str):
                raise SettingOutOfRangeError(f"{self.key} must be a string")
            return value
        if self.type is list:
            if not isinstance(value, list):
                raise SettingOutOfRangeError(f"{self.key} must be a list")
            return list(value)
        return value

    def validate(self, value: Any) -> Any:
        """Coerce then validate. Returns the value that should be stored."""
        coerced = self.coerce(value)
        if self.validator is not None:
            self.validator(coerced)
        return coerced


def _in_range(key: str, low: float, high: float) -> Callable[[Any], None]:
    def check(value: Any) -> None:
        if not low <= value <= high:
            raise SettingOutOfRangeError(f"{key} must be between {low} and {high}")

    return check


def _one_of(key: str, allowed: Iterable[str]) -> Callable[[Any], None]:
    permitted = tuple(allowed)

    def check(value: Any) -> None:
        if value not in permitted:
            raise SettingOutOfRangeError(f"{key} must be one of {', '.join(permitted)}")

    return check


def _resolution_list(value: Any) -> None:
    if not value:
        raise SettingOutOfRangeError("default_resolutions must not be empty")
    unknown = [str(item) for item in value if item not in VALID_RESOLUTIONS]
    if unknown:
        raise SettingOutOfRangeError(
            f"default_resolutions contains unknown resolution codes: {', '.join(unknown)}"
        )


def _spec(
    key: str,
    type_: type,
    default: Any,
    description: str,
    validator: Callable[[Any], None] | None = None,
) -> SettingSpec:
    return SettingSpec(
        key=key, type=type_, default=default, description=description, validator=validator
    )


# The keys seeded by migration 0001, in the order DATA-MODEL.md section 1.2 lists them. Adding a
# key here without adding it to that seed is fine: the default below answers until a user changes
# it, and the row appears on first write.
SETTINGS: Mapping[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        _spec(
            "plan_tier",
            str,
            "standard",
            "Fyers subscription tier, which sets the per-minute and per-day ceilings.",
            _one_of("plan_tier", ("standard", "prime")),
        ),
        _spec(
            "throttle_per_second",
            int,
            8,
            "Outbound requests per second. Held under the published 10 on purpose.",
            _in_range("throttle_per_second", 1, 10),
        ),
        _spec(
            "throttle_per_minute",
            int,
            170,
            "Outbound requests per minute. Under the published 200, because exceeding the "
            "per-minute limit three times in a day blocks the account for the rest of it.",
            _in_range("throttle_per_minute", 1, 600),
        ),
        _spec(
            "throttle_in_flight",
            int,
            6,
            "Concurrent in-flight Fyers requests.",
            _in_range("throttle_in_flight", 1, 32),
        ),
        _spec(
            "daily_budget",
            int,
            100_000,
            "Non-transactional market data requests allowed per day on this plan.",
            _in_range("daily_budget", 1, 1_000_000),
        ),
        _spec(
            "budget_reserve_fraction",
            float,
            0.70,
            "Largest fraction of the daily budget the unattended sweep may spend, leaving "
            "headroom for downloads the user starts by hand.",
            _in_range("budget_reserve_fraction", 0.0, 1.0),
        ),
        _spec(
            "worker_count",
            int,
            8,
            "Download worker coroutines.",
            _in_range("worker_count", 1, 32),
        ),
        _spec(
            "chunk_days",
            int,
            95,
            "Calendar days per historical-data request. Under the documented 100 because the "
            "docs do not say whether the limit counts calendar or trading days.",
            _in_range("chunk_days", 1, 95),
        ),
        _spec(
            "default_resolutions",
            list,
            ["1", "5"],
            "Resolutions preselected on the download sheet for a new underlying.",
            _resolution_list,
        ),
        _spec(
            "include_oi_default",
            bool,
            True,
            "Whether open interest is requested by default for derivative contracts.",
        ),
        _spec(
            "option_life_days",
            int,
            200,
            "How far back before its expiry an option contract is assumed to have traded.",
            _in_range("option_life_days", 1, 3650),
        ),
        _spec(
            "future_life_days",
            int,
            400,
            "How far back before its expiry a futures contract is assumed to have traded.",
            _in_range("future_life_days", 1, 3650),
        ),
        _spec(
            "estimate_confirm_threshold",
            int,
            5_000,
            "Estimated request count above which the download sheet requires an explicit "
            "confirmation before it will start the job.",
            _in_range("estimate_confirm_threshold", 0, 1_000_000),
        ),
        _spec(
            "raw_payload_capture",
            str,
            "discovery",
            "Which raw broker payloads are gzipped under the raw directory. `all` adds candle "
            "bodies, which is gigabytes per backfill and is for debugging only.",
            _one_of("raw_payload_capture", ("none", "discovery", "all")),
        ),
        _spec(
            "request_log_retention_days",
            int,
            30,
            "How long finished task rows are kept for the Diagnostics request log.",
            _in_range("request_log_retention_days", 1, 3650),
        ),
        _spec(
            "cookie_secure",
            bool,
            True,
            "Secure attribute on the session and CSRF cookies. True everywhere, because the "
            "server speaks https on 127.0.0.1 in development as well as in production.",
        ),
        _spec(
            "chart_persist",
            bool,
            True,
            "Whether the chart widget persists drawings, indicators and interval per contract.",
        ),
        _spec(
            "symbol_year_window_lo",
            int,
            2015,
            "Earliest year a two-digit symbol year may resolve to.",
            _in_range("symbol_year_window_lo", 1990, 2100),
        ),
        _spec(
            "symbol_year_window_hi_offset",
            int,
            5,
            "Years past the current year a two-digit symbol year may resolve to.",
            _in_range("symbol_year_window_hi_offset", 0, 50),
        ),
    )
}


def defaults() -> dict[str, Any]:
    """Every setting at its code default. Used before the database exists."""
    return {key: spec.default for key, spec in SETTINGS.items()}


def _spec_for(key: str) -> SettingSpec:
    try:
        return SETTINGS[key]
    except KeyError:
        raise UnknownSettingError(f"Unknown setting: {key}") from None


_SELECT_ALL = "SELECT key, value_json FROM settings"
_SELECT_ONE = "SELECT value_json FROM settings WHERE key = :key"
_UPSERT = (
    "INSERT INTO settings (key, value_json, updated_at) VALUES (:key, :value_json, :updated_at) "
    "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
    "updated_at = excluded.updated_at"
)
_DELETE = "DELETE FROM settings WHERE key = :key"


class SettingsStore:
    """Read-through cache over the `settings` table.

    Constructed once in the lifespan and injected. Reads come from memory, because the governor
    and the planner ask for the same handful of values thousands of times per job; writes go
    straight through and invalidate.

    An engine of None makes the store answer entirely from code defaults, which is what bootstrap
    uses before the database file exists.
    """

    def __init__(self, engine: Any = None) -> None:
        self._engine = engine
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None

    # Reads

    def get(self, key: str) -> Any:
        spec = _spec_for(key)
        with self._lock:
            cache = self._ensure_cache()
            return cache.get(key, spec.default)

    def get_int(self, key: str) -> int:
        return int(self.get(key))

    def get_float(self, key: str) -> float:
        return float(self.get(key))

    def get_bool(self, key: str) -> bool:
        return bool(self.get(key))

    def get_str(self, key: str) -> str:
        return str(self.get(key))

    def get_list(self, key: str) -> list[Any]:
        return list(self.get(key))

    def all(self) -> dict[str, Any]:
        """Every setting, defaults filled in for rows that do not exist."""
        with self._lock:
            cache = self._ensure_cache()
            merged = defaults()
            merged.update({k: v for k, v in cache.items() if k in SETTINGS})
            return merged

    def describe(self) -> dict[str, dict[str, Any]]:
        """Key, current value, default and description, for the Settings screen."""
        current = self.all()
        return {
            key: {
                "value": current[key],
                "default": spec.default,
                "type": spec.type.__name__,
                "description": spec.description,
            }
            for key, spec in SETTINGS.items()
        }

    # Writes

    def set(self, key: str, value: Any) -> Any:
        """Validate and persist one setting. Returns the stored value."""
        spec = _spec_for(key)
        stored = spec.validate(value)
        if self._engine is not None:
            self._execute_write(_UPSERT, self._upsert_params(spec.key, stored))
        with self._lock:
            if self._cache is None:
                self._cache = {}
            self._cache[key] = stored
        return stored

    def set_many(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Validate every key before writing any, so a partial PATCH is all or nothing."""
        validated = {key: _spec_for(key).validate(value) for key, value in values.items()}
        if self._engine is not None and validated:
            params = [self._upsert_params(key, value) for key, value in validated.items()]
            self._execute_write(_UPSERT, params)
        with self._lock:
            if self._cache is None:
                self._cache = {}
            self._cache.update(validated)
        return validated

    def reset(self, key: str) -> Any:
        """Delete the row so the code default takes over again."""
        spec = _spec_for(key)
        if self._engine is not None:
            self._execute_write(_DELETE, {"key": key})
        with self._lock:
            if self._cache is not None:
                self._cache.pop(key, None)
        return spec.default

    def invalidate(self) -> None:
        """Drop the cache. Called after a migration or an out-of-band write."""
        with self._lock:
            self._cache = None

    # Mapping conveniences, so a caller can write `settings["worker_count"]`.

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __contains__(self, key: object) -> bool:
        return key in SETTINGS

    def __iter__(self) -> Iterator[str]:
        return iter(SETTINGS)

    # Internals

    @staticmethod
    def _upsert_params(key: str, value: Any) -> dict[str, str]:
        return {
            "key": key,
            "value_json": json.dumps(value, separators=(",", ":")),
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    def _ensure_cache(self) -> dict[str, Any]:
        if self._cache is None:
            self._cache = self._load()
        return self._cache

    def _load(self) -> dict[str, Any]:
        if self._engine is None:
            return {}
        rows = self._execute_read(_SELECT_ALL)
        if rows is None:
            # The table does not exist yet, which is the normal state on a first run before
            # migrations. Defaults answer until it does.
            return {}
        loaded: dict[str, Any] = {}
        for key, value_json in rows:
            spec = SETTINGS.get(key)
            if spec is None:
                # A key written by a newer build. Ignored rather than fatal, so a downgrade
                # starts instead of refusing on a row it does not understand.
                continue
            try:
                decoded = json.loads(value_json)
            except (TypeError, ValueError):
                continue
            try:
                loaded[key] = spec.validate(decoded)
            except SettingsError:
                # A stored value that no longer passes validation, for example after a range was
                # tightened. The default is the safe answer.
                continue
        return loaded

    def _execute_read(self, sql: str) -> list[tuple[str, str]] | None:
        from sqlalchemy import text
        from sqlalchemy.exc import DatabaseError

        try:
            with self._engine.connect() as conn:
                return [(row[0], row[1]) for row in conn.execute(text(sql))]
        except DatabaseError:
            return None

    def _execute_write(self, sql: str, params: Any) -> None:
        from sqlalchemy import text

        with self._engine.begin() as conn:
            conn.execute(text(sql), params)


# `_SELECT_ONE` is retained for callers that need a single uncached read straight from the
# database, for example a diagnostics endpoint proving what is actually stored.
def read_uncached(engine: Any, key: str) -> Any:
    """Read one setting directly, bypassing any cache. Returns the default if absent."""
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError

    spec = _spec_for(key)
    try:
        with engine.connect() as conn:
            row = conn.execute(text(_SELECT_ONE), {"key": key}).first()
    except DatabaseError:
        return spec.default
    if row is None:
        return spec.default
    try:
        return spec.validate(json.loads(row[0]))
    except (TypeError, ValueError, SettingsError):
        return spec.default
