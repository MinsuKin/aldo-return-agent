# Implementation Plan

Order is dependency order. Workstream tags (`[data]`, `[domain]`, `[agent]`, `[api]`, `[infra]`, `[ui]`, `[eval]`) mark what can be handed to different people later; tasks with the same tag after a checkpoint can run in parallel. Tasks marked `*` are P1 and start only after checkpoint 3 passes. Feature freeze 2026-10-16 12:00.

- [ ] 1. Project skeleton and pinned tooling `[infra]`
  - Create `backend/` (pyproject with pinned versions: boto3, pydantic, aws-lambda-powertools, strands-agents, bedrock-agentcore, pytest, moto), `frontend/` (Vite React TS), `infra/` (CDK Python), `fixtures/`, `docs/impact/`, `docs/eval/`.
  - Check current Strands and AgentCore CLI/starter-toolkit install commands and entrypoint API against official docs; record exact versions in lockfiles and README.
  - Add `.gitignore` entries: `dataset/`, `docs/sources/`, `backend/fixtures_bundle/`, `cdk.out/`, `.bedrock_agentcore*`; keep `new.txt` ignored.
  - _Requirements: 15.1, 15.2, 15.4_

- [ ] 2. Derived data from the provided CSVs `[data]`
  - [ ] 2.1 `scripts/build_fit_insights.py`: stream returns CSV, normalized reason match for `too small` / `too big`, per product name for ALDO_CA and ALL, thresholds per req 5.3, write `fixtures/derived/fit_insights.json` with SHA-256, row counts, filter text. No customer identifiers.
    - _Requirements: 5.1, 5.2, 5.3_
  - [ ] 2.2 Pick the hero product (`runs_small`, n ≥ 100 in ALDO_CA) and a contrast product (`mixed` or `runs_large`) from the output; record choice and numbers in `fixtures/demo/README.md`.
    - _Requirements: 5.4_
  - [ ] 2.3 `scripts/build_impact_model.py`: window 2025-02-01..2026-01-31, ALDO_CA, RETURN_COMPLETED; fit lines, refund sum/median, lines/day mean+p95, exchange CONFIRMED/ASSUMED counts; low/base/high scenarios; write `docs/impact/impact-model.{json,md}` with measured/derived/assumed tags. Agent cost input reads `docs/eval/results.json` if present, else placeholder tagged `pending_measurement`.
    - _Requirements: 13.1, 13.2, 13.3_

- [ ] 3. Demo fixtures `[data]`
  - Write `fixtures/demo/{customers,orders,products,locations,inventory,logistics,policy}.json` per design section 4 (10 orders, 3 personas, 4 locations, size runs for hero/contrast products, reset variants `default`, `dc_stockout`, `all_stockout`). All IDs `DEMO-`, every record has `source: "synthetic"`.
  - _Requirements: 2.1, 4.1, 6.6, 16.1_

- [ ] 4. Domain core (pure logic) `[domain]`
  - [ ] 4.1 `config.py`, `errors.py`, `money.py`, `clock.py`, `taxonomy.py`, `auth.py` (HMAC token sign/verify/expiry).
    - _Requirements: 1.1, 1.3, 12.3, 12.5_
  - [ ] 4.2 `policy.py` with rule order from design 5.1; unit tests for day 30/31, final sale × reason, qty remaining, missing delivery date, unknown/worn condition, CAD 250 authority limit, "ignore policy" text having no effect (policy takes no free text).
    - _Requirements: 4.1, 4.2, 4.3, 4.4_
  - [ ] 4.3 `fit.py` lookup over the derived file; unit test thresholds.
    - _Requirements: 5.3_

- [ ] 5. Persistence and transactional services `[domain]`
  - [ ] 5.1 `store.py` repository with all key builders from design section 3; `fixtures.py` reset/seed with variants; `scripts/seed_demo.py`.
    - _Requirements: 16.1_
  - [ ] 5.2 `trace.py` (atomic seq, redaction) and audit writer.
    - _Requirements: 11.1, 11.4, 1.4_
  - [ ] 5.3 `options.py` (replacement SKU lookup, multi-location ATP, five option types, deadline flag, persisted candidate set).
    - _Requirements: 6.1–6.6, 3.3_
  - [ ] 5.4 `quotes.py` (immutable quote, payload hash, 15-min expiry, conditional approval, conflict handling).
    - _Requirements: 7.1, 7.4, 7.5, 7.6_
  - [ ] 5.5 `execution.py` (`TransactWriteItems`, cancellation-reason mapping, idempotent replay, status read, server-generated next step).
    - _Requirements: 8.2–8.7, 9.1_
  - [ ] 5.6 `cases.py`.
    - _Requirements: 10.2_
  - [ ] 5.7 moto integration tests: happy path, double approve same/different, execute replay, stock condition failure writes nothing, qty condition, ownership on every service function. Assert table state, not return messages.
    - _Requirements: 7.4, 7.6, 8.3–8.6, 1.4_

- [ ] 6. Checkpoint 1: domain green
  - All unit and integration tests pass locally (`pytest backend/tests`). Ask the user before continuing if any requirement turned out ambiguous.

