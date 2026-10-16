-- Fit-insights counts, replicating scripts/build_fit_insights.py exactly over the Parquet
-- curated_returns table (reason already normalised in the CTAS as reason_norm_raw).
--
-- Builder rules reproduced:
--   * scope ALL   = every return line; scope ALDO_CA = lines with store_banner = 'ALDO_CA'.
--     A line with banner ALDO_CA is counted in BOTH scopes (the builder adds ALDO_CA rows to
--     ("ALL", "ALDO_CA") and every other row to ("ALL",)).
--   * too_small = reason_norm = 'too small'; too_big = reason_norm = 'too big'.
--   * total_returns per product = all return lines for that product in the scope.
--   * per-product counts EXCLUDE blank product_name; blank lines are still counted in the
--     scope totals (as lines_without_product_name).
--
-- Output: one row per (scope, product) for NON-blank products (row_kind='product'), PLUS one
-- row per scope (row_kind='total') carrying the scope grand totals including blank lines.
-- build_insights.py turns 'product' rows into `products` and 'total' rows into `totals`.
-- Signals and too_small_share are recomputed in Python with fit.classify / fit.small_share,
-- so this query never classifies.
--
-- Placeholder ${DB} is substituted by build_insights.py.

WITH scoped AS (
  SELECT 'ALL' AS scope, trim(product_name) AS product_name, reason_norm_raw AS reason_norm
  FROM ${DB}.curated_returns
  UNION ALL
  SELECT 'ALDO_CA' AS scope, trim(product_name) AS product_name, reason_norm_raw AS reason_norm
  FROM ${DB}.curated_returns
  WHERE store_banner = 'ALDO_CA'
),
products AS (
  SELECT
    'product'                             AS row_kind,
    scope,
    product_name,
    count(*)                              AS total_returns,
    count_if(reason_norm = 'too small')   AS too_small,
    count_if(reason_norm = 'too big')     AS too_big,
    CAST(0 AS bigint)                     AS lines_without_product_name,
    CAST(0 AS bigint)                     AS products_count
  FROM scoped
  WHERE product_name <> ''
  GROUP BY scope, product_name
),
totals AS (
  SELECT
    'total'                                        AS row_kind,
    scope,
    CAST(NULL AS varchar)                          AS product_name,
    count(*)                                       AS total_returns,
    count_if(reason_norm = 'too small')            AS too_small,
    count_if(reason_norm = 'too big')              AS too_big,
    count_if(product_name = '')                    AS lines_without_product_name,
    count(DISTINCT CASE WHEN product_name <> '' THEN product_name END) AS products_count
  FROM scoped
  GROUP BY scope
)
SELECT row_kind, scope, product_name, total_returns, too_small, too_big,
       lines_without_product_name, products_count
FROM products
UNION ALL
SELECT row_kind, scope, product_name, total_returns, too_small, too_big,
       lines_without_product_name, products_count
FROM totals
ORDER BY scope, row_kind, product_name;
