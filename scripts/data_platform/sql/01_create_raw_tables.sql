-- External tables over the raw CSVs in s3://<bucket>/raw/<table>/ (design "Architecture").
-- OpenCSVSerDe, every column a string, header row skipped. The orders file was written by
-- the uploader as UTF-8 without a BOM (the uploader strips the BOM while streaming), so no
-- BOM handling is needed in the SerDe here.
--
-- Placeholders ${DB} and ${BUCKET} are substituted by build_insights.py before execution.
-- Only the columns the pipeline needs are declared; OpenCSVSerDe maps by position, so the
-- declared columns must stay in the file's column order and cover the leading columns used.

CREATE EXTERNAL TABLE IF NOT EXISTS ${DB}.raw_returns (
  store_banner string,
  order_number string,
  sku string,
  orderitem_product_name string,
  return_order_number string,
  return_channel string,
  return_creation_date string,
  return_creation_date_tmstmp string,
  last_return_status string,
  refund_amount string,
  item_return_qty string,
  reason string,
  instore_return_location string
)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '"', 'escapeChar' = '\\')
STORED AS TEXTFILE
LOCATION 's3://${BUCKET}/raw/returns/'
TBLPROPERTIES ('skip.header.line.count' = '1');

-- NOTE: the returns raw file has columns cnsmr_prof_sid and customer_uid DROPPED by the
-- uploader, so the positional layout above (store_banner, order_number, sku,
-- orderitem_product_name, return_order_number, ...) matches the scrubbed header.

CREATE EXTERNAL TABLE IF NOT EXISTS ${DB}.raw_orders (
  banner string,
  order_number string,
  order_date_est string,
  sku string,
  style string,
  size string,
  category_name string,
  requested_qty string,
  shipped string
)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '"', 'escapeChar' = '\\')
STORED AS TEXTFILE
LOCATION 's3://${BUCKET}/raw/orders/'
TBLPROPERTIES ('skip.header.line.count' = '1');
