# Agentic Returns Gateway impact model: fit-related returns, ALDO_CA

## Slide-ready sentences

Business impact

- Of 61,998 completed ALDO_CA return lines between 2025-02-01 and 2026-01-31, 13,317 (21.5%) gave a size reason, Too small or Too big (measured).
- If 10%, 20% or 30% of those size-related lines became exchanges, annual net value would be about CAD 57,000, CAD 132,000 or CAD 209,000 after agent cost (derived: conversion rates assumed, agent cost measured).
- Another 36,462 lines (58.8%) carry a change-of-mind or not-as-expected reason, but 91% of them use the combined label 'Changed my mind / Other', which sits on 72.5% of in-store lines against 9.5% of other lines, so this group is an upper bound (measured).
- Converting 5%, 10% or 15% of them to a colour or style exchange where the alternative is in stock (60% assumed) would add about CAD 43,000, CAD 88,000 or CAD 135,000 a year on top of the size-related exchanges (gross; the agent already runs on every line), or about CAD 8,000 in the base case if only the 3,139 lines with a specific reason count (assumed).
- ALDO_CA footwear earned a 64.18% gross margin in fiscal 2025 (72.94% on regular-price sales) and a fit-returned unit carries CAD 52.83 of margin, so one retained exchange pays for about 216 agent runs at CAD 0.2449 per run (derived: margin measured, agent cost measured).
- At the measured margin rate the size-related exchanges retain about CAD 46,000, CAD 92,000 or CAD 138,000 of gross margin a year (a conservative reading), and 288 retained exchanges a year (2.16% of size-related lines) would already pay the whole agent bill (derived: conversion rates assumed).

Scalability

- Because 19.1% of return numbers hold two or more lines (35.5% of lines), one agent run per return number instead of per line lowers the yearly runs from 61,998 to 49,404; at a measured CAD 0.2449 per run (6 live runs at assumed model prices) the annual agent cost falls from about CAD 15,000 to about CAD 12,000 (derived: counts measured).
- On the busiest day (638 lines) the agent would process about 35.5 million tokens, roughly 49,000 tokens per minute over a 12-hour window, with about 0.4 runs in flight on average in the busiest hour (15% of the day, assumed); check account quotas, request an increase before launch (assumed).

Figures in these sentences are rounded; the tables below keep the exact values and every number keeps its tag.

## Slide summary

| Headline number | Value | Tag | Basis |
| --- | ---: | --- | --- |
| Size-related return lines (Too small + Too big) | 13,317 | measured | 21.48% of 61,998 completed ALDO_CA return lines, 2025-02-01 to 2026-01-31. Lines, not customers. |
| Annual net value, post-purchase exchange (base case) | CAD 131,570.03 | derived | Range CAD 56,728.49 (low) to CAD 209,341.31 (high). Rests on assumed 10/20/30% exchange conversion and the measured agent cost per run. |
| Pre-purchase prevention what-if (base case) | CAD 50,604.60 | assumed | 5% of the measured size-related lines prevented. Range CAD 0.00 (0%) to CAD 101,209.20 (10%). Pilot hypothesis, separate lever, not additive. |
| Preference-return exchange what-if, added to the fit scenarios (base case, gross) | CAD 87,705.69 | assumed | 36,462 preference lines x 60% availability of the alternative colour x 10% conversion (both assumed) x CAD 38.99 per case, derived from the measured median refund, plus the avoided second-return value as in the fit scenarios. Range CAD 42,649.60 (5%) to CAD 135,168.28 (15%); floor CAD 7,550.55 if only the 3,139 lines with a specific reason count. Fit and preference lines never overlap: both levers together give CAD 219,275.72 net in the base case, agent cost counted once. |
| Return numbers with two or more lines | 19.06% | measured | 9,417 of 49,404 return numbers; they hold 22,011 of 61,998 lines (35.50%), up to 14 lines in one return. 99.19% of them contain two or more distinct SKUs. |
| Agent cost per run (measured) | CAD 0.2449 | derived | Mean of 6 live runs of the ALDO returns-agent scenarios at assumed model prices (prices in returns_domain.config): USD 0.1775 per run, converted at 1.38 CAD per USD (assumed, from returns_domain.config). Single scenarios range from CAD 0.0864 (H) to CAD 0.3956 (A). |

Key assumptions

