"""Arrow builder tests: column mapping, the IST offset and price precision."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pyarrow as pa
import pytest

from expirymanager.db.arrow import (
    CANDLE_SCHEMA,
    IST_OFFSET_SECONDS,
    CandleSchemaError,
    PrecisionError,
    candles_to_arrow,
    empty_batch,
    map_columns,
)

FYERS_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
NO_OI_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def utc_epoch_for_ist(year, month, day, hour, minute):
    """The UTC epoch second whose IST wall clock is the given naive time."""
    naive = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    return int(naive.timestamp()) - IST_OFFSET_SECONDS


def test_schema_is_pinned_at_eleven_four():
    assert CANDLE_SCHEMA.field("open").type == pa.decimal128(11, 4)
    assert CANDLE_SCHEMA.field("close").type == pa.decimal128(11, 4)
    assert CANDLE_SCHEMA.field("ts").type == pa.timestamp("us")
    assert CANDLE_SCHEMA.field("res_id").type == pa.uint8()
    assert CANDLE_SCHEMA.names == [
        "contract_id",
        "res_id",
        "ts",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "oi",
    ]


def test_epoch_becomes_naive_ist():
    epoch = utc_epoch_for_ist(2025, 3, 26, 9, 15)
    batch = candles_to_arrow([[epoch, 730.0, 750.0, 645.0, 706.0, 359100, 5131225]],
                             FYERS_COLUMNS, contract_id=1000, res_id=2)
    assert batch.column("ts")[0].as_py() == datetime(2025, 3, 26, 9, 15)


def test_documented_payload_row_maps_correctly():
    # The verbatim example from the Fyers expired contracts documentation.
    batch = candles_to_arrow([[1742960700, 730.00, 750.00, 645.00, 706.00, 359100, 5131225]],
                             FYERS_COLUMNS, contract_id=1000, res_id=2)
    assert batch.column("ts")[0].as_py() == datetime(2025, 3, 26, 9, 15)
    assert batch.column("open")[0].as_py() == Decimal("730.0000")
    assert batch.column("volume")[0].as_py() == 359100
    assert batch.column("oi")[0].as_py() == 5131225


def test_mapping_follows_the_columns_array_not_the_position():
    reordered = ["timestamp", "close", "open", "high", "low", "open_interest", "volume"]
    epoch = utc_epoch_for_ist(2025, 3, 26, 9, 15)
    batch = candles_to_arrow([[epoch, 4.0, 1.0, 2.0, 3.0, 99, 7]],
                             reordered, contract_id=1000, res_id=2)
    assert batch.column("open")[0].as_py() == Decimal("1.0000")
    assert batch.column("high")[0].as_py() == Decimal("2.0000")
    assert batch.column("low")[0].as_py() == Decimal("3.0000")
    assert batch.column("close")[0].as_py() == Decimal("4.0000")
    assert batch.column("volume")[0].as_py() == 7
    assert batch.column("oi")[0].as_py() == 99


def test_unknown_column_is_ignored():
    columns = [*FYERS_COLUMNS, "something_fyers_added_later"]
    epoch = utc_epoch_for_ist(2025, 3, 26, 9, 15)
    batch = candles_to_arrow([[epoch, 1.0, 1.0, 1.0, 1.0, 1, 1, "x"]],
                             columns, contract_id=1000, res_id=2)
    assert batch.num_rows == 1


def test_missing_required_column_is_rejected():
    with pytest.raises(CandleSchemaError):
        map_columns(["timestamp", "open", "high", "low", "volume"])


def test_oi_is_null_when_not_requested():
    epoch = utc_epoch_for_ist(2025, 3, 26, 9, 15)
    batch = candles_to_arrow([[epoch, 1.0, 1.0, 1.0, 1.0, 5]],
                             NO_OI_COLUMNS, contract_id=1000, res_id=2)
    assert batch.column("oi")[0].as_py() is None


def test_currency_tick_survives():
    # The reason prices are DECIMAL(11,4): a currency derivative ticks at 0.0025 and two
    # decimal places would truncate it to zero without saying so.
    epoch = utc_epoch_for_ist(2025, 3, 26, 9, 15)
    batch = candles_to_arrow([[epoch, 0.0025, 87.1275, 0.0025, 87.1250, 10, 20]],
                             FYERS_COLUMNS, contract_id=1000, res_id=2)
    assert batch.column("open")[0].as_py() == Decimal("0.0025")
    assert batch.column("high")[0].as_py() == Decimal("87.1275")


def test_excess_precision_raises_rather_than_rounds():
    epoch = utc_epoch_for_ist(2025, 3, 26, 9, 15)
    with pytest.raises(PrecisionError):
        candles_to_arrow([[epoch, 0.00025, 1.0, 1.0, 1.0, 1, 1]],
                         FYERS_COLUMNS, contract_id=1000, res_id=2)


def test_out_of_range_price_raises():
    epoch = utc_epoch_for_ist(2025, 3, 26, 9, 15)
    with pytest.raises(PrecisionError):
        candles_to_arrow([[epoch, 12345678.0, 1.0, 1.0, 1.0, 1, 1]],
                         FYERS_COLUMNS, contract_id=1000, res_id=2)


def test_short_row_is_rejected():
    with pytest.raises(CandleSchemaError):
        candles_to_arrow([[1742960700, 1.0, 1.0]], FYERS_COLUMNS, contract_id=1, res_id=2)


def test_empty_batch_carries_the_schema():
    batch = empty_batch()
    assert batch.num_rows == 0
    assert batch.schema == CANDLE_SCHEMA
