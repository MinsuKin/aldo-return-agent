# Pivot Plan: ALDO Fit Gateway for Agentic Commerce

Status: proposal for review. Builds on the existing `agentic-returns-gateway` spec; does not replace it.

Related docs:
- `aldo.txt` (data analysis + original "Fit Loop" recommendation)
- `.kiro/specs/agentic-returns-gateway/{requirements,design,tasks}.md`
- `HACKATHON_IMPLEMENTATION_HANDOFF.en.md` section 0 (scope + deadline)

Deadline reminder: feature freeze 2026-10-16 12:00, end 14:00 (America/Toronto). Account wiped after the event.

---

## 0. Why now: agents are already buying ALDO's shoes — blind (the pitch opener)

ALDO is already in agentic commerce. It just has no control over how.

- **Nov 2025 — ALDO Group moved its global e-commerce onto Shopify**, leaving the composable/custom stack it had run since 2017. ([Retail-Insider](https://retail-insider.com/retail-insider/2025/11/aldo-group-partners-with-shopify-to-redefine-unified-commerce/), [Shopify case study](https://www.shopify.com/case-studies/aldo-group))
- **Sept 8, 2026 — Meta launched Muse**, a consumer personal agent that browses and buys on a user's behalf; Shopify products were purchasable inside it within hours. ([Engadget](https://www.engadget.com/2253133/meta-reveals-its-ai-agent-that-can-shop-send-emails-and-plan-trips-on-your-behalf/), [Codilar](https://www.codilar.com/blog/meta-muse-ai-agent-what-it-means-for-shopify-merchants/))
- **Shopify auto-enrolls eligible stores into every new agentic channel, including Muse**, with direct checkout on by default unless the merchant opts out. ([Shopify help — managing agentic storefronts](https://help.shopify.com/en/manual/online-sales-channels/agentic-storefronts/agentic-home), [TechWyse](https://www.techwyse.com/news/ai-advertising/shopify-auto-enrollment-ai-shopping-channels-meta-muse))
- **Oct 6, 2026 — the Personal Agent Protocol (PAP)** was announced by Sierra with Meta, Shopify, Stripe, Walmart and others: an open standard for personal agents to shop on your behalf. The first spec is still pending — the rules are being written right now. ([Quasa](https://quasa.io/stories/personal-agent-protocol-lets-ai-shop-but-its-first-specification-is-pending))

**Where that leaves ALDO.** Because ALDO is on Shopify, personal agents like Muse can very likely already discover and buy ALDO shoes through Shopify's agentic storefront — ALDO was opted in by default, not by choice. But what that pipe provides is **transactional plumbing only**: catalog discovery and checkout. It gives ALDO none of what a footwear retailer with a returns problem actually needs:

- **No fit intelligence.** An agent can check out an ALDO shoe, but nothing tells it "this style runs small, size up." Agent purchases inherit — and at machine scale likely *worsen* — the size-return problem.
- **No returns control.** Shopify agentic checkout sells; it does not run ALDO's exchange-first logic, loyalty fee waivers or store-pickup economics.
- **No feedback loop.** The outcome ("8.5 fit") never flows back to the personal agent, so the next agent purchase repeats the mistake.
- **Generic, not ALDO's.** The reasoning in that pipe is Shopify's default checkout, not ALDO's brain.

So the urgency is not "ALDO is missing out." It is sharper and present-tense: **agents are already buying ALDO's shoes and sizing them blind. Every one is a return waiting to happen, at machine speed and scale, and ALDO has no gateway to intervene.** The gateway is not how ALDO gets *in* — Shopify already put it in. The gateway is how ALDO puts its **fit-and-returns brain on top of the checkout rail it already has**, so agent-driven sales fit the first time and stay sold. This prototype is that gateway, and its highest-value first job: getting the size right at purchase.

(Sources above rephrased for licensing compliance; see links for originals. Note: whether a specific ALDO storefront currently has direct agentic checkout toggled on should be confirmed with ALDO — Shopify's default is on, but merchants can opt out.)

---

## 1. One-line pitch

Personal agents like Meta Muse are already buying ALDO's shoes through Shopify's agentic checkout — but sizing them blind, with no fit intelligence and no returns control. This builds the gateway that adds ALDO's brain on top of that rail: any customer's personal agent can call it, and at purchase it negotiates the right size using the agent's cross-retailer fit memory, so the shoe fits the first time. If it still doesn't, the same gateway turns the return into a free in-store exchange and writes the correct size back to the agent's memory, so the next purchase — anywhere — is right too.

---

## 2. What changes vs. the current build

| Aspect | Current spec | After pivot |
| --- | --- | --- |
| Who talks to ALDO | A human customer in a web chat | A **personal agent** (A2A), with a human behind it |
| When the agent acts | After delivery, at return time | At **purchase** (sizing) **and** at return |
| Where fit memory lives | Inside ALDO (returns-derived only) | In the **personal agent** (cross-retailer), ALDO contributes its fit signal |
| Gateway role | P1 stretch item | **Centerpiece**, bidirectional |
| Return flow | Terminal outcome | Becomes a **feedback signal** that updates the personal agent's memory |

Nothing already built is thrown away. The policy engine, fit insight, options/quote/execution, trace UI and persona sign-in all stay. The pivot adds one coherent layer on top.

---

## 3. Why the agent-to-agent framing is stronger

1. **Solves cold-start.** ALDO's fit data only covers people who bought from ALDO and returned. A first-time customer has no ALDO history. Their personal agent already knows their sizes across retailers — exactly the data ALDO lacks and cannot get alone.
2. **Genuinely agentic.** Two agents negotiate a decision neither can make alone: ALDO contributes the per-style fit signal + live stock; the personal agent contributes the owner's cross-retailer size history and constraints (deadline, channel). This directly answers the rubric's "would an analytics model do the same?" — no, because the knowledge is split across two parties that must communicate.
3. **Privacy by design.** ALDO never sees raw purchase history or biometrics. The personal agent holds that and returns only a recommended size + confidence. Cleaner than ALDO hoarding a size profile; the right data boundary for agentic commerce.

---

## 4. Architecture

```
Personal Agent (holds cross-retailer fit memory)
        |  A2A: "my owner wants style X, help me size it"
        v
ALDO Agent Gateway  <-- the hero
        |
        |-- at PURCHASE: returns fit signal + stock + a size question
        |     -> personal agent answers with a recommended size
        |     -> ALDO confirms the size, cart updated
        |
        \-- at RETURN (existing flow): if a return still happens,
              prefer free in-store exchange, execute it, then PUSH the
              corrected size back to the personal agent's memory
```

Key insight: the **return flow already built becomes the training signal** for the personal agent. Each exchange outcome ("8.5 worked in this style") flows back across the gateway, so the loop closes outside ALDO's walls.

---

## 5. Demo: a real personal agent calling ALDO's gateway

### Decision: Option C — a local personal agent (our stand-in for Muse)

We cannot integrate with the real Meta Muse app: it ships with a fixed connector list, runs on Meta's infrastructure, and has no third-party plugin host — nothing we write executes inside it. ([Parallel.ai](https://parallel.ai/articles/meta-muse-custom-integrations), [Vectorize](https://hindsight.vectorize.io/blog/2026/09/28/meta-muse-agent-memory)) So instead of faking two chats, we build a **real personal agent that runs locally on the presenter's machine** and connects to ALDO's gateway the way the Personal Agent Protocol envisions.

What it is, precisely:

- A **real GPT-powered agent** (OpenAI API as the brain), not a scripted mock.
- It holds a seeded **cross-retailer size memory** (synthetic prior purchases).
- It calls ALDO's gateway tools over the A2A handshake (section 7).
- The gateway runs against the **synthetic ALDO environment** (DynamoDB fixtures) we already built.

**Honest framing for the pitch** (do not overclaim):
> "This is a Muse-style personal agent — PAP-aligned — powered by GPT and running locally. Real Meta Muse is a closed platform, so we built a stand-in that behaves the way a personal agent would and connects to ALDO's gateway exactly as Muse would under an open protocol."

The only thing that is "mock" is the ALDO data behind the gateway. The agent, its reasoning, its memory, and the gateway handshake are all real. Never imply we integrated with the actual Meta Muse product — a technical judge knows the platform is closed, and the claim would backfire.

**Why local, not the ChatGPT.com UI or a real Muse connector:** a public endpoint + vendor connector (Custom GPT Actions, or a real Muse connector that does not exist for third parties) depends on OpenAI/Meta servers reaching back into our network and on conference wifi, all live in front of judges. Running locally, the only outbound calls are our code → OpenAI API and our code → our gateway. We control every frame and it does not break on bad wifi.

### The demo flow

The selling moment is watching a real personal agent and ALDO negotiate, with a visible A2A trace between them.

- **Left — the personal agent chat (new, local, GPT-powered).**
  Customer: "Order me the ALDO Stessy heels for the wedding Saturday."
  The agent reasons over its own seeded memory ("I have your sizes from 3 retailers").
- **Right — existing ALDO Agent (gateway side).**
  Receives the A2A request, runs `get_fit_insight` (real data: Stessy runs small, n >= 100), checks stock, and **asks the personal agent** for a size decision instead of guessing.
- **Middle — A2A trace (reuse the existing trace timeline UI).**
  `fit_signal_shared` -> `size_requested` -> `size_recommended` (personal agent) -> `size_confirmed` -> cart updated.
  Then fast-forward: "shoe arrived, still slightly tight" -> return flow -> `free_exchange_created` -> `memory_updated` pushed back to the personal agent.

Already-have components: trace UI, real-data fit insight, options/quote/execution engine, persona sign-in.
Genuinely new (small): the local personal-agent chat (GPT API + tool-calling), its seeded cross-retailer size-memory store, and the gateway handshake endpoint (section 7).

---

## 6. Scope for the remaining time

### P0 — build this
- [ ] Local personal-agent chat (GPT API + tool-calling) with a seeded **cross-retailer size memory** (3–4 synthetic prior purchases, e.g. Nike 9, Adidas 9.5 returned-too-big, Clarks 9). Invented demo data — label clearly. Runs on the presenter's machine; a clean browser window for projecting.
- [ ] Gateway endpoint accepting an A2A "help me size this" request; returns ALDO's fit signal + stock + a size question (wraps existing `get_fit_insight` + a pre-purchase mode of `find_resolution_options`).
- [ ] Personal agent answers with a recommended size derived from its memory + ALDO's signal.
- [ ] Visible A2A trace tying the two agents together (new event types below).
- [ ] Existing return flow, re-ranked **store-exchange-first**, ending with a `memory_updated` write back to the personal agent.

### Cut to P1 / slide (per aldo.txt)
- [ ] Scenario I.
- [ ] The concurrent-execute half of scenario E (keep repeat-request protection).
- [ ] Impact tab -> slide instead.
- [ ] Foot-shape picker -> stretch only (the A2A memory largely replaces its purpose).

### New trace event types
`fit_signal_shared`, `size_requested`, `size_recommended`, `size_confirmed`, `memory_updated`.

---

## 7. A2A handshake contract (draft)

Purchase-time sizing request (personal agent -> ALDO gateway):
```json
{
  "intent": "size_assist",
  "style": "Stessy2.0",
  "color": "black",
  "deadline_date": "2026-10-18",
  "channel_pref": null,
  "owner_fit_hint": { "typical_size": 9.0, "unit": "US_W", "confidence": "med" }
}
```

ALDO gateway response:
```json
{
  "fit_signal": { "signal": "runs_small", "too_small_share": 0.71, "n_fit": 240, "provenance": "derived" },
  "in_stock_sizes": [8.0, 8.5, 9.0],
  "question": "This style runs small (n=240). Recommend a size for your owner.",
  "size_options": [8.5, 9.0]
}
```

Personal agent decision (-> ALDO):
```json
{ "chosen_size": 8.5, "rationale": "runs small + owner returned a 9.5 too big elsewhere", "confidence": "high" }
```

Post-exchange memory write-back (ALDO -> personal agent):
```json
{ "style": "Stessy2.0", "confirmed_size": 8.5, "outcome": "kept_after_exchange", "source": "ALDO_CA" }
```

Server owns all amounts/stock/policy as today; the personal agent's numbers are never authoritative on ALDO's side.

---

## 8. Honest caveats (keep these in the pitch)

- **Pre-purchase impact is estimated, not measured.** The returns CSV proves the size-return problem is real (53–56% of web returns are size) but cannot prove a prevention rate. Frame prevention as a pilot hypothesis against that measured baseline.
- **Cross-retailer memory is simulated, and the personal agent is a stand-in.** The agent is a real GPT-powered local agent, but its memory is seeded synthetic data and it is *not* the actual Meta Muse product (closed platform). Frame it as a Muse-style, PAP-aligned stand-in. In reality the memory and handshake would ride a shared standard like the Personal Agent Protocol — whose first spec is still pending — so position ALDO as an early mover getting ahead of a standard that is being written right now.
- **"Ship two sizes, return one"** (3% of orders, 46% of which return nothing) is a gentle nudge the agents negotiate, never a block. A2A handles it: personal agent can say "owner wants to try both" -> ALDO offers try-both-in-store.
- **Continuity.** This is an evolution of the Phase 1 gateway pitch, not a reversal. The gateway is how outside personal agents reach ALDO; its highest-value job turns out to be sizing at purchase, not just returns.

---

## 9. Open questions for ALDO

- Why are ~two-thirds of web/mail returns fee-free today (loyalty, exchanges)?
- Any fit-review or width data available?
- Does "Style" include colour?

---

## 10. Next step

On approval, update the spec:
1. New requirements section: A2A fit-gateway + pre-purchase sizing handshake.
2. Design: mock personal-agent panel, gateway request/response contract, `memory_updated` write-back, new trace events.
3. tasks.md: the P0 build list above and the P0/P1 re-cut.