- Exchange conversion of size-related lines 10/20/30% and prevention before purchase 0/5/10% (low/base/high, assumed). Both are assumptions to validate in a pilot.
- Value per converted or retained case CAD 54.00 and avoided return cost CAD 22.00 (assumed, synthetic unit economics from handoff section 15).
- Repeat-return reduction on converted cases 0/5/10% (assumed).
- Agent cost per run CAD 0.2449, measured on 6 live runs at assumed model prices (prices in returns_domain.config).
- Fit lines are return lines, not customers.
- Preference-return exchange: conversion 5/10/15% of preference lines where the alternative colour or style is in stock (60% availability), repeat-return reduction as in the fit scenarios, value per case derived from the measured median refund and the synthetic unit economics (all assumed, to validate in a pilot).
- Fit and preference lines never overlap (one reason group per line), so the two levers can be added; the pre-purchase prevention what-if stays separate.
- Agent load: a 12-hour active window and 15% of the busiest day in its busiest hour (assumed).
- Currency: CAD assumed for ALDO_CA, unconfirmed.

**CAD assumed for ALDO_CA, unconfirmed.** The source files have no currency column.

Window: 2025-02-01 to 2026-01-31 (365 days, 12 months). Scope: store_banner ALDO_CA, last return status RETURN_COMPLETED.

## Headline

13,317 of 61,998 completed return lines in scope (both measured) have a fit reason (Too small or Too big). These are lines, not customers. Annual value below rests on assumed conversion rates (validate in a pilot).

| Item | Low | Base | High | Tag |
| --- | ---: | ---: | ---: | --- |
| Gross annual value (before agent cost) | CAD 71,911.80 | CAD 146,753.34 | CAD 224,524.62 | derived |
| Agent cost (total requests x cost per run) | CAD 15,183.31 | CAD 15,183.31 | CAD 15,183.31 | derived |
| Annual net value | CAD 56,728.49 | CAD 131,570.03 | CAD 209,341.31 | derived |

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

Agent load (return lines per day are used as agent runs per day; assumptions are tagged assumed)

| Metric | Value | Tag | Note |
| --- | ---: | --- | --- |
| Agent runs per day, mean | 169.86 | derived | One agent run per return line, the same basis as the agent cost model. Counting runs per return number instead would lower the volume by about 20%. |
| Agent runs per day, p95 | 450.80 | derived |  |
| Agent runs per day, busiest day | 638 | derived |  |
| Active window of the busiest day | 12 h | assumed | Assumption: the day's returns arrive over 12 hours. |
| Share of the busiest day that falls in its busiest hour | 15% | assumed | Assumption (an even 12-hour spread would be 8.3%). |
| Agent runs in the busiest hour of the busiest day | 95.70 | assumed | Busiest-day runs x busiest-hour share. |
| Agent run duration (wall clock), mean of the scenario p50 latencies | 16.0 s | measured | Baseline eval, 6 scenarios, 6 runs; scenario p50 ranges from 10.4 s to 22.5 s. Reset and database reads are excluded. |
| Input tokens per run, mean | 54,794 | measured | Mean over the baseline eval scenarios; includes the prompt and tool specs resent on every model round. |
| Output tokens per run, mean | 871 | measured |  |
| Implied tokens per day, mean day | 9,455,205 | derived | Runs per day x (input + output tokens per run). |
| Implied tokens per day, p95 day | 25,094,007 | derived |  |
| Implied tokens per day, busiest day | 35,514,589 | derived |  |
| Tokens per minute on the busiest day over a 12-hour window | 49,326 | assumed | Average over the window; about 98% of it is input. Compare with the account's on-demand quota for the model: check account quotas, request an increase before launch. |
| Tokens per minute in the busiest hour of the busiest day | 88,786 | assumed | Busiest-hour runs x tokens per run / 60. Compare with the account's on-demand quota for the model: check account quotas, request an increase before launch. |
| Concurrent agent runs in the busiest hour, average | 0.43 | assumed | Little's law: runs per second x mean run duration. An average; bursts are higher. Not a load test. |

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
| Agent cost per run | CAD 0.2449 | CAD 0.2449 | CAD 0.2449 | derived |

Results

| Item | Low | Base | High | Tag |
| --- | ---: | ---: | ---: | --- |
| Converted cases | 1,332 | 2,663 | 3,995 | derived |
| Exchange value | CAD 71,911.80 | CAD 143,823.60 | CAD 215,735.40 | derived |
| Avoided second returns | 0 | 133 | 400 | derived |
| Avoided second-return value | CAD 0.00 | CAD 2,929.74 | CAD 8,789.22 | derived |
| Gross annual value (before agent cost) | CAD 71,911.80 | CAD 146,753.34 | CAD 224,524.62 | derived |
| Agent cost (total requests x cost per run) | CAD 15,183.31 | CAD 15,183.31 | CAD 15,183.31 | derived |
| Annual net value | CAD 56,728.49 | CAD 131,570.03 | CAD 209,341.31 | derived |

Formula:

```text
annual_net_value = fit_lines x exchange_conversion x value_per_converted_case
                 + fit_lines x exchange_conversion x repeat_return_reduction x avoided_second_return_cost
                 - total_requests x agent_cost_per_run
```

## 5. Agent cost input

