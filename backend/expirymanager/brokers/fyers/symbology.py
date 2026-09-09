"""Decomposition of Fyers contract symbols into warehouse metadata.

Every column the warehouse keys on (root, expiry, strike, option right, segment) is derived
here, and the three expired-contract endpoints return nothing but the symbol string, so a
wrong decomposition is a silently wrong row rather than a visible failure. That is why this
module enumerates and scores instead of running one clever regex, and why it raises rather
than guesses when two decompositions are equally plausible.

The two symbol shapes that matter:

    monthly coded   {Ex}:{Root}{YY}{MMM}{Strike}{CE|PE}     NSE:BANKNIFTY25MAR52000PE
    weekly coded    {Ex}:{Root}{YY}{M}{dd}{Strike}{CE|PE}   NSE:NIFTY2510923000CE

{MMM} is three uppercase letters. {M} is ONE character from the alphabet 1-9 for January to
September and then the LETTERS O, N, D for October, November and December. The digit 0 is
never a valid {M}, which is the cheapest validity check available and also the thing that
separates the letter O of October from a digit zero.

Monthly coded is an encoding, not a cycle: on NSE and BSE the last weekly expiry of a calendar
month is written in the monthly form. Callers wanting the cycle must look elsewhere, which is
why this module reports ``symbol_expiry_encoding`` and never an expiry cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Sequence

from .roots import (
    CURRENCY_PAIR_RE,
    SEGMENT_CODES,
    RootRegistry,
    UnderlyingRoot,
    default_registry,
)

__all__ = [
    "AmbiguousSymbolError",
    "EXCHANGE_CODES",
    "MIN_YEAR",
    "MONTHLY_MONTHS",
    "ParsedSymbol",
    "SymbolParseError",
    "WEEKLY_MONTHS",
    "WeeklyCandidate",
    "build_cash_symbol",
    "build_future_symbol",
    "build_monthly_option_symbol",
    "build_weekly_option_symbol",
    "max_year",
    "parse_symbol",
    "weekly_month_code",
    "year_window",
]


MONTHLY_MONTHS: Mapping[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
MONTHLY_MONTH_NAMES: Mapping[int, str] = {v: k for k, v in MONTHLY_MONTHS.items()}

# No "0" key by design. October, November and December are the letters O, N and D.
WEEKLY_MONTHS: Mapping[str, int] = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
    "7": 7, "8": 8, "9": 9, "O": 10, "N": 11, "D": 12,
}
WEEKLY_MONTH_CODES: Mapping[int, str] = {v: k for k, v in WEEKLY_MONTHS.items()}

EXCHANGE_CODES: Mapping[str, int] = {"NSE": 10, "MCX": 11, "BSE": 12}

MONTHLY_CODED = "MONTHLY_CODED"
WEEKLY_CODED = "WEEKLY_CODED"
NO_ENCODING = "NONE"

KIND_SPOT = "SPOT"
KIND_FUTURE = "FUT"
KIND_OPTION = "OPT"

METHOD_NONE = "none"
METHOD_HINTED = "hinted"
METHOD_ENUMERATED = "enumerated"

CONFIDENCE_EXACT = "exact"
CONFIDENCE_SCORED = "scored"
# Never returned. It is the value the pipeline writes to dim_contract when this module raises.
CONFIDENCE_QUARANTINED = "quarantined"

WARN_ROOT_UNKNOWN = "root_unknown"
WARN_EXPIRY_DAY_UNKNOWN = "expiry_day_unknown"
WARN_INSTRUMENT_CLASS_UNKNOWN = "instrument_class_unknown"
WARN_SEGMENT_INFERRED = "segment_inferred"
WARN_MULTIPLE_CANDIDATES = "multiple_candidates"

_MON_ALT = "|".join(MONTHLY_MONTHS)

# Both patterns are used with fullmatch and never with search. A search would match the MAR
# inside MARUTI at offset 0 and produce a confident wrong answer.
RE_FUT = re.compile(rf"(?P<root>.+?)(?P<yy>\d{{2}})(?P<mon>{_MON_ALT})FUT")
RE_OPT_MONTHLY = re.compile(
    rf"(?P<root>.+?)(?P<yy>\d{{2}})(?P<mon>{_MON_ALT})"
    rf"(?P<strike>\d+(?:\.\d+)?)(?P<opt>CE|PE)"
)
# & is here for M&M, J&KBANK. The hyphen is here for BAJAJ-AUTO. Digits are here for
# NIFTYNXT50 and SENSEX50, and the leading letter rules out a split that starts mid-number.
RE_ROOT = re.compile(r"[A-Z][A-Z0-9&_.\-]*")
RE_STRIKE = re.compile(r"\d+(?:\.\d+)?")

# The floor predates the earliest data any Indian exchange serves through this API (NSE and MCX
# from 2022-01-03, BSE from 2023-08-07) with room for a vendor backfill. The ceiling follows the
# clock instead of being frozen, because the longest dated live contract in the vendor docs
# expires in 2030 and a frozen ceiling would start rejecting real contracts.
MIN_YEAR = 2015
MAX_YEAR_LOOKAHEAD = 5

# The window is load bearing, not cosmetic. It is what rejects the 2038 reading of
# BSE:SENSEX2381161000CE, so widening it costs determinism on real symbols.


def max_year(today: date | None = None) -> int:
    """Upper bound of the plausible expiry year, computed against the clock."""
    return (today or date.today()).year + MAX_YEAR_LOOKAHEAD


def year_window(today: date | None = None) -> tuple[int, int]:
    """The (min, max) expiry year a decomposition must fall inside to be considered."""
    return (MIN_YEAR, max_year(today))


def weekly_month_code(month: int) -> str:
    """The single character weekly month token for a 1 to 12 month number."""
    try:
        return WEEKLY_MONTH_CODES[month]
    except KeyError:
        raise ValueError(f"month out of range: {month!r}") from None


class SymbolParseError(ValueError):
    """The symbol could not be decomposed. The caller quarantines the row."""


class AmbiguousSymbolError(SymbolParseError):
    """Two or more decompositions scored equally. Never resolved by guessing."""

    def __init__(self, message: str, candidates: Sequence["WeeklyCandidate"] = ()) -> None:
        super().__init__(message)
        self.candidates = tuple(candidates)


@dataclass(frozen=True, slots=True)
class WeeklyCandidate:
    """One surviving split of a weekly coded body, with the score that ranked it."""

    root: str
    expiry: date
    strike: Decimal
    strike_raw: str
    score: tuple[bool, bool, bool]

    def describe(self) -> str:
        return f"{self.root}/{self.expiry.isoformat()}/{self.strike_raw}"


@dataclass(frozen=True, slots=True)
class ParsedSymbol:
    """Everything the warehouse can learn from the symbol string alone.

    ``expiry`` is None for a monthly coded contract unless the caller supplied
    ``expected_expiry``, because the monthly form carries no day. Never reconstruct the day
    from a last-Thursday rule: NSE and BSE expiry weekdays have changed several times since
    2022 and any such rule silently corrupts historical rows.
    """

    raw: str
    exchange: str
    exchange_code: int
    segment: str
    segment_code: int
    kind: str
    instrument_class: str | None
    root: str
    series: str | None
    expiry: date | None
    expiry_year: int | None
    expiry_month: int | None
    expiry_day: int | None
    symbol_expiry_encoding: str
    strike: Decimal | None
    strike_raw: str | None
    option_type: str | None
    parse_method: str
    parse_confidence: str
    parse_warnings: tuple[str, ...] = field(default=())

    @property
    def is_weekly_coded(self) -> bool:
        return self.symbol_expiry_encoding == WEEKLY_CODED

    @property
    def is_monthly_coded(self) -> bool:
        return self.symbol_expiry_encoding == MONTHLY_CODED

    @property
    def expiry_dow(self) -> int | None:
        """Monday is 0, matching date.weekday. None when the day is unknown."""
        return None if self.expiry is None else self.expiry.weekday()

    def as_dict(self) -> dict[str, object]:
        """JSON friendly projection. Decimal and date become strings so a fixture can hold it."""
        return {
            "raw": self.raw,
            "exchange": self.exchange,
            "exchange_code": self.exchange_code,
            "segment": self.segment,
            "segment_code": self.segment_code,
            "kind": self.kind,
            "instrument_class": self.instrument_class,
            "root": self.root,
            "series": self.series,
            "expiry": None if self.expiry is None else self.expiry.isoformat(),
            "expiry_year": self.expiry_year,
            "expiry_month": self.expiry_month,
            "expiry_day": self.expiry_day,
            "symbol_expiry_encoding": self.symbol_expiry_encoding,
            "strike": None if self.strike is None else str(self.strike),
            "strike_raw": self.strike_raw,
            "option_type": self.option_type,
            "parse_method": self.parse_method,
            "parse_confidence": self.parse_confidence,
            "parse_warnings": list(self.parse_warnings),
        }


def _resolve_registry(
    registry: RootRegistry | None,
    known_roots: Iterable[str] | None,
) -> tuple[RootRegistry, frozenset[str]]:
    reg = default_registry() if registry is None else registry
    extra = {r.strip().upper() for r in (known_roots or ()) if r and r.strip()}
    return reg, reg.known_roots() | extra


def _segment_for(
    exchange: str,
    kind: str,
    root: str,
    entry: UnderlyingRoot | None,
) -> tuple[str, int, bool]:
    """Return (segment, segment_code, inferred). Only the symbol master is authoritative."""
    if exchange == "MCX":
        # MCX has exactly one segment in the vendor table, commodity derivatives.
        return ("COM", SEGMENT_CODES["COM"], False)
    if kind == KIND_SPOT:
        return ("CM", SEGMENT_CODES["CM"], False)
    if entry is not None:
        return (entry.derivative_segment, SEGMENT_CODES[entry.derivative_segment], False)
    if CURRENCY_PAIR_RE.fullmatch(root):
        return ("CD", SEGMENT_CODES["CD"], True)
    return ("FO", SEGMENT_CODES["FO"], True)


def _instrument_class_for(kind: str, series: str | None, entry: UnderlyingRoot | None) -> str | None:
    """Index versus stock is registry data, never a guess from the root name."""
    if kind == KIND_SPOT:
        return "INDEX" if series == "INDEX" else "EQUITY"
    if entry is None:
        return None
    if entry.instrument_kind == "INDEX":
        return "FUTIDX" if kind == KIND_FUTURE else "OPTIDX"
    if entry.instrument_kind == "EQUITY":
        return "FUTSTK" if kind == KIND_FUTURE else "OPTSTK"
    # Currency and commodity roots have their own vendor instrument types (FUTCUR, OPTCUR,
    # FUTCOM, OPTCOM) which dim_contract.instrument_class does not enumerate. Leave it to the
    # symbol master rather than inventing a value the CHECK constraint would reject.
    return None


def _split_exchange(symbol: str) -> tuple[str, str]:
    # partition, not split, because only the FIRST colon separates the exchange.
    exchange, sep, body = symbol.strip().upper().partition(":")
    if not sep or not body:
        raise SymbolParseError(f"missing exchange prefix in {symbol!r}")
    if exchange not in EXCHANGE_CODES:
        raise SymbolParseError(f"unknown exchange {exchange!r} in {symbol!r}")
    return exchange, body


def _valid_strike(raw: str) -> Decimal | None:
    """Positive Decimal, no leading zeros.

    A strike is written by the exchange as a plain number, so a leading zero means the split
    started one character too far right and swallowed part of the day field.
    """
    if not RE_STRIKE.fullmatch(raw):
        return None
    if raw[0] == "0" and not raw.startswith("0."):
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value > 0 else None


def parse_symbol(
    symbol: str,
    *,
    registry: RootRegistry | None = None,
    known_roots: Iterable[str] | None = None,
    expected_root: str | None = None,
    expected_expiry: date | None = None,
    today: date | None = None,
    years: tuple[int, int] | None = None,
) -> ParsedSymbol:
    """Decompose a Fyers symbol into warehouse metadata.

    The hints exist because the download pipeline always knows the underlying and the expiry
    it asked for, which collapses every weekly split ambiguity to a single answer. Supply them
    whenever they are available: parsing is there to verify the vendor's string, not to
    discover facts the caller already holds. A hint that disagrees with the symbol is a hard
    error, because a disagreement means one of the two is wrong and neither can be trusted.

    ``years`` overrides the runtime plausibility window and exists so tests stay deterministic
    across calendar years.
    """
    exchange, body = _split_exchange(symbol)
    reg, roots = _resolve_registry(registry, known_roots)
    if expected_root:
        expected_root = expected_root.strip().upper()
        roots = roots | {expected_root}
    lo, hi = years if years is not None else year_window(today)

    parsed = _parse_body(
        symbol=symbol,
        exchange=exchange,
        body=body,
        registry=reg,
        roots=roots,
        expected_root=expected_root,
        expected_expiry=expected_expiry,
        years=(lo, hi),
    )
    _verify_hints(parsed, expected_root, expected_expiry)
    return parsed


def _parse_body(
    *,
    symbol: str,
    exchange: str,
    body: str,
    registry: RootRegistry,
    roots: frozenset[str],
    expected_root: str | None,
    expected_expiry: date | None,
    years: tuple[int, int],
) -> ParsedSymbol:
    hinted = expected_root is not None or expected_expiry is not None

    # Futures and options are tested before the cash form, because a cash exchange symbol can
    # end in CE or PE inside its own name but always carries a -SERIES suffix.
    if body.endswith("FUT"):
        match = RE_FUT.fullmatch(body)
        if match and RE_ROOT.fullmatch(match["root"]):
            year = 2000 + int(match["yy"])
            if not years[0] <= year <= years[1]:
                raise SymbolParseError(
                    f"futures expiry year {year} outside {years[0]}-{years[1]} in {symbol!r}"
                )
            return _build(
                symbol=symbol,
                exchange=exchange,
                registry=registry,
                roots=roots,
                kind=KIND_FUTURE,
                root=match["root"],
                series=None,
                expiry=expected_expiry,
                expiry_year=year,
                expiry_month=MONTHLY_MONTHS[match["mon"]],
                encoding=MONTHLY_CODED,
                strike=None,
                strike_raw=None,
                option_type=None,
                method=METHOD_HINTED if hinted else METHOD_ENUMERATED,
                confidence=CONFIDENCE_EXACT,
                warnings=(),
            )

    if body.endswith(("CE", "PE")):
        match = RE_OPT_MONTHLY.fullmatch(body)
        if match and RE_ROOT.fullmatch(match["root"]):
            year = 2000 + int(match["yy"])
            strike = _valid_strike(match["strike"])
            if strike is not None and years[0] <= year <= years[1]:
                return _build(
                    symbol=symbol,
                    exchange=exchange,
                    registry=registry,
                    roots=roots,
                    kind=KIND_OPTION,
                    root=match["root"],
                    series=None,
                    expiry=expected_expiry,
                    expiry_year=year,
                    expiry_month=MONTHLY_MONTHS[match["mon"]],
                    encoding=MONTHLY_CODED,
                    strike=strike,
                    strike_raw=match["strike"],
                    option_type=match["opt"],
                    method=METHOD_HINTED if hinted else METHOD_ENUMERATED,
                    confidence=CONFIDENCE_EXACT,
                    warnings=(),
                )
        return _parse_weekly_option(
            symbol=symbol,
            exchange=exchange,
            body=body,
            registry=registry,
            roots=roots,
            expected_root=expected_root,
            expected_expiry=expected_expiry,
            years=years,
        )

    if "-" in body:
        # rpartition, because NSE exchange symbols can contain a hyphen (BAJAJ-AUTO). A split
        # on the first hyphen would report the series as AUTO.
        head, _, series = body.rpartition("-")
        if head and series and RE_ROOT.fullmatch(head):
            return _build(
                symbol=symbol,
                exchange=exchange,
                registry=registry,
                roots=roots,
                kind=KIND_SPOT,
                root=head,
                series=series,
                expiry=None,
                expiry_year=None,
                expiry_month=None,
                encoding=NO_ENCODING,
                strike=None,
                strike_raw=None,
                option_type=None,
                method=METHOD_NONE,
                confidence=CONFIDENCE_EXACT,
                warnings=(),
            )

    raise SymbolParseError(f"unrecognised symbol shape {symbol!r}")


def _parse_weekly_option(
    *,
    symbol: str,
    exchange: str,
    body: str,
    registry: RootRegistry,
    roots: frozenset[str],
    expected_root: str | None,
    expected_expiry: date | None,
    years: tuple[int, int],
) -> ParsedSymbol:
    """Enumerate every split of the body, keep the valid ones, then score.

    A single lazy regex returns the shortest root, which is frequently wrong, and a
    longest-root rule returns the wrong answer for BSE:SENSEX2381161000CE (the longer SENSEX2
    yields a perfectly well formed 2038-01-16). Root length is therefore not a scoring signal
    at all: it decides nothing, and a tie at the top raises.
    """
    core, option_type = body[:-2], body[-2:]
    candidates: list[WeeklyCandidate] = []

    # A tail is {YY}{M}{dd}{Strike}, so it is at least 6 characters and the root is at least 1.
    for i in range(1, len(core) - 5):
        root, tail = core[:i], core[i:]
        if not RE_ROOT.fullmatch(root):
            continue
        yy, month_char, dd, strike_raw = tail[:2], tail[2], tail[3:5], tail[5:]
        if not (yy.isascii() and yy.isdigit() and dd.isascii() and dd.isdigit()):
            continue
        month = WEEKLY_MONTHS.get(month_char)
        if month is None:
            continue
        year = 2000 + int(yy)
        if not years[0] <= year <= years[1]:
            continue
        try:
            expiry = date(year, month, int(dd))
        except ValueError:
            continue
        strike = _valid_strike(strike_raw)
        if strike is None:
            continue
        score = (
            expected_expiry is not None and expiry == expected_expiry,
            expected_root is not None and root == expected_root,
            root in roots,
        )
        candidates.append(WeeklyCandidate(root, expiry, strike, strike_raw, score))

    if not candidates:
        raise SymbolParseError(f"no valid weekly decomposition for {symbol!r}")

    best = max(c.score for c in candidates)
    winners = [c for c in candidates if c.score == best]
    if len(winners) > 1:
        raise AmbiguousSymbolError(
            f"{symbol!r} has {len(winners)} equally plausible parses: "
            + ", ".join(w.describe() for w in winners),
            winners,
        )

    won = winners[0]
    hinted = any(best[:2])
    warnings: tuple[str, ...] = () if len(candidates) == 1 else (WARN_MULTIPLE_CANDIDATES,)
    return _build(
        symbol=symbol,
        exchange=exchange,
        registry=registry,
        roots=roots,
        kind=KIND_OPTION,
        root=won.root,
        series=None,
        expiry=won.expiry,
        expiry_year=won.expiry.year,
        expiry_month=won.expiry.month,
        encoding=WEEKLY_CODED,
        strike=won.strike,
        strike_raw=won.strike_raw,
        option_type=option_type,
        method=METHOD_HINTED if hinted else METHOD_ENUMERATED,
        confidence=CONFIDENCE_EXACT if len(candidates) == 1 or hinted else CONFIDENCE_SCORED,
        warnings=warnings,
    )


def _build(
    *,
    symbol: str,
    exchange: str,
    registry: RootRegistry,
    roots: frozenset[str],
    kind: str,
    root: str,
    series: str | None,
    expiry: date | None,
    expiry_year: int | None,
    expiry_month: int | None,
    encoding: str,
    strike: Decimal | None,
    strike_raw: str | None,
    option_type: str | None,
    method: str,
    confidence: str,
    warnings: tuple[str, ...],
) -> ParsedSymbol:
    entry = registry.get(root)
    segment, segment_code, inferred = _segment_for(exchange, kind, root, entry)
    instrument_class = _instrument_class_for(kind, series, entry)

    collected = list(warnings)
    if kind != KIND_SPOT and root not in roots:
        collected.append(WARN_ROOT_UNKNOWN)
    if inferred:
        collected.append(WARN_SEGMENT_INFERRED)
    if instrument_class is None:
        collected.append(WARN_INSTRUMENT_CLASS_UNKNOWN)
    if encoding == MONTHLY_CODED and expiry is None:
        collected.append(WARN_EXPIRY_DAY_UNKNOWN)

    # The year and month the symbol itself encodes are never overwritten by the hinted expiry.
    # Keeping them is what lets _verify_hints notice that the request and the response describe
    # different contracts on a monthly coded symbol, where only the day comes from the request.
    if expiry is not None and expiry_year is None:
        expiry_year, expiry_month = expiry.year, expiry.month

    return ParsedSymbol(
        raw=symbol,
        exchange=exchange,
        exchange_code=EXCHANGE_CODES[exchange],
        segment=segment,
        segment_code=segment_code,
        kind=kind,
        instrument_class=instrument_class,
        root=root,
        series=series,
        expiry=expiry,
        expiry_year=expiry_year,
        expiry_month=expiry_month,
        expiry_day=None if expiry is None else expiry.day,
        symbol_expiry_encoding=encoding,
        strike=strike,
        strike_raw=strike_raw,
        option_type=option_type,
        parse_method=method,
        parse_confidence=confidence,
        parse_warnings=tuple(collected),
    )


def _verify_hints(
    parsed: ParsedSymbol,
    expected_root: str | None,
    expected_expiry: date | None,
) -> None:
    """A hint that disagrees with the symbol quarantines the row.

    The pipeline asks the vendor for one underlying and one expiry and receives symbols back,
    so a disagreement means the request and the response describe different contracts. Papering
    over it would write a row that claims an expiry the data does not have.
    """
    if expected_root is not None and parsed.kind != KIND_SPOT and parsed.root != expected_root:
        raise SymbolParseError(
            f"{parsed.raw!r} parses to root {parsed.root!r}, expected {expected_root!r}"
        )
    if expected_expiry is None:
        return
    if parsed.symbol_expiry_encoding == WEEKLY_CODED and parsed.expiry != expected_expiry:
        raise SymbolParseError(
            f"{parsed.raw!r} parses to expiry {parsed.expiry}, expected {expected_expiry}"
        )
    if parsed.symbol_expiry_encoding == MONTHLY_CODED:
        # A monthly coded symbol carries year and month only, so that is all there is to check.
        if (expected_expiry.year, expected_expiry.month) != (
            parsed.expiry_year,
            parsed.expiry_month,
        ):
            raise SymbolParseError(
                f"{parsed.raw!r} encodes {parsed.expiry_year}-{parsed.expiry_month:02d}, "
                f"expected {expected_expiry.year}-{expected_expiry.month:02d}"
            )


def build_cash_symbol(exchange: str, exchange_symbol: str, series: str) -> str:
    """{Ex}:{Ex_Symbol}-{Series}."""
    return f"{exchange.upper()}:{exchange_symbol.upper()}-{series.upper()}"


def build_future_symbol(exchange: str, root: str, year: int, month: int) -> str:
    """{Ex}:{Root}{YY}{MMM}FUT."""
    return f"{exchange.upper()}:{root.upper()}{year % 100:02d}{MONTHLY_MONTH_NAMES[month]}FUT"


def build_monthly_option_symbol(
    exchange: str, root: str, year: int, month: int, strike: str | Decimal, right: str
) -> str:
    """{Ex}:{Root}{YY}{MMM}{Strike}{CE|PE}."""
    return (
        f"{exchange.upper()}:{root.upper()}{year % 100:02d}"
        f"{MONTHLY_MONTH_NAMES[month]}{strike}{right.upper()}"
    )


def build_weekly_option_symbol(
    exchange: str, root: str, expiry: date, strike: str | Decimal, right: str
) -> str:
    """{Ex}:{Root}{YY}{M}{dd}{Strike}{CE|PE}."""
    return (
        f"{exchange.upper()}:{root.upper()}{expiry.year % 100:02d}"
        f"{weekly_month_code(expiry.month)}{expiry.day:02d}{strike}{right.upper()}"
    )
