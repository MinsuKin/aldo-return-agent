# ALDO data platform: runbook

Design and rules: `docs/data-platform/DESIGN.md`. This file is the operator's view: what to run, in which order, how to switch the demo to the AWS data and back.

## What exists
| Piece | Where | Notes |
| --- | --- | --- |
| CDK stack `AldoDataPlatform` | `infra/aldo_returns_demo/data_platform_stack.py`, `infra/deploy_data_platform.sh` | Only synthesised with `-c dataPlatform=true`; the main stack template is byte-identical with or without it. |
| Dataset upload | `scripts/data_platform/upload_dataset.py` | Streams the five CSVs, drops `cnsmr_prof_sid` and `customer_uid` from the returns file, flattens line breaks inside fields, uploads to `raw/<table>/` with SSE-S3. Prints headers and counts only. |
| Athena SQL | `scripts/data_platform/sql/01..03_*.sql` | Raw external tables, Parquet CTAS, the fit-insights query (the counting rules of `scripts/build_fit_insights.py`). |
| Pipeline | `scripts/data_platform/build_insights.py` | Runs the SQL in workgroup `aldo-demo`, shapes `fit_insights.json`, writes `curated/fit_insights/v=<utc>/fit_insights.json` and `latest.json`. |
| Parity | `scripts/data_platform/parity_report.py` | Compares the curated document with `fixtures/derived/fit_insights.json` per product and scope; Athena bytes scanned and cost. |
| The door in the backend | `backend/returns_domain/data_source.py` (+ the seam in `fit.load_insights`) | Modes `static` (default) / `platform` / `shadow`; any platform problem falls back to the bundled file. |
| Admin routes | `GET/POST /api/demo/data-platform` (admin key) | Also the three buttons in the admin panel (`/?admin=1`). |

## Order (needs valid AWS credentials in `new.txt`; run from the project root)
```
infra/deploy_data_platform.sh --diff        # read it: additions only
infra/deploy_data_platform.sh               # stack AldoDataPlatform
scripts/with_aws_env.sh backend/.venv/bin/python scripts/data_platform/upload_dataset.py
scripts/with_aws_env.sh backend/.venv/bin/python scripts/data_platform/build_insights.py
scripts/with_aws_env.sh backend/.venv/bin/python scripts/data_platform/parity_report.py --from-s3
infra/deploy.sh --real-code                 # backend with the door (default static, nothing changes for the demo)
```
Then in the admin panel (or with `aws ssm put-parameter`): `Shadow` first (compare), `Platform` when the gates in the design pass.

## Switch and rollback (no deploy needed, effective within 15 seconds)
- Admin panel: `/?admin=1`, type the admin secret, `Check`, then `Static` / `Platform` / `Shadow`.
- CLI: `scripts/with_aws_env.sh aws ssm put-parameter --name /aldo-demo/data-platform/mode --value static --overwrite --type String --region us-east-1`
- Emergency override inside a Lambda: environment variable `ALDO_DATA_MODE=static`.
- A missing parameter, an SSM error, a missing or broken curated document: all mean `static` for that call (log line `data_platform_fallback`).

## Remove everything (owner only; agents never delete)
`cd infra && ... cdk destroy AldoDataPlatform` (same environment variables as `infra/deploy_data_platform.sh`). The bucket is emptied by the stack (`autoDeleteObjects`). The demo keeps working: the door falls back to `static` by itself.

## Cost
S3 about 0.25 GB, Athena USD 5 per TB scanned (a full pipeline run scans about 0.5 GB because CSV is read twice: DDL/CTAS and the fit query read Parquet), Glue Data Catalog and SSM standard parameters free at this scale.

## Log lines to look for (CloudWatch, Lambda logs)
`data_platform_fallback mode=... reason=...`, `data_platform_shadow_mismatch key=... result={...}`, `data_platform_shadow_ok key=... products=N`.
