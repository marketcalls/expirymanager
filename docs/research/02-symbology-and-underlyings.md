# Fyers Symbology and Underlyings: Definitive Reference

Research note 02 for ExpiryManager.

Sources, all read directly from disk, no memory:

- `/Users/openalgo/AIBootcamp2026/Day26/fyers api docs/29-appendix.md` (read in full)
- `/Users/openalgo/AIBootcamp2026/Day26/fyers api docs/23-data-api.md` (history, quotes, depth, option chain, futures chain)
- `/Users/openalgo/AIBootcamp2026/Day26/fyers api docs/20-broker-config.md` (Symbol Master CSV and JSON, this is where the instrument dump actually lives, NOT in the Data API section)
- `/Users/openalgo/AIBootcamp2026/Day26/fyers api docs/24-expired-f-o-contracts-data.md` (expired contract symbol shapes)
- `/Users/openalgo/AIBootcamp2026/Day26/fyers api docs/04-request-response-structure.md` (auth header, error codes, rate limits)
- `/Users/openalgo/AIBootcamp2026/Day26/fyers api docs/30-change-log.md`

Statements below are marked CONFIRMED (present verbatim in the docs), DERIVED (arithmetic or logic applied to confirmed doc samples), or VERIFY (must be checked against a live symbol master file or a live API call before the pipeline depends on it).

---

## 1. Token layout by segment (CONFIRMED, appendix Symbology Format table)

| Segment | Format | Doc examples |
|---|---|---|
| Equity (cash) | `{Ex}:{Ex_Symbol}-{Series}` | `NSE:SBIN-EQ`, `NSE:ACC-EQ`, `NSE:MODIRUBBER-BE`, `BSE:SBIN-A`, `BSE:ACC-A`, `BSE:MODIRUBBER-T` |
| Equity Futures | `{Ex}:{Ex_UnderlyingSymbol}{YY}{MMM}FUT` | `NSE:NIFTY20OCTFUT`, `NSE:BANKNIFTY20NOVFUT`, `BSE:SENSEX23AUGFUT` |
| Equity Options, monthly coded | `{Ex}:{Ex_UnderlyingSymbol}{YY}{MMM}{Strike}{Opt_Type}` | `NSE:NIFTY20OCT11000CE`, `NSE:BANKNIFTY20NOV25000PE`, `BSE:SENSEX23AUG60400CE` |
| Equity Options, weekly coded | `{Ex}:{Ex_UnderlyingSymbol}{YY}{M}{dd}{Strike}{Opt_Type}` | `NSE:NIFTY2010811000CE`, `NSE:NIFTY20O0811000CE`, `BSE:SENSEX2381161000CE`, `NSE:NIFTY20D1025000CE` |
| Currency Futures | `{Ex}:{Ex_CurrencyPair}{YY}{MMM}FUT` | `NSE:USDINR20OCTFUT`, `NSE:GBPINR20NOVFUT` |
| Currency Options, monthly coded | `{Ex}:{Ex_CurrencyPair}{YY}{MMM}{Strike}{Opt_Type}` | `NSE:USDINR20OCT75CE`, `NSE:GBPINR20NOV80.5PE` |
| Currency Options, weekly coded | `{Ex}:{Ex_CurrencyPair}{YY}{M}{dd}{Strike}{Opt_Type}` | `NSE:USDINR20O0875CE`, `NSE:GBPINR20N0580.5PE`, `NSE:USDINR20D1075CE` |
| Commodity Futures | `{Ex}:{Ex_Commodity}{YY}{MMM}FUT` | `MCX:CRUDEOIL20OCTFUT`, `MCX:GOLD20DECFUT` |
| Commodity Options, monthly coded | `{Ex}:{Ex_Commodity}{YY}{MMM}{Strike}{Opt_Type}` | `MCX:CRUDEOIL20OCT4000CE`, `MCX:GOLD20DEC40000PE` |

Notes on the table:

- There is no weekly row for commodity options in the appendix. MCX options in the docs are monthly coded only, for example `MCX:CRUDEOIL25DEC5500PE` (from the option chain samples). Do not assume weekly MCX coding exists. VERIFY against `MCX_COM.csv` if MCX is ever enabled.
- The index cash form `{Ex}:{Name}-INDEX` is not listed in the Symbology Format table, but it is used consistently across the whole documentation set as the cash or spot instrument ticker for indices: `NSE:NIFTY50-INDEX`, `NSE:NIFTYBANK-INDEX`, `NSE:INDIAVIX-INDEX`, `NSE:NIFTYMIDSELECT-INDEX`, `NSE:NIFTYHEALTHCARE-INDEX`, `BSE:SENSEX-INDEX`. Treat `INDEX` as the series token in the equity cash format. CONFIRMED by usage, DERIVED as a rule.
- The exchange prefix set is exactly `NSE`, `BSE`, `MCX` (CONFIRMED, appendix Symbology Possible Values).

## 2. Field encodings (CONFIRMED, appendix Symbology Possible Values)

| Variable | Meaning | Values |
|---|---|---|
| `{Ex}` | Exchange | `NSE`, `BSE`, `MCX` |
| `{YY}` | Last 2 digits of the expiry year | `19`, `20`, `21`, `22`, ... |
| `{MMM}` | Month for monthly coded contracts, always uppercase | `JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC` |
| `{M}` | Month for weekly coded contracts, exactly 1 character | `1 2 3 4 5 6 7 8 9` for January to September, then `O` for October, `N` for November, `D` for December. October, November and December are LETTERS, not numbers. |
| `{dd}` | Day of month for weekly coded contracts, always 2 characters, zero padded | `01`, `06`, `10`, `25`, `30` |
| `{Opt_Type}` | Option right | `CE` for call, `PE` for put |
| `{Strike}` | Strike price | `11000`, `75.5`. Decimals occur, mainly in currency and commodity. |

The weekly month alphabet is therefore `[1-9OND]`. The digit `0` is never a valid `{M}`. That single fact is the cheapest validity check available and it also resolves the classic `O` versus `0` confusion: in `NSE:NIFTY20O0811000CE` the character after `20` is the letter O (October) and the `08` after it is the day.

## 3. Monthly versus weekly: how to tell them apart reliably

The two option forms differ only in the month field: 3 uppercase letters (monthly coded) versus 1 character from `[1-9OND]` followed by 2 digits (weekly coded).

Reliable discriminator, in this order:

