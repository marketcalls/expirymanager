-- 0004_underlyings: the four builtin underlyings and the seeded exchange holidays.
--
-- root here is the value the Fyers docs show. resolved_root_echo stays NULL until the first
-- expiry-dates call, and a mismatch between the two raises a notification rather than silently
-- overwriting what the user declared.
INSERT INTO underlying_registry (
    underlying_id, fyers_symbol, root, exchange, segment, instrument_kind, display_name,
    data_from, default_resolutions, include_oi, option_life_days, future_life_days,
    spot_contract_id, is_builtin, is_active, created_at, updated_at
) VALUES
    (1, 'NSE:NIFTY50-INDEX',   'NIFTY',     'NSE', 'CM', 'INDEX',  'Nifty 50',
     '2022-01-03', '["1","5","15","60"]', 1, 200, 400, 1, 1, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    (2, 'NSE:NIFTYBANK-INDEX', 'BANKNIFTY', 'NSE', 'CM', 'INDEX',  'Nifty Bank',
     '2022-01-03', '["1","5","15","60"]', 1, 200, 400, 2, 1, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    (3, 'BSE:SENSEX-INDEX',    'SENSEX',    'BSE', 'CM', 'INDEX',  'Sensex',
     '2023-08-07', '["1","5","15","60"]', 1, 200, 400, 3, 1, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    (4, 'NSE:RELIANCE-EQ',     'RELIANCE',  'NSE', 'CM', 'EQUITY', 'Reliance Industries',
     '2022-01-03', '["1","5","15","60"]', 1, 200, 400, 4, 1, 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'));

-- Trading holidays for NSE and BSE. Both exchanges publish the same equity and derivatives
-- holiday list, so the two sets are identical and are inserted from one source list.
--
-- Weekends are not listed: the calendar derives them. Days where only a Muhurat session traded
-- are listed as holidays, because the regular session was closed and the pipeline must not plan
-- a normal fetch for them.
--
-- MCX is deliberately not seeded. Its commodity sessions follow a different list and the four
-- builtin underlyings are NSE and BSE only. Callers must treat a missing exchange as unknown
-- rather than as a full trading year.
--
-- The 2026 rows are the fixed national holidays plus the calculable Good Friday and Holi. They
-- are a partial list and must be reconciled against the exchange circular for that year; every
-- row carries source 'seed' so a later authoritative import can replace them.
CREATE TEMP TABLE seed_holiday (holiday_date TEXT NOT NULL, description TEXT NOT NULL);
INSERT INTO seed_holiday VALUES
    ('2022-01-26', 'Republic Day'),
    ('2022-03-01', 'Mahashivratri'),
    ('2022-03-18', 'Holi'),
    ('2022-04-14', 'Dr Baba Saheb Ambedkar Jayanti and Mahavir Jayanti'),
    ('2022-04-15', 'Good Friday'),
    ('2022-05-03', 'Id-Ul-Fitr'),
    ('2022-08-09', 'Muharram'),
    ('2022-08-15', 'Independence Day'),
    ('2022-08-31', 'Ganesh Chaturthi'),
    ('2022-10-05', 'Dussehra'),
    ('2022-10-24', 'Diwali Laxmi Pujan'),
    ('2022-10-26', 'Diwali Balipratipada'),
    ('2022-11-08', 'Gurunanak Jayanti'),

    ('2023-01-26', 'Republic Day'),
    ('2023-03-07', 'Holi'),
    ('2023-03-30', 'Ram Navami'),
    ('2023-04-04', 'Mahavir Jayanti'),
    ('2023-04-07', 'Good Friday'),
    ('2023-04-14', 'Dr Baba Saheb Ambedkar Jayanti'),
    ('2023-05-01', 'Maharashtra Day'),
    ('2023-06-29', 'Bakri Id'),
    ('2023-08-15', 'Independence Day'),
    ('2023-09-19', 'Ganesh Chaturthi'),
    ('2023-10-02', 'Mahatma Gandhi Jayanti'),
    ('2023-10-24', 'Dussehra'),
    ('2023-11-14', 'Diwali Balipratipada'),
    ('2023-11-27', 'Gurunanak Jayanti'),
    ('2023-12-25', 'Christmas'),

    ('2024-01-22', 'Special holiday'),
    ('2024-01-26', 'Republic Day'),
    ('2024-03-08', 'Mahashivratri'),
    ('2024-03-25', 'Holi'),
    ('2024-03-29', 'Good Friday'),
    ('2024-04-11', 'Id-Ul-Fitr'),
    ('2024-04-17', 'Shri Ram Navami'),
    ('2024-05-01', 'Maharashtra Day'),
    ('2024-05-20', 'General Parliamentary Elections'),
    ('2024-06-17', 'Bakri Id'),
    ('2024-07-17', 'Muharram'),
    ('2024-08-15', 'Independence Day'),
    ('2024-10-02', 'Mahatma Gandhi Jayanti'),
    ('2024-11-01', 'Diwali Laxmi Pujan'),
    ('2024-11-15', 'Gurunanak Jayanti'),
    ('2024-12-25', 'Christmas'),

    ('2025-02-26', 'Mahashivratri'),
    ('2025-03-14', 'Holi'),
    ('2025-03-31', 'Id-Ul-Fitr'),
    ('2025-04-10', 'Shri Mahavir Jayanti'),
    ('2025-04-14', 'Dr Baba Saheb Ambedkar Jayanti'),
    ('2025-04-18', 'Good Friday'),
    ('2025-05-01', 'Maharashtra Day'),
    ('2025-08-15', 'Independence Day'),
    ('2025-08-27', 'Ganesh Chaturthi'),
    ('2025-10-02', 'Mahatma Gandhi Jayanti and Dussehra'),
    ('2025-10-21', 'Diwali Laxmi Pujan'),
    ('2025-10-22', 'Diwali Balipratipada'),
    ('2025-11-05', 'Prakash Gurpurb Sri Guru Nanak Dev'),
    ('2025-12-25', 'Christmas'),

    ('2026-01-26', 'Republic Day'),
    ('2026-03-04', 'Holi'),
    ('2026-04-03', 'Good Friday'),
    ('2026-05-01', 'Maharashtra Day'),
    ('2026-10-02', 'Mahatma Gandhi Jayanti'),
    ('2026-12-25', 'Christmas');

INSERT INTO market_holiday (exchange, holiday_date, description, source)
SELECT 'NSE', holiday_date, description, 'seed' FROM seed_holiday;

INSERT INTO market_holiday (exchange, holiday_date, description, source)
SELECT 'BSE', holiday_date, description, 'seed' FROM seed_holiday;

DROP TABLE seed_holiday;
