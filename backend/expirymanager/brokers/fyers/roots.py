"""The underlying root registry.

A derivative root (NIFTY) and the cash instrument ticker it is quoted against
(NSE:NIFTY50-INDEX) are two different strings, and only the second one is accepted by the
Fyers expired-contract endpoints. Nothing in the symbol string connects them, so the mapping
is data rather than a rule, seeded with the four built-in underlyings and extended at runtime
from ``sqlite.underlying_registry`` and from the symbol master.

The registry is load bearing for parsing, not decoration. Two weekly decompositions of
NSE:NIFTY2292217000CE are syntactically valid (NIFTY expiring 2022-09-22 and NIFTY2 expiring
2029-02-21) and knowing that NIFTY is a real root is what picks the right one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable, Iterator, Mapping

from .calendar import exchange_data_floor

__all__ = [
    "CURRENCY_PAIR_RE",
    "DERIVATIVE_SEGMENTS",
    "INSTRUMENT_KINDS",
    "RootRegistry",
    "SEED_ROOTS",
    "SEGMENT_CODES",
    "UnderlyingRoot",
    "builtin_registry",
    "default_registry",
]

SEGMENT_CODES: Mapping[str, int] = {"CM": 10, "FO": 11, "CD": 12, "COM": 20}
DERIVATIVE_SEGMENTS = frozenset({"FO", "CD", "COM"})
INSTRUMENT_KINDS = frozenset({"INDEX", "EQUITY", "COMMODITY", "CURRENCY"})

# Currency pairs are the one root family whose derivative segment can be told from the string
# itself, and getting it wrong puts a contract in the wrong segment. This is a fallback for a
# root the registry has never seen: a registered root always wins over the pattern.
CURRENCY_PAIR_RE = re.compile(r"(?:USD|EUR|GBP|JPY|AUD|CAD|CHF|SGD|AED|CNY)(?:INR|USD|JPY)")

# A root may only contain the characters the exchanges actually use. & is here for M&M and
# J&KBANK, the hyphen for BAJAJ-AUTO, digits for NIFTYNXT50 and SENSEX50.
_ROOT_RE = re.compile(r"[A-Z][A-Z0-9&_.\-]*")
_FYERS_SYMBOL_RE = re.compile(r"(?:NSE|BSE|MCX):[A-Z0-9&_.\-]+")


@dataclass(frozen=True, slots=True)
class UnderlyingRoot:
    """One underlying, from its derivative root to its cash ticker.

    ``fyers_symbol`` is what goes on the wire to expiry-dates, underlying-symbols,
    options-chain and history. ``root`` is what appears inside contract symbols and is
    confirmed against ``data.symbol`` in the first expiry-dates response.
    """

    root: str
    fyers_symbol: str
    display_name: str
    exchange: str
    instrument_kind: str
    derivative_segment: str
    data_from: date
    underlying_id: int | None = None
    spot_contract_id: int | None = None
    is_builtin: bool = False
    resolved_root_echo: str | None = None

    def __post_init__(self) -> None:
        if not _ROOT_RE.fullmatch(self.root):
            raise ValueError(f"invalid root {self.root!r}")
        if not _FYERS_SYMBOL_RE.fullmatch(self.fyers_symbol):
            raise ValueError(f"invalid fyers symbol {self.fyers_symbol!r}")
        if self.exchange not in ("NSE", "BSE", "MCX"):
            raise ValueError(f"invalid exchange {self.exchange!r}")
        if self.instrument_kind not in INSTRUMENT_KINDS:
            raise ValueError(f"invalid instrument kind {self.instrument_kind!r}")
        if self.derivative_segment not in DERIVATIVE_SEGMENTS:
            raise ValueError(f"invalid derivative segment {self.derivative_segment!r}")
        if not self.fyers_symbol.startswith(f"{self.exchange}:"):
            raise ValueError(
                f"exchange {self.exchange!r} does not match symbol {self.fyers_symbol!r}"
            )

    @property
    def segment_code(self) -> int:
        return SEGMENT_CODES[self.derivative_segment]


def _seed(
    root: str,
    fyers_symbol: str,
    display_name: str,
    exchange: str,
    instrument_kind: str,
    underlying_id: int,
) -> UnderlyingRoot:
    return UnderlyingRoot(
        root=root,
        fyers_symbol=fyers_symbol,
        display_name=display_name,
        exchange=exchange,
        instrument_kind=instrument_kind,
        derivative_segment="COM" if exchange == "MCX" else "FO",
        data_from=exchange_data_floor(exchange),
        underlying_id=underlying_id,
        # Contract ids 1 to 999 are reserved for spot series, one per underlying, allocated at
        # registry insert. The seeds take the first four.
        spot_contract_id=underlying_id,
        is_builtin=True,
    )


# Every string here is verbatim from the vendor documentation. The root for NIFTY and
# BANKNIFTY is confirmed by the option chain ex_symbol field and by contract samples
# (NSE:BANKNIFTY25NOV58900PE), and is re-confirmed at runtime from the expiry-dates echo.
SEED_ROOTS: tuple[UnderlyingRoot, ...] = (
    _seed("NIFTY", "NSE:NIFTY50-INDEX", "NIFTY 50", "NSE", "INDEX", 1),
    _seed("BANKNIFTY", "NSE:NIFTYBANK-INDEX", "NIFTY BANK", "NSE", "INDEX", 2),
    _seed("SENSEX", "BSE:SENSEX-INDEX", "SENSEX", "BSE", "INDEX", 3),
    _seed("RELIANCE", "NSE:RELIANCE-EQ", "Reliance Industries", "NSE", "EQUITY", 4),
)


class RootRegistry:
    """Roots keyed by root string, matched longest first.

    Longest first is not a preference, it is a correctness requirement: BANKNIFTY, FINNIFTY
    and MIDCPNIFTY all end in NIFTY, so a shortest-first or unordered scan would classify
    NSE:BANKNIFTY2510952000CE as NIFTY with stray leading text. Matching is anchored at the
    start of the body for the same reason.
    """

    def __init__(self, entries: Iterable[UnderlyingRoot] = ()) -> None:
        self._by_root: dict[str, UnderlyingRoot] = {}
        self._order: tuple[str, ...] = ()
        for entry in entries:
            self.register(entry)

    def _reindex(self) -> None:
        self._order = tuple(
            sorted(self._by_root, key=lambda r: (-len(r), r))
        )

    def register(self, entry: UnderlyingRoot, *, replace_existing: bool = False) -> UnderlyingRoot:
        """Add a user-supplied or symbol-master-derived root."""
        key = entry.root.upper()
        if key in self._by_root and not replace_existing:
            raise ValueError(f"root {key!r} is already registered")
        self._by_root[key] = entry if entry.root == key else replace(entry, root=key)
        self._reindex()
        return self._by_root[key]

    def unregister(self, root: str) -> None:
        self._by_root.pop(root.strip().upper(), None)
        self._reindex()

    def get(self, root: str | None) -> UnderlyingRoot | None:
        if not root:
            return None
        return self._by_root.get(root.strip().upper())

    def by_fyers_symbol(self, fyers_symbol: str) -> UnderlyingRoot | None:
        wanted = fyers_symbol.strip().upper()
        for entry in self._by_root.values():
            if entry.fyers_symbol == wanted:
                return entry
        return None

    def match_prefix(self, body: str) -> UnderlyingRoot | None:
        """Longest registered root that the body starts with, or None.

        ``body`` is the symbol with the exchange prefix already removed.
        """
        candidate = body.strip().upper()
        for root in self._order:
            if candidate.startswith(root):
                return self._by_root[root]
        return None

    def known_roots(self) -> frozenset[str]:
        return frozenset(self._by_root)

    def roots_longest_first(self) -> tuple[str, ...]:
        return self._order

    def __contains__(self, root: object) -> bool:
        return isinstance(root, str) and root.strip().upper() in self._by_root

    def __iter__(self) -> Iterator[UnderlyingRoot]:
        return iter(self._by_root[r] for r in self._order)

    def __len__(self) -> int:
        return len(self._by_root)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, object]]) -> "RootRegistry":
        """Build from underlying_registry rows, which is how the app loads it at startup."""
        entries = []
        for row in rows:
            data_from = row.get("data_from")
            if isinstance(data_from, str):
                data_from = date.fromisoformat(data_from)
            exchange = str(row["exchange"]).upper()
            entries.append(
                UnderlyingRoot(
                    root=str(row["root"]).upper(),
                    fyers_symbol=str(row["fyers_symbol"]).upper(),
                    display_name=str(row.get("display_name") or row["root"]),
                    exchange=exchange,
                    instrument_kind=str(row["instrument_kind"]).upper(),
                    derivative_segment=str(
                        row.get("derivative_segment") or ("COM" if exchange == "MCX" else "FO")
                    ).upper(),
                    data_from=data_from or exchange_data_floor(exchange),
                    underlying_id=row.get("underlying_id"),  # type: ignore[arg-type]
                    spot_contract_id=row.get("spot_contract_id"),  # type: ignore[arg-type]
                    is_builtin=bool(row.get("is_builtin", False)),
                    resolved_root_echo=(
                        str(row["resolved_root_echo"]) if row.get("resolved_root_echo") else None
                    ),
                )
            )
        return cls(entries)


def builtin_registry() -> RootRegistry:
    """A fresh registry holding only the four seeded underlyings."""
    return RootRegistry(SEED_ROOTS)


_DEFAULT = builtin_registry()


def default_registry() -> RootRegistry:
    """The process-wide registry the parser falls back to.

    It starts as the four seeds and the app extends it at startup from underlying_registry, so
    a user-added root is known to the parser without threading a registry through every call.
    """
    return _DEFAULT
