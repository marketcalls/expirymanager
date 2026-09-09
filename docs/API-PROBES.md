# Live API probes

Questions the Fyers documentation does not answer, settled by asking the live service. Run on
2026-09-09 against a real account token, under 30 requests in total.

Re-run these after any vendor change. The planner's request arithmetic depends on them.

## 1. Is the 100 day historical limit calendar days or trading days

**Calendar days, and the boundary is a hard error rather than a truncation.**

Measured against `NSE:NIFTY2541722900CE`, resolution 1, expiry 2025-04-17:

| Span between range_from and range_to | Result |
|---|---|
| 95 days | HTTP 200, 5794 candles |
| 100 days | HTTP 200, 5794 candles |
| 101 days | HTTP 422, `s: error`, code -50, "Invalid input" |
| 110, 125, 140 days | HTTP 422, same error |

Both accepted spans returned the same candle count, which is the contract's whole life: the limit
was never the binding constraint on the data, only on the request.

Consequences:

- The chunker may use 100 calendar days, not the 95 it used while this was ambiguous. On a full
  NIFTY backfill that is roughly 3,000 requests saved against a 100,000 per day budget.
- Exceeding it cannot be detected by inspecting the response, because there is no response. A
  chunker that overshoots produces a hard failure, so the chunk arithmetic has to be right rather
  than approximately right.
- `MAX_DAYS_PER_REQUEST` counts days INCLUSIVE while the probe measured the span, so 100 inclusive
  leaves one day of margin against the boundary being evaluated in another timezone.

## 2. Does the 366 day expiry-dates window truncate silently or error

**It errors. Nothing is silently dropped.**

Measured against `NSE:NIFTY50-INDEX`, range ending 2025-12-31:

| Span | Result |
|---|---|
| 366 days | HTTP 200, 54 option expiries, `from_date` and `to_date` echoed back unchanged |
| 400 days | HTTP 422, "Invalid input" |
| 500 days | HTTP 422, "Invalid input" |

The echoed `from_date` and `to_date` matched the request exactly, so the cross-check the client
performs against them is sound but will never fire on span alone.

## 3. Do the expired F&O endpoints cover MCX

**No. NSE and BSE only.**

Every MCX underlying form tried returned HTTP 422 with code -50, "Invalid input":

`MCX:CRUDEOIL-COM`, `MCX:GOLD-COM`, `MCX:CRUDEOIL`, `MCX:CRUDEOILM`, `MCX:GOLD`,
`MCX:SILVER-COM`, `MCX:NATURALGAS`

`BSE:SENSEX-INDEX` returned HTTP 200 in the same run, so the failures are the symbol being
rejected and not a broken token or a malformed request.

This is consistent with the endpoint paths, which carry `fno`. Consequences:

- MCX must not be offered in Add Underlying.
- The MCX row in the exchange availability floor table is retained but unreachable. Leave it: the
  cost is nothing and the vendor may extend coverage.
- An MCX symbol that reaches the client should fail fast with a message naming the real reason,
  rather than surfacing a bare "Invalid input" from upstream.

## 4. Is the contracts array capped for a wide index weekly

**No cap observed.**

`NSE:NIFTY50-INDEX` expiry 2025-04-17 returned 482 option contracts and 0 futures. 482 is not a
round number, so nothing suggests a limit was applied. Futures being empty is expected: NIFTY
futures are monthly, and this is a weekly options expiry.

Strikes ran from 16900 upward in 50 point steps, both rights present per strike.

## 5. The historical response envelope

Confirmed as the docs describe, and different from the rest of the API. On success the top level
carries `s`, `columns` and `candles` with no `data` wrapper and no `code` or `message`.

With `include_oi=1` the columns array is exactly:

```
["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
```

Fields must be mapped through that array rather than by position, since open interest is only
present when it was asked for.
