# Final A2A pitch revision

This supersedes the two-main-slide structure in FINAL_DEMO_SLIDES_PLAN.md. The live Figma deck is https://www.figma.com/slides/hJdVlodsi8FH07rKXiqyib/BSRMxALDO-FInal.

## Story and timing

Five short main slides support a four-minute demo. Target 7:30, leaving 30 seconds within the official eight-minute limit.

1. **The caller is changing** — 0:00–0:35. Define A2A as agent to agent. Personal agents create another way to shop. Meta launched Muse September 8. PAP was announced October 6 and a draft published October 9. Lead with what this changes for a retailer, not a list of partner logos.
2. **Fit knowledge has to travel with the purchase** — 0:35–1:05. 13,317 of 61,998 recorded return lines gave a size reason. ALDO has style-specific evidence that a generic checkout does not itself supply. If agent shopping grows without that knowledge, the existing problem could grow with it; no volume multiplier is measured.
3. **ALDO Fit Gateway** — 1:05–1:40. Show the gateway loop: shopper memory, ALDO fit evidence and stock, purchase recommendation, human approval, return resolution and outcome write-back. Current prototype uses a personal agent and an ALDO agent (purchase and return workflows) over HTTP. PAP connector is planned, not integrated or certified.
4. **Live demo** — 1:40–5:40, off-slide. John purchases, confirms and exchanges for another colour, with confirmed fit recorded. David returns a bundle using a bounded tier benefit. Use recorded David footage if needed. The exchange must suit the customer, who can still choose a refund.
5. **Exchange value, step by step** — 5:40–6:40. 13,317 × assumed 20% = about 2,663 additional retained exchanges. At assumed CAD 54 each, CAD 143,824 illustrative value less CAD 15,183 estimated AI cost gives about CAD 129k. The earlier CAD 132k additionally assumed fewer repeat returns. The new headline excludes that bonus.
6. **The ask: a small ALDO pilot** — 6:40–7:30. Define an eligible cohort, compare with current flow, measure incremental retained exchanges, customer satisfaction and actual costs. Then implement the PAP connector and permission mapping against the published draft, with conformance verification before claiming support.

The numbered list includes the off-slide demo, so the deck has five main slides plus four skipped jury backups.

## Opening script

“Your next customer may send an agent. A2A means agent to agent: the customer's assistant works directly with a business. Meta launched Muse last month. Sierra and Meta have already published a draft Personal Agent Protocol with industry partners, including Shopify.

“That gives businesses a way to recognize an agent and control its access. ALDO still has to supply the knowledge behind a good shoe purchase: how the style fits, what is available, and which resolution is allowed. That's the role of ALDO Fit Gateway.”

## Sources checked October 15, 2026

- Meta launch: https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/ — September 8, 2026.
- Sierra announcement: https://sierra.ai/blog/introducing-personal-agent-protocol — October 6, 2026.
- Sierra draft publication: https://sierra.ai/blog/poppy — October 9, 2026.
- Official draft: https://personalagentprotocol.org/docs/introduction — Draft 0.1, OAuth/JWT/HTTPS, company-controlled access and extensibility.
- Shopify eligibility and defaults: https://help.shopify.com/en/manual/online-sales-channels/agentic-storefronts/meta — direct checkout on by default for eligible stores; current guidance includes US, Canada and Mexico. ALDO Canada's own eligibility, terms acceptance and settings remain unconfirmed.
- ALDO Shopify announcement: https://www.aldogroup.com/en/news — company announcement dated October 27, 2025.

## Corrections to the supplied narrative

The draft has landed. Do not say the first specification is still due later this month. It remains a draft, not a final standard.

PAP's published materials explicitly discuss returns and exchanges. Our claim is that the transport and authorization standard does not supply ALDO-specific fit evidence and retail decision logic. Do not claim PAP ignores post-purchase tasks.

Do not say ALDO is already selling through Muse unless ALDO verifies its participation. A Shopify relationship and channel defaults alone do not establish that.

“A2A-shaped, with PAP integration planned” accurately separates the prototype's workflow structure from protocol integration. “PAP-ready” would suggest compatibility that has not been tested.

## Jury backups

- AWS execution and data boundaries: existing editable architecture.
- Exchange-adoption sensitivity: CAD 57k / 129k / 201k, same unit-value and AI-cost assumptions, no repeat-return bonus.
- Responsible AI and prototype scope: approval, code-enforced policy, restricted context, review screening, escalation and disclosure.
- Where CAD 54 comes from: hypothetical refund and exchange comparison, with source and excluded costs.

## Business impact companion

Read `docs/impact/BUSINESS_IMPACT_EXPLAINED.md` before presenting. It explains every input, the original CAD 132k calculation, the simpler CAD 129k scenario, cost limitations, and why a pilot must measure incremental retained exchanges beyond today's behavior.