Agent cost per run is CAD 0.2449, measured on 6 live runs at assumed model prices (prices in returns_domain.config). Source: `.agents/tasks/agentic-returns-gateway-p0-2026-10-15/eval-baseline-1/results.json` (field `est_cost_per_run_usd`). Tokens are measured by the live eval; the token prices are an assumption set in the eval config. The eval reports USD 0.1775 per run, converted at 1.38 CAD per USD (assumed; one source of truth: backend/returns_domain/config.py (USD_TO_CAD default)).

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

## Return reasons by group (all in-window lines)

All completed ALDO_CA return lines in the window, each in exactly one reason group. Counts, sums and medians come straight from the returns file; the grouping uses the product taxonomy.

| Group | Lines | Share of lines | Refund sum | Median refund | Returned in store | Tag |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Fit (Too small, Too big) | 13,317 | 21.48% | CAD 1,150,050.58 | CAD 79.99 | 53.14% | measured |
| Preference (changed mind or idea, not as expected, colour) | 36,462 | 58.81% | CAD 3,006,553.77 | CAD 74.98 | 89.96% | measured |
| Quality or damage | 2,236 | 3.61% | CAD 169,855.01 | CAD 67.49 | 46.60% | measured |
| Delivery or wrong item | 4,393 | 7.09% | CAD 307,725.50 | CAD 62.99 | 21.17% | measured |
| Other or unclear | 5,590 | 9.02% | CAD 433,111.88 | CAD 72.25 | 30.79% | measured |

Composition of the preference group

| Metric | Value | Tag | Note |
| --- | ---: | --- | --- |
| Preference lines with the combined label 'Changed my mind / Other' | 33,323 | measured | One label for change of mind and unspecified reasons. |
| Combined label as share of the preference lines | 91.39% | measured |  |
| Preference lines with a specific reason (not as expected, changed idea, found something else, colour, advertised) | 3,139 | measured | Preference lines without the combined label. Floor for the preference lever. |
| Lines returned in store (channel InStore) | 43,570 | measured |  |
| Lines returned in store, share of all lines in scope | 70.28% | measured |  |
| Lines with the combined label that were returned in store | 31,574 | measured |  |
| In-store lines that carry the combined label | 72.47% | measured | In store the label may be entered as a default, so it is weaker evidence of a taste-based return. |
| Lines from other channels (web, mail, call centre) that carry the combined label | 9.49% | measured |  |

Reason to group mapping (normalized reason text; the normalization is strip, lower-case, whitespace collapse and removal of the 'return.page.reasons.' prefix)

| Raw reason | Lines | In store | Taxonomy category | Group | Note |
| --- | ---: | ---: | --- | --- | --- |
| too big | 7,477 | 4,153 | FIT_SIZE | fit |  |
| too small | 5,840 | 2,924 | FIT_SIZE | fit |  |
| changed my mind / other | 33,305 | 31,574 | CHANGE_OF_MIND | preference | Combined label: change of mind and unspecified reasons share one option. Counted as preference because the product taxonomy maps it to CHANGE_OF_MIND, so the preference group is an upper bound. |
| not what i expected | 2,718 | 823 | EXPECTATION | preference |  |
| changed idea | 376 | 365 | CHANGE_OF_MIND | preference |  |
| found something else | 33 | 32 | CHANGE_OF_MIND | preference |  |
| changed my mind / ot | 18 | 0 | CHANGE_OF_MIND | preference | Truncated spelling of 'Changed my mind / Other' (see above). |
| not satisfied with the colour | 7 | 4 | EXPECTATION | preference |  |
| not as advertised | 3 | 2 | EXPECTATION | preference |  |
| ne répond pas aux at | 2 | 0 | EXPECTATION | preference |  |
| arrived in poor condition | 1,207 | 471 | QUALITY_DAMAGE | quality_damage |  |
| manufacturer defect | 844 | 498 | QUALITY_DAMAGE | quality_damage |  |
| warranty | 156 | 49 | QUALITY_DAMAGE | quality_damage |  |
| defective | 19 | 17 | QUALITY_DAMAGE | quality_damage |  |
| poor quality | 9 | 7 | QUALITY_DAMAGE | quality_damage |  |
| arrived in poor cond | 1 | 0 | QUALITY_DAMAGE | quality_damage |  |
| lost in transit | 2,338 | 0 | DELIVERY | delivery_wrong_item |  |
| wrong item delivered | 887 | 312 | DELIVERY | delivery_wrong_item |  |
| wrong size delivered | 537 | 417 | DELIVERY | delivery_wrong_item |  |
| return to sender | 323 | 0 | DELIVERY | delivery_wrong_item |  |
| arrived too late | 301 | 199 | DELIVERY | delivery_wrong_item |  |
| wrong item | 7 | 2 | DELIVERY | delivery_wrong_item |  |
| uncomfortable | 3,139 | 1,613 | COMFORT | other | Comfort complaint, fit-adjacent but not a size reason and not a preference: other. |
| return without merch | 1,868 | 0 | OTHER | other | Process reason, no product problem stated: other. |
| return extension | 456 | 0 | OTHER | other | Process reason, no product problem stated: other. |
| return without merchandise | 121 | 107 | OTHER | other | Process reason, no product problem stated: other. |
| trop grand | 3 | 0 | FIT_SIZE | other | French for 'too big'. Kept out of fit so that the fit group equals the earlier definition (Too small and Too big only): other. |
| inconfortable | 2 | 0 | COMFORT | other | French spelling of 'uncomfortable' (see above): other. |
| gift return | 1 | 1 | OTHER | other | Process reason, no product problem stated: other. |

