-- ---------------------------------------------------------------------------
-- 002_price_series: keep a compact close series for sparklines.
--
-- The aggregates in price_metrics answer "how much did it move", but not
-- "what did the move look like", and a shape is what a person reads a stock
-- by. Stored as a small array of weekly closes rather than a separate table:
-- it is written and read as one unit, never queried across rows, and ~52
-- floats is far cheaper to fetch than 250 rows of a join.
-- ---------------------------------------------------------------------------

ALTER TABLE price_metrics
    ADD COLUMN IF NOT EXISTS close_series jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN price_metrics.close_series IS
    'Up to 52 weekly closing prices in INR, oldest first. Powers the sparkline.';