1. Strip the exchange prefix at the FIRST colon and strip the `CE` or `PE` suffix.
2. If the remainder full-matches `^(.+?)(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d+(?:\.\d+)?)$` then it is monthly coded.
3. Otherwise attempt the weekly enumeration described in section 7. The weekly tail is `{YY}{M}{dd}{Strike}` where `{M}` is one character from `[1-9OND]`.

Because a monthly tail always contains three consecutive A to Z characters that spell a real month, and a weekly tail contains at most one letter (and only `O`, `N` or `D`), the two forms cannot both match the same string. There is no cross form ambiguity. The ambiguity that actually bites is inside the weekly form, and it is about where the underlying root ends, not about weekly versus monthly. See section 6.

### 3.1 The trap that matters most for downstream semantics

Monthly coded is NOT the same as "this is a monthly expiry contract", and weekly coded is NOT the same as "this is a weekly expiry contract".

On NSE and BSE the last expiry of a calendar month for an index is written in the monthly `{YY}{MMM}` form even though it is simply the last weekly expiry of that month. The symbol format tells you how the expiry is ENCODED, not what the exchange calls the contract cycle. Store two separate fields:

- `symbol_expiry_encoding` with values `MONTHLY_CODED` or `WEEKLY_CODED`, derived from the symbol string.
- `expiry_cycle` with values `W` or `M` or `UNKNOWN`, which can only come from an authoritative source.

The only place in the entire documentation that publishes an authoritative weekly or monthly flag is the Option Chain response, field `data.expiryData[].expiry_flag`, values `W` and `M` (CONFIRMED, `23-data-api.md`, option chain sample). The expired contracts APIs in `24-expired-f-o-contracts-data.md` do NOT return this flag. For expired data, set `expiry_cycle` to `UNKNOWN` unless you compute it yourself (for example, mark the chronologically last options expiry inside a calendar month for a given root as `M`, the rest as `W`). Do that as a derived, clearly labelled column, never as a claim from the vendor.

### 3.2 A monthly coded symbol does not contain the expiry day

`NSE:NIFTY25MAR23000CE` encodes year 2025 and month March only. The exact expiry date is not recoverable from the symbol. This is critical for the pipeline: the expiry date must be carried from the `expiry_date` value that the Get Expired Contracts call was made with (or from `expiryDate` in the symbol master for live contracts). Never reconstruct it from a last Thursday or last Tuesday rule: the NSE and BSE weekly and monthly expiry weekdays have changed several times since 2022, and any hardcoded rule will silently produce wrong dates for older data.

Weekly coded symbols do contain the full date (`{YY}{M}{dd}`), so for those the symbol is self-describing and can be cross-checked against the request `expiry_date`.

## 4. Exchange, segment, instrument type and fytoken (CONFIRMED, appendix)

### Exchange codes

| Code | Exchange |
|---|---|
| 10 | NSE |
| 11 | MCX |
| 12 | BSE |

Note the ordering trap: 11 is MCX and 12 is BSE, which is not alphabetical and not what most people guess.

### Segment codes

| Code | Segment |
|---|---|
| 10 | Capital Market |
| 11 | Equity Derivatives |
| 12 | Currency Derivatives |
| 20 | Commodity Derivatives |

### Valid exchange and segment combinations

| Exchange | Segment | Exchange code | Segment code |
|---|---|---|---|
| NSE | Capital Market | 10 | 10 |
| NSE | Equity Derivatives | 10 | 11 |
| NSE | Currency Derivatives | 10 | 12 |
| NSE | Commodity Derivatives | 10 | 20 |
| BSE | Capital Market | 12 | 10 |
| BSE | Equity Derivatives | 12 | 11 |
| BSE | Currency Derivatives | 12 | 12 |
| MCX | Commodity Derivatives | 11 | 20 |

Use this as a CHECK constraint in SQLite. Anything outside these eight pairs is a parse bug.

### Instrument types (`exInstType` in the symbol master JSON, `Exchange Instrument type` in the CSV)

Capital Market segment: `0` EQ, `1` PREFSHARES, `2` DEBENTURES, `3` WARRANTS, `4` MISC (NSE, BSE), `5` SGB, `6` G-Secs, `7` T-Bills, `8` MF, `9` ETF, `10` INDEX, `50` MISC (BSE).

FO segment: `11` FUTIDX, `12` FUTIVX, `13` FUTSTK, `14` OPTIDX, `15` OPTSTK.

CD segment: `16` FUTCUR, `17` FUTIRT, `18` FUTIRC, `19` OPTCUR, `20` UNDCUR, `21` UNDIRC, `22` UNDIRT, `23` UNDIRD, `24` INDEX_CD, `25` FUTIRD.

COM segment: `11` FUTIDX, `30` FUTCOM, `31` OPTFUT, `32` OPTCOM, `33` FUTBAS, `34` FUTBLN, `35` FUTENR, `36` OPTBLN, `37` OPTFUT (NCOM).

Gotcha: the numeric space is reused across segments. `11` means FUTIDX in the FO segment and also FUTIDX in the COM segment, but `20` means UNDCUR in CD and nothing in FO. An instrument type integer is only meaningful together with its segment code. Store both, and always join on the pair.

The instrument type is also the clean way to answer "is this an index option or a stock option": `14` OPTIDX versus `15` OPTSTK, and `11` FUTIDX versus `13` FUTSTK. Do not try to infer index versus stock from the root name.

### Fytoken layout (CONFIRMED table plus DERIVED validation against live samples)

| Part | Width | Meaning |
|---|---|---|
| Exchange | 2 digits | exchange code from the table above |
| Segment | 2 digits | segment code from the table above |
| Expiry | 6 digits | `YYMMDD`, for example `200827`. `000000` for non derivative instruments. |
| Exchange token | 2 to 6 digits | the token assigned by the exchange |

Total length is therefore 12 to 16 characters. Worked examples taken from doc samples, decomposed (DERIVED):

| Fytoken | Exchange | Segment | Expiry | Ex token | Symbol |
|---|---|---|---|---|---|
| `10100000003045` | 10 NSE | 10 Capital Market | `000000` none | 3045 | `NSE:SBIN-EQ` |
| `101000000026000` | 10 NSE | 10 Capital Market | `000000` none | 26000 | `NSE:NIFTY50-INDEX` |
| `101000000026017` | 10 NSE | 10 Capital Market | `000000` none | 26017 | `NSE:INDIAVIX-INDEX` |
| `101126032462574` | 10 NSE | 11 Equity Derivatives | `260324` = 2026-03-24 | 62574 | `NSE:NIFTY2632423050PE` |
| `101126092968407` | 10 NSE | 11 Equity Derivatives | `260929` = 2026-09-29 | 68407 | `NSE:NIFTY26SEPFUT` |

