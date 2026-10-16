-- CTAS to Parquet (design "Architecture": Athena CTAS --> curated/parquet/...).
-- Builds a small, typed, de-BOM'd Parquet copy of the two tables the fit query needs, with
-- the reason normalised EXACTLY as scripts/build_fit_insights.py does so the counting query
-- that follows is a cheap group-by over clean columns.
--
-- Python normalize_reason(raw): text = raw.strip().lower(); if text.startswith(
-- "return.page.reasons.") drop that prefix; then " ".join(text.split()) (collapse ALL
-- whitespace runs to single spaces and trim). We reproduce that order precisely:
--   1. lower(raw)
--   2. trim                                  (strip; leading/trailing)
--   3. if it starts with the prefix, remove the prefix
--   4. regexp_replace(\s+ -> ' ') then trim  (collapse internal whitespace)
-- Step 4's trim also removes any space left when the prefix was followed by whitespace.
--
-- Placeholders ${DB} and ${BUCKET} are substituted by build_insights.py. The CTAS drops any
-- prior table of the same name first (build_insights.py issues the DROP) so the run is
-- idempotent; output goes under curated/parquet/<table>/.

CREATE TABLE ${DB}.curated_returns
WITH (
  format = 'PARQUET',
  parquet_compression = 'SNAPPY'
) AS
SELECT
  trim(store_banner)                                        AS store_banner,
  trim(orderitem_product_name)                              AS product_name,
  trim(regexp_replace(
    CASE
      WHEN trim(lower(reason)) LIKE 'return.page.reasons.%'
        THEN substr(trim(lower(reason)), length('return.page.reasons.') + 1)
      ELSE trim(lower(reason))
    END,
    '\s+', ' '
  ))                                                        AS reason_norm_raw
FROM ${DB}.raw_returns;

-- A thin typed copy of orders for phase-2 joins (not used by the fit query). Kept small.
CREATE TABLE ${DB}.curated_orders
WITH (
  format = 'PARQUET',
  parquet_compression = 'SNAPPY'
) AS
SELECT
  trim(banner)        AS banner,
  trim(order_number)  AS order_number,
  trim(sku)           AS sku,
  trim(style)         AS style,
  trim(size)          AS size
FROM ${DB}.raw_orders;
