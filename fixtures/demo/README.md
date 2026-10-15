# Demo fixtures (synthetic)

All records in this folder are synthetic and carry `"source": "synthetic"`. Every id starts with `DEMO-`. Money is integer cents in CAD. Dates are `YYYY-MM-DD` Toronto calendar dates computed from `DEMO_NOW = 2026-10-16T12:00:00-04:00` (a Friday). Policy, logistics rates and stock are demo assumptions, not official ALDO data. The only real data are the product names (real ALDO_CA names from the provided returns CSV) and the fit numbers in `../derived/fit_insights.json` (derived from that CSV).

Shapes follow `.agents/tasks/agentic-returns-gateway-p0-2026-10-15/contracts.md` section (d) exactly. No deviations.

Tests and eval must not hard-code product names. They refer to ids and SKUs (`DEMO-1001`, `DEMO-SKU-HERO-BLACK-075`, ...). Product names live only in `orders.json` and `products.json`.

## Hero and contrast product choice (task 2.2)

Source: `fixtures/derived/fit_insights.json`, built by `scripts/build_fit_insights.py` from `dataset/OMS Data_Feb24-Jan26_new.csv` (649,745 rows read, SHA-256 `76ebd5fb9cc7e16e13bb191ebdf1f5c916c295afe3289a6e913d800feb50d1a5`). `n_fit = too_small + too_big`, `share = too_small / n_fit`. Signal rule: `runs_small` if share >= 0.65 and n_fit >= 30, `runs_large` if share <= 0.35 and n_fit >= 30, `mixed` otherwise, `insufficient_data` if n_fit < 30.

### Hero: Stessy2.0 (SKU token `HERO`, order DEMO-1001)

| Scope | too small | too big | n_fit | share | total return lines | signal |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| ALDO_CA | 465 | 117 | 582 | 0.799 | 1,809 | runs_small |
| ALL banners | 1,581 | 353 | 1,934 | 0.817 | 5,094 | runs_small |

Why this one. It has the most ALDO_CA fit lines of any `runs_small` product (582), it clears the 0.65 threshold with a wide margin, and the all-banner scope agrees. The other design candidates (ALDO_CA scope):

| Candidate | too small | too big | n_fit | share | signal | Why not |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Aahliyah | 406 | 85 | 491 | 0.827 | runs_small | Fewer fit lines; the provided orders CSV lists it under `L Cas Boots`, which does not suit the wedding story |
| Veadith2.0 | 352 | 188 | 540 | 0.652 | runs_small | Only 0.002 above the threshold; a few lines would flip it to `mixed` |

The provided orders CSV has no rows for Stessy2.0, so its product category is unknown from the data. The wedding story is the demo script's, not a data claim.

### Contrast: Clementinne (SKU token `CONTRAST`, order DEMO-1009)

| Scope | too small | too big | n_fit | share | total return lines | signal |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| ALDO_CA | 63 | 354 | 417 | 0.151 | 1,148 | runs_large |
| ALL banners | 136 | 626 | 762 | 0.178 | 2,025 | runs_large |

Why this one. It is the clearest `runs_large` product with n_fit above 400 (84.9% of fit returns say "too big"). Chloe's DEMO-1009 complaint is "too big", so the size-down suggestion is backed by evidence, the mirror image of the hero (go up for Stessy2.0, go down for Clementinne). Runner-up `mixed` candidates, for reference: Pristine (ALDO_CA 126 / 141, share 0.472) and Gavedessi (103 / 183, share 0.360, close to the 0.35 line).

## Files