Two useful consequences:

1. The fytoken independently encodes the expiry date, including for monthly coded symbols where the symbol itself does not. `NSE:NIFTY26SEPFUT` gives `260929`, and the futures chain sample confirms the same contract with `expiry` epoch `1790676600` which is 2026-09-29 15:30 IST. So wherever a fytoken is available, it is a free cross check on the parsed expiry, and a free source of the day component for monthly coded symbols.
2. Fytoken must be stored as TEXT, never as an integer. Lengths vary from 12 to 16, the value has no arithmetic meaning, and treating it as a number invites precision loss and loss of any structure you might want to slice.

Limitation: the three expired F&O endpoints in `24-expired-f-o-contracts-data.md` return only symbol strings. They return NO fytoken, NO lot size, NO tick size, NO strike, NO instrument type. All of that metadata has to come from parsing plus the symbol master. That is exactly why the parser in section 7 has to be airtight.

## 5. The instrument dump: Symbol Master (CONFIRMED, `20-broker-config.md`)

Yes, a symbol master exists. It is a set of static public files, not an authenticated API endpoint, and it is documented under Broker Config rather than under the Data API. No access token is required to fetch them.

### CSV files

- NSE Currency Derivatives: `https://public.fyers.in/sym_details/NSE_CD.csv`
- NSE Equity Derivatives: `https://public.fyers.in/sym_details/NSE_FO.csv`
- NSE Commodity: `https://public.fyers.in/sym_details/NSE_COM.csv`
- NSE Capital Market: `https://public.fyers.in/sym_details/NSE_CM.csv`
- BSE Capital Market: `https://public.fyers.in/sym_details/BSE_CM.csv`
- BSE Equity Derivatives: `https://public.fyers.in/sym_details/BSE_FO.csv`
- MCX Commodity: `https://public.fyers.in/sym_details/MCX_COM.csv`

CSV columns, in the order the documentation lists them:

`Fytoken`, `Symbol Details`, `Exchange Instrument type`, `Minimum lot size`, `Tick size`, `ISIN`, `Trading Session`, `Last update date`, `Expiry date`, `Symbol ticker`, `Exchange`, `Segment`, `Scrip code`, `Underlying symbol`, `Underlying scrip code`, `Strike price`, `Option type`, `Underlying FyToken`, `Reserved column` (string), `Reserved column` (int), `Reserved column` (float).

`Option type` is `CE` or `PE` for options and the literal `XX` for everything else. `Expiry date` applies only to derivative rows. The change log entry dated 22 Mar 2024 says two reserved columns were appended and should be ignored; the table above already shows three reserved columns, so treat every column past `Underlying FyToken` as reserved and ignore it by position. VERIFY at ingest time whether the CSV ships a header row (the docs call the table "File Headers" but never show a raw line), and pin the parser to positional indices with a column count assertion so a future appended column cannot silently shift your fields.

### JSON files (richer, prefer these)

- NSE CD: `https://public.fyers.in/sym_details/NSE_CD_sym_master.json`
- NSE FO: `https://public.fyers.in/sym_details/NSE_FO_sym_master.json`
- NSE COM: `https://public.fyers.in/sym_details/NSE_COM_sym_master.json`
- NSE CM: `https://public.fyers.in/sym_details/NSE_CM_sym_master.json`
- BSE CM: `https://public.fyers.in/sym_details/BSE_CM_sym_master.json`
- BSE FO: `https://public.fyers.in/sym_details/BSE_FO_sym_master.json`
- MCX COM: `https://public.fyers.in/sym_details/MCX_COM_sym_master.json`

Shape: a single JSON object whose KEY is the symbol ticker (for example `NSE:SBIN-EQ`) and whose value is the metadata object. Fields:

`fyToken`, `isin`, `exSymbol`, `symDetails`, `symTicker`, `exchange` (int), `segment` (int), `exSymName`, `exToken` (int), `exSeries` (CM only), `optType` (`CE`, `PE` or `XX`), `underSym`, `underFyTok`, `exInstType` (int), `minLotSize` (int), `tickSize` (float), `tradingSession` (IST), `lastUpdate` (`YYYY-MM-DD`), `expiryDate` (timestamp), `strikePrice` (float), `qtyFreeze`, `tradeStatus` (1 active, 0 inactive), `currencyCode`, `upperPrice`, `lowerPrice`, `faceValue`, `qtyMultiplier`, `previousClose`, `previousOi`, `asmGsmVal`, `exchangeName` (`NSE`, `BSE`, `MCX`), `symbolDesc`, `originalExpDate` (documented as "kindly ignore"), `is_mtf_tradable`, `mtf_margin`, `stream`, `isCasEligible` (boolean, absent means not eligible).

The JSON gives everything the CSV gives plus `underSym`, `underFyTok`, `exSeries`, `qtyFreeze`, `qtyMultiplier`, `previousOi`, circuit limits and `stream`. For a project that wants maximum metadata, ingest the JSON. Note that `expiryDate` here is a timestamp while the CSV `Expiry date` is described as a string; normalise both to a date in Asia/Kolkata at ingest.

Critical limitation: the symbol master contains only CURRENTLY LISTED instruments. Expired contracts disappear from it. So the master is the registry of underlyings and of live contract metadata (lot size, tick size, freeze quantity), but it can NEVER be used to look up an expired option contract's metadata after the fact. That is the structural reason ExpiryManager must persist a full metadata snapshot for every contract it ever downloads, and the reason the symbol parser is load bearing rather than a convenience.

Operational recommendation: schedule a daily symbol master refresh before the session (Fyers publishes updated files each morning; `lastUpdate` tells you the vintage), store each day's snapshot, and keep a slowly changing dimension of lot size and tick size by root and expiry so that historical backtests use the lot size that was in force at the time.

## 6. Underlying symbols

### 6.1 Exact strings for the four seed underlyings

The underlying (spot or cash) instrument ticker is what you pass to `expiry-dates`, `underlying-symbols`, `futures-chain`, `options-chain-v3` and to the plain `history` endpoint. The derivative root is the different, shorter token that appears inside contract symbols.

