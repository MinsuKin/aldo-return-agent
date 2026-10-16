# Business impact, in plain English

The CAD 132k number is an illustrative calculation. It is not money ALDO has saved and it is not an estimate fitted to observed adoption. Two big inputs, the share of returns we change and the value of changing one return, are assumptions.

## 1. What the data actually tells us

The model counts 61,998 completed ALDO_CA return lines from February 1, 2025 to January 31, 2026. Of those, 13,317 have a Too small or Too big reason. That is 21.48%, rounded to 21.5%.

Those counts establish a recorded problem, not the number of customers the agent can persuade. A line is a row in the returns file. It is not necessarily an independent physical unit or a unique customer. The data has incomplete in-store coverage in some months, so these are scoped extract counts, not a complete ALDO-wide annual total.

The recorded refund amounts on those lines sum to 1,150,050.58. Currency is assumed CAD, and line versus transaction granularity is unconfirmed. Do not call that CAD 1.15M of losses, profit, savings, or addressable incremental value.

## 2. Where the 20% comes from

We chose it as a scenario. There is no measured 20% uplift.

For the model to describe incremental value, it must mean **additional exchanges caused by our experience compared with today's flow**. Exchanges that ALDO would have completed anyway do not count.

13,317 baseline lines × 20% = 2,663.4 modeled additional exchanges, about 2,663 when spoken on stage.

We have not measured how many baseline lines are eligible, reachable, supported by stock, or already exchanged. The scenario implicitly assumes enough can become additional exchanges. A pilot must establish that. Twenty percent of these baseline lines is different from a 20% relative increase in today's exchange rate.

## 3. Where CAD 54 per exchange comes from

It comes from a hypothetical CAD 100 order in the original implementation handoff, not ALDO's measured profit per exchange.

Compare what happens after the customer wants to return the same item. The original payment and original item cost have already happened in both cases, so we compare the next cash/value changes.

**Refund path:** give back CAD 100, recover CAD 30 of item value, spend CAD 12 on return processing. Change: −100 + 30 − 12 = **−CAD 82**.

**Exchange path:** give back no cash, recover the same CAD 30 of item value, provide a replacement costing CAD 40, spend CAD 10 on processing and CAD 8 on reshipping. Change: 0 + 30 − 40 − 10 − 8 = **−CAD 28**.

The exchange is CAD 54 better in this example: −28 − (−82) = **+CAD 54**.

Another way to see it: avoid a CAD 100 refund, pay CAD 40 for the replacement, and spend CAD 6 more on handling/shipping than the refund path. **100 − 40 − 6 = 54**. The equal recovery value cancels.

Every amount in that example is assumed. A cheaper shoe, a more expensive replacement, a second return, or a different resale outcome changes the answer. The recovered item can be resold under either path, so we cannot count its full resale value again as an exchange-only benefit.

## 4. The clear core scenario: about CAD 129k

Keep only the exchange lever first:

1. **13,317 × 20% = 2,663.4** additional exchanges, assumed.
2. **2,663.4 × CAD 54 = CAD 143,823.60** illustrative exchange value before AI cost.
3. **61,998 × CAD 0.2449 ≈ CAD 15,183.31** estimated AI cost for the scoped year.
4. **CAD 143,823.60 − CAD 15,183.31 = CAD 128,640.29**, about **CAD 129k after estimated AI cost**.

This is the main-deck illustration. It excludes any bonus for preventing a second return.

The operating-cost assumption charges one model run for every completed return line, including lines the model does not convert. It is not just AI cost for successful exchanges. Bundles can reduce runs per line, while multi-turn conversations and separate purchase/return workflows can increase them. One run per line is a modeling choice, not a measured production usage mix.

CAD 0.2449 is **estimated from observed token usage in six earlier live runs, using assumed model prices and an assumed USD/CAD exchange rate**. It is not an AWS invoice, a measured cost per current end-to-end conversation, or total operating cost. Current John/David flow cost, hosting, monitoring, integration, rollout and support costs are not established here. The CAD 129k result therefore is not fully loaded profit or ROI.