| File | Content |
| --- | --- |
| `customers.json` | 3 personas: `DEMO-CUST-AVA` (QC), `DEMO-CUST-BEN` (ON), `DEMO-CUST-CHLOE` (QC). First names only |
| `orders.json` | 10 orders `DEMO-1001`..`DEMO-1010`, one nested line each |
| `products.json` | 19 SKUs: hero run (7), contrast run (4), 8 other order-line SKUs |
| `locations.json` | `DEMO-DC-QC`, `DEMO-STORE-MTL-01`, `DEMO-STORE-LAVAL-01`, `DEMO-STORE-TOR-01` |
| `inventory.json` | 52 base rows plus variants `default`, `dc_stockout`, `all_stockout` |
| `logistics.json` | `DEMO-LOGISTICS-CA`: mail-in return CAD 5.95, in-store CAD 0.00, exchanges CAD 0.00 |
| `policy.json` | `DEMO-POLICY-CA` version `2026.10-demo`: 30 days inclusive, CAD 250.00 authority limit, rule ids |

## Orders

`delivered_at` offsets are from 2026-10-16. Product names are real ALDO_CA names; the fit columns are read from `fit_insights.json` (ALDO_CA scope) for orientation only.

| Order | Customer | SKU | Size | Paid | Qty (returned) | Final sale | Delivered | Product | ALDO_CA fit signal |
| --- | --- | --- | --- | ---: | --- | --- | --- | --- | --- |
| DEMO-1001 | AVA | `DEMO-SKU-HERO-BLACK-070` | 7.0 | 129.99 | 1 (0) | false | 2026-10-04 (-12d) | Stessy2.0 | runs_small, n 582, 0.799 |
| DEMO-1002 | AVA | `DEMO-SKU-FINALSALE-TAN-080` | 8.0 | 59.99 | 1 (0) | **true** | 2026-10-06 (-10d) | Krios | mixed, n 71, 0.577 |
| DEMO-1003 | BEN | `DEMO-SKU-STD1-BLACK-100` | 10.0 | 89.99 | 1 (0) | false | 2026-09-16 (-30d) | Harriot | runs_small, n 132, 0.697 |
| DEMO-1004 | BEN | `DEMO-SKU-STD2-BROWN-100` | 10.0 | 89.99 | 1 (0) | false | 2026-09-15 (-31d) | Cherflex | mixed, n 164, 0.537 |
| DEMO-1005 | BEN | `DEMO-SKU-BOOT-BLACK-100` | 10.0 | **289.00** | 1 (0) | false | 2026-10-09 | Goer | runs_small, n 233, 0.704 |
| DEMO-1006 | CHLOE | `DEMO-SKU-STD3-WHITE-070` | 7.0 | 79.99 | 1 (0) | false | 2026-10-05 | Lagoon | runs_small, n 146, 0.842 |
| DEMO-1007 | CHLOE | `DEMO-SKU-STD4-NAVY-075` | 7.5 | 69.99 | 1 (0) | false | **null** | Verity | mixed, n 222, 0.590 |
| DEMO-1008 | AVA | `DEMO-SKU-STD5-BLACK-080` | 8.0 | 59.99 | **2 (1)** | false | 2026-10-07 | Dorenna | runs_large, n 451, 0.308 |
| DEMO-1009 | CHLOE | `DEMO-SKU-CONTRAST-BLACK-080` | 8.0 | 119.99 | 1 (0) | false | 2026-10-08 | Clementinne | runs_large, n 417, 0.151 |
| DEMO-1010 | BEN | `DEMO-SKU-STD6-GREY-100` | 10.0 | 74.99 | 1 (0) | false | 2026-10-10 | Jellyicious | insufficient_data, n 26, 0.269 (ALL banners: mixed, n 101, 0.366) |

No line has `final_sale: null`. DEMO-1010 is deliberately `insufficient_data` in the ALDO_CA scope so the fit tool's fallback to the all-banner scope can be shown. `ordered_at` is always before `delivered_at`; DEMO-1007 was ordered on 2026-10-12 and has no delivery date.

## Locations and ETAs

