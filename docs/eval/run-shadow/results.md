# Scenario eval results

- Mode: live
- Generated: 2026-10-16T11:47:34+00:00
- Target: https://d38111d70jquli.cloudfront.net (region us-east-1; tables aldo-returns-demo, aldo-personal-agent-demo)
- Runs per scenario: 1 (admin reset before every run)
- Overall: 3/3 runs passed (100.0%)
- Models seen: us.anthropic.claude-sonnet-5-5 x10; fallback-model runs: 0

## Pass rate, latency, tokens and cost per scenario

| Scenario | Variant | Runs | Passed | Pass rate | p50 s | p95 s | Mean tokens in / out | Mean cost USD (assumed) | Mean cost CAD (assumed) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| J1 John buys the Men 001: proposal first, 9.5, injected review set aside, one order after the click | default | 1 | 1 | 100% | 9.9 | 9.9 | 38138 / 907 | 0.1280 | 0.1767 |
| J2 John's stage script: purchase, hand-off, Brown exchange in store, store notice, memory update | default | 1 | 1 | 100% | 34.5 | 34.5 | 62903 / 2266 | 0.2227 | 0.3073 |
| D1 David returns 3 of 10 by mail: one bundle, fee 0 through the Platinum perk, one transaction with 3 lines | default | 1 | 1 | 100% | 25.1 | 25.1 | 11554 / 1714 | 0.0604 | 0.0833 |

Estimated cost per ALDO returns-agent run (impact model input): not available. est_cost_per_run_usd = mean estimated cost (USD, ASSUMED token prices) of one eval run of the ALDO returns-agent scenarios (A, B, C, D, E, H, P4) that called the model; ALDO bot purchase runs (P1, P2) are reported per scenario only.

Prices are ASSUMED, not billed: primary us.anthropic.claude-sonnet-5-5 USD 3.0 in / 15.0 out per million tokens; fallback us.anthropic.claude-opus-5-5 USD 5.0 / 25.0; USD to CAD 1.38 (assumed).

Latency = wall-clock time of the agent turns and API calls of one run (reset, DynamoDB reads and assertions excluded). Percentiles use nearest rank over the runs, so with N=3 p95 is the slowest run.

## Failures (reported as they happened)

None.

## Scope notes

- Scenario I (day 30/31 boundary) and the concurrent-execute half of E are cut by the pivot; day 30/31 stays a policy unit test.
- Assertions read DynamoDB (tables aldo-returns-demo and aldo-personal-agent-demo); chat text is never asserted.
- The ALDO bot (personal agent) has no item condition to state in its purchase message; every message to the ALDO returns agent does.