## 5. Why the older deck said CAD 132k

The original base case adds a second assumed benefit:

2,663.4 converted cases × 5% fewer repeat returns × CAD 22 per avoided second return = **CAD 2,929.74**.

CAD 128,640.29 + CAD 2,929.74 = **CAD 131,570.03**, rounded to CAD 132k.

Neither that repeat-return improvement nor CAD 22 of avoided cost is observed in the current demo. The new pitch leaves the roughly CAD 3k bonus out of the headline so the audience can follow one value mechanism.

## 6. Sensitivity without the repeat-return bonus

With the same CAD 54 value per additional exchange and CAD 15,183.31 AI-cost estimate:

- At 10% of baseline lines: CAD 71,911.80 exchange value less AI cost = **CAD 56,728.49**, about CAD 57k.
- At 20%: CAD 143,823.60 less AI cost = **CAD 128,640.29**, about CAD 129k.
- At 30%: CAD 215,735.40 less AI cost = **CAD 200,552.09**, about CAD 201k.

The original 57k / 132k / 209k series additionally assumes repeat-return improvements of 0% / 5% / 10%. It varies two factors at once. The new 57k / 129k / 201k series changes only exchange adoption, making it easier to explain.

At CAD 54 value per incremental exchange, **282 additional exchanges** would cover the modeled AI bill: 15,183.31 ÷ 54 = 281.17, rounded up. That is about **2.12%** of 13,317 lines. It covers the modeled AI bill only, not all costs. The old 288 figure uses a different CAD 52.83 measured-margin proxy, so do not mix the two break-even methods.

## 7. Which economic inputs need replacing

The transaction analysis reports an average CAD 52.83 margin on fit-returned units in a different web/POS cohort. That is useful context, but it does not directly measure incremental value from an exchange and should not silently replace CAD 54.

Also do not multiply CAD 54 by the 64.18% footwear margin rate and call the result a more conservative profit calculation. CAD 54 already subtracts the hypothetical replacement cost. Applying another margin rate mixes accounting bases and double-counts a cost adjustment.

Fit-related and preference-return groups are disjoint labels, but that alone does not prove their economic benefits are additive in a deployed journey. We lack a clean preference baseline because most preference lines use a combined Changed my mind / Other reason. Keep John's colour exchange as a capability demonstration and the size cohort as the narrower financial illustration.

Do not add pre-purchase prevention to post-purchase exchange value for the same return: a prevented return cannot also become an exchange.

## 8. The pilot that makes the numbers credible

Start with a defined eligible cohort and compare the new experience with today's flow. Measure **incremental exchanges that remain retained after the return window**, customer satisfaction, repeat returns, and actual cost per completed resolution. Track refunds and current exchanges in the control group, so we do not claim value for exchanges ALDO already makes.

Replace CAD 54 with ALDO's actual price, replacement cost, fees, handling, delivery, resale and repeat-return economics. Price observed model usage with actual rates. Subtract the incremental cost of deploying and operating the experience. Report prevention separately until the cohorts and attribution support combining it.

## Source trail

- Existing outputs: `docs/impact/impact-model.json` and `docs/impact/impact-model.md`.
- CAD 54 example: `HACKATHON_IMPLEMENTATION_HANDOFF.en.md`, section 15.
- Model assumptions and formulas: `scripts/build_impact_model.py`, VALUE_PER_CONVERSION_CAD, SCENARIOS and AVOIDED_SECOND_RETURN_COST_CAD.
- Underlying token-cost input: field `est_cost_per_run_usd` in the earlier baseline evaluation; assumed USD/CAD 1.38.

The new core figures are a transparent presentation derivative. The original scenario outputs remain available for comparison; they are not silently replaced.
