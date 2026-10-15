# Pitch notes (running list, add more below)

## Scalability: how the data side works today and how it scales

Use for the "Scalability and feasibility" slide and for jury questions.

What is true today (say this):
- The raw ALDO files (OMS orders, returns, in-store exchanges, the 2025 transaction detail) are analysed offline on a laptop with plain Python. They are never uploaded to AWS.
- The analysis produces small derived files: per-style fit signals, the impact model, and demo fixtures. These are bundled with the backend and read at run time.
- At run time ALDO bot reads those small precomputed files, so answers are fast and no large file is scanned during a conversation.
- AWS services the live demo actually uses: Amazon Bedrock (Claude Sonnet 5.5), Bedrock AgentCore Runtime, API Gateway, Lambda, DynamoDB, CloudFront, Secrets Manager.

What to propose as the next step (say "design", not "built"):
- Land the raw files in Amazon S3, catalogue them with AWS Glue, and aggregate with Amazon Athena on a schedule (for example nightly).
- Publish the refreshed per-style fit signals to the gateway, so the signal stays current as new returns arrive.
- Keep orders, approvals, benefit ledgers and owner memory in DynamoDB as they are now.

Wording rules:
- Do not claim the S3, Glue and Athena pipeline exists. It is a proposed production design.
- Fit signals are derived from the provided ALDO data. Cross-retailer owner memory, product reviews, customer tiers and perks are simulated or demo assumptions.
- The supplied e-commerce (OMS) extract has ladies' categories only, but the ALDO POS transaction file contains men's footwear in sizes 7 to 14. "ALDO Men 001" is a demo product; its fit signal is the real 24-month OMS return signal of a men's style (118 fit returns in ALDO_CA, 91.5% "too big"). See `fixtures/demo/README.md`.

Likely jury question: "Could this operate at retail scale?"
Short answer: the per-request work is cheap and bounded (measured cost about CAD 0.24 per return run), the data refresh is a batch job, and the transactional state is DynamoDB. Busiest-day load from the impact model is well within normal Bedrock and Lambda patterns; account quotas should be checked and raised before launch.