| Underlying | Cash or spot instrument ticker | Derivative root in contract symbols | Exchange code | Cash segment | Derivative segment |
|---|---|---|---|---|---|
| NIFTY 50 | `NSE:NIFTY50-INDEX` | `NIFTY` | 10 NSE | 10 | 11 |
| NIFTY BANK | `NSE:NIFTYBANK-INDEX` | `BANKNIFTY` | 10 NSE | 10 | 11 |
| SENSEX | `BSE:SENSEX-INDEX` | `SENSEX` | 12 BSE | 10 | 11 |
| RELIANCE | `NSE:RELIANCE-EQ` | `RELIANCE` | 10 NSE | 10 | 11 |

`NSE:NIFTY50-INDEX`, `NSE:NIFTYBANK-INDEX`, `BSE:SENSEX-INDEX` and `NSE:RELIANCE-EQ` all appear verbatim in the docs. The root mapping `NIFTY50-INDEX` to `NIFTY` and `NIFTYBANK-INDEX` to `BANKNIFTY` is DERIVED but it is directly supported: the option chain sample returns `"ex_symbol": "NIFTY"` alongside `"symbol": "NSE:NIFTY50-INDEX"`, the futures chain sample returns `NSE:NIFTY26SEPFUT` for input `NSE:NIFTY50-INDEX`, and the expiry dates sample returns `"symbol": "SBIN"` for input `NSE:SBIN-EQ`. Contract samples `NSE:BANKNIFTY25NOV58900PE` and `NSE:BANKNIFTY25MARFUT` confirm the `BANKNIFTY` root, and `BSE:SENSEX2381161000CE` confirms the `SENSEX` root.

Two API behaviours worth hardcoding into the client:

- The expired data endpoints echo the normalised root back in `data.symbol`. Input `NSE:SBIN-EQ` yields `"symbol": "SBIN"`. That echo is a free, authoritative root resolver: call `expiry-dates` once for any new underlying and read `data.symbol` to learn the exact root string used in its contract symbols. This is the recommended discovery path and it costs one request.
- The option chain response carries `ex_symbol` on the underlying row, which is the same root.

Related indices seen in the docs, useful for the "add your own underlying" picker: `NSE:INDIAVIX-INDEX`, `NSE:NIFTYMIDSELECT-INDEX`, `NSE:NIFTYHEALTHCARE-INDEX`. Roots seen in contract or subscription samples: `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `SENSEX`, `SBIN`, `IOC`, `TCS`, `ABB`, `ABCAPITAL`, `CRUDEOIL`, `CRUDEOILM`, `GOLD`, `GOLDPETAL`, `SILVERM`, `SILVERMIC`, `USDINR`, `GBPINR`.

### 6.2 How a user discovers the correct string for a NEW underlying

Recommended flow, entirely offline except for the last confirmation step:

1. Ingest `NSE_FO_sym_master.json` and `BSE_FO_sym_master.json` (add `MCX_COM` and `NSE_CD` later if the user wants them). Group the rows by `underSym`. The distinct set of `underSym` values IS the complete list of underlyings that currently have listed derivatives. This is the search index for the "add underlying" screen.
2. For each distinct `underSym`, take any of its rows' `underFyTok` and look that fytoken up in the Capital Market master (`NSE_CM_sym_master.json` or `BSE_CM_sym_master.json`) by `fyToken`. The matching row's `symTicker` is the exact cash or spot instrument ticker to pass to the APIs, for example `NSE:NIFTY50-INDEX`. Its `exInstType` tells you whether it is an index (`10`) or equity (`0`), and `symDetails` gives the human readable name for the UI.
3. Persist the resulting pair (root, cash ticker) plus exchange, segment, instrument type, lot size and tick size in SQLite as the underlying registry.
4. Confirm with one live call before enabling downloads: `GET /data/history/fno/expired/expiry-dates` with the cash ticker and a short range. If `s` is `ok`, the underlying is valid and `data.symbol` gives the authoritative root. If the symbol is wrong, the API returns error code `-300` (invalid symbol).

This procedure requires no guessing and no hardcoded index name table. It also naturally handles the awkward cases: the mapping `NIFTYBANK-INDEX` to `BANKNIFTY` is data, not a rule.

Fallback for a user who wants to type a ticker manually: search the CM master by `symDetails` and `exSymbol` substring, present the matching `symTicker` values, and run step 4 as validation.

### 6.3 URL encoding

Error code `-300` in `04-request-response-structure.md` carries an explicit note: symbols containing special characters such as `M&M` must be URL encoded (`M%26M`) when building direct URLs; the vendor SDKs do this automatically. Since ExpiryManager will call the REST endpoints directly with httpx rather than through the vendor SDK, every symbol must go through `urllib.parse.quote` before being placed in a query string. `&` in a raw query string will otherwise truncate the parameter and produce a confusing invalid symbol error. The same applies to the colon in `NSE:SBIN-EQ`, which the doc samples send raw but which should be percent encoded for safety.

## 7. The parsing strategy

### 7.1 Design principle: parse to verify, not to discover

In the ExpiryManager pipeline, contract symbols are always obtained from `GET /data/history/fno/expired/underlying-symbols`, which is called with a known underlying and a known `expiry_date`. So at parse time the pipeline ALREADY knows the correct root and the correct expiry date. The parser's job is to decompose the strike, the option right and the encoding, and to prove that the decomposition is consistent with what was requested. Any disagreement is a hard error that quarantines the row rather than a value to be trusted.

Design the parser to accept optional `expected_root` and `expected_expiry` hints. With both hints supplied the parse is completely deterministic and every ambiguity below disappears. Without hints (for example when a user pastes a symbol into the UI), fall back to the enumeration and scoring algorithm, and surface an explicit ambiguity error rather than guessing.

### 7.2 Alphabets

```
MONTHLY_MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                  "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

WEEKLY_MONTHS  = {"1":1,"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,
                  "O":10,"N":11,"D":12}