- [ ] 7. AWS base infrastructure `[infra]`
  - CDK stack: DynamoDB table + GSI1, Secrets Manager secret, API Lambda, Worker Lambda, HTTP API, S3 + CloudFront with `/api/*` behavior and SPA fallback, IAM per design section 9, outputs. Deploy to `us-east-1` with the Workshop Studio credentials (never commit them). Seed the real table.
  - _Requirements: 15.1, 15.3_

- [ ] 8. Agent `[agent]`
  - [ ] 8.1 `agent/tools.py`: ten tools from design 6.3 bound to `RunContext`, uniform result envelope, trace events, retries, round limit.
    - _Requirements: 2.4, 6.2, 7.7, 10.1, 11.1, 11.3, 12.1–12.3_
  - [ ] 8.2 `agent/prompt.py` system prompt per design 6.4.
    - _Requirements: 3.1–3.4, 4.4, 6.3–6.5, 9.2, 10.3, 12.4_
  - [ ] 8.3 `agent/runner.py` + `agent_app.py`: conversation rehydration from DynamoDB, approval continuation message, metrics capture, fallback model.
    - _Requirements: 8.1, 11.1_
  - [ ] 8.4 Local smoke run against the real table and real Bedrock (using Sonnet 5.5 with Opus fallback): hero message on DEMO-1001 reaches `quote_created`; approval continuation creates one transaction. Fix prompt/tool issues found.
    - _Requirements: 3, 5.4, 6, 7, 8, 9_
  - [ ] 8.5 Deploy to AgentCore Runtime with the CLI/starter toolkit from `backend/` (bundle fixtures first), set env vars and execution role permissions, store ARN in SSM `/aldo-demo/runtime-arn`, test one invocation.
    - _Requirements: 15.2_

- [ ] 9. API and worker `[api]`
  - Powertools routes from design section 7 with Pydantic validation, error mapping, one-active-run guard, approve → continuation run; worker invoking the runtime (and `AGENT_MODE=local`). Redeploy CDK with the runtime ARN.
  - _Requirements: 1.1–1.5, 2.1, 2.2, 7.2–7.5, 8.1, 11.2, 16.1_

- [ ] 10. Checkpoint 2: end to end through the cloud API
  - Using curl or a small script against the CloudFront URL: sign in → message → events → quote → approve → events → transaction. Confirm in DynamoDB: one TXN, ATP −1, line qty +1, approval consumed.

- [ ] 11. Frontend `[ui]`
  - [ ] 11.1 Sign-in, demo banner, API client with token, order list.
    - _Requirements: 1.1, 2.1, 16.2_
  - [ ] 11.2 Chat with suggested prompts, run polling, transcript with `aria-live`.
    - _Requirements: 2.2, 11.2, 16.3_
  - [ ] 11.3 Option cards from `GET /quotes/{id}`, confirmation dialog, approve, confirmation panel with persisted transaction and "simulated" wording.
    - _Requirements: 7.2, 7.3, 8.7, 9.1_
  - [ ] 11.4 Agent trace timeline with provenance badges, decision lines, error codes, run metrics footer.
    - _Requirements: 5.5, 11.1, 11.2_
  - [ ] 11.5 Impact tab, Cases list, hidden admin reset controls.
    - _Requirements: 13.4, 10.2, 16.1_
  - [ ] 11.6 Build and deploy to S3 + CloudFront (`scripts/deploy_frontend.sh`); keyboard-only and contrast check.
    - _Requirements: 15.1, 16.3_

- [ ] 12. Scenario eval and measured metrics `[eval]`
  - `scripts/eval_scenarios.py` for A, B, C, D, E, H, I per requirement 14, reset before each, 3 runs, DynamoDB assertions, latency/tokens/cost; write `docs/eval/results.{json,md}`. Re-run `build_impact_model.py` so agent cost is measured. Fix failures in prompt/tools and re-run.
  - _Requirements: 14.1–14.3, 13.2_

- [ ] 13. Checkpoint 3: demo ready (target Oct 16 09:00)
  - Eval pass rate recorded; full UI demo of hero flow (wedding message, `dc_stockout` variant) and final-sale defect escalation works twice in a row from reset; screen recording saved locally.

- [ ] 14. Presentation evidence and README `[eval]`
  - README: architecture, deploy order, env var names, local run, reset, eval, teardown, export-before-wipe checklist. Export impact and eval markdown, architecture diagram, screenshots for slides. Update handoff section 16 run-of-show numbers with measured values.
  - _Requirements: 15.4_

- [ ]* 15. Bedrock Guardrail on customer input (`ApplyGuardrail` before run, `guardrail_blocked` event)
  - _Requirements: 17.1_
- [ ]* 16. AgentCore Observability / Transaction Search, capture one trace screenshot
  - _Requirements: 17.2_
- [ ]* 17. `route_returned_item` disposition tool and trace event
  - _Requirements: 17.3_
- [ ]* 18. AgentCore Gateway (MCP) Lambda target with inbound JWT, one external MCP client call
  - _Requirements: 17.4_
- [ ]* 19. Associate console approve/deny
  - _Requirements: 17.5_
- [ ]* 20. Cognito sign-in
  - _Requirements: 17.6_

- [ ] 21. Feature freeze and backup (Oct 16 12:00)
  - Tag release in Git, push to the private repo, save recording, eval results, impact model and CloudWatch exports to the laptop before the account wipe.
