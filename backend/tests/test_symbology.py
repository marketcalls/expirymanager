"""Tests for the Fyers symbol parser.

The clock is pinned everywhere it matters. The expiry year plausibility window runs to the
current year plus five, and that window is what rejects the 2038 reading of
BSE:SENSEX2381161000CE, so a test that used the live clock would start behaving differently in
2033. Anything asserting a specific decomposition passes today=AS_OF.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from itertools import product
from pathlib import Path

import pytest

# W01 owns pyproject and any shared conftest, and the package may not be installed into the
# interpreter running this file yet. Adding the backend root keeps this suite runnable alone.
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from expirymanager.brokers.fyers.roots import (  # noqa: E402
    RootRegistry,
    UnderlyingRoot,
    builtin_registry,
)
from expirymanager.brokers.fyers.symbology import (  # noqa: E402
    AmbiguousSymbolError,
    SymbolParseError,
    build_cash_symbol,
    build_future_symbol,
    build_monthly_option_symbol,
    build_weekly_option_symbol,
    max_year,
    parse_symbol,
    weekly_month_code,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "symbols.json").read_text())
AS_OF = date.fromisoformat(FIXTURE["as_of"])
CASES = FIXTURE["cases"]
REJECTS = FIXTURE["rejects"]


def _ids(entries):
    return [e["symbol"] for e in entries]


# ---------------------------------------------------------------- the golden corpus


def test_fixture_holds_the_documented_corpus():
    assert FIXTURE["counts"]["doc"] >= 38
    assert len({c["symbol"] for c in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_corpus_decomposition(case):
    parsed = parse_symbol(case["symbol"], today=AS_OF)
    actual = parsed.as_dict()
    for key, expected in case["expect"].items():
        assert actual[key] == expected, f"{case['symbol']} field {key}"


@pytest.mark.parametrize("case", REJECTS, ids=_ids(REJECTS))
def test_corpus_rejects(case):
    expected = AmbiguousSymbolError if case["error"] == "ambiguous" else SymbolParseError
    with pytest.raises(expected):
        parse_symbol(case["symbol"], today=AS_OF)


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_corpus_still_parses_against_the_live_clock(case):
    """The corpus must survive the window moving with real time, not only at as_of."""
    parse_symbol(case["symbol"])


# ---------------------------------------------------------------- the weekly month alphabet


@pytest.mark.parametrize(
    "symbol,expiry",
    [
        ("NSE:NIFTY2010811000CE", date(2020, 1, 8)),
        ("NSE:NIFTY2290811000CE", date(2022, 9, 8)),
        ("NSE:NIFTY20O0811000CE", date(2020, 10, 8)),
        ("NSE:NIFTY20N0811000CE", date(2020, 11, 8)),
        ("NSE:NIFTY20D1025000CE", date(2020, 12, 10)),
    ],
)
def test_weekly_month_alphabet_digits_and_letters(symbol, expiry):
    """January to September are digits 1 to 9, October to December are the letters O, N, D."""
    assert parse_symbol(symbol, today=AS_OF).expiry == expiry


def test_letter_o_is_october_not_a_zero():
    """The pair the vendor prints side by side: 2010 8 is 8 Jan, 20 O 08 is 8 Oct."""
    january = parse_symbol("NSE:NIFTY2010811000CE", today=AS_OF)
    october = parse_symbol("NSE:NIFTY20O0811000CE", today=AS_OF)
    assert january.expiry == date(2020, 1, 8)
    assert october.expiry == date(2020, 10, 8)
    assert january.strike == october.strike == Decimal("11000")


def test_zero_is_never_a_weekly_month():
    with pytest.raises(SymbolParseError):
        parse_symbol("NSE:NIFTY2500923000CE", today=AS_OF)


@pytest.mark.parametrize("month,code", [(1, "1"), (9, "9"), (10, "O"), (11, "N"), (12, "D")])
def test_weekly_month_code(month, code):
    assert weekly_month_code(month) == code


@pytest.mark.parametrize("month", [0, 13, -1])
def test_weekly_month_code_rejects_out_of_range(month):
    with pytest.raises(ValueError):
        weekly_month_code(month)


# ---------------------------------------------------------------- the ambiguous splits


def test_sensex_weekly_beats_the_longer_wrong_root():
    """BSE:SENSEX2381161000CE is the symbol that proves enumeration is necessary.

    SENSEX2 is a longer root and yields the perfectly well formed 2038-01-16, so a
    longest-root rule returns the wrong answer here. The year window and the root registry
    both kill it, and root length is not a scoring signal at all.
    """
    parsed = parse_symbol("BSE:SENSEX2381161000CE", today=AS_OF)
    assert parsed.root == "SENSEX"
    assert parsed.expiry == date(2023, 8, 11)
    assert parsed.strike == Decimal("61000")
    assert parsed.exchange_code == 12
    assert parsed.segment_code == 11


def test_year_window_rejects_the_2038_split():
    """Widening the window re-introduces the ambiguity, which is why it is not cosmetic."""
    with pytest.raises(AmbiguousSymbolError) as excinfo:
        parse_symbol(
            "BSE:SENSEX2381161000CE",
            registry=RootRegistry(),
            years=(2015, 2050),
        )
    roots = {c.root for c in excinfo.value.candidates}
    assert roots == {"SENSEX", "SENSEX2"}


def test_registry_resolves_the_two_valid_nifty_splits():
    """NIFTY expiring 2022-09-22 and NIFTY2 expiring 2029-02-21 are both well formed."""
    parsed = parse_symbol("NSE:NIFTY2292217000CE", today=AS_OF)
    assert parsed.root == "NIFTY"
    assert parsed.expiry == date(2022, 9, 22)
    assert parsed.parse_confidence == "scored"
    assert "multiple_candidates" in parsed.parse_warnings

    with pytest.raises(AmbiguousSymbolError):
        parse_symbol("NSE:NIFTY2292217000CE", registry=RootRegistry(), today=AS_OF)


def test_ambiguity_raises_rather_than_guessing():
    with pytest.raises(AmbiguousSymbolError) as excinfo:
        parse_symbol("NSE:ABC225125100CE", today=AS_OF)
    described = {(c.root, c.expiry, c.strike_raw) for c in excinfo.value.candidates}
    assert described == {
        ("ABC", date(2022, 5, 12), "5100"),
        ("ABC2", date(2025, 1, 25), "100"),
    }


def test_expiry_hint_resolves_an_ambiguous_symbol():
    parsed = parse_symbol(
        "NSE:ABC225125100CE", expected_expiry=date(2022, 5, 12), today=AS_OF
    )
    assert (parsed.root, parsed.strike_raw) == ("ABC", "5100")
    assert parsed.parse_method == "hinted"
    assert parsed.parse_confidence == "exact"


def test_root_hint_resolves_an_ambiguous_symbol():
    parsed = parse_symbol("NSE:ABC225125100CE", expected_root="ABC2", today=AS_OF)
    assert (parsed.root, parsed.expiry) == ("ABC2", date(2025, 1, 25))
    assert parsed.parse_method == "hinted"


def test_registering_the_root_resolves_an_ambiguous_symbol():
    registry = builtin_registry()
    registry.register(
        UnderlyingRoot(
            root="ABC",
            fyers_symbol="NSE:ABC-EQ",
            display_name="ABC",
            exchange="NSE",
            instrument_kind="EQUITY",
            derivative_segment="FO",
            data_from=date(2022, 1, 3),
        )
    )
    parsed = parse_symbol("NSE:ABC225125100CE", registry=registry, today=AS_OF)
    assert parsed.root == "ABC"
    assert parsed.instrument_class == "OPTSTK"


# ---------------------------------------------------------------- roots that fool a regex


def test_root_ending_in_digits_weekly():
    parsed = parse_symbol("NSE:NIFTYNXT502510923000CE", today=AS_OF)
    assert parsed.root == "NIFTYNXT50"
    assert parsed.expiry == date(2025, 1, 9)
    assert parsed.strike == Decimal("23000")


def test_root_ending_in_digits_futures():
    parsed = parse_symbol("NSE:NIFTYNXT5026MARFUT", today=AS_OF)
    assert (parsed.root, parsed.expiry_year, parsed.expiry_month) == ("NIFTYNXT50", 2026, 3)


def test_root_ending_in_digits_monthly_option():
    parsed = parse_symbol("BSE:SENSEX5025NOV25000CE", today=AS_OF)
    assert (parsed.root, parsed.expiry_year, parsed.expiry_month) == ("SENSEX50", 2025, 11)
    assert parsed.strike == Decimal("25000")


def test_root_ending_in_a_month_letter():
    """MARICO ends in the letter O, which is also the October token."""
    weekly = parse_symbol("NSE:MARICO2510923000CE", today=AS_OF)
    assert (weekly.root, weekly.expiry) == ("MARICO", date(2025, 1, 9))
    october = parse_symbol("NSE:MARICO20O0811000CE", today=AS_OF)
    assert (october.root, october.expiry) == ("MARICO", date(2020, 10, 8))


def test_root_containing_a_month_name():
    """MARUTI starts with MAR. A search rather than a fullmatch would split at offset zero."""
    parsed = parse_symbol("NSE:MARUTI25MAR11000CE", today=AS_OF)
    assert (parsed.root, parsed.expiry_year, parsed.expiry_month) == ("MARUTI", 2025, 3)


def test_root_containing_another_root_as_a_suffix():
    """BANKNIFTY ends in NIFTY. Root matching is anchored, so this is never NIFTY."""
    parsed = parse_symbol("NSE:BANKNIFTY2510952000CE", today=AS_OF)
    assert parsed.root == "BANKNIFTY"
    assert parsed.expiry == date(2025, 1, 9)


def test_ampersand_and_hyphen_roots():
    assert parse_symbol("NSE:M&M25OCTFUT", today=AS_OF).root == "M&M"
    assert parse_symbol("NSE:BAJAJ-AUTO25OCTFUT", today=AS_OF).root == "BAJAJ-AUTO"


def test_cash_series_splits_on_the_last_hyphen():
    parsed = parse_symbol("NSE:BAJAJ-AUTO-EQ", today=AS_OF)
    assert (parsed.root, parsed.series) == ("BAJAJ-AUTO", "EQ")


# ---------------------------------------------------------------- strikes


@pytest.mark.parametrize(
    "symbol,strike",
    [
        ("NSE:GBPINR20N0580.5PE", Decimal("80.5")),
        ("NSE:GBPINR20NOV80.5PE", Decimal("80.5")),
        ("NSE:USDINR2280580.5CE", Decimal("80.5")),
        ("NSE:USDINR20OCT75CE", Decimal("75")),
    ],
)
def test_decimal_strikes(symbol, strike):
    parsed = parse_symbol(symbol, today=AS_OF)
    assert parsed.strike == strike
    assert Decimal(parsed.strike_raw) == strike


def test_decimal_strike_keeps_the_raw_substring():
    """A float round trip loses 0.0025 style currency ticks, so the substring is kept as text."""
    parsed = parse_symbol("NSE:GBPINR20N0580.5PE", today=AS_OF)
    assert parsed.strike_raw == "80.5"
    assert isinstance(parsed.strike, Decimal)


@pytest.mark.parametrize(
    "symbol", ["NSE:NIFTY2011300000CE", "NSE:NIFTY2510900000CE"]
)
def test_zero_and_leading_zero_strikes_are_rejected(symbol):
    with pytest.raises(SymbolParseError):
        parse_symbol(symbol, today=AS_OF)


# ---------------------------------------------------------------- monthly coding semantics


def test_monthly_coded_option_has_no_day_without_a_hint():
    parsed = parse_symbol("NSE:BANKNIFTY25MAR52000PE", today=AS_OF)
    assert parsed.expiry is None
    assert (parsed.expiry_year, parsed.expiry_month) == (2025, 3)
    assert "expiry_day_unknown" in parsed.parse_warnings
    assert parsed.symbol_expiry_encoding == "MONTHLY_CODED"


def test_monthly_coded_option_takes_the_day_from_the_request():
    parsed = parse_symbol(
        "NSE:BANKNIFTY25MAR52000PE", expected_expiry=date(2025, 3, 27), today=AS_OF
    )
    assert parsed.expiry == date(2025, 3, 27)
    assert parsed.expiry_day == 27
    assert parsed.expiry_dow == 3
    assert "expiry_day_unknown" not in parsed.parse_warnings


def test_encoding_is_not_a_cycle():
    """Monthly coded and weekly coded describe the string, never the contract cycle."""
    monthly = parse_symbol("NSE:NIFTY25MAR23000CE", today=AS_OF)
    weekly = parse_symbol("NSE:NIFTY2510923000CE", today=AS_OF)
    assert monthly.is_monthly_coded and not monthly.is_weekly_coded
    assert weekly.is_weekly_coded and not weekly.is_monthly_coded
    for parsed in (monthly, weekly):
        assert not hasattr(parsed, "expiry_cycle")


# ---------------------------------------------------------------- hints are verified


def test_disagreeing_expiry_hint_is_a_hard_error():
    with pytest.raises(SymbolParseError, match="expected"):
        parse_symbol(
            "NSE:NIFTY2510923000CE", expected_expiry=date(2025, 1, 16), today=AS_OF
        )


def test_disagreeing_month_hint_on_a_monthly_symbol_is_a_hard_error():
    with pytest.raises(SymbolParseError, match="expected"):
        parse_symbol(
            "NSE:BANKNIFTY25MAR52000PE", expected_expiry=date(2025, 4, 24), today=AS_OF
        )


def test_disagreeing_root_hint_is_a_hard_error():
    with pytest.raises(SymbolParseError, match="expected"):
        parse_symbol("NSE:NIFTY2510923000CE", expected_root="BANKNIFTY", today=AS_OF)


def test_matching_hints_report_hinted_and_exact():
    parsed = parse_symbol(
        "NSE:NIFTY2510923000CE",
        expected_root="NIFTY",
        expected_expiry=date(2025, 1, 9),
        today=AS_OF,
    )
    assert parsed.parse_method == "hinted"
    assert parsed.parse_confidence == "exact"


def test_unhinted_parse_reports_enumerated():
    parsed = parse_symbol("NSE:NIFTY2510923000CE", today=AS_OF)
    assert parsed.parse_method == "enumerated"


def test_cash_reports_no_parse_method():
    parsed = parse_symbol("NSE:SBIN-EQ", today=AS_OF)
    assert parsed.parse_method == "none"
    assert parsed.symbol_expiry_encoding == "NONE"


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_every_result_carries_method_and_confidence(case):
    parsed = parse_symbol(case["symbol"], today=AS_OF)
    assert parsed.parse_method in {"none", "hinted", "enumerated"}
    assert parsed.parse_confidence in {"exact", "scored"}


# ---------------------------------------------------------------- exchange and segment


@pytest.mark.parametrize(
    "symbol,exchange,code", [
        ("NSE:SBIN-EQ", "NSE", 10),
        ("MCX:GOLD20DECFUT", "MCX", 11),
        ("BSE:SENSEX-INDEX", "BSE", 12),
    ],
)
def test_exchange_codes_are_not_alphabetical(symbol, exchange, code):
    """11 is MCX and 12 is BSE, which is not what most people guess."""
    parsed = parse_symbol(symbol, today=AS_OF)
    assert (parsed.exchange, parsed.exchange_code) == (exchange, code)


@pytest.mark.parametrize(
    "symbol,segment,code", [
        ("NSE:SBIN-EQ", "CM", 10),
        ("NSE:NIFTY25MAR23000CE", "FO", 11),
        ("NSE:GBPINR20N0580.5PE", "CD", 12),
        ("MCX:CRUDEOIL20OCT4000CE", "COM", 20),
    ],
)
def test_segment_assignment(symbol, segment, code):
    parsed = parse_symbol(symbol, today=AS_OF)
    assert (parsed.segment, parsed.segment_code) == (segment, code)


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_exchange_and_segment_pair_is_one_of_the_eight_valid_combinations(case):
    valid = {
        ("NSE", "CM"), ("NSE", "FO"), ("NSE", "CD"), ("NSE", "COM"),
        ("BSE", "CM"), ("BSE", "FO"), ("BSE", "CD"),
        ("MCX", "COM"),
    }
    parsed = parse_symbol(case["symbol"], today=AS_OF)
    assert (parsed.exchange, parsed.segment) in valid


def test_instrument_class_comes_from_the_registry_not_the_root_name():
    index_option = parse_symbol("NSE:NIFTY2510923000CE", today=AS_OF)
    assert index_option.instrument_class == "OPTIDX"
    unknown = parse_symbol("NSE:MARICO2510923000CE", today=AS_OF)
    assert unknown.instrument_class is None
    assert "instrument_class_unknown" in unknown.parse_warnings


def test_registered_equity_root_gives_a_stock_class():
    parsed = parse_symbol("NSE:RELIANCE25MAR1200CE", today=AS_OF)
    assert parsed.instrument_class == "OPTSTK"
    assert parse_symbol("NSE:RELIANCE25MARFUT", today=AS_OF).instrument_class == "FUTSTK"
    assert parse_symbol("NSE:NIFTY25MARFUT", today=AS_OF).instrument_class == "FUTIDX"


# ---------------------------------------------------------------- malformed input


@pytest.mark.parametrize(
    "symbol",
    ["", "   ", "NIFTY25MARFUT", "XYZ:NIFTY25MARFUT", "NSE:", ":NIFTY25MARFUT", "NSE:GARBAGE"],
)
def test_malformed_symbols_raise(symbol):
    with pytest.raises(SymbolParseError):
        parse_symbol(symbol, today=AS_OF)


def test_whitespace_and_case_are_normalised():
    assert parse_symbol("  nse:nifty2510923000ce  ", today=AS_OF).root == "NIFTY"


def test_max_year_follows_the_clock():
    assert max_year(date(2026, 1, 1)) == 2031
    assert max_year(date(2030, 6, 30)) == 2035


# ---------------------------------------------------------------- round trip properties

# Roots chosen so that no shorter prefix of one is a registered root, which keeps the unhinted
# leg of the property meaningful rather than accidentally decided by the registry.
_RT_ROOTS = ["ZED", "QQ7", "MARUTI", "MARICO", "NIFTYNXT50", "M&M", "BAJAJ-AUTO", "SENSEX50"]
_RT_EXPIRIES = [
    date(2022, 1, 6), date(2023, 8, 11), date(2024, 9, 30), date(2025, 1, 9),
    date(2025, 10, 8), date(2026, 11, 5), date(2027, 12, 31), date(2028, 2, 29),
]
_RT_STRIKES = ["1", "75", "80.5", "23050", "0.5", "99999.25"]
_RT_RIGHTS = ["CE", "PE"]

_WEEKLY_ROUND_TRIP = [
    (root, expiry, strike, right)
    for root, expiry, strike, right in product(
        _RT_ROOTS, _RT_EXPIRIES, _RT_STRIKES, _RT_RIGHTS
    )
]


@pytest.mark.parametrize("root,expiry,strike,right", _WEEKLY_ROUND_TRIP)
def test_weekly_round_trip_with_hints(root, expiry, strike, right):
    """Generating a weekly symbol and parsing it back must return what generated it.

    With both hints supplied the parse is deterministic by construction, which is exactly the
    situation the download pipeline is in: it asked for one underlying and one expiry.
    """
    symbol = build_weekly_option_symbol("NSE", root, expiry, strike, right)
    parsed = parse_symbol(
        symbol,
        expected_root=root,
        expected_expiry=expiry,
        today=date(2030, 1, 1),
    )
    assert parsed.root == root
    assert parsed.expiry == expiry
    assert parsed.strike == Decimal(strike)
    assert parsed.strike_raw == strike
    assert parsed.option_type == right
    assert parsed.symbol_expiry_encoding == "WEEKLY_CODED"
    assert parsed.raw == symbol


@pytest.mark.parametrize("root,expiry,strike,right", _WEEKLY_ROUND_TRIP)
def test_weekly_round_trip_without_hints_never_answers_wrongly(root, expiry, strike, right):
    """Unhinted, the parser either returns the generating components or refuses.

    It is never allowed to return a different decomposition silently. That is the whole point
    of scoring on hints and registry membership and refusing on a tie.
    """
    symbol = build_weekly_option_symbol("NSE", root, expiry, strike, right)
    try:
        parsed = parse_symbol(symbol, today=date(2030, 1, 1))
    except AmbiguousSymbolError:
        return
    assert (parsed.root, parsed.expiry, parsed.strike_raw, parsed.option_type) == (
        root,
        expiry,
        strike,
        right,
    )


@pytest.mark.parametrize(
    "root,year,month,strike,right",
    [
        (root, expiry.year, expiry.month, strike, right)
        for root, expiry, strike, right in product(
            _RT_ROOTS, _RT_EXPIRIES, ["320", "80.5", "52000"], _RT_RIGHTS
        )
    ],
)
def test_monthly_option_round_trip(root, year, month, strike, right):
    symbol = build_monthly_option_symbol("NSE", root, year, month, strike, right)
    parsed = parse_symbol(symbol, expected_root=root, today=date(2030, 1, 1))
    assert parsed.root == root
    assert (parsed.expiry_year, parsed.expiry_month) == (year, month)
    assert parsed.strike == Decimal(strike)
    assert parsed.option_type == right
    assert parsed.symbol_expiry_encoding == "MONTHLY_CODED"


@pytest.mark.parametrize(
    "root,year,month",
    [(root, e.year, e.month) for root, e in product(_RT_ROOTS, _RT_EXPIRIES)],
)
def test_future_round_trip(root, year, month):
    symbol = build_future_symbol("NSE", root, year, month)
    parsed = parse_symbol(symbol, expected_root=root, today=date(2030, 1, 1))
    assert parsed.root == root
    assert (parsed.expiry_year, parsed.expiry_month) == (year, month)
    assert parsed.kind == "FUT"


@pytest.mark.parametrize(
    "exchange,symbol_part,series",
    [
        ("NSE", "SBIN", "EQ"), ("NSE", "BAJAJ-AUTO", "EQ"), ("NSE", "M&M", "EQ"),
        ("BSE", "ACC", "A"), ("NSE", "NIFTY50", "INDEX"), ("BSE", "SENSEX", "INDEX"),
    ],
)
def test_cash_round_trip(exchange, symbol_part, series):
    symbol = build_cash_symbol(exchange, symbol_part, series)
    parsed = parse_symbol(symbol, today=AS_OF)
    assert (parsed.root, parsed.series, parsed.kind) == (symbol_part, series, "SPOT")
    assert parsed.instrument_class == ("INDEX" if series == "INDEX" else "EQUITY")


def test_as_dict_is_json_serialisable():
    payload = parse_symbol("NSE:GBPINR20N0580.5PE", today=AS_OF).as_dict()
    assert json.loads(json.dumps(payload))["strike"] == "80.5"