```

`WEEKLY_MONTHS` has no `0` key. Reject any candidate whose month character is `0`.

### 7.3 Classification order

Split once on the first colon: everything before it is the exchange, everything after it is the body. The exchange is one of `NSE`, `BSE`, `MCX`; anything else is an error.

Then classify the body:

1. Ends with `FUT` and full-matches the futures pattern: FUTURE.
2. Ends with `CE` or `PE` and a valid option decomposition exists: OPTION.
3. Contains a hyphen and neither of the above matched: CASH. Split on the LAST hyphen: the tail is the series, the head is the exchange symbol. Series `INDEX` means an index; anything else (`EQ`, `BE`, `A`, `T`, `XT`, ...) means a tradable scrip.
4. Otherwise: unparseable, quarantine.

Do the FUT and option checks before the hyphen check, because a cash ticker can legitimately end in `CE` or `PE` inside its name but will always carry a `-SERIES` suffix. Do the hyphen split from the RIGHT, because exchange symbols can themselves contain hyphens (`BAJAJ-AUTO` on NSE). Splitting on the first hyphen would give series `AUTO`.

### 7.4 Futures

```
^(?P<root>.+?)(?P<yy>\d{2})(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)FUT$
```

Applied with `re.fullmatch`. The lazy `root` plus the literal `FUT` anchor plus the month alternation make this safe even when the root ends in digits: for `NIFTYNXT5026MARFUT` the engine backtracks until `root=NIFTYNXT50`, `yy=26`, `mon=MAR`. Futures symbols never encode a day; the expiry date must come from the request or from the fytoken.

### 7.5 Monthly coded options

```
^(?P<root>.+?)(?P<yy>\d{2})(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?P<strike>\d+(?:\.\d+)?)(?P<opt>CE|PE)$
```

Applied with `re.fullmatch`. Worked traces:

- `SBIN25MAR320CE`: root `SBIN`, yy `25`, mon `MAR`, strike `320`, opt `CE`.
- `SENSEX5025NOV25000CE` (hypothetical SENSEX 50 contract, shown to exercise the digit suffixed root): the lazy root first tries `SENSEX`, then needs two digits and a month at `5025NOV`, gets `50` then `25N` which is not a month, backtracks, and settles on root `SENSEX50`, yy `25`, mon `NOV`, strike `25000`. Correct without any special case.
- `MARUTI25MAR11000CE`: the `MAR` inside `MARUTI` does not trap the engine, because the month must be immediately preceded by exactly two digits and immediately followed by a pure numeric strike and the option suffix. A regex that merely SEARCHES for a month substring anywhere would match `MAR` at offset 0 and produce garbage. Always use a fully anchored match, never `re.search`.

### 7.6 Weekly coded options: enumeration, not a single regex

The weekly tail `{YY}{M}{dd}{Strike}` is (almost) all digits, so there is no lexical boundary between the end of the root and the start of the date, and none between the end of the day and the start of the strike once the root position is fixed. A single lazy regex returns the SHORTEST root, which is frequently wrong. Enumerate instead.

Algorithm:

1. Let `body` be the symbol after the exchange prefix, and require the last two characters to be `CE` or `PE`. Let `core` be `body` without that suffix.
2. For every split position `i` from `len(core) - 1` down to `1`, let `root = core[:i]` and `tail = core[i:]`.
3. Reject unless `len(tail) >= 6` (2 for year, 1 for month, 2 for day, at least 1 for strike).
4. Reject unless `tail[0:2]` are both digits, `tail[2]` is in `WEEKLY_MONTHS`, `tail[3:5]` are both digits, and `tail[5:]` matches `\d+(?:\.\d+)?`.
5. Reject unless `root` matches `^[A-Z0-9&_.\-]+$` and starts with a letter.
6. Build the candidate date from `2000 + int(tail[0:2])`, `WEEKLY_MONTHS[tail[2]]`, `int(tail[3:5])`, and reject unless it is a real calendar date (use `datetime.date`, which rejects 31 February and similar).
7. Reject unless the year is inside a configured plausibility window. Data availability starts 03 Jan 2022 for NSE and MCX and 07 Aug 2023 for BSE, and the longest dated live contract seen in the docs is a NIFTY option expiring 31 Dec 2030, so the window should be 2015 to (current year + 5), computed at runtime rather than frozen. This window is load bearing, not cosmetic: widening it re-introduces genuine ambiguity (see the `BSE:SENSEX2381161000CE` trace below), so it belongs in configuration with a comment explaining the consequence.
8. Reject unless the strike parses as a positive Decimal.
9. Score the surviving candidates: exact match on `expected_expiry` first (this alone resolves everything in the pipeline path), then `root in known_roots`, then longest root, then most plausible year.
10. If the top score is held by exactly one candidate, return it. If it is held by two or more, raise an ambiguity error and quarantine the symbol with all candidates recorded. Never silently pick the first.

Worked traces:

- `NSE:NIFTY2510923000CE`. core `NIFTY2510923000`. The winning split is root `NIFTY`, tail `2510923000`: yy `25`, month char `1` (January), dd `09`, strike `23000`, right `CE`. Expiry 2025-01-09, weekly coded. A competing split at root `NIFTY2` gives tail `510923000`: yy `51`, month char `0` which is not in the alphabet, rejected. Root `NIFTY25` gives tail `10923000`: yy `10`, month `9`, dd `23`, strike `000` which is zero, rejected on the positive strike rule and on the year window.
- `NSE:BANKNIFTY25MAR52000PE`. This is monthly coded and never reaches the weekly path: root `BANKNIFTY`, yy `25`, mon `MAR`, strike `52000`, right `PE`. The expiry day is unknown from the symbol; take it from the request `expiry_date`.
- `BSE:SENSEX2381161000CE`. core `SENSEX2381161000`. Winner: root `SENSEX`, yy `23`, month char `8` (August), dd `11`, strike `61000`, right `CE`. Expiry 2023-08-11, weekly coded, exchange BSE (code 12), segment 11. This is the symbol that proves the enumeration is necessary. A competing split at root `SENSEX2` gives yy `38`, month char `1` (January), dd `16`, strike `1000`, which is the perfectly well formed date 2038-01-16. Both parses are syntactically valid. Two independent guards kill the wrong one: the root registry (`SENSEX` is a known root, `SENSEX2` is not) and the year plausibility window (2038 is beyond current year plus 5). Worse, the "longest root wins" tiebreak alone would pick the WRONG answer here, since `SENSEX2` is longer than `SENSEX`. Length is therefore the LAST tiebreak, never the first, and a tie at the top of the scoring must raise rather than guess. A third split at root `SENSEX23` gives yy `81`, rejected by the window.
- `NSE:NIFTY2632423050CE`. Winner: root `NIFTY`, yy `26`, month char `3` (March), dd `24`, strike `23050`. Confirmed independently by the option chain sample, which pairs this symbol with `"strike_price": 23050` and fytoken `101126032462574` whose expiry field is `260324`.
- `NSE:GBPINR20N0580.5PE`. Winner: root `GBPINR`, yy `20`, month char `N` (November), dd `05`, strike `80.5`, right `PE`. Decimal strike, letter month, currency derivatives segment (code 12).
- `NSE:NIFTY20O0811000CE`. Root `NIFTY`, yy `20`, month char `O` (letter O, October), dd `08`, strike `11000`. The letter O is the only non digit in the tail.
- `NSE:NIFTY20D1025000CE`. Root `NIFTY`, yy `20`, month `D` (December), dd `10`, strike `25000`.

### 7.7 Edge cases a naive regex gets wrong

1. **Roots that contain another root as a suffix.** `BANKNIFTY`, `FINNIFTY` and `MIDCPNIFTY` all end with `NIFTY`. Any root matching that uses substring search or a right anchored root lookup will classify `NSE:BANKNIFTY2510952000CE` as NIFTY with stray leading text. Root matching must be anchored to the start of the body, and the root registry must be tried longest first.
2. **Roots that end in digits.** `NIFTYNXT50` and `SENSEX50` exist. A pattern like `^([A-Z]+)(\d{2})` splits `NIFTYNXT` plus year `50`, which then fails or, worse, accidentally validates. Enumerate splits and score, or match with a lazy root plus a strong right anchor.
3. **Roots that contain a month name.** `MARUTI`, `MARICO`, `AUGUST`-like names, `DECCAN`-like names. A month alternation used with `re.search` matches inside the name. Use fully anchored matches only.
4. **Letter O versus digit 0 in the weekly month slot.** October is the LETTER `O`. A tokenizer that uppercases and then treats the tail as an integer will corrupt October, November and December contracts. Also beware any code path that runs OCR-like normalisation or that strips non digits from the tail.
5. **Decimal strikes.** `80.5`, `75.5` appear in currency options and can appear in commodity. A `\d+` strike pattern silently truncates and produces a wrong strike, and float arithmetic then makes strikes like `80.5` compare unequal. Parse with `decimal.Decimal`, store the raw strike substring alongside the numeric value, and use a DECIMAL or scaled integer column, never a float, in DuckDB.
6. **Zero or short strikes.** After a bad split the strike substring can be `000` or empty. Require at least one digit and a strictly positive value.
7. **Hyphenated cash tickers.** `NSE:BAJAJ-AUTO-EQ` style tickers exist on NSE. Split the cash form on the LAST hyphen. VERIFY separately whether such roots keep the hyphen inside their derivative symbols (that is, whether the FO ticker is `BAJAJ-AUTO25OCTFUT` or `BAJAJAUTO25OCTFUT`) by grepping `NSE_FO.csv` at ingest; the docs do not say, and the answer changes the permitted character class for the root.
8. **Ampersand in roots.** `M&M`, `M&MFIN`, `J&KBANK`. The root character class must include `&`, and every outbound URL must percent encode it (`%26`) or the API returns `-300`.
9. **Monthly coded symbols have no day.** Never infer the day from a weekday rule. See section 3.2.
10. **Weekly coded does not mean weekly cycle.** See section 3.1.
11. **Two digit years.** `{YY}` is ambiguous across centuries by construction. Fix the century at 2000 and bound the year with a plausibility window; the data itself only starts in 2022, so this is safe for the lifetime of the project.
12. **Exchange prefix split.** Split on the FIRST colon only. Use `partition(":")`, not `split(":")` without a limit.

### 7.8 Reference implementation

```python
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