Reasons placed in other because they are ambiguous or not a product problem: uncomfortable (3,139), return without merch (1,868), return extension (456), return without merchandise (121), trop grand (3), inconfortable (2), gift return (1).

backend/returns_domain/taxonomy.py categorize() gives the category; CATEGORY_TO_GROUP in scripts/build_impact_model.py folds categories into groups. The fit group stays identical to the earlier definition (Too small and Too big only).

Lines by month and channel

| Month | Returned in store | Other channels |
| --- | ---: | ---: |
| 2025-02 | 6,016 | 1,571 |
| 2025-03 | 3,291 | 1,361 |
| 2025-04 | 176 | 1,495 |
| 2025-05 | 181 | 1,609 |
| 2025-06 | 293 | 1,593 |
| 2025-07 | 311 | 1,676 |
| 2025-08 | 393 | 1,413 |
| 2025-09 | 3,144 | 1,174 |
| 2025-10 | 4,971 | 1,403 |
| 2025-11 | 6,164 | 1,545 |
| 2025-12 | 10,743 | 2,219 |
| 2026-01 | 7,887 | 1,369 |

- Check: Group lines sum to the return lines in scope: 61,998 versus 61,998, match True
- Check: Mapping rows sum to the return lines in scope: 61,998 versus 61,998, match True
- Check: Monthly channel mix sums to the return lines in scope: 61,998 versus 61,998, match True
- Check: Fit group lines equal the fit lines of the exchange scenarios (Too small + Too big): 13,317 versus 13,317, match True
- Check: Fit group refund sum equals the fit refund sum: 115,005,058 versus 115,005,058, match True
- Check: Fit group refund median equals the fit refund median: 7,999 versus 7,999, match True

## Preference-return exchange lever (what-if, assumed)

What-if on a measured baseline: some returns driven by taste (colour, style, change of mind) could end as an in-store or mail exchange for another colour or style in the same price band instead of a refund. Baseline lines and the median refund are measured; availability, conversion and the unit economics are assumptions to validate in a pilot. Not a forecast.

Additive by construction: each return line has exactly one reason group, so fit and preference lines never overlap and the two levers can be added. The combined rows add the gross values and subtract the agent cost once. The pre-purchase prevention what-if is a different lever and is not part of this total.

Inputs

