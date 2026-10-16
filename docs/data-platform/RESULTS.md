# Data platform: first live run (2026-10-16)

Design and gates: `DESIGN.md`. Runbook: `scripts/data_platform/README.md`. Account ids are not written here.

## What ran
| Step | Result |
| --- | --- |
| Upload (`upload_dataset.py`) | 5 files, 3,831,260 rows, 232 MB; `cnsmr_prof_sid` and `customer_uid` dropped from returns; SSE-S3 |
| Athena pipeline (`build_insights.py`) | 7 queries SUCCEEDED, 185.6 MB scanned (about USD 0.0008), 6,847 products |
| Parity (`parity_report.py --from-s3`) | 100%: 6,848 products, 13,696 scope cells, 0 product mismatches, totals equal |
| `P6` (no model) | 20/20: same fit numbers in static, platform and shadow; only platform names `aws-athena` and its query ids; mode restored |
| Shadow (J1, J2, D1) | 3/3 pass, 0 `data_platform_shadow_mismatch` and 0 `data_platform_fallback` warnings in both Lambda log groups |

## Gates (DESIGN.md)
| Gate | Result |
| --- | --- |
| 1 Parity | pass |
| 2 Shadow, 0 mismatches over a John + David pass | pass (no warning lines; the OK line is INFO and the Lambdas log at WARNING, so it is not visible) |
| 3 Live eval in platform mode 19/19 | pass, 19/19, 43 model calls, USD 0.0129 per returns-agent run |
| 4 Latency, J1 and J2 mean within 10% of static | pass: platform 25.9 s (J1 10.9, J2 41.0) against static 27.0 s (J1 9.7, J2 44.2); one run each, so only a coarse check |
| 5 Browser demo + a11y specs in platform mode | pass: 11 passed, 5 skipped (mock only), 0 axe findings |

All gates passed. The mode was restored to `static` afterwards: the owner decides when to switch (Admin panel `/?admin=1`, or `aws ssm put-parameter --name /aldo-demo/data-platform/mode --value platform --overwrite --type String --region us-east-1`).

## Scope
The door covers the fit signal of the purchase flow (gateway). The AgentCore returns agent (runtime v7) still reads its own bundled fit file.
