# Agentic Returns Gateway impact model: fit-related returns, ALDO_CA

## Slide summary

| Headline number | Value | Tag | Basis |
| --- | ---: | --- | --- |
| Size-related return lines (Too small + Too big) | 13,317 | measured | 21.48% of 61,998 completed ALDO_CA return lines, 2025-02-01 to 2026-01-31. Lines, not customers. |
| Annual net value, post-purchase exchange (base case) | CAD 115,754.34 | pending_measurement | Range CAD 40,912.80 (low) to CAD 193,525.62 (high). Rests on assumed 10/20/30% exchange conversion and a placeholder agent cost (pending_measurement). |
| Pre-purchase prevention what-if (base case) | CAD 50,604.60 | assumed | 5% of the measured size-related lines prevented. Range CAD 0.00 (0%) to CAD 101,209.20 (10%). Pilot hypothesis, separate lever, not additive. |

Key assumptions

- Exchange conversion of size-related lines 10/20/30% and prevention before purchase 0/5/10% (low/base/high, assumed). Both are assumptions to validate in a pilot.
- Value per converted or retained case CAD 54.00 and avoided return cost CAD 22.00 (assumed, synthetic unit economics from handoff section 15).
- Repeat-return reduction on converted cases 0/5/10% (assumed).
- Agent cost per run CAD 0.5000 is a placeholder (pending_measurement) until the live eval.
- Fit lines are return lines, not customers.
- Currency: CAD assumed for ALDO_CA, unconfirmed.

**CAD assumed for ALDO_CA, unconfirmed.** The source files have no currency column.

Window: 2025-02-01 to 2026-01-31 (365 days, 12 months). Scope: store_banner ALDO_CA, last return status RETURN_COMPLETED.

## Headline

13,317 of 61,998 completed return lines in scope (both measured) have a fit reason (Too small or Too big). These are lines, not customers. Annual value below rests on assumed conversion rates (validate in a pilot) and a placeholder agent cost (provisional).

| Item | Low | Base | High | Tag |
| --- | ---: | ---: | ---: | --- |
| Gross annual value (before agent cost) | CAD 71,911.80 | CAD 146,753.34 | CAD 224,524.62 | derived |
| Agent cost (total requests x cost per run) | CAD 30,999.00 | CAD 30,999.00 | CAD 30,999.00 | pending_measurement |
| Annual net value | CAD 40,912.80 | CAD 115,754.34 | CAD 193,525.62 | pending_measurement |

## How to read this

Every number carries a tag.

| Tag | Meaning |
| --- | --- |
| measured | Counted or summed directly from the provided CSV files. |
| derived | Calculated from measured numbers and/or assumptions with the formula shown. |
| assumed | An input chosen by the team, or a what-if result built only from such inputs and a measured baseline. It is not observed in ALDO data and must be validated in a pilot. |
| pending_measurement | Placeholder until the live eval measures it (docs/eval/results.json). |

- Fit lines are return lines, not customers.
- Exchange conversion rates are assumptions to validate in a pilot. The fit share of returns is not a conversion rate.
- The value per converted case is incremental economic value versus a refund (synthetic unit economics from the handoff), not gross margin.

## 1. Measured from the returns file

| Metric | Value | Tag | Note |
| --- | ---: | --- | --- |
| Return lines in scope | 61,998 | measured | One line is one CSV row. The source has duplicate (order, SKU) groups, so a line is not guaranteed to be an independent return unit. |
| Fit-reason lines (Too small + Too big) | 13,317 | measured | Return lines, not customers. |
| Fit lines: Too small | 5,840 | measured |  |
| Fit lines: Too big | 7,477 | measured |  |
| Fit lines as share of all lines in scope | 21.48% | derived | Not an exchange conversion rate. |
| Refund amount of fit lines, sum | CAD 1,150,050.58 | measured | Column summed as provided. Context for the funnel, not an input to the annual-value formula and not a realized loss. Line versus transaction grain is unconfirmed. |
| Refund amount of fit lines, median | CAD 79.99 | measured |  |
| Distinct return numbers in scope | 49,404 | measured | Several lines can share one return number. |
| Distinct return numbers with a fit line | 11,300 | measured |  |

## 2. Daily volume (scale)

