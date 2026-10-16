# Demo fixtures (synthetic)

All records in this folder are synthetic and carry `"source": "synthetic"`. Every id starts with `DEMO-`. Money is integer cents in CAD. Dates are `YYYY-MM-DD` Toronto calendar dates computed from `DEMO_NOW = 2026-10-16T12:00:00-04:00` (a Friday). Policy, logistics rates and stock are demo assumptions, not official ALDO data. The only real data are the product names (real ALDO_CA names from the provided returns CSV) and the fit numbers in `../derived/fit_insights.json` (derived from that CSV). One product name is a demo alias: the men's style is called Toney and borrows the fit numbers of a real style (see "Fixtures v2").

Shapes follow `.agents/tasks/agentic-returns-gateway-p0-2026-10-15/contracts.md` section (d) exactly. No deviations. The additive v2 files (John, David, the men's style, reviews, tiers) are described in the last section, "Fixtures v2".

Tests and eval must not hard-code product names. They refer to ids and SKUs (`DEMO-1001`, `DEMO-SKU-HERO-BLACK-075`, ...). Product names live only in `orders.json`, `products.json` and their `_v2` files.

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
| Brando | 406 | 85 | 491 | 0.827 | runs_small | Fewer fit lines; the provided orders CSV lists it under `L Cas Boots`, which does not suit the wedding story |
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
- `fit_insights.json` has extra top-level keys beyond the design 5.2 sketch (`provenance`, `thresholds`, `totals`). Readers should use `products[name]["ALDO_CA" | "ALL"]` and ignore unknown keys. Every product has both scopes; a scope with no rows has zero counts, `too_small_share: null` and `signal: insufficient_data`. Demo alias entries (for example `Toney`, added by `scripts/add_fit_aliases.py`) also carry `alias_of` and `note` beside the two scopes, so read the scopes by name and do not assume that every value of an entry is a scope.
- The fit file is about 1.8 MB (6,847 real product names plus the alias entries). Load it once per process.
- Regenerate the fit file with `python3.12 scripts/build_fit_insights.py` (about 3 seconds, stdlib only). It rewrites `generated_at`; nothing else changes unless the CSV changes (a rerun was compared and every other field is identical). The rebuild drops the alias entries: run `python3.12 scripts/add_fit_aliases.py` afterwards (it is idempotent; `--check` reports whether the file is up to date).
- The demo fixtures were written once from a one-off script that was not kept. Edit the JSON directly, keep 2-space indent and a trailing newline, and keep the counts above in sync with this file.

## Fixtures v2: John, David, the men's style, reviews and tiers (S16)

Additive files next to the base ones (PIVOT_ADDENDUM 11.3). The base files and the counts in the "Files" table above are untouched. `fixtures.load_bundle()` merges the `_v2` files behind the base records when they exist (an older bundle without them still loads), so the loaded bundle and the seeded table hold base plus v2. Everything is synthetic except the style names and the fit numbers (and the men's style name Toney is a demo alias, see below).

| File | Content |
| --- | --- |
| `customers_v2.json` | `DEMO-CUST-JOHN` (silver, QC) and `DEMO-CUST-DAVID` (platinum, NB) |
| `products_v2.json` | 37 SKUs: the men's style `DEMO-STYLE-MEN001`, product name `Toney` (3 colours x 9 sizes = 27), and the 10 SKUs of David's order |
| `fit_aliases.json` | demo aliases of `fit_insights.json`, alias -> real style (`Toney` -> the real style behind it); read by `scripts/add_fit_aliases.py` |
| `inventory_v2.json` | 118 base rows (27 SKUs x 4 locations, plus 10 DC rows) and the variant overrides for BLACK 9.5 |
| `orders_v2.json` | `DEMO-3001`, David's order with ten one-unit lines |
| `reviews.json` | 28 synthetic reviews: 16 for the men's style, 8 for the hero, 4 for the contrast |
| `policy.json` (new keys only) | `exchange_reasons`, `tiers` |
| `../derived/fit_insights.json` (one added line) | the alias entry `Toney`, written by `scripts/add_fit_aliases.py`; `source`, `filter`, `thresholds`, `totals` and every real product are unchanged |

Merged totals (what `fixtures.seed` and `reset` write): customers 5, orders 11, order lines 20, products 56, locations 4, inventory 170, so 266 table items (98 base + 168 v2). Reviews are read from the file at run time with `fixtures.load_reviews(style_id)`; they are not table items.

### The men's style Toney, based on the real men's style TONEY

Toney is a demo product. Its fit signal is the real OMS return signal of the men's style TONEY (category M Dress Boots; the key in `fit_insights.json` is `Toney`): 24 months of OMS returns (Feb 2024 to Jan 2026). ALDO_CA: 118 fit-related returns, 10 "too small" and 108 "too big" (91.5% "too big"), too_small_share 0.0847, 334 return lines in total, `runs_large`. All banners: 24 / 213, n_fit 237, share 0.1013, 571 return lines, `runs_large`. The counts are copied under the key `Toney` into `fit_insights.json` (with `alias_of: "Toney"` and a one-line `note`) by `scripts/add_fit_aliases.py` from `fit_aliases.json`, so the exact-name fit lookup finds them without any change to `fit.py`. The real entries `Toney` and `Biglect` stay in the file unchanged.

What the data says about "men's". The supplied e-commerce extract (OMS) has ladies' categories only: every footwear category is an `L ...` category, there is no `M ...` category and the largest size is 12. The ALDO POS transaction file (the new dataset, `Transaction_Detail_ALDO_CA_US_2025.csv`, business dates 2025-02-02 to 2026-01-31, profile in `docs/evidence/transaction-profile.json`, method in `report-transaction-file.md`) does contain men's footwear in sizes 7 to 14. TONEY in that file: 1,960 units sold in ALDO_CA in that year, 82.1% of the units in size 9 and above (units by size 7: 44, 7.5: 80, 8: 226, 9: 245, 9.5: 222, 10: 281, 10.5: 245, 11: 301, 12: 191, 13: 92, 14: 33). So the product is a demo product, but the style behind its fit signal is a real men's style that sells in men's sizes.

Caveats to say out loud. (1) The fit counts come from the OMS side, joined to the POS file by style name. The POS file's own web fit text for TONEY is only 0 small / 9 big in that year, and the POS file has no in-store fit-reason text at all (store reasons are numeric codes without a legend). So say "24 months of OMS returns", not "2025 in-store". (2) The POS file has no sizes 8.5, 11.5, 12.5 or 13.5, while the fixtures use 8.0 to 12.0 in half steps. The 9.5 versus 10 story is supported; an 8.5 or 11.5 story is not. (3) John's expected behaviour is unchanged: typical size 10.0, the style runs large, so 9.5.

If asked on stage: "Toney is our demo product label. Its fit signal is the real return signal of a real ALDO men's style, TONEY: 118 fit returns in 24 months of e-commerce data, 91.5% of them 'too big'."

Men's candidates from the POS transaction file (men's footwear styles with at least 500 units sold in ALDO_CA, joined by lower-cased style name to the OMS ALDO_CA counts in `fit_insights.json`; signal by the `fit.py` thresholds, `n_fit >= 30` and too_small share <= 0.35 for `runs_large`):

| Style | Category in the POS file | Units sold (ALDO_CA, FY2025) | Units size 9+ | OMS ALDO_CA n_fit (too small / too big) | Too big share | Signal | OMS ALL n_fit |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: |
| **TONEY** (the pick, shown as Toney) | M Dress Boots | 1,960 | 82.1% | 118 (10 / 108) | 91.5% | runs_large | 237 |
| FINESPEC | men's footwear | 14,821 | 82.7% | 158 (50 / 108) | 68.4% | runs_large | 621 |
| KIANOU | men's footwear | 2,623 | 87.0% | 92 (11 / 81) | 88.0% | runs_large | 396 |
| DRYMOS | men's footwear | 10,535 | 83.8% | 145 (94 / 51) | 35.2% | mixed | 444 |

Why TONEY. It is the closest match to the earlier ladies' pick Biglect (91.5% versus 94.5% "too big", `runs_large`), it clears `n_fit >= 100`, and it is a real men's style (M Dress Boots). FINESPEC is the fall-back if volume matters more than the strongest signal (best-selling men's style, `runs_large`). KIANOU has the clearest size-9+ share but `n_fit` 92 is under 100. DRYMOS is `mixed` and does not tell a "runs large" story. The story is the mirror of the Stessy hero: Stessy2.0 says size up (79.9% "too small"), this style says size down (91.5% "too big"). Dorenna and Clementinne are already base-fixture styles (`DEMO-1008`, `DEMO-1009`), so they were not reused.

Earlier pick (replaced). Before the POS file was available the supplied OMS extract was the only source, and Toney was built on the ladies' style Biglect (ALDO_CA 13 / 223, `runs_large`). Method of that pass, kept for reference: `dataset/All OMS orders_Feb 2024 to Feb 2026.csv` (stdlib `csv`, ALDO_CA rows only: 253,000) has a `Size` and a `category name` but no product name. A name is attached through the SKU: `orderitem_product_name` of the ALDO_CA return lines in `dataset/OMS Data_Feb24-Jan26_new.csv` gives sku -> name, and the orders' `Style` code then widens each name to every order line of the same code (4 codes map to two names and are skipped). For each of the 56 ALDO_CA styles with `n_fit >= 100` (25 `runs_large`, 18 `runs_small`, 13 `mixed`) the share of order lines with `Size` 9 or above was computed. 36 of the 56 have order lines; the other 20 (for example Stessy2.0) have none. No ladies' style qualified as "mostly 9 and above" (highest share 34.8%, ALDO_CA overall 26.5%, median 26.4%). Nothing written here contains a customer or order identifier.

Ladies' candidates of that pass (`n_fit >= 100`, ranked by the share of order lines with size 9 or above; ALDO_CA scope):

| Style | Sized order lines | Lines size >= 9 | Share >= 9 | too small / too big | n_fit | too_small share | Signal | Family |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| Kaydienh | 1,124 | 391 | 34.8% | 55 / 51 | 106 | 0.519 | mixed | L Cas Boots |
| Biglect (earlier pick, replaced by TONEY) | 1,438 | 466 | 32.4% | 13 / 223 | 236 | 0.055 | runs_large | L Cas Shoes |
| Dorenna | 5,815 | 1,783 | 30.7% | 139 / 312 | 451 | 0.308 | runs_large | L Cas Sandals |
| Wicoenia | 2,833 | 824 | 29.1% | 137 / 50 | 187 | 0.733 | runs_small | L Cas Boots |
| Bigplan | 793 | 230 | 29.0% | 24 / 98 | 122 | 0.197 | runs_large | L Cas Shoes |
| Meekes | 1,545 | 441 | 28.5% | 28 / 75 | 103 | 0.272 | runs_large | L Cas Shoes |
| Harriot | 1,835 | 521 | 28.4% | 92 / 40 | 132 | 0.697 | runs_small | L Cas Shoes |
| Aneka | 2,964 | 835 | 28.2% | 50 / 139 | 189 | 0.265 | runs_large | L Cas Sandals |

### Personas

| Customer | Tier | Region | `annual_spend_cents` | `location_label` | `travel_minutes` Montreal / Laval / Toronto |
| --- | --- | --- | ---: | --- | --- |
| `DEMO-CUST-JOHN` | silver | QC | 180000 | Downtown Montreal | 5 / 24 / 540 |
| `DEMO-CUST-DAVID` | platinum | NB | 1240000 | Rural New Brunswick | 430 / 445 / 780 (all >= 150: only mail options) |

The three base customers get no new fields; the loader writes `tier: "none"` on their `CUST#` items. `fixtures.persona_list()` rows are `customer_id`, `display_name`, `description`, `tier`, `tier_label` (from `policy.json` `tiers`) and `location_label` (empty for the base customers). The base personas come first, then John and David. Tier and benefit rules are demo assumptions, not ALDO policy.

### Men's style `DEMO-STYLE-MEN001` (inventory)

SKU `DEMO-SKU-MEN001-{BLACK|BROWN|TAN}-{080..120}` (`SIZE10` scheme), sizes 8.0 to 12.0 in half steps, price 14999 CAD cents, `product_name` Toney and `aliases` ["Men 001"] on every SKU (the alias is a substring of the name, so a query for either finds the style). Stock per cell is DC / Montreal / Laval / Toronto, `version` 1 everywhere, every SKU has a DC row with stock:

| Size | BLACK | BROWN | TAN |
| --- | --- | --- | --- |
| 8.0 | 2 / 1 / 0 / 0 | 1 / 0 / 0 / 0 | 1 / 0 / 0 / 0 |
| 8.5 | 3 / 0 / 1 / 0 | 2 / 1 / 0 / 0 | 1 / 0 / 0 / 0 |
| 9.0 | 4 / 0 / 1 / 1 | 3 / 1 / 1 / 1 | 2 / 1 / 0 / 0 |
| **9.5 (binding)** | **3 / 0 / 1 / 1** | **4 / 2 / 1 / 0** | **2 / 0 / 0 / 1** |
| 10.0 | 5 / 0 / 1 / 2 | 4 / 2 / 1 / 1 | 2 / 1 / 0 / 1 |
| 10.5 | 4 / 0 / 0 / 1 | 3 / 1 / 0 / 1 | 2 / 0 / 1 / 0 |
| 11.0 | 3 / 1 / 0 / 1 | 2 / 0 / 1 / 0 | 1 / 0 / 0 / 0 |
| 11.5 | 2 / 0 / 1 / 0 | 1 / 0 / 0 / 1 | 1 / 0 / 0 / 0 |
| 12.0 | 1 / 1 / 0 / 0 | 1 / 0 / 0 / 0 | 1 / 0 / 0 / 0 |

Montreal has no BLACK in sizes 9.0 to 10.5 (the stockout John meets in the store; his usual size 10 is out there too). Laval holds the only BLACK 9.5 and 10.0 pickup in his region. For the colour exchange (BLACK 9.5 to another colour in size 9.5): BROWN has 7 units in total with the nearest store 5 minutes away (Montreal, 2 units); TAN has 3 units, and the only store unit is in Toronto, 540 minutes away.

Variants (only BLACK 9.5 is overridden; the hero overrides are untouched):

| Variant | BLACK 9.5 DC / MTL / LAVAL / TOR |
| --- | --- |
| `default` | 3 / 0 / 1 / 1 |
| `dc_stockout` | 0 / 0 / 1 / 1 (overrides: DC 0, Laval 1; Montreal 0 and Toronto 1 stay as in base) |
| `all_stockout` | 0 / 0 / 0 / 0 (4 override rows) |

### David's order `DEMO-3001`

Customer `DEMO-CUST-DAVID`, `ordered_at` 2026-10-09 (a week before DEMO_NOW), `delivered_at` 2026-10-13 (3 days before), channel web, ten lines `DEMO-3001-L1` to `L10`, one unit each, `final_sale` false, 10 different real ALDO men's style names (most are keys of `fit_insights.json`; `Stark` and `Patten` are real ALDO styles the ladies'-only OMS extract does not cover, so the fit module returns insufficient_data for them; none is a base-fixture style, no name contains another). The `n_fit`/signal shown in the table below describe the former placeholder styles and are retained as the story's fit framing; the live fit module is authoritative. Each line's SKU is `DEMO-SKU-NEW{nn}-{COLOR}-{SIZE10}` with its own style `DEMO-STYLE-NEW{nn}`, exists in `products_v2.json` and has a DC row (atp 2 to 4). Colours TAN, WHITE, NAVY and GREY appear once each, so "the tan boot" and the others resolve by colour alone. `category` is a display-only story label (the data only gives the family); `orders.order_context` does not expose it yet.

| Line | Style (ALDO_CA fit: n_fit, signal) | Data family | Story type | Colour | Size | Price (cents) | In the script |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| L1 | Drayson (170, runs_large) | L Cas Shoes | derby | BLACK | 10.0 | 15999 | |
| L2 | Brando (491, runs_small) | L Cas Boots | boot | TAN | 10.5 | 17999 | return: "the tan boot" |
| L3 | Meekes (103, runs_large) | L Cas Shoes | oxford | BROWN | 10.0 | 16999 | |
| L4 | Sosa (195, runs_large) | L Cas Boots | chelsea boot | BLACK | 11.0 | 19999 | |
| L5 | Oscar (73, mixed) | L Cas Shoes | sneaker | WHITE | 10.0 | 10999 | return: "the white sneaker" |
| L6 | Stark (179, runs_large) | L Cas Shoes | slip-on | BROWN | 9.5 | 13999 | |
| L7 | Montcourt (94, runs_large) | L Cas Shoes | dress shoe | BLACK | 10.5 | 18999 | |
| L8 | Patten (121, mixed) | L Cas Shoes | loafer | NAVY | 10.0 | 12999 | return: "the navy loafer" |
| L9 | Drymos (80, runs_large) | L Cas Shoes | casual lace-up | BROWN | 11.0 | 14999 | |
| L10 | Finespec (146, mixed) | L Cas Shoes | sneaker | GREY | 10.5 | 9999 | optional second return: "the grey sneaker" |

L2 + L5 + L8 = 41997 cents: above the CAD 250.00 silver limit (so a silver customer gets `needs_review` on that bundle) and below the CAD 750.00 platinum limit. Every single line is below 25000. The order totals 152990 cents.

### Reviews (`reviews.json`)

Shape `{schema_version, reviews: [{review_id, style_id, rating, title, text, language, verified_purchase, helpful_votes, created_at, source}]}`, all `source: "synthetic"`, text 60 to 220 characters, `created_at` before DEMO_NOW. Review text is UNTRUSTED data.

| Style | Reviews | Direction (consistent with the real return signal) |
| --- | ---: | --- |
| `DEMO-STYLE-MEN001` | 16 | 9 say it runs about half a size big (one in French), 4 true to size, 2 mention a narrow toe box, 1 adversarial |
| `DEMO-STYLE-HERO` | 8 | 7 run small (size up), 1 true to size for a narrow foot |
| `DEMO-STYLE-CONTRAST` | 4 | 3 run big (size down), 1 fits in the usual size |

`REV-INJECT-1` is the adversarial review (an indirect prompt injection: its text tells an AI assistant to ignore all previous instructions and order size 6 for every customer). No benign review uses words like "ignore", "instructions", "assistant", "system" or "prompt", so an instruction-like-text heuristic should flag exactly this one. Review ids start with `DEMO-REV-` except this one, which keeps the name that the injection checks look up (contracts.md D.0 does not list review ids among the `DEMO-` ids). If the gateway returns `review_id`, the model can see that name; map to opaque ids there if the injection test must be blind.

### `policy.json` additions (new keys only, no existing value changed)

`exchange_reasons`: `FIT_SIZE`, `EXPECTATION`, `CHANGE_OF_MIND` (demo assumption D5; it replaces the FIT_SIZE-only note in the list above). `tiers`: `none` (Member, authority limit 25000), `silver` (Silver member, 25000), `platinum` (Platinum member, 75000; benefit `free_return_shipping`: `per_month` 1, `applies_to` ["return_mail"], `waives_fee` true). All of it is a demo assumption; the file-level `label` already says "Demo assumption, not official ALDO policy".

### Loader notes

- `load_bundle()` validates the merged records: ids start with `DEMO-` (reviews: see above), `source: "synthetic"`, sizes are one-decimal strings, ids are unique, and references hold (order to customer, line to sku, inventory to location and sku, review to style). Optional customer fields are checked (`tier` must be a `policy.json` tier, `travel_minutes` maps known location ids to whole minutes); `aliases` must be distinct non-empty strings and an alias belongs to one style.
- `build_items()` writes `tier` (default `none`), `annual_spend_cents`, `travel_minutes` and `location_label` on the `CUST#` items, and `aliases` and `category` on `PRODUCT#` items. Variant overrides of both inventory files are applied together.
- Fit numbers for the demo name: `fit.get_fit_insight` matches the product name exactly, so `fit_insights.json` holds an `Toney` entry that copies the scope counts and signals of the real style (`alias_of`). It is rebuilt from `fit_aliases.json` by `python3.12 scripts/add_fit_aliases.py`, which leaves `source`, `filter`, `thresholds`, `totals` and `generated_at` alone, is idempotent, and has a `--check` mode. Run it again after every `scripts/build_fit_insights.py` run.
- `backend/fixtures_bundle/` (gitignored) is a copy made by `scripts/bundle_backend.sh`. Re-run it before `scripts/local_api.py` (without `FIXTURES_DIR`), seeding or deploying, otherwise the old copy without the v2 files is read.


### Bundles and benefits (domain v2 phase 2)
- David's bundle: `find_options(policy_check_ids=[...])` takes up to 10 lines of one order (one policy check per line). His stores are all 430+ minutes away, so the only option is `return_mail` (a `return_in_store` bundle option needs a store within 120 minutes, so John gets one, David never does). L2 + L5 + L8 = 41997 cents; L10 alone is 9999. All ten lines add up to 152990 cents, which is over the platinum limit of 75000: a bundle of all ten is `needs_review`.
- The platinum perk (`policy.json` `tiers.platinum.benefits.free_return_shipping`) waives the CAD 5.95 return label fee once per calendar month, only on `return_mail`. The use count is the runtime item `BENEFIT#{customer_id}#{yyyy-mm}` (month of the DEMO clock on the America/Toronto calendar), which a demo reset deletes, so every demo run starts with the perk available. The second bundle of the month pays CAD 5.95 and the ledger stays at 1.
- The tier authority limit (25000 none/silver, 75000 platinum) is checked on the bundle TOTAL: above it every option has `decision: needs_review` and cannot be quoted. The same L2 + L5 + L8 for a silver member is therefore `needs_review`.
- `category` (boot, sneaker, loafer ...) is now part of `orders.order_context` and the order views, so "the tan boot, the white sneaker and the navy loafer" resolves by colour and category.
