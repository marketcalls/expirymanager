-- 0003_reference: exchange, segment, instrument type and resolution reference data.
--
-- These are Fyers integer codes, not our own. They are seeded rather than hardcoded in Python so
-- that a plain SQLite session can join against them and see the same vocabulary the app uses.

CREATE TABLE ref_exchange (
    code                INTEGER PRIMARY KEY,   -- NSE 10, MCX 11, BSE 12. Not alphabetical.
    name                TEXT NOT NULL UNIQUE,
    data_available_from TEXT NOT NULL
);
INSERT INTO ref_exchange VALUES (10,'NSE','2022-01-03'),(11,'MCX','2022-01-03'),(12,'BSE','2023-08-07');

CREATE TABLE ref_segment (
    code INTEGER PRIMARY KEY,   -- 10 CM, 11 FO, 12 CD, 20 COM
    name TEXT NOT NULL UNIQUE
);
INSERT INTO ref_segment VALUES (10,'CM'),(11,'FO'),(12,'CD'),(20,'COM');

-- Instrument type integers are reused across segments, so a type is only meaningful joined with
-- its segment. That is why the primary key is the pair and not the type alone.
CREATE TABLE ref_instrument_type (
    segment_code INTEGER NOT NULL REFERENCES ref_segment(code),
    type_code    INTEGER NOT NULL,
    name         TEXT NOT NULL,
    PRIMARY KEY (segment_code, type_code)
);
INSERT INTO ref_instrument_type VALUES
    (10,  0, 'EQ'),
    (10,  9, 'ETF'),
    (10, 10, 'INDEX'),
    (11, 11, 'FUTIDX'),
    (11, 13, 'FUTSTK'),
    (11, 14, 'OPTIDX'),
    (11, 15, 'OPTSTK'),
    (12, 16, 'FUTCUR'),
    (12, 19, 'OPTCUR'),
    (20, 30, 'FUTCOM'),
    (20, 31, 'OPTFUT'),
    (20, 32, 'OPTCOM'),
    (20, 37, 'OPTFUT');

CREATE TABLE ref_resolution (
    fyers_code               TEXT PRIMARY KEY,
    res_id                   INTEGER NOT NULL UNIQUE,
    seconds                  INTEGER NOT NULL,
    label                    TEXT NOT NULL,
    chart_interval           TEXT NOT NULL,   -- the openalgo-charts interval code
    max_days_per_request     INTEGER NOT NULL,
    availability_window_days INTEGER,         -- 30 for second resolutions, NULL otherwise
    is_intraday              INTEGER NOT NULL DEFAULT 1
);

-- max_days_per_request is 95 for every minute code, deliberately under the documented 100,
-- because the docs do not say whether that limit is calendar or trading days.
-- res_id 100 is reserved for the derived daily series produced by v_candle_daily and is never
-- written by ingest, so it is deliberately absent here.
INSERT INTO ref_resolution
    (fyers_code, res_id, seconds, label, chart_interval, max_days_per_request, availability_window_days, is_intraday)
VALUES
    ('5S',    1,     5, '5 seconds',  '5s',  30,   30, 1),
    ('1',     2,    60, '1 minute',   '1m',  95, NULL, 1),
    ('2',     3,   120, '2 minutes',  '2m',  95, NULL, 1),
    ('3',     4,   180, '3 minutes',  '3m',  95, NULL, 1),
    ('5',     5,   300, '5 minutes',  '5m',  95, NULL, 1),
    ('10',    6,   600, '10 minutes', '10m', 95, NULL, 1),
    ('15',    7,   900, '15 minutes', '15m', 95, NULL, 1),
    ('20',    8,  1200, '20 minutes', '20m', 95, NULL, 1),
    ('30',    9,  1800, '30 minutes', '30m', 95, NULL, 1),
    ('45',   10,  2700, '45 minutes', '45m', 95, NULL, 1),
    ('60',   11,  3600, '1 hour',     '1h',  95, NULL, 1),
    ('120',  12,  7200, '2 hours',    '2h',  95, NULL, 1),
    ('180',  13, 10800, '3 hours',    '3h',  95, NULL, 1),
    ('240',  14, 14400, '4 hours',    '4h',  95, NULL, 1);