| Metric | Value | Tag | Note |
| --- | ---: | --- | --- |
| Calendar days in window | 365 | derived |  |
| Days with at least one line | 365 | measured |  |
| Lines per day, mean | 169.86 | measured | Mean over all calendar days in the window (days without lines count as 0). |
| Lines per day, p95 | 450.80 | measured | 95th percentile of the daily counts, linear interpolation between closest ranks. |
| Lines per day, busiest day | 638 | measured |  |
| Peak multiplier (p95 / mean) | 2.65x | derived |  |

## 3. In-store exchange evidence

| Metric | Value | Tag | Note |
| --- | ---: | --- | --- |
| CONFIRMED in-store exchanges, all stores in file | 42,565 | measured | Replacement sale recorded inside the return transaction. |
| ASSUMED in-store exchanges, all stores in file | 14,946 | measured | Inferred when a separate in-store sale followed within five minutes. Never merge with CONFIRMED. |
| CONFIRMED in-store exchanges, stores attributed to ALDO_CA | 12,853 | derived | The exchange file has no banner column and covers several banners. Attribution maps Return store to the banner seen for the same in-store return location in the returns file. |
| ASSUMED in-store exchanges, stores attributed to ALDO_CA | 4,802 | derived |  |
| Exchange rows in stores attributed to other banners | 39,504 | derived |  |
| Exchange rows whose store could not be attributed to a banner | 352 | derived |  |
| Exchanged units (sum of Exchange QTY) in window | 57,511 | measured |  |

Behavioral context only: it shows exchanges already happen in store. It is not used in the dollar model, no exchange rate is computed and no causal effect is claimed.

## 4. Scenarios: annual value

Inputs

| Item | Low | Base | High | Tag |
| --- | ---: | ---: | ---: | --- |
| Exchange conversion of fit-reason lines | 10% | 20% | 30% | assumed |
| Repeat-return reduction on converted cases | 0% | 5% | 10% | assumed |
| Incremental value per converted case | CAD 54.00 | CAD 54.00 | CAD 54.00 | assumed |
| Avoided second-return cost | CAD 22.00 | CAD 22.00 | CAD 22.00 | assumed |
| Fit-reason lines (window) | 13,317 | 13,317 | 13,317 | measured |
| Agent runs charged (total return lines in window) | 61,998 | 61,998 | 61,998 | derived |
| Agent cost per run | CAD 0.5000 | CAD 0.5000 | CAD 0.5000 | pending_measurement |

Results

| Item | Low | Base | High | Tag |
| --- | ---: | ---: | ---: | --- |
| Converted cases | 1,332 | 2,663 | 3,995 | derived |
| Exchange value | CAD 71,911.80 | CAD 143,823.60 | CAD 215,735.40 | derived |
| Avoided second returns | 0 | 133 | 400 | derived |
| Avoided second-return value | CAD 0.00 | CAD 2,929.74 | CAD 8,789.22 | derived |
| Gross annual value (before agent cost) | CAD 71,911.80 | CAD 146,753.34 | CAD 224,524.62 | derived |
| Agent cost (total requests x cost per run) | CAD 30,999.00 | CAD 30,999.00 | CAD 30,999.00 | pending_measurement |
| Annual net value | CAD 40,912.80 | CAD 115,754.34 | CAD 193,525.62 | pending_measurement |

Formula:

```text
annual_net_value = fit_lines x exchange_conversion x value_per_converted_case
                 + fit_lines x exchange_conversion x repeat_return_reduction x avoided_second_return_cost
                 - total_requests x agent_cost_per_run
```

## 5. Agent cost input

Agent cost per run is a deliberately conservative placeholder of CAD 0.5000, tagged pending_measurement (file not found: `docs/eval/results.json`). It is not a measurement. Run the live eval, then re-run `python3.12 scripts/build_impact_model.py` so the cost becomes derived from measured tokens. The agent cost and annual net value are provisional until then.

## Pre-purchase prevention what-if (pilot hypothesis)

Pilot hypothesis against a measured baseline: sizing guidance before purchase could stop some size-related returns from happening at all. The baseline is measured; the prevention rates and unit values are assumptions to validate in a pilot. This is not a forecast.

Inputs

| Item | Low | Base | High | Tag |
| --- | ---: | ---: | ---: | --- |
| Baseline: size-related return lines (Too small + Too big), window | 13,317 | 13,317 | 13,317 | measured |
| Share of size-related return lines prevented before purchase | 0% | 5% | 10% | assumed |
| Avoided return cost per prevented case | CAD 22.00 | CAD 22.00 | CAD 22.00 | assumed |
| Value per retained case | CAD 54.00 | CAD 54.00 | CAD 54.00 | assumed |