MONTHLY_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
WEEKLY_MONTHS = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
    "7": 7, "8": 8, "9": 9, "O": 10, "N": 11, "D": 12,
}
EXCHANGE_CODES = {"NSE": 10, "MCX": 11, "BSE": 12}

_MON = "|".join(MONTHLY_MONTHS)
RE_FUT = re.compile(rf"^(?P<root>.+?)(?P<yy>\d{{2}})(?P<mon>{_MON})FUT$")
RE_OPT_MONTHLY = re.compile(
    rf"^(?P<root>.+?)(?P<yy>\d{{2}})(?P<mon>{_MON})"
    rf"(?P<strike>\d+(?:\.\d+)?)(?P<opt>CE|PE)$"
)
RE_ROOT = re.compile(r"^[A-Z][A-Z0-9&_.\-]*$")

# Load these from config. The window is what breaks ties between two otherwise
# valid weekly decompositions, so widening it silently costs determinism.
MIN_YEAR = 2015
MAX_YEAR = date.today().year + 5


class SymbolParseError(ValueError):
    pass


class AmbiguousSymbolError(SymbolParseError):
    pass


@dataclass(frozen=True)
class ParsedSymbol:
    raw: str
    exchange: str            # NSE, BSE, MCX
    exchange_code: int       # 10, 11, 12
    instrument_class: str    # CASH, INDEX, FUTURE, OPTION
    root: str                # derivative root, or the cash exchange symbol
    series: Optional[str]    # cash only: EQ, BE, A, INDEX, ...
    expiry: Optional[date]   # None for cash, None for monthly coded unless resolved
    expiry_year: Optional[int]
    expiry_month: Optional[int]
    encoding: Optional[str]  # MONTHLY_CODED, WEEKLY_CODED
    strike: Optional[Decimal]
    strike_raw: Optional[str]
    option_type: Optional[str]  # CE, PE


def _year(yy: str) -> int:
    return 2000 + int(yy)


def _plausible(y: int) -> bool:
    return MIN_YEAR <= y <= MAX_YEAR


def parse_symbol(
    symbol: str,
    known_roots: Optional[Iterable[str]] = None,
    expected_root: Optional[str] = None,
    expected_expiry: Optional[date] = None,
) -> ParsedSymbol:
    """Decompose a Fyers symbol.

    Hints exist because the download pipeline always knows the underlying and the
    expiry it asked for, which collapses every weekly split ambiguity.
    """
    exchange, sep, body = symbol.strip().upper().partition(":")
    if not sep or exchange not in EXCHANGE_CODES:
        raise SymbolParseError(f"bad exchange prefix in {symbol!r}")
    roots = {r.upper() for r in (known_roots or ())}
    if expected_root:
        roots.add(expected_root.upper())

    m = RE_FUT.fullmatch(body)
    if m and RE_ROOT.fullmatch(m["root"]):
        y = _year(m["yy"])
        if _plausible(y):
            return ParsedSymbol(
                raw=symbol, exchange=exchange,
                exchange_code=EXCHANGE_CODES[exchange],
                instrument_class="FUTURE", root=m["root"], series=None,
                expiry=expected_expiry, expiry_year=y,
                expiry_month=MONTHLY_MONTHS[m["mon"]],
                encoding="MONTHLY_CODED", strike=None, strike_raw=None,
                option_type=None,
            )

    if body.endswith(("CE", "PE")):
        m = RE_OPT_MONTHLY.fullmatch(body)
        if m and RE_ROOT.fullmatch(m["root"]):
            y = _year(m["yy"])
            if _plausible(y) and Decimal(m["strike"]) > 0:
                return ParsedSymbol(
                    raw=symbol, exchange=exchange,
                    exchange_code=EXCHANGE_CODES[exchange],
                    instrument_class="OPTION", root=m["root"], series=None,
                    expiry=expected_expiry, expiry_year=y,
                    expiry_month=MONTHLY_MONTHS[m["mon"]],
                    encoding="MONTHLY_CODED",
                    strike=Decimal(m["strike"]), strike_raw=m["strike"],
                    option_type=m["opt"],
                )
        return _parse_weekly_option(
            symbol, exchange, body, roots, expected_expiry
        )

    if "-" in body:
        head, _, series = body.rpartition("-")
        if head and series:
            return ParsedSymbol(
                raw=symbol, exchange=exchange,
                exchange_code=EXCHANGE_CODES[exchange],
                instrument_class="INDEX" if series == "INDEX" else "CASH",
                root=head, series=series, expiry=None, expiry_year=None,
                expiry_month=None, encoding=None, strike=None,
                strike_raw=None, option_type=None,
            )

    raise SymbolParseError(f"unrecognised symbol shape {symbol!r}")