| Location | Type | City, region | pickup_ready_days | ship_days |
| --- | --- | --- | ---: | ---: |
| `DEMO-DC-QC` | DC | Montreal, QC | none | 4 |
| `DEMO-STORE-MTL-01` | STORE | Montreal, QC | 1 | 3 |
| `DEMO-STORE-LAVAL-01` | STORE | Laval, QC | 1 | 3 |
| `DEMO-STORE-TOR-01` | STORE | Toronto, ON | 1 | 3 |

With DEMO_NOW on Friday 2026-10-16 (calendar days, no business-day logic): store pickup Saturday 2026-10-17, ship from a store Monday 2026-10-19, ship from the DC Tuesday 2026-10-20. A "Saturday" deadline (2026-10-17) is met only by store pickup.

## Inventory and reset variants

Base stock (`version` is 1 everywhere; each cell is ATP at DC / MTL / LAVAL / TOR):

| SKUs | DC / MTL / LAVAL / TOR |
| --- | --- |
| Hero sizes 7.5 and 8.0 (`DEMO-SKU-HERO-BLACK-075`, `-080`) | 5 / 1 / 0 / 1 |
| Other hero sizes 6.0, 6.5, 7.0, 8.5, 9.0 | 3 / 1 / 1 / 0 |
| Contrast 7.0, 8.0, 8.5 | 2 / 0 / 0 / 0 |
| Contrast 7.5 (`DEMO-SKU-CONTRAST-BLACK-075`) | 3 / 1 / 0 / 0 |
| Other order-line SKUs (DC row only) | FINALSALE 2, STD1 3, STD2 3, BOOT 2, STD3 4, STD4 3, STD5 4, STD6 3 |

Variants. Each override replaces `atp` of an existing base row; `version` is reset to 1 on reset. Only the two hero replacement sizes (7.5 and 8.0) are touched.

| Variant | Hero 7.5 and 8.0, DC / MTL / LAVAL / TOR |
| --- | --- |
| `default` | 5 / 1 / 0 / 1 (base, no overrides) |
| `dc_stockout` | 0 / 2 / 0 / 0 (8 override rows) |
| `all_stockout` | 0 / 0 / 0 / 0 (8 override rows) |

Hero line DEMO-1001-L1 (size 7.0, too small, replacement 7.5, Ava in QC, deadline 2026-10-17):

- `default`: exchange from the DC (Tue 2026-10-20, misses the deadline) and store pickup at Montreal (Sat 2026-10-17, meets it). The Montreal and Toronto stores both hold 1 unit at ship_days 3, so ship-from-store is a tie that the options code must break deterministically.
- `dc_stockout`: no DC exchange; Montreal ship (Mon 2026-10-19, misses) and Montreal pickup (Sat 2026-10-17, meets).
- `all_stockout`: no exchange options, only `return_in_store` and `return_mail`.

Only the hero SKUs and the contrast run have a size run. The other order-line SKUs exist at the DC only, so an exchange on those orders finds no replacement size and returns `NO_SUCH_SIZE` (return options still work).

## Notes for other streams

- `policy.json` lists all five option types in `option_types`. Design 5.1 rule 9 limits exchange types to `reason_category == FIT_SIZE`. That restriction is a demo assumption with no requirement behind it; the policy module owns it, not the fixture.
- `fit_insights.json` has extra top-level keys beyond the design 5.2 sketch (`provenance`, `thresholds`, `totals`). Readers should use `products[name]["ALDO_CA" | "ALL"]` and ignore unknown keys. Every product has both scopes; a scope with no rows has zero counts, `too_small_share: null` and `signal: insufficient_data`.
- The fit file is about 1.8 MB (6,847 product names). Load it once per process.
- Regenerate the fit file with `python3.12 scripts/build_fit_insights.py` (about 3 seconds, stdlib only). It rewrites `generated_at`; nothing else changes unless the CSV changes (a rerun was compared and every other field is identical).
- The demo fixtures were written once from a one-off script that was not kept. Edit the JSON directly, keep 2-space indent and a trailing newline, and keep the counts above in sync with this file.
