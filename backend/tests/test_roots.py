"""Tests for the underlying root registry."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from expirymanager.brokers.fyers.roots import (  # noqa: E402
    SEED_ROOTS,
    RootRegistry,
    UnderlyingRoot,
    builtin_registry,
    default_registry,
)
from expirymanager.brokers.fyers.symbology import parse_symbol  # noqa: E402

AS_OF = date(2026, 9, 9)


def _root(name, symbol="NSE:X-EQ", exchange="NSE", kind="EQUITY", segment="FO"):
    return UnderlyingRoot(
        root=name,
        fyers_symbol=symbol,
        display_name=name,
        exchange=exchange,
        instrument_kind=kind,
        derivative_segment=segment,
        data_from=date(2022, 1, 3),
    )


# ---------------------------------------------------------------- the seeds


def test_seeds_are_the_four_builtin_underlyings():
    assert [r.root for r in SEED_ROOTS] == ["NIFTY", "BANKNIFTY", "SENSEX", "RELIANCE"]
    assert all(r.is_builtin for r in SEED_ROOTS)


@pytest.mark.parametrize(
    "root,fyers_symbol,exchange,kind,data_from",
    [
        ("NIFTY", "NSE:NIFTY50-INDEX", "NSE", "INDEX", date(2022, 1, 3)),
        ("BANKNIFTY", "NSE:NIFTYBANK-INDEX", "NSE", "INDEX", date(2022, 1, 3)),
        ("SENSEX", "BSE:SENSEX-INDEX", "BSE", "INDEX", date(2023, 8, 7)),
        ("RELIANCE", "NSE:RELIANCE-EQ", "NSE", "EQUITY", date(2022, 1, 3)),
    ],
)
def test_seed_maps_to_the_exact_fyers_underlying_string(
    root, fyers_symbol, exchange, kind, data_from
):
    """The cash ticker is what goes on the wire. The root is what appears in contracts."""
    entry = builtin_registry().get(root)
    assert entry is not None
    assert entry.fyers_symbol == fyers_symbol
    assert entry.exchange == exchange
    assert entry.instrument_kind == kind
    assert entry.data_from == data_from
    assert entry.derivative_segment == "FO"


def test_seed_ids_match_the_underlying_registry_migration():
    ids = {r.root: (r.underlying_id, r.spot_contract_id) for r in SEED_ROOTS}
    assert ids == {
        "NIFTY": (1, 1),
        "BANKNIFTY": (2, 2),
        "SENSEX": (3, 3),
        "RELIANCE": (4, 4),
    }


def test_lookup_by_cash_ticker():
    assert builtin_registry().by_fyers_symbol("NSE:NIFTYBANK-INDEX").root == "BANKNIFTY"
    assert builtin_registry().by_fyers_symbol("NSE:NOSUCH-EQ") is None


# ---------------------------------------------------------------- longest first matching


def test_match_prefix_is_longest_first():
    """BANKNIFTY, FINNIFTY and MIDCPNIFTY all end in NIFTY."""
    registry = builtin_registry()
    registry.register(_root("FINNIFTY", "NSE:FINNIFTY-INDEX", kind="INDEX"))
    registry.register(_root("MIDCPNIFTY", "NSE:NIFTYMIDSELECT-INDEX", kind="INDEX"))
    assert registry.match_prefix("BANKNIFTY2510952000CE").root == "BANKNIFTY"
    assert registry.match_prefix("FINNIFTY2510923000CE").root == "FINNIFTY"
    assert registry.match_prefix("MIDCPNIFTY2510923000CE").root == "MIDCPNIFTY"
    assert registry.match_prefix("NIFTY2510923000CE").root == "NIFTY"


def test_match_prefix_is_anchored_at_the_start():
    """A substring search would find NIFTY inside XBANKNIFTY and answer confidently."""
    assert builtin_registry().match_prefix("XBANKNIFTY2510952000CE") is None


def test_roots_longest_first_ordering_is_stable():
    registry = RootRegistry([_root("AB"), _root("ABC"), _root("AD")])
    assert registry.roots_longest_first() == ("ABC", "AB", "AD")


# ---------------------------------------------------------------- extension


def test_user_added_root_is_visible_to_the_parser():
    registry = builtin_registry()
    assert parse_symbol("NSE:MARICO2510923000CE", registry=registry, today=AS_OF).root == "MARICO"
    registry.register(
        UnderlyingRoot(
            root="MARICO",
            fyers_symbol="NSE:MARICO-EQ",
            display_name="Marico",
            exchange="NSE",
            instrument_kind="EQUITY",
            derivative_segment="FO",
            data_from=date(2022, 1, 3),
        )
    )
    parsed = parse_symbol("NSE:MARICO2510923000CE", registry=registry, today=AS_OF)
    assert parsed.instrument_class == "OPTSTK"
    assert "root_unknown" not in parsed.parse_warnings


def test_duplicate_registration_is_refused_unless_replacing():
    registry = builtin_registry()
    with pytest.raises(ValueError):
        registry.register(_root("NIFTY", "NSE:NIFTY50-INDEX", kind="INDEX"))
    registry.register(
        _root("NIFTY", "NSE:NIFTY50-INDEX", kind="INDEX"), replace_existing=True
    )
    assert registry.get("NIFTY").instrument_kind == "INDEX"


def test_unregister_and_membership():
    registry = builtin_registry()
    assert "NIFTY" in registry and len(registry) == 4
    registry.unregister("nifty")
    assert "NIFTY" not in registry and len(registry) == 3


def test_lookup_is_case_and_whitespace_insensitive():
    registry = builtin_registry()
    assert registry.get("  banknifty  ").root == "BANKNIFTY"
    assert registry.get(None) is None
    assert registry.get("") is None


def test_currency_root_can_declare_the_currency_segment():
    registry = builtin_registry()
    registry.register(
        UnderlyingRoot(
            root="GBPINR",
            fyers_symbol="NSE:GBPINR-INDEX",
            display_name="GBP INR",
            exchange="NSE",
            instrument_kind="CURRENCY",
            derivative_segment="CD",
            data_from=date(2022, 1, 3),
        )
    )
    parsed = parse_symbol("NSE:GBPINR20N0580.5PE", registry=registry, today=AS_OF)
    assert parsed.segment == "CD"
    assert "segment_inferred" not in parsed.parse_warnings


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "kwargs",
    [
        {"root": "9NIFTY"},
        {"root": "NIF TY"},
        {"exchange": "NYSE"},
        {"instrument_kind": "CRYPTO"},
        {"derivative_segment": "CM"},
        {"fyers_symbol": "NIFTY50-INDEX"},
    ],
)
def test_invalid_entries_are_refused(kwargs):
    base = {
        "root": "NIFTY",
        "fyers_symbol": "NSE:NIFTY50-INDEX",
        "display_name": "NIFTY 50",
        "exchange": "NSE",
        "instrument_kind": "INDEX",
        "derivative_segment": "FO",
        "data_from": date(2022, 1, 3),
    }
    with pytest.raises(ValueError):
        UnderlyingRoot(**(base | kwargs))


def test_exchange_must_match_the_cash_ticker_prefix():
    with pytest.raises(ValueError, match="does not match"):
        UnderlyingRoot(
            root="SENSEX",
            fyers_symbol="BSE:SENSEX-INDEX",
            display_name="SENSEX",
            exchange="NSE",
            instrument_kind="INDEX",
            derivative_segment="FO",
            data_from=date(2023, 8, 7),
        )


@pytest.mark.parametrize("root", ["M&M", "BAJAJ-AUTO", "NIFTYNXT50", "J&KBANK"])
def test_real_root_shapes_are_accepted(root):
    assert UnderlyingRoot(
        root=root,
        fyers_symbol=f"NSE:{root}-EQ",
        display_name=root,
        exchange="NSE",
        instrument_kind="EQUITY",
        derivative_segment="FO",
        data_from=date(2022, 1, 3),
    ).root == root


# ---------------------------------------------------------------- loading from sqlite rows


def test_from_rows_matches_the_underlying_registry_shape():
    rows = [
        {
            "underlying_id": 1,
            "fyers_symbol": "NSE:NIFTY50-INDEX",
            "root": "nifty",
            "exchange": "nse",
            "instrument_kind": "index",
            "display_name": "NIFTY 50",
            "data_from": "2022-01-03",
            "spot_contract_id": 1,
            "is_builtin": 1,
            "resolved_root_echo": "NIFTY",
        },
        {
            "underlying_id": 3,
            "fyers_symbol": "BSE:SENSEX-INDEX",
            "root": "SENSEX",
            "exchange": "BSE",
            "instrument_kind": "INDEX",
            "display_name": "SENSEX",
            "data_from": date(2023, 8, 7),
            "spot_contract_id": 3,
            "is_builtin": 1,
            "resolved_root_echo": None,
        },
    ]
    registry = RootRegistry.from_rows(rows)
    assert len(registry) == 2
    nifty = registry.get("NIFTY")
    assert nifty.data_from == date(2022, 1, 3)
    assert nifty.resolved_root_echo == "NIFTY"
    assert nifty.is_builtin
    assert registry.get("SENSEX").resolved_root_echo is None


def test_from_rows_defaults_the_derivative_segment_by_exchange():
    registry = RootRegistry.from_rows(
        [
            {
                "fyers_symbol": "MCX:GOLD-COM",
                "root": "GOLD",
                "exchange": "MCX",
                "instrument_kind": "COMMODITY",
                "display_name": "Gold",
                "data_from": None,
            }
        ]
    )
    entry = registry.get("GOLD")
    assert entry.derivative_segment == "COM"
    assert entry.segment_code == 20
    assert entry.data_from == date(2022, 1, 3)


# ---------------------------------------------------------------- the process default


def test_default_registry_is_shared_and_seeded():
    assert default_registry() is default_registry()
    assert default_registry().known_roots() >= {"NIFTY", "BANKNIFTY", "SENSEX", "RELIANCE"}


def test_builtin_registry_is_a_fresh_copy():
    first = builtin_registry()
    first.unregister("NIFTY")
    assert "NIFTY" in builtin_registry()
    assert "NIFTY" in default_registry()