def _parse_weekly_option(symbol, exchange, body, roots, expected_expiry):
    core, option_type = body[:-2], body[-2:]
    candidates = []
    for i in range(len(core) - 1, 0, -1):
        root, tail = core[:i], core[i:]
        if len(tail) < 6 or not RE_ROOT.fullmatch(root):
            continue
        yy, mc, dd, strike_raw = tail[:2], tail[2], tail[3:5], tail[5:]
        if not (yy.isdigit() and dd.isdigit()):
            continue
        month = WEEKLY_MONTHS.get(mc)
        if month is None:
            continue
        if not re.fullmatch(r"\d+(?:\.\d+)?", strike_raw):
            continue
        year = _year(yy)
        if not _plausible(year):
            continue
        try:
            expiry = date(year, month, int(dd))
        except ValueError:
            continue
        strike = Decimal(strike_raw)
        if strike <= 0:
            continue
        # Root length is deliberately the weakest signal: for SENSEX2381161000CE
        # the longer root SENSEX2 yields a valid but wrong 2038 parse.
        score = (
            expected_expiry is not None and expiry == expected_expiry,
            root in roots,
            len(root),
        )
        candidates.append((score, root, expiry, strike, strike_raw))

    if not candidates:
        raise SymbolParseError(f"no valid weekly decomposition for {symbol!r}")
    best = max(c[0] for c in candidates)
    winners = [c for c in candidates if c[0] == best]
    if len(winners) > 1:
        raise AmbiguousSymbolError(
            f"{symbol!r} has {len(winners)} equally plausible parses: "
            + ", ".join(f"{w[1]}/{w[2]}/{w[3]}" for w in winners)
        )
    _, root, expiry, strike, strike_raw = winners[0]
    return ParsedSymbol(
        raw=symbol, exchange=exchange, exchange_code=EXCHANGE_CODES[exchange],
        instrument_class="OPTION", root=root, series=None, expiry=expiry,
        expiry_year=expiry.year, expiry_month=expiry.month,
        encoding="WEEKLY_CODED", strike=strike, strike_raw=strike_raw,
        option_type=option_type,
    )
```

Golden test corpus (every one of these strings is taken verbatim from the vendor documentation, so they belong in the unit test file):

```
NSE:SBIN-EQ                 NSE:MODIRUBBER-BE        BSE:MODIRUBBER-T
NSE:NIFTY50-INDEX           NSE:NIFTYBANK-INDEX      BSE:SENSEX-INDEX
NSE:INDIAVIX-INDEX          NSE:NIFTYMIDSELECT-INDEX BSE:BIOGEN-XT
NSE:NIFTY20OCTFUT           NSE:BANKNIFTY25MARFUT    BSE:SENSEX23AUGFUT
NSE:NIFTY26SEPFUT           MCX:CRUDEOILM26MARFUT    MCX:GOLDPETAL26FEBFUT
NSE:NIFTY20OCT11000CE       NSE:BANKNIFTY20NOV25000PE
NSE:SBIN25MAR320PE          BSE:SENSEX23AUG60400CE   NSE:NIFTY25MAR23000CE
NSE:ABCAPITAL23JUL190CE     MCX:CRUDEOIL20OCT4000CE  MCX:GOLD20DEC40000PE
NSE:NIFTY2010811000CE       NSE:NIFTY20O0811000CE    NSE:NIFTY20D1025000CE
BSE:SENSEX2381161000CE      BSE:SENSEX2640976500CE   NSE:NIFTY2292217000CE
NSE:NIFTY2632423050CE       NSE:NIFTY2632423150PE
NSE:USDINR20OCT75CE         NSE:GBPINR20NOV80.5PE
NSE:USDINR20O0875CE         NSE:GBPINR20N0580.5PE    NSE:USDINR20D1075CE
```

Expected decompositions for the four contracts the task named explicitly:

| Symbol | Exchange | Ex code | Segment | Root | Expiry | Encoding | Strike | Right |
|---|---|---|---|---|---|---|---|---|
| `NSE:NIFTY2510923000CE` | NSE | 10 | 11 | NIFTY | 2025-01-09 | WEEKLY_CODED | 23000 | CE |
| `NSE:BANKNIFTY25MAR52000PE` | NSE | 10 | 11 | BANKNIFTY | month only, 2025-03, day from request | MONTHLY_CODED | 52000 | PE |
| `BSE:SENSEX2381161000CE` | BSE | 12 | 11 | SENSEX | 2023-08-11 | WEEKLY_CODED | 61000 | CE |
| `NSE:GBPINR20N0580.5PE` | NSE | 10 | 12 | GBPINR | 2020-11-05 | WEEKLY_CODED | 80.5 | PE |

Segment assignment is DERIVED: it comes from the root's own instrument class (equity derivatives 11 for index and stock roots, currency derivatives 12 for currency pairs, commodity derivatives 20 for MCX), which should be read from the symbol master `segment` field for live contracts and copied forward onto expired contracts by root.

### 7.9 Metadata columns to persist per contract

Derive and store all of these so the Phase 2 backtesting engine never has to re-parse:

`raw_symbol`, `exchange`, `exchange_code`, `segment_code`, `instrument_class`, `exchange_instrument_type`, `root`, `underlying_instrument_symbol`, `underlying_fytoken`, `expiry_date`, `expiry_year`, `expiry_month`, `expiry_day`, `expiry_dow`, `symbol_expiry_encoding`, `expiry_cycle`, `strike` (DECIMAL), `strike_raw` (TEXT), `option_type`, `fytoken` (TEXT, when known), `exchange_token`, `lot_size`, `tick_size`, `qty_freeze`, `isin`, `trading_session`, `symbol_description`, `first_seen_at`, `last_seen_at`, `source_expiry_date_requested`, `parse_confidence`, `parse_warnings`.

`source_expiry_date_requested` is the value passed to the Get Expired Contracts call. Keeping it separate from the parsed `expiry_date` is what makes the pipeline auditable: a mismatch between the two is a data quality alarm, not something to be papered over.

## 8. Historical data for the underlying itself

The plain History API serves indices and equities with the identical call shape used for anything else. This is what backs the "underlying equity and index data" requirement.

**Endpoint**: `GET https://api-t1.fyers.in/data/history`

