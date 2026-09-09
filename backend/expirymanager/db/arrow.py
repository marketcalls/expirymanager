"""Arrow record batch construction for the candle ingest path.

Every candle row that reaches DuckDB goes through this module. The Arrow path is not a style
choice: measured throughput is 3.6 to 11.2 million rows per second against 8,350 for
``executemany``, a factor of roughly 1200 on the single hottest loop in the system.

Two invariants live here and nowhere else.

Column mapping is by the ``columns`` array Fyers returns, never by fixed position. The seventh
element (``open_interest``) is present only when ``include_oi=1``, and ``include_greeks`` will
append further elements when it ships for expired contracts, so any code that indexes ``row[6]``
is a future silent corruption.

Timestamps are converted to naive IST by adding a fixed 19800 seconds. India has had a fixed
UTC+05:30 offset since 1945 and no daylight saving, so the conversion is lossless and needs no
timezone database. Storing naive IST rather than TIMESTAMPTZ means no query result depends on a
session ``TimeZone`` setting that a caller might forget to apply.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

import pyarrow as pa

# UTC+05:30, fixed. Recorded in the meta table as ist_offset_seconds so the convention cannot be
# lost, and reversed on the way out with epoch(ts) - 19800.
IST_OFFSET_SECONDS = 19800

PRICE_PRECISION = 11
PRICE_SCALE = 4

# Prices are DECIMAL(11,4) rather than DECIMAL(9,2). The product lets a user register their own
# underlying, and a currency derivative ticks at 0.0025, which two decimal places truncate
# silently. Widening after the first backfill would mean rewriting every row of candles, so the
# extra bytes per row are paid up front and deliberately.
PRICE_TYPE = pa.decimal128(PRICE_PRECISION, PRICE_SCALE)

_PRICE_QUANTUM = Decimal(1).scaleb(-PRICE_SCALE)
_PRICE_LIMIT = Decimal(10) ** (PRICE_PRECISION - PRICE_SCALE)

CANDLE_SCHEMA = pa.schema(
    [
        ("contract_id", pa.int32()),
        ("res_id", pa.uint8()),
        ("ts", pa.timestamp("us")),
        ("open", PRICE_TYPE),
        ("high", PRICE_TYPE),
        ("low", PRICE_TYPE),
        ("close", PRICE_TYPE),
        ("volume", pa.int64()),
        ("oi", pa.int64()),
    ]
)

# Fyers names on the left, our column names on the right.
COLUMN_ALIASES: Mapping[str, str] = {
    "timestamp": "ts",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "open_interest": "oi",
}

PRICE_FIELDS = ("open", "high", "low", "close")
REQUIRED_FIELDS = ("ts", "open", "high", "low", "close", "volume")


class CandleSchemaError(ValueError):
    """The response columns array cannot be mapped onto the pinned candle schema."""


class PrecisionError(ValueError):
    """A price cannot be stored at DECIMAL(11,4) without losing information."""


def map_columns(columns: Sequence[str]) -> dict[str, int]:
    """Return a field name to row position map built from the response columns array.

    Unknown column names are ignored rather than rejected, so a field Fyers adds later does not
    break ingest. Missing required fields are an error, because a candle without a close is not
    a candle.
    """
    positions: dict[str, int] = {}
    for index, raw in enumerate(columns):
        field = COLUMN_ALIASES.get(str(raw).strip().lower())
        if field is None:
            continue
        if field in positions:
            raise CandleSchemaError(
                f"column {field} appears more than once in the response columns array"
            )
        positions[field] = index
    missing = [field for field in REQUIRED_FIELDS if field not in positions]
    if missing:
        raise CandleSchemaError(
            "response columns array is missing required fields: " + ", ".join(missing)
        )
    return positions


def to_price(value: Any, field: str, row_index: int) -> Decimal:
    """Convert one Fyers price to an exact DECIMAL(11,4) value.

    A value that would lose precision raises rather than rounds. Rounding a price is a silent
    data corruption that nobody would ever notice, and the whole point of choosing DECIMAL over
    DOUBLE was to keep prices exact.
    """
    try:
        # str() first: Decimal(float) would carry the binary representation error into the
        # quantize comparison and reject values that are actually fine.
        candidate = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PrecisionError(
            f"row {row_index} field {field} is not a number: {value!r}"
        ) from exc
    if not candidate.is_finite():
        raise PrecisionError(f"row {row_index} field {field} is not finite: {value!r}")
    quantized = candidate.quantize(_PRICE_QUANTUM)
    if quantized != candidate:
        raise PrecisionError(
            f"row {row_index} field {field} value {value!r} needs more than "
            f"{PRICE_SCALE} decimal places and would be truncated"
        )
    if abs(quantized) >= _PRICE_LIMIT:
        raise PrecisionError(
            f"row {row_index} field {field} value {value!r} exceeds "
            f"DECIMAL({PRICE_PRECISION},{PRICE_SCALE})"
        )
    return quantized


def candles_to_arrow(
    candles: Sequence[Sequence[Any]],
    columns: Sequence[str],
    contract_id: int,
    res_id: int,
) -> pa.RecordBatch:
    """Build one record batch from a Fyers historical-data payload.

    ``candles`` is the list of rows exactly as returned, ``columns`` the array that came with
    them. The batch always carries all nine schema columns; ``oi`` is a null column when the
    response had no open_interest field.
    """
    positions = map_columns(columns)
    row_count = len(candles)
    width = len(columns)

    ts_field = positions["ts"]
    volume_field = positions["volume"]
    oi_field = positions.get("oi")

    ts_micros: list[int] = []
    prices: dict[str, list[Decimal]] = {field: [] for field in PRICE_FIELDS}
    volumes: list[int] = []
    ois: list[int | None] = []

    for row_index, row in enumerate(candles):
        if len(row) < width:
            raise CandleSchemaError(
                f"row {row_index} has {len(row)} values but the columns array declares {width}"
            )
        ts_micros.append((int(row[ts_field]) + IST_OFFSET_SECONDS) * 1_000_000)
        for field in PRICE_FIELDS:
            prices[field].append(to_price(row[positions[field]], field, row_index))
        volumes.append(int(row[volume_field]))
        if oi_field is not None:
            raw_oi = row[oi_field]
            ois.append(None if raw_oi is None else int(raw_oi))

    arrays = [
        pa.array([contract_id] * row_count, pa.int32()),
        pa.array([res_id] * row_count, pa.uint8()),
        pa.array(ts_micros, pa.int64()).cast(pa.timestamp("us")),
        *[pa.array(prices[field], PRICE_TYPE) for field in PRICE_FIELDS],
        pa.array(volumes, pa.int64()),
        pa.array(ois, pa.int64()) if oi_field is not None else pa.nulls(row_count, pa.int64()),
    ]
    return pa.RecordBatch.from_arrays(arrays, schema=CANDLE_SCHEMA)


def empty_batch() -> pa.RecordBatch:
    """A zero row batch on the pinned schema, for a no_data response."""
    return pa.RecordBatch.from_arrays(
        [pa.array([], field.type) for field in CANDLE_SCHEMA],
        schema=CANDLE_SCHEMA,
    )


def batch_bounds(batch: pa.RecordBatch | None) -> tuple[Any, Any]:
    """First and last ts in a batch, or (None, None) when it is empty.

    Rows arrive in ascending ts, so this is a read of the two end values rather than a scan.
    """
    if batch is None or batch.num_rows == 0:
        return (None, None)
    ts = batch.column(CANDLE_SCHEMA.get_field_index("ts"))
    return (ts[0].as_py(), ts[batch.num_rows - 1].as_py())


def iter_prices(batch: pa.RecordBatch) -> Iterable[Decimal]:
    """Every price value in a batch, for assertions and diagnostics."""
    for field in PRICE_FIELDS:
        column = batch.column(CANDLE_SCHEMA.get_field_index(field))
        for value in column:
            yield value.as_py()
