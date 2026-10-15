# Pivot Plan: ALDO Fit Gateway for Agentic Commerce

Status: proposal for review. Builds on the existing `agentic-returns-gateway` spec; does not replace it.

Related docs:
- `aldo.txt` (data analysis + original "Fit Loop" recommendation)
- `.kiro/specs/agentic-returns-gateway/{requirements,design,tasks}.md`
- `HACKATHON_IMPLEMENTATION_HANDOFF.en.md` section 0 (scope + deadline)

Deadline reminder: feature freeze 2026-10-16 12:00, end 14:00 (America/Toronto). Account wiped after the event.

---

## 1. One-line pitch

ALDO runs an agent gateway that any customer's personal AI agent can call. At purchase it negotiates the right size using the personal agent's cross-retailer fit memory, so the shoe fits the first time. If it still doesn't, the same gateway turns the return into a free in-store exchange and writes the correct size back to the personal agent's memory, so the next purchase — anywhere — is right too.

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

## 5. Demo: two chat windows side by side

The selling moment is seeing two agents talk, with a visible A2A trace between them.

- **Left — mock Personal Agent chat (new, lightweight).**
  Customer: "Order me the ALDO Stessy heels for the wedding Saturday."
  Personal agent reasons over its own seeded memory ("I have your sizes from 3 retailers").
- **Right — existing ALDO Agent.**
  Receives the A2A request, runs `get_fit_insight` (real data: Stessy runs small, n >= 100), checks stock, and **asks the personal agent** for a size decision instead of guessing.
- **Middle — A2A trace (reuse the existing trace timeline UI).**
  `fit_signal_shared` -> `size_requested` -> `size_recommended` (personal agent) -> `size_confirmed` -> cart updated.
  Then fast-forward: "shoe arrived, still slightly tight" -> return flow -> `free_exchange_created` -> `memory_updated` pushed back to the personal agent.

Already-have components: trace UI, real-data fit insight, options/quote/execution engine, persona sign-in.
Genuinely new (small): a mock personal-agent chat panel, a size-memory store for that mock agent, two "handshake" messages across the gateway.

---

## 6. Scope for the remaining time

### P0 — build this
- [ ] Mock personal-agent panel with a seeded **cross-retailer size memory** (3–4 synthetic prior purchases, e.g. Nike 9, Adidas 9.5 returned-too-big, Clarks 9). Invented demo data — label clearly.
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
- **Cross-retailer memory is simulated.** In reality this needs a shared fit protocol/standard. Position ALDO as an early adopter of agentic commerce — the first retailer whose gateway speaks to personal agents.
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