**Header**: `Authorization: app_id:access_token` (that is `api_id:access_token`, a single colon separated string, CONFIRMED in `04-request-response-structure.md`).

Request attributes (CONFIRMED, `23-data-api.md`):

| Attribute | Type | Notes |
|---|---|---|
| `symbol` | string | Mandatory. `NSE:NIFTY50-INDEX`, `NSE:NIFTYBANK-INDEX`, `BSE:SENSEX-INDEX`, `NSE:RELIANCE-EQ`. |
| `resolution` | string | `5S`, `10S`, `15S`, `30S`, `45S`, `1`, `2`, `3`, `5`, `10`, `15`, `20`, `30`, `60`, `120`, `240`, `D` or `1D`, `1W`, `1M`. Note that the History table lists `45` for minutes only in the expired data endpoint; the live History table omits 45 but includes 20 and 30. |
| `date_format` | int | `0` for epoch, `1` for `yyyy-mm-dd`. |
| `range_from` | string | Start, epoch or `yyyy-mm-dd` per `date_format`. |
| `range_to` | string | End, same encoding. |
| `cont_flag` | int | Set to `1` for continuous data and future options. Relevant for building continuous futures series. |
| `oi_flag` | int | Set to `1` to include open interest as part of the candle. |

Limits (CONFIRMED):

- Unlimited number of instruments per day.
- Up to 100 days per request for the 1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 120, 180 and 240 minute resolutions. Data available from 3 July 2017.
- Up to 366 days per request for `1D`, `1W` and `1M`.
- Seconds resolutions: only the last 30 trading days.
- Week and Month resolutions were added on 27 Mar 2026 per the change log.

Response: `{"s": "ok", "candles": [[epoch, open, high, low, close, volume], ...]}`. With `oi_flag=1` an open interest element is appended.

Timestamp semantics (DERIVED from the doc samples, and important enough to encode in tests): daily candle timestamps are midnight UTC, for example `1621814400` is 2021-05-24T00:00:00Z. Intraday timestamps are the true epoch of the candle open in UTC, for example the expired data sample's `1742960700` is 2025-03-26T03:45:00Z which is 09:15 IST, the NSE session open. Store epochs as UTC integers in DuckDB and convert to Asia/Kolkata only for display and for date bucketing. Do not assume the daily timestamp is IST midnight.

Partial candle rule (CONFIRMED): to receive only completed candles, set `range_to` to at least one resolution period before the current time. The doc's example: at 12:10:20 request `range_from` 12:08:00 and `range_to` 12:09:20 to receive the completed 12:08 and 12:09 one minute candles. The scheduler must apply this offset per resolution or it will persist a truncated final bar.

Practical notes for indices:

- Index candles carry no meaningful volume (the field is present and typically zero) and no open interest. Do not treat a zero volume index bar as missing data.
- `NSE:INDIAVIX-INDEX` is available the same way and is worth capturing alongside NIFTY for volatility research.

Companion endpoints for the underlying, all on the same base and the same auth header:

- `GET /data/quotes?symbols=` accepts up to 50 comma separated symbols and returns last price, day OHLC, previous close, ask, bid, spread, average traded price, volume, `fyToken` and `tt`. Good for a live snapshot row next to the historical chart.
- `GET /data/depth?symbol=&ohlcv_flag=1` accepts exactly one symbol and returns bids, asks, OHLC, circuit limits, `expiry`, `oi`, `pdoi` and `oipercent`.
- `GET /data/options-chain-v3?symbol=&strikecount=&greeks=1` returns the live chain plus `expiryData` with the `W` or `M` `expiry_flag`, plus IndiaVIX, plus per strike delta, gamma, theta, vega and iv, plus `fp` (future price). `strikecount` maximum is 50. This is the only documented source of the authoritative weekly versus monthly expiry flag and of live greeks, so it is worth scheduling a daily snapshot even though Phase 1 is about expired data.
- `GET /data/futures-chain?symbol=` lists the live futures contracts for an underlying with `fyToken`, last price, change and `expiry` as an epoch. The first element is the underlying itself with an empty `expiry`. Useful for building the futures continuation map.

One documentation defect to be aware of: the option chain cURL sample uses `symbol=NSE%3ANIFTY50-EQ`, with the `-EQ` series on an index. Every other reference, including the response body of that same sample, uses `NSE:NIFTY50-INDEX`. Treat `NSE:NIFTY50-EQ` as a typo in the docs, not as an accepted alias.

## 9. Ambiguities and open items to settle before coding

1. Whether hyphenated NSE roots such as `BAJAJ-AUTO` retain the hyphen in derivative tickers. Resolve by grepping `NSE_FO.csv` at first ingest. It decides the root character class.
2. Whether the symbol master CSV files ship a header row. The docs describe the columns but never show a raw line. The JSON files are unambiguous, so prefer them and treat the CSV as a fallback.
3. Whether the expired contracts endpoint accepts the derivative root (`NIFTY`) as well as the cash ticker (`NSE:NIFTY50-INDEX`). The docs only ever show the cash ticker as input. Use the cash ticker.
4. Whether MCX or currency weekly coded options exist in practice. The appendix has a currency weekly row but no commodity weekly row.
5. Whether `expiry_flag` (`W` or `M`) is available anywhere for expired contracts. Currently it is not, so `expiry_cycle` for historical rows must be derived and labelled as derived.
6. Exact `underSym` values for BSE indices (`SENSEX`, `BANKEX`, `SENSEX50`) and their `underFyTok` mappings. Read them from `BSE_FO_sym_master.json` at ingest rather than hardcoding.
