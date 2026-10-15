"""Read-only profiling of supplied ALDO CSVs; emits aggregate evidence, no customer IDs."""
from pathlib import Path
import hashlib
import json
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'dataset'
OUT = ROOT / 'docs' / 'evidence'
OUT.mkdir(parents=True, exist_ok=True)
frames = {}
report = {'files': {}, 'joins': {}}
for path in sorted(DATA.glob('*.csv')):
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    frames[path.name] = df
    entry = {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
             'rows': len(df), 'columns': list(df.columns),
             'blank_counts': {c: int(df[c].str.strip().eq('').sum()) for c in df},
             'exact_duplicate_rows': int(df.duplicated().sum()), 'categories': {}}
    for c in df:
        if c in ['store_banner','Banner','return_channel','last return status','reason','Is item final sale?',
                 'Exchange_Confidence','restocking revenue']:
            entry['categories'][c] = {str(k):int(v) for k,v in df[c].value_counts().head(30).items()}
        if c in ['return_creation_date','order_date_est','transactions_businessdaydate']:
            dates=pd.to_datetime(df[c], errors='coerce')
            entry[c]={'min':str(dates.min()),'max':str(dates.max()),'invalid':int(dates.isna().sum())}
        if c in ['Refund amount','item return qty','restocking revenue','Exchange QTY','Sales','Requested_QTY','Shipped']:
            nums=pd.to_numeric(df[c],errors='coerce')
            entry[c]={'sum':float(nums.sum()),'min':float(nums.min()),'max':float(nums.max()),'invalid':int(nums.isna().sum()),'negative':int(nums.lt(0).sum())}
    report['files'][path.name]=entry

returns=frames['OMS Data_Feb24-Jan26_new.csv']
orders=frames['All OMS orders_Feb 2024 to Feb 2026.csv']
flags=frames['McGill - Returns - Returnable Flag 2024-2025.csv']
fees=frames['McGill - Returns - Restocking Fees 2024 - 2025.csv']
exchanges=frames['McGill_Instore_return_exchanges.csv']

def key_stats(df, cols):
    counts=df.groupby(cols,dropna=False).size()
    return {'unique_keys':len(counts),'duplicate_key_groups':int(counts.gt(1).sum()),'max_rows_per_key':int(counts.max())}

for name,df,cols in [('returns_order_sku',returns,['order_number','sku']),('returns_return_sku',returns,['return_order_number','sku']),
                     ('orders_order_sku',orders,['order_number','sku']),('flags_order_sku',flags,['order_number','sku']),
                     ('fees_return_number',fees,['return_order_number'])]:
    report['joins'][name]=key_stats(df,cols)
for name,other,cols in [('return_to_flag',flags,['order_number','sku']),('return_to_order',orders,['order_number','sku']),
                       ('return_to_fee',fees,['return_order_number'])]:
    left=pd.MultiIndex.from_frame(returns[cols]); right=pd.MultiIndex.from_frame(other[cols].drop_duplicates())
    match=left.isin(right)
    report['joins'][name]={'matched_return_rows':int(match.sum()),'total_return_rows':len(returns),'fraction':float(match.mean())}
report['joins']['flag_conflicting_keys']=int(flags.groupby(['order_number','sku'])['Is item final sale?'].nunique().gt(1).sum())
report['joins']['fee_conflicting_keys']=int(fees.groupby('return_order_number')['restocking revenue'].nunique().gt(1).sum())
report['joins']['exchange_to_original_order']={'matched_exchange_rows':int(exchanges['Original Order Number'].isin(orders['order_number']).sum()),'total_exchange_rows':len(exchanges)}
report['return_unique_orders']=int(returns['order_number'].nunique())
report['return_unique_transactions']=int(returns['return_order_number'].nunique())
report['shipped_unique_orders']=int(orders['order_number'].nunique())
(OUT/'data-profile.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')

joined = returns.merge(orders, on=['order_number', 'sku'], how='inner', validate='many_to_one')
joined = joined.merge(flags, on=['order_number', 'sku'], how='left', validate='many_to_one')
fit = returns['reason'].str.contains('too big|too small', case=False, regex=True)
ca = returns[returns['store_banner'].eq('ALDO_CA')]
cohort = {
    'fit_size_reason_rows': int(fit.sum()),
    'fit_size_fraction_all_return_rows': float(fit.mean()),
    'joined_orders_and_flags_nonblank': int(joined['Is item final sale?'].notna().sum()),
    'joined_banner_mismatch': int(joined['store_banner'].ne(joined['Banner']).sum()),
    'return_dates_before_order_dates': int(pd.to_datetime(joined['return_creation_date']).lt(pd.to_datetime(joined['order_date_est'])).sum()),
    'aldo_ca': {
        'rows': len(ca),
        'completed_rows': int(ca['last return status'].eq('RETURN_COMPLETED').sum()),
        'fit_size_rows': int(ca['reason'].str.contains('too big|too small', case=False, regex=True).sum()),
        'raw_refund_sum_no_status_filter': float(pd.to_numeric(ca['Refund amount']).sum()),
    },
}
(OUT/'cohort-profile.json').write_text(json.dumps(cohort, ensure_ascii=False, indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))
