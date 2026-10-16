# ALDO data platform: the dataset on AWS, behind a two-way door

Status: design, 2026-10-15. Owner request: put the ALDO dataset on AWS data services, analyse it there, let the agentic workflow use the result in the demo, and be able to go back to the current demo at any time without a redeploy.

## Principles (the two-way door)
1. **Default = today's demo.** Mode `static` serves the bundled `fixtures/derived/fit_insights.json` exactly as now. Nothing changes until the mode is flipped.
2. **Flip without a deploy, both ways.** The mode lives in SSM Parameter Store and is changed from the admin panel (or `aws ssm put-parameter`). The code reads it with a 15 s cache.
3. **Never worse than static.** Any platform failure (missing object, S3 error, malformed document, timeout over 2 s) falls back to the bundled file for that call and logs `data_platform_fallback`.
4. **No Athena in the request path.** Athena runs offline (the pipeline). The agents read a curated document from S3 that the pipeline wrote. Request latency is one cached S3 object.
5. **Separate, removable unit.** One new CDK stack `AldoDataPlatform` (bucket, Glue database, Athena workgroup, SSM parameters, one IAM policy attached to the existing Lambda roles). The main stack `AldoReturnsDemo` and the AgentCore runtime (version 7) are NOT changed. Deleting the new stack returns the account to today's state (the owner runs `cdk destroy AldoDataPlatform`; agents never delete).
6. **Same schema.** The curated document has the schema of `fit_insights.json`, so the parser, the gateway tools and the agents do not change.

## Architecture
```
 local CSVs (dataset/, scrubbed of customer ids)
      | scripts/data_platform/upload_dataset.py
      v
 S3  s3://<bucket>/raw/{orders,returns,returnable,restocking,exchanges}/...      (private, SSE, TLS only)
      | Glue Data Catalog database aldo_oms (external tables)
      v
 Athena workgroup aldo-demo  --CTAS-->  s3://<bucket>/curated/parquet/...
      | scripts/data_platform/build_insights.py (boto3: start/poll Athena, shape the result)
      v
 s3://<bucket>/curated/fit_insights/v=<utc>/fit_insights.json  + curated/fit_insights/latest.json (pointer)
      | returns_domain/data_source.py   (mode: static | platform | shadow; ETag cache; fallback)
      v
 gateway (fit signal tools)  -->  personal agent / returns agent (unchanged)  -->  A2A trace shows the source
```

## Names and layout
- Bucket `aldo-data-platform-<account>-us-east-1`; Glue database `aldo_oms`; workgroup `aldo-demo` (results `s3://<bucket>/athena-results/`, enforced, scan cutoff 10 GB, engine v3).
- SSM: `/aldo-demo/data-platform/config` (JSON: bucket, region, database, workgroup, curated_prefix), `/aldo-demo/data-platform/mode` (`static` | `platform` | `shadow`, created as `static`).
- IAM: one managed policy `AldoDataPlatformAccess` attached to the worker and API Lambda roles: `s3:GetObject` and `s3:ListBucket` on `curated/*`, `ssm:GetParameter` on `/aldo-demo/data-platform/*`, `ssm:PutParameter` on the mode parameter only (admin route), `cloudwatch` metrics not needed (logs only). Athena/Glue permissions go to the pipeline's own credentials, not to the Lambdas.
- `curated/fit_insights/latest.json` = `{"key": "curated/fit_insights/v=2026-10-15T23-10-00Z/fit_insights.json", "etag": "...", "generated_at": "..."}`.

## Data handling
- Upload only what the analysis needs; **drop the customer identifier columns** `cnsmr_prof_sid` and `customer_uid` from the returns file before upload. Curated outputs are aggregates only (no order, return or customer ids).
- Bucket is private, `BlockPublicAccess.BLOCK_ALL`, SSE-S3, `enforceSSL`. The dataset stays out of git (`.gitignore` already lists it).

## The curated document
Same top-level keys as `fixtures/derived/fit_insights.json` (`generated_at`, `provenance`, `source`, `filter`, `thresholds`, `totals`, `products`) with the identical counting rules of `scripts/build_fit_insights.py` (reason normalised: trimmed, lower case, prefix `return.page.reasons.` dropped, whitespace collapsed; keep `too small` / `too big`; scopes `ALDO_CA` and `ALL`; blank product names excluded). Extra top-level key `platform`: `{"engine": "athena", "workgroup": "aldo-demo", "query_execution_ids": [...], "bytes_scanned": N, "pipeline_version": 1}`. `provenance` stays `derived`; `source` becomes `aws-athena`.
Parity gate: for every product and scope, `too_small`, `too_big`, `total_returns` equal the bundled file (or the difference is listed and explained).

## Modes and switching
- `static`: bundled file (today). `platform`: curated document from S3, fallback to bundled on any error. `shadow`: serve static, load platform in the same call, compare `n_fit` and `signal` for the requested product, log `data_platform_shadow_mismatch` (count, product).
- Resolution order: env `ALDO_DATA_MODE` (tests, emergencies) > SSM mode parameter > `static`. A missing parameter or any SSM error means `static`.
- Admin API (same admin key as the demo reset): `GET /api/demo/data-platform` returns `{mode, available, curated: {generated_at, key}}`; `POST /api/demo/data-platform {"mode": "..."}` writes the parameter. The admin panel (`?admin=1`) shows three buttons and the current mode.
- Trace: the `fit_signal_shared` event payload gains `data_source` (`bundled-json` or `aws-athena`) and `query_execution_ids` for the platform source; the trace row shows the extra chip `via S3 + Athena`. Static mode shows exactly what it shows today.

## Go / no-go gates before `platform` becomes the default
1. Parity report: 100% of products and scopes equal (or each difference explained).
2. Shadow: 0 mismatches over a John + David pass.
3. Live eval in `platform` mode: 19/19 (same as static), no new failing check.
4. Latency: the mean wall time of the John purchase scenarios (J1, J2) in `platform` mode is not more than 10% above `static`, and the no-model gateway call timed by `P6` is reported side by side (the platform path adds a few S3/SSM reads per container and minute; `P6` measures what that is instead of assuming it).
5. Browser: demo + a11y + visual specs green, 0 axe findings.
If any gate fails the mode stays `static` and the stack can be left idle or removed.

## Rollback
Admin panel button `Static`, or `aws ssm put-parameter --name /aldo-demo/data-platform/mode --value static --overwrite --type String --region us-east-1`. Full removal (owner): `cdk destroy AldoDataPlatform`; the code falls back to `static` by itself when the parameters are gone.

## Cost (estimate, simulated demo scale)
S3 about 0.3 GB (cents per month), Athena USD 5 per TB scanned: a full pipeline run scans about 1 to 2 GB (under 1 cent), Glue Data Catalog and SSM standard parameters free at this scale.

## Phase 2 (additive, not used for decisions)
Size-level fit rates (orders joined to returns on order number + SKU) and in-store exchange patterns (`Original Order Number`, `Exchange SKU`) as extra documents under `curated/`, shown only in the trace as evidence. Built only if phase 1 passes its gates.
