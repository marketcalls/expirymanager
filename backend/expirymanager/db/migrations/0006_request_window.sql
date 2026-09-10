-- 0006_request_window: raise the minute request window from 95 days to 100.
--
-- 0003 seeded 95 for every minute resolution and said why: the vendor documentation never states
-- whether its documented 100 day limit counts calendar days or trading days, so five days of
-- margin bought certainty at the cost of about five percent more requests.
--
-- That ambiguity is now settled by measurement rather than by reading. Probed against the live
-- API on 2026-09-09 with NSE:NIFTY2541722900CE at resolution 1, recorded in docs/API-PROBES.md:
-- a span of 100 calendar days between range_from and range_to answers HTTP 200, and a span of 101
-- answers HTTP 422 with vendor code -50. So the limit is calendar days, and the boundary is a hard
-- error rather than a silent truncation.
--
-- calendar.MAX_DAYS_PER_REQUEST was raised to 100 when the probe landed, but the planner does not
-- read that constant for minute resolutions: it reads this column. So the two disagreed, and the
-- database quietly won. Every minute chunk has been planned at 95 since, which is roughly 3,000
-- wasted requests on a full NIFTY backfill against a 100,000 per day budget.
--
-- 100 and not the measured 101 because the planner counts days inclusive while the probe measured
-- the span, so 100 inclusive leaves exactly one day of margin. That margin is deliberate and costs
-- under one percent: being one day over is a refused request, not a shorter answer.
--
-- The 5S row keeps 30. That is an availability window, not a request window: second resolution
-- data only exists for the last 30 trading days at all.

UPDATE ref_resolution
   SET max_days_per_request = 100
 WHERE is_intraday = 1
   AND fyers_code NOT LIKE '%S'
   AND max_days_per_request = 95;