| Item | Low | Base | High | Tag |
| --- | ---: | ---: | ---: | --- |
| Preference lines (baseline, window) | 36,462 | 36,462 | 36,462 | measured |
| Preference lines with a specific reason (floor baseline) | 3,139 | 3,139 | 3,139 | measured |
| Median refund of the preference lines (price of the typical returned item) | CAD 74.98 | CAD 74.98 | CAD 74.98 | measured |
| Availability of the alternative colour or style (same price band, customer's size) | 60% | 60% | 60% | assumed |
| Exchange conversion of preference lines where the alternative is available | 5% | 10% | 15% | assumed |
| Repeat-return reduction on converted cases | 0% | 5% | 10% | assumed |
| Item cost as share of price | 40% | 40% | 40% | assumed |
| Net extra handling of an exchange versus a refund | CAD 6.00 | CAD 6.00 | CAD 6.00 | assumed |
| Incremental value per converted case (from the measured median refund) | CAD 38.99 | CAD 38.99 | CAD 38.99 | derived |
| Avoided second-return cost | CAD 22.00 | CAD 22.00 | CAD 22.00 | assumed |
| Agent runs charged (total return lines in window) | 61,998 | 61,998 | 61,998 | derived |
| Agent cost per run | CAD 0.2449 | CAD 0.2449 | CAD 0.2449 | derived |

Results

| Item | Low | Base | High | Tag |
| --- | ---: | ---: | ---: | --- |
| Converted cases | 1,094 | 2,188 | 3,282 | assumed |
| Exchange value | CAD 42,649.60 | CAD 85,299.20 | CAD 127,948.80 | assumed |
| Avoided second returns | 0 | 109 | 328 | assumed |
| Avoided second-return value | CAD 0.00 | CAD 2,406.49 | CAD 7,219.48 | assumed |
| Gross annual value (before agent cost) | CAD 42,649.60 | CAD 87,705.69 | CAD 135,168.28 | assumed |
| Agent cost (total requests x cost per run) | CAD 15,183.31 | CAD 15,183.31 | CAD 15,183.31 | derived |
| Annual net value if this were the only lever | CAD 27,466.29 | CAD 72,522.38 | CAD 119,984.97 | assumed |
| Fit scenarios: gross annual value (reference) | CAD 71,911.80 | CAD 146,753.34 | CAD 224,524.62 | derived |
| Fit + preference: gross annual value | CAD 114,561.40 | CAD 234,459.03 | CAD 359,692.90 | assumed |
| Fit + preference: annual net value | CAD 99,378.09 | CAD 219,275.72 | CAD 344,509.59 | assumed |
| Memo: gross value if the flat CAD 54 of the fit scenarios were used per converted case | CAD 59,068.44 | CAD 120,543.37 | CAD 184,424.80 | assumed |
| Floor: gross value if only the lines with a specific reason count | CAD 3,671.69 | CAD 7,550.55 | CAD 11,636.58 | assumed |

Formula:

```text
preference_gross_value = preference_lines x alt_colour_availability x exchange_conversion x value_per_converted_case
                       + preference_lines x alt_colour_availability x exchange_conversion x repeat_return_reduction x avoided_second_return_cost
value_per_converted_case = (1 - item_cost_ratio) x median_refund_of_preference_lines - net_extra_handling
standalone_net  = preference_gross_value - total_requests x agent_cost_per_run
combined_net    = fit_gross_value + preference_gross_value - total_requests x agent_cost_per_run   (agent cost counted once)
```

## Multi-line returns (bundle)

| Metric | Value | Tag | Note |
| --- | ---: | --- | --- |
| Distinct return numbers in scope | 49,404 | measured |  |
| Return lines in scope | 61,998 | measured |  |
| Lines per return number, mean | 1.25 | measured | Return lines divided by distinct return numbers. |
| Lines per return number, maximum | 14 | measured |  |
| Return numbers with two or more lines | 9,417 | measured |  |
| Share of return numbers with two or more lines | 19.06% | measured |  |
| Lines that sit in a multi-line return | 22,011 | measured |  |
| Share of lines that sit in a multi-line return | 35.50% | measured |  |
| Multi-line returns that contain two or more distinct SKUs | 99.19% | measured | Checks that extra lines are different items, not repeats of the same SKU (the source has duplicate order and SKU groups). |

A customer who returns several items is one request, so one conversation, one quote, one shipment and one confirmation should cover all lines instead of one flow per line.

Agent runs: one per return number versus one per line

| Item | One run per return number | One run per line | Tag |
| --- | ---: | ---: | --- |
| Agent runs per year | 49,404 | 61,998 | derived |
| Agent cost per run | CAD 0.2449 | CAD 0.2449 | derived |
| Annual agent cost | CAD 12,099.04 | CAD 15,183.31 | derived |
| Annual agent cost saved versus one run per line | CAD 3,084.27 | CAD 0.00 | derived |

## Agent cost sensitivity

Cost per run is the input that moves. The measured mean mixes full exchange flows with refusals and is not the production mix; the most expensive measured scenario is a conservative case.

| Item | Measured mean of 6 live runs | Most expensive measured scenario (A) | Tag |
| --- | ---: | ---: | --- |
| Agent cost per run | CAD 0.2449 | CAD 0.3956 | derived |
| Cost per run as a multiple of the measured mean | 1.00x | 1.62x | derived |
| Annual agent cost (total requests x cost per run) | CAD 15,183.31 | CAD 24,526.41 | derived |
| Annual net value, fit scenarios, low case | CAD 56,728.49 | CAD 47,385.39 | derived |
| Annual net value, fit scenarios, base case | CAD 131,570.03 | CAD 122,226.93 | derived |
| Annual net value, fit scenarios, high case | CAD 209,341.31 | CAD 199,998.21 | derived |

Placeholder of CAD 0.50 per run, used before the live eval and about 2.0x the measured mean. Kept as a stress test, and so that the earlier base-case annual net value stays visible.

| Item | Placeholder before the live eval | Tag |
| --- | ---: | --- |
| Agent cost per run | CAD 0.5000 | assumed |
| Cost per run as a multiple of the measured mean | 2.04x | derived |
| Annual agent cost (total requests x cost per run) | CAD 30,999.00 | assumed |
| Annual net value, fit scenarios, low case | CAD 40,912.80 | assumed |
| Annual net value, fit scenarios, base case | CAD 115,754.34 | assumed |
| Annual net value, fit scenarios, high case | CAD 193,525.62 | assumed |

## Tier perk what-if (inputs only, assumed)

Inputs and cost bounds only. No revenue, retention or loyalty uplift is claimed: the break-even only states how many extra retained orders, valued at the model's retained-case value, would pay for the perk.

Inputs

| Item | Low | Base | High | Tag |
| --- | ---: | ---: | ---: | --- |
| Return shipping fee waived per use | CAD 5.95 | CAD 5.95 | CAD 5.95 | assumed |
| Uses per member per year, minimum | 1 | 1 | 1 | assumed |
| Uses per member per year, maximum | 12 | 12 | 12 | assumed |
| Platinum members per 100 customers | 1% | 2% | 3% | assumed |
| Platinum members per 10,000 customers | 100 | 200 | 300 | assumed |
| Value per retained order (average retained-case value of the model) | CAD 54.00 | CAD 54.00 | CAD 54.00 | assumed |

Results (all tagged assumed)

| Item | Low | Base | High | Tag |
| --- | ---: | ---: | ---: | --- |
| Perk cost per member per year, minimum uses | CAD 5.95 | CAD 5.95 | CAD 5.95 | assumed |
| Maximum perk cost per member per year | CAD 71.40 | CAD 71.40 | CAD 71.40 | assumed |
| Perk cost per 10,000 customers per year, minimum uses | CAD 595.00 | CAD 1,190.00 | CAD 1,785.00 | assumed |
| Maximum perk cost per 10,000 customers per year | CAD 7,140.00 | CAD 14,280.00 | CAD 21,420.00 | assumed |
| Break-even extra retained orders per member per year, minimum uses | 0.11 | 0.11 | 0.11 | assumed |
| Break-even extra retained orders per member per year, maximum uses | 1.32 | 1.32 | 1.32 | assumed |
| Break-even extra retained orders per 10,000 customers per year, minimum uses | 12 | 23 | 34 | assumed |
| Break-even extra retained orders per 10,000 customers per year, maximum uses | 133 | 265 | 397 | assumed |

Formula:

```text
max_perk_cost_per_member = fee_per_use x max_uses_per_year
perk_cost_per_10k_customers = 10,000 x platinum_share x perk_cost_per_member
break_even_extra_retained_orders = perk_cost / value_per_retained_order
```

## Gross margin lever (transaction file)

Margin version of the retained-sale value, next to the existing revenue-based numbers (unchanged). The rate and the margin per unit are measured from the 2025 ALDO transaction file; conversion rates and the CAD 54 per case remain the model's assumptions. Currency assumed CAD for ALDO_CA, unconfirmed.

Source: `docs/evidence/transaction-profile.json` (SHA-256 `f72662af956d428c056270e1d296d226a59fe0ff75f59fa018dcec663e344b15`), 8,701,520 rows read, banner ALDO_CA, business dates 2025-02-02 to 2026-01-31.

Measured margin facts

| Metric | Value | Tag | Note |
| --- | ---: | --- | --- |
| ALDO_CA footwear gross margin rate, all price groups | 64.18% | measured | Gross margin / retail on sales lines. Basis: ALDO_CA footwear (men's and ladies' categories), sales lines, all price groups, fiscal 2025 (business dates 2025-02-02 to 2026-01-31; the file is named 2025), 1,233,675 units sold. Returns lines are excluded from the rate. |
| ALDO_CA footwear gross margin rate, Regular price group | 72.94% | measured | Same basis, price_group Regular only (693,289 units). |
| Average gross margin per unit sold, Regular price | CAD 74.53 | measured | Currency assumed CAD, unconfirmed. |
| Average gross margin per unit sold, Markdown price group | CAD 23.29 | measured |  |
| Average gross margin per returned unit, fit returns (Too small, Too big) | CAD 52.83 | measured | 3,654 fit return lines (3,662 units) with a text reason in the transaction file (web channel). Margin the sale carried, lost on a refund and retained by an exchange. |
| Same, units returned from Regular price sales | CAD 70.86 | measured |  |
| Share of fit return lines that were Markdown price | 34.78% | measured |  |

Value per retained case

| Metric | Value | Tag | Note |
| --- | ---: | --- | --- |
| Existing value per retained case (unchanged) | CAD 54.00 | assumed | Handoff section 15 synthetic unit economics, as in the scenarios. |
| Existing value per retained case x measured margin rate | CAD 34.66 | derived | Literal product of the existing value and the measured rate. The CAD 54 is already net of an assumed item cost (40% of price), so applying a margin rate on top counts cost twice: read this as the conservative end. |
| Average gross margin per returned unit, fit returns (Too small, Too big) | CAD 52.83 | measured | 3,654 fit return lines (3,662 units) with a text reason in the transaction file (web channel). Margin the sale carried, lost on a refund and retained by an exchange. |

Retained-sale value: revenue-based (existing) next to margin-based

| Item | Low | Base | High | Tag |
| --- | ---: | ---: | ---: | --- |
| Fit exchange lever: retained-sale value, revenue-based (existing, unchanged) | CAD 71,911.80 | CAD 143,823.60 | CAD 215,735.40 | derived |
| Fit exchange lever: retained-sale value x margin rate (margin-based) | CAD 46,150.44 | CAD 92,300.87 | CAD 138,451.31 | derived |
| Fit exchange lever: memo, cases x measured margin per fit-returned unit | CAD 70,353.71 | CAD 140,707.42 | CAD 211,061.13 | derived |
| Preference exchange lever: retained-sale value, revenue-based (existing, unchanged) | CAD 42,649.60 | CAD 85,299.20 | CAD 127,948.80 | assumed |
| Preference exchange lever: retained-sale value x margin rate (margin-based) | CAD 27,371.00 | CAD 54,741.99 | CAD 82,112.99 | assumed |
| Preference exchange lever: memo, cases x measured margin per fit-returned unit | CAD 57,788.62 | CAD 115,577.25 | CAD 173,365.87 | assumed |
| Pre-purchase prevention: retained-sale value, revenue-based (existing, unchanged) | CAD 0.00 | CAD 35,955.90 | CAD 71,911.80 | assumed |
| Pre-purchase prevention: retained-sale value x margin rate (margin-based) | CAD 0.00 | CAD 23,075.22 | CAD 46,150.44 | assumed |
| Pre-purchase prevention: memo, cases x measured margin per fit-returned unit | CAD 0.00 | CAD 35,176.86 | CAD 70,353.71 | assumed |

Break-even

| Metric | Value | Tag | Note |
| --- | ---: | --- | --- |
| Margin earned by one retained exchange (average fit-returned unit) | CAD 52.83 | measured | Measured average margin of a fit-returned footwear unit, all price groups. |
| Agent cost per run | CAD 0.2449 | derived | Same cost per run as the scenarios. |
| Agent runs paid for by the margin of one retained exchange | 216 | derived | Margin per retained exchange / agent cost per run. |
| Retained exchanges per year that pay the whole agent bill (every return line costs one run) | 288 | derived | Annual agent cost CAD 15,183.31 / margin per retained exchange, rounded up. |
| Same, as share of the measured fit lines | 2.16% | derived | Against the 13,317 fit lines; the scenarios assume 10 to 30 percent. |
| Platinum perk cost per use (return shipping fee waived) | CAD 5.95 | assumed | Same fee as the tier perk block. |
| Perk uses paid for by the margin of one retained exchange | 8.9 | derived |  |
| Extra retained exchanges one perk use must cause to pay for itself | 0.113 | derived | Perk fee per use / margin per retained exchange. Cost bound only; no uplift is claimed. |

Markdown

ALDO_CA footwear units sold at Regular price earned on average CAD 74.53 of gross margin against CAD 23.29 at Markdown, a gap of CAD 51.24 per unit. If a returned unit would only be resold at a markdown, an exchange at full price protects about that much more margin. 35% of the fit returns were themselves Markdown-priced sales, where the margin at stake is lower (CAD 70.86 per unit for Regular-price fit returns).

A comparison of sales lines by price group in fiscal 2025. The file does not show what price a returned unit was later resold at, so the gap is a reference size, not an observed resale loss.

Formula:

```text
margin_based_value = existing_retained_sale_value x gross_margin_rate_footwear_ALDO_CA
memo_retained_margin = cases x measured_margin_per_fit_returned_unit
break_even_exchanges_per_year = total_requests x agent_cost_per_run / margin_per_retained_exchange
perk_break_even_exchanges_per_use = perk_fee_per_use / margin_per_retained_exchange
```

- The transaction file is store and web point-of-sale data, not the e-commerce OMS files behind the rest of the model. Fit text reasons exist only on web-channel return lines; in-store return lines carry numeric POS reason codes with no legend, so they are not counted as fit. The margin rate describes ALDO_CA footwear sales as a whole, not only the units that get returned.
- The existing CAD 54 per case is net of an assumed 40% item cost, so the margin-based rows (value x rate) are a conservative reading. The memo rows use the measured margin per fit-returned unit directly.
- Margin is gross margin as recorded in the file (retail minus cost); handling, freight and restocking are not in it. Currency assumed CAD, unconfirmed.

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
- In-store lines fall to 176 to 393 a month in 2025-04, 2025-05, 2025-06, 2025-07, 2025-08 against 3,144 to 10,743 in the other months (a break of 8.0x between neighbouring months), while the other channels stay between 1,174 and 2,219 a month. The data validation report (caveat C2) shows the returns file misses most in-store returns for those months, so in-store volumes, and the reason mix that depends on them, are understated. This applies to every measured volume in this model, including the fit lines.
- Return reason groups follow the product taxonomy (backend/returns_domain/taxonomy.py). 'Changed my mind / Other' mixes change of mind with unspecified reasons in one option and makes up 91.39% of the preference group, so the preference group is an upper bound on taste-based returns. The label sits on 72.47% of in-store lines against 9.49% of lines from other channels, so in store it may be a default entry. The preference lever therefore also shows a floor that counts only the 3,139 lines with a specific reason.
- Comfort complaints, process reasons (return without merchandise, return extension, gift return) and one French spelling of 'too big' sit in the group other because they are ambiguous or not a product problem. The fit group is identical to the earlier definition (Too small and Too big only), and the five groups add up to all lines in scope.
- The preference lever takes its value per converted case from the measured median refund of the preference lines and the synthetic unit economics of handoff section 15 (item cost 40 percent of price, net extra handling of an exchange CAD 6). At a CAD 100 order the formula returns the CAD 54 of the fit scenarios, which keep the flat CAD 54. The median refund stands in for the item price; line versus transaction grain is unconfirmed.
- 89.96% of the preference lines were returned in store, where the customer is already at the counter, so the lever assumes an exchange is offered there (by an associate or by ALDO bot); this is not tested. Stores already exchange today: the exchange file has 12,853 CONFIRMED and 4,802 ASSUMED in-store rows attributed to ALDO_CA in the window. The preference conversion rates (5, 10 and 15 percent) and the 60 percent availability of the alternative colour are assumptions to validate in a pilot: they are meant as additional to current in-store exchanges and are not calibrated against them, and no stock data supports the availability.
- Fit and preference lines are disjoint by construction (one reason group per line), so the two levers are added in the combined rows with the agent cost counted once. The pre-purchase prevention what-if stays a separate lever and is not part of that total.
- Bundle metrics count lines per return number in scope. 99.19% of the multi-line returns contain two or more distinct SKUs, so extra lines are normally different items. The baseline eval has no multi-line scenario, so the agent cost of a bundled run is not measured: one run per return number assumes it costs the same as a single-order run.
- The measured agent cost is the mean of 6 live runs priced at assumed token prices and is not the production mix: single scenarios differ widely, and the agent cost sensitivity shows the most expensive measured scenario and the CAD 0.50 placeholder used before the live eval.
- Agent-load numbers use return lines per day as runs per day, the mean of the scenario p50 wall-clock latencies as run duration, an assumed 12-hour active window and an assumed 15 percent busiest-hour share. Concurrency is an average (Little's law) and bursts are higher. This is not a load test. Compare with the account's on-demand quotas for the model: check account quotas, request an increase before launch. No quota numbers are stated.
- Prompt caching is not modelled: the baseline eval carries no cache-read token counts, so no saving is claimed.
- The tier perk block gives inputs and cost bounds only; no revenue, retention or loyalty uplift is claimed. The return shipping fee and the monthly allowance come from the demo fixtures; the Platinum share and the uses per member are assumptions.

## 7. Provenance and checks

- Filter: store_banner = 'ALDO_CA' AND last return status = 'RETURN_COMPLETED' AND return_creation_date in 2025-02-01..2026-01-31; fit line = reason equals 'too small' or 'too big' after strip, lower-case, whitespace collapse and removal of the 'return.page.reasons.' prefix.
- Returns file: `dataset/OMS Data_Feb24-Jan26_new.csv`, SHA-256 `76ebd5fb9cc7e16e13bb191ebdf1f5c916c295afe3289a6e913d800feb50d1a5`, 649,745 rows read, matches data profile: True.
- Exchange file: `dataset/McGill_Instore_return_exchanges.csv`, SHA-256 `3f5255834922aa15b79cb28247883345ae1548ec9c4da9b1d871c75d241e8e5b`, 61,934 rows read, business days 2025-02-02 to 2026-03-16, matches data profile: True.
- Eval results: `.agents/tasks/agentic-returns-gateway-p0-2026-10-15/eval-baseline-1/results.json`, status measured, SHA-256 `9310414b066c09b39457c248891928287e373ccee544983ac15a45d9a58d47d1`.
- Reason groups: taxonomy `backend/returns_domain/taxonomy.py`; checks passed: True.
- USD to CAD 1.38 (assumed), source: backend/returns_domain/config.py (USD_TO_CAD default).
- Reconciliation with `docs/evidence/cohort-profile.json` (earlier pandas profile):
  - ALDO_CA rows, all statuses and dates: script 160,059, profile 160,059, match: True
  - ALDO_CA RETURN_COMPLETED rows, all dates: script 153,739, profile 153,739, match: True
  - ALDO_CA fit-reason rows, all statuses and dates: script 37,210, profile 37,210, match: True