Results (all tagged assumed)

| Item | Low | Base | High | Tag |
| --- | ---: | ---: | ---: | --- |
| Prevented return lines | 0 | 666 | 1,332 | assumed |
| Avoided return cost | CAD 0.00 | CAD 14,648.70 | CAD 29,297.40 | assumed |
| Retained-sale value | CAD 0.00 | CAD 35,955.90 | CAD 71,911.80 | assumed |
| Annual value (what-if) | CAD 0.00 | CAD 50,604.60 | CAD 101,209.20 | assumed |

Formula:

```text
annual_value = fit_lines x prevention_rate x (avoided_return_cost + value_per_retained_case)
```

This is a separate lever from section 4. Do not add the two together: a prevented return cannot also be converted to an exchange.

## 6. Caveats

- Fit lines are return lines, not customers. A customer can appear on several lines and some rows have no customer identifier (see the data profile), so customers cannot be counted from this file.
- Exchange conversion rates (10, 20 and 30 percent) are assumptions to validate in a pilot. The share of fit-reason lines is not a conversion rate and must not be used as one.
- CAD assumed for ALDO_CA, unconfirmed: the source files have no currency column.
- Value per converted case (CAD 54) comes from the synthetic unit economics in handoff section 15 (CAD 100 order, cost 40, refund path -82 versus exchange path -28). It is incremental economic value, not accounting gross profit and not ALDO-measured.
- Refund amounts are summed as provided. Whether the column is line or transaction grain is unconfirmed, so the sum is context for the funnel only and is not a realized loss or a revenue figure.
- Total requests is set to the number of return lines in scope (one agent run per line). This is conservative because one return can have several lines and the agent would not run on every line.
- The exchange file has no banner column. Store attribution to ALDO_CA is inferred from the returns file and is tagged derived. No exchange rate is computed: only about 26 percent of exchange rows match an original order (handoff section 5), so dividing by returns would be misleading.
- CONFIRMED means the replacement sale is inside the return transaction. ASSUMED is inferred from a separate in-store sale within five minutes. The two are reported separately and no causal effect is claimed.
- The window covers 12 months, so window totals are used as annual values without seasonality scaling.
- Pre-purchase prevention is a pilot hypothesis, not a forecast. The baseline (size-related return lines) is measured; the prevention rates (0, 5, 10 percent) and both unit values are assumptions to validate in a pilot, so every what-if result is tagged assumed.
- The prevention what-if is a separate lever and is not additive with the exchange-conversion scenarios: a prevented return cannot also be converted to an exchange. Do not add the two tables together.
- Per prevented case the what-if adds the avoided return cost (CAD 22) and the retained-sale value (CAD 54). Both come from the synthetic unit economics, and part of the CAD 54 may already include return handling cost, so the sum may overstate. No sizing-assistant operating cost is subtracted.
- The prevention baseline counts size-related lines from every return channel in the returns file. The share that pre-purchase guidance could reach is not measured.

## 7. Provenance and checks

- Filter: store_banner = 'ALDO_CA' AND last return status = 'RETURN_COMPLETED' AND return_creation_date in 2025-02-01..2026-01-31; fit line = reason equals 'too small' or 'too big' after strip, lower-case, whitespace collapse and removal of the 'return.page.reasons.' prefix.
- Returns file: `dataset/OMS Data_Feb24-Jan26_new.csv`, SHA-256 `76ebd5fb9cc7e16e13bb191ebdf1f5c916c295afe3289a6e913d800feb50d1a5`, 649,745 rows read, matches data profile: True.
- Exchange file: `dataset/McGill_Instore_return_exchanges.csv`, SHA-256 `3f5255834922aa15b79cb28247883345ae1548ec9c4da9b1d871c75d241e8e5b`, 61,934 rows read, business days 2025-02-02 to 2026-03-16, matches data profile: True.
- Eval results: `docs/eval/results.json`, status pending_measurement.
- Reconciliation with `docs/evidence/cohort-profile.json` (earlier pandas profile):
  - ALDO_CA rows, all statuses and dates: script 160,059, profile 160,059, match: True
  - ALDO_CA RETURN_COMPLETED rows, all dates: script 153,739, profile 153,739, match: True
  - ALDO_CA fit-reason rows, all statuses and dates: script 37,210, profile 37,210, match: True
