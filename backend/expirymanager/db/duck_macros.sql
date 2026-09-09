-- The shipped query vocabulary.
--
-- These live in the database itself rather than in Python so that a future backtester, an ad hoc
-- duckdb CLI session and the API all speak the same language and cannot drift. The chain
-- endpoints call these macros instead of reimplementing ATM selection in Python.
--
-- Applied idempotently after duck_schema.sql on every startup.

CREATE OR REPLACE MACRO bars(p_contract_id, p_res_id, p_from, p_to) AS TABLE
SELECT ts, open, high, low, close, volume, oi
  FROM candles
 WHERE contract_id = p_contract_id
   AND res_id      = p_res_id
   AND ts >= p_from AND ts < p_to
 ORDER BY ts;

-- Spot bars live in the same candles table as contracts of kind SPOT with reserved low ids, so
-- this is a scan of a handful of row groups at the front of the file rather than a second store.
CREATE OR REPLACE MACRO spot_at(p_underlying_id, p_res_id, p_ts) AS TABLE
SELECT k.ts, k.close
  FROM candles k
  JOIN dim_underlying u ON u.spot_contract_id = k.contract_id
 WHERE u.underlying_id = p_underlying_id
   AND k.res_id = p_res_id
   AND k.ts <= p_ts
 ORDER BY k.ts DESC
 LIMIT 1;

CREATE OR REPLACE MACRO atm_strike(p_underlying_id, p_expiry, p_res_id, p_ts) AS TABLE
WITH spot AS (SELECT close FROM spot_at(p_underlying_id, p_res_id, p_ts))
SELECT c.strike
  FROM dim_contract c, spot
 WHERE c.underlying_id = p_underlying_id
   AND c.expiry_date   = p_expiry
   AND c.option_type   = 'CE'
 ORDER BY abs(c.strike - spot.close)
 LIMIT 1;

-- The BETWEEN on contract_id is the whole justification for allocating ids in a contiguous
-- padded block per (underlying, expiry): it prunes on the leading sort key instead of doing
-- several hundred scattered lookups.
CREATE OR REPLACE MACRO chain_at(p_underlying_id, p_expiry, p_res_id, p_ts) AS TABLE
WITH ids AS (
    SELECT contract_id_lo AS lo, contract_id_hi AS hi
      FROM dim_expiry
     WHERE underlying_id = p_underlying_id AND expiry_date = p_expiry
)
SELECT c.strike, c.option_type, c.fyers_symbol, c.lot_size,
       k.open, k.high, k.low, k.close, k.volume, k.oi
  FROM candles k
  JOIN ids ON k.contract_id BETWEEN ids.lo AND ids.hi
  JOIN dim_contract c USING (contract_id)
 WHERE k.res_id = p_res_id
   AND k.ts     = p_ts
   AND c.kind   = 'OPT'
 ORDER BY c.strike, c.option_type;

-- Ids inside a block are ordered by strike, so a plus or minus N band around ATM is itself a
-- narrow contiguous id range and reads a fraction of the block.
CREATE OR REPLACE MACRO chain_window(p_underlying_id, p_expiry, p_res_id, p_from, p_to,
                                     p_strikes_each_side) AS TABLE
WITH atm AS (SELECT strike FROM atm_strike(p_underlying_id, p_expiry, p_res_id, p_from)),
     step AS (SELECT strike_step FROM dim_expiry
               WHERE underlying_id = p_underlying_id AND expiry_date = p_expiry),
     ids  AS (SELECT contract_id_lo AS lo, contract_id_hi AS hi FROM dim_expiry
               WHERE underlying_id = p_underlying_id AND expiry_date = p_expiry)
SELECT k.ts, c.strike, c.option_type, k.close, k.volume, k.oi
  FROM candles k
  JOIN ids ON k.contract_id BETWEEN ids.lo AND ids.hi
  JOIN dim_contract c USING (contract_id), atm, step
 WHERE k.res_id = p_res_id
   AND k.ts >= p_from AND k.ts < p_to
   AND c.kind = 'OPT'
   AND abs(c.strike - atm.strike) <= p_strikes_each_side * step.strike_step
 ORDER BY k.ts, c.strike, c.option_type;
