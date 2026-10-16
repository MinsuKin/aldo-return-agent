import { expect, test, type Page } from "@playwright/test";
import { ADMIN_SECRET, IS_MOCK, MOCK_URL, latestSizeRequestId, mockPost, onMock, resetDemo, shot, signInAs } from "./helpers";

// Functional specs for the phone UI: one chat with the personal agent on a phone (left) and the live A2A trace (right).
// They run against the mock API by default (scripted John and David as in addendum 11.2) and, with
// E2E_TARGET=deployed, against the real site (scripted wording is only asserted on the mock).

const STRIP = "Prototype with simulated data";
const trace = (page: Page) => page.getByTestId("a2a-trace");
const traceEvent = (page: Page, type: string) => trace(page).locator(`[data-event-type="${type}"]`);

/** The user-visible naming rules (addendum 12): one brand, no second agent, no demo labels on elements. */
async function expectNaming(page: Page): Promise<void> {
  const body = page.locator("body");
  await expect(body).not.toContainText(/Returns Agent/i);
  await expect(body).not.toContainText(/Muse/);
  await expect(body).not.toContainText("Simulated data");
  await expect(page.getByTestId("phone")).not.toContainText(/simulated|synthetic/i);
  await expect(page.getByTestId("a2a-trace")).not.toContainText(/simulated|synthetic/i);
}

test.describe("first screen and layout", () => {
  test("persona picker: John and David with tier and place, the strip, regression customers only in admin mode", async ({ page, request }) => {
    await resetDemo(request, "default");
    await page.goto("/");
    await expect(page.getByTestId("proto-banner")).toHaveText(STRIP);
    await expect(page.getByRole("heading", { name: "Who is using the personal agent?" })).toBeVisible();
    const john = page.getByTestId("persona-DEMO-CUST-JOHN");
    const david = page.getByTestId("persona-DEMO-CUST-DAVID");
    await expect(john).toContainText("Silver member");
    await expect(john).toContainText("Bronfman Building, Montreal");
    await expect(david).toContainText("Platinum member");
    await expect(david).toContainText("Rural New Brunswick");
    for (const id of ["AVA", "BEN", "CHLOE"]) await expect(page.getByTestId(`persona-DEMO-CUST-${id}`)).toHaveCount(0);
    await expect(page.getByTestId("admin-panel")).toHaveCount(0);
    await expectNaming(page);

    await page.goto("/?admin=1");
    for (const id of ["JOHN", "DAVID", "AVA", "BEN", "CHLOE"]) await expect(page.getByTestId(`persona-DEMO-CUST-${id}`)).toBeVisible();
    await expect(page.getByTestId("admin-panel")).toBeVisible();
    await expect(page.getByTestId("proto-banner")).toHaveText(STRIP);
  });

  test("wide screens: phone on the left at its aspect ratio, trace on the right, no page scroll; narrow screens stack", async ({ page, request }) => {
    await resetDemo(request, "default");
    await page.goto("/");
    const phone = page.getByTestId("phone");
    const traceBox = page.getByTestId("trace-panel");
    await expect(phone).toBeVisible();
    await expect(traceBox).toBeVisible();
    const p = (await phone.boundingBox())!;
    const t = (await traceBox.boundingBox())!;
    expect(p.x).toBeLessThan(t.x);
    expect(p.x + p.width).toBeLessThanOrEqual(t.x + 2);
    expect(Math.abs(p.width / p.height - 440 / 956)).toBeLessThan(0.01);
    expect(p.y).toBeGreaterThanOrEqual(0);
    expect(p.y + p.height).toBeLessThanOrEqual(900);
    expect(t.height).toBeGreaterThan(p.height * 0.9);
    const scroll = await page.evaluate(() => ({ scrollHeight: document.documentElement.scrollHeight, clientHeight: document.documentElement.clientHeight }));
    expect(scroll.scrollHeight).toBeLessThanOrEqual(scroll.clientHeight + 1);
    await expect(page.getByTestId("proto-banner")).toBeVisible();
    await expect(page.getByTestId("trace-note")).toContainText("public fields only");

    await page.setViewportSize({ width: 1000, height: 900 });
    await expect(page.getByTestId("stage")).toHaveAttribute("data-layout", "stacked");
    const p2 = (await phone.boundingBox())!;
    const t2 = (await traceBox.boundingBox())!;
    expect(p2.y + p2.height).toBeLessThanOrEqual(t2.y + 2);
    expect(Math.abs(p2.width / p2.height - 440 / 956)).toBeLessThan(0.01);
    await expect(page.getByTestId("proto-banner")).toBeVisible();
  });
});

test.describe("John (Silver): the stage demo", () => {
  test("buy the Toney, confirm, delivery push, then a colour exchange in store", async ({ page, request }) => {
    await resetDemo(request, "default");
    await signInAs(page, "John");
    await expect(page.getByTestId("chat-sub")).toContainText("Silver member");
    await expect(page.getByTestId("workflow-pill")).toContainText("Purchase");
    await expect(page.getByTestId("chat-log")).toHaveAttribute("aria-live", "polite");

    // ---- Act 1: the personal agent proposes, nothing is ordered before the click -----------------------------
    await page.getByTestId("quick-john-1").click();
    await expect(page.getByTestId("bubble-user").last()).toContainText("Toney");
    const card = page.getByTestId("purchase-card");
    await expect(card).toHaveAttribute("data-status", "proposed");
    await expect(page.getByTestId("typing")).toHaveCount(0);
    await expect(page.getByTestId("confirm-purchase-open")).toBeEnabled();
    await expect(page.getByTestId("order-card")).toHaveCount(0);
    await expect(card.getByTestId("purchase-facts")).toContainText(/CAD \d+\.\d\d/);
    await expect(card).toContainText("derived from provided ALDO data");
    await expect(card.getByTestId("purchase-countdown")).toBeVisible();
    // The real ALDO photo of the Toney in black is on the card and it loaded. A missing file would be answered with the app's own page
    // (HTTP 200, not an image), so look at the decoded pixels, not at the status.
    await expect
      .poll(() => card.locator("img").first().evaluate((el) => (el as HTMLImageElement).complete && (el as HTMLImageElement).naturalWidth > 0))
      .toBe(true);
    await onMock(async () => {
      await expect(card).toContainText("Toney");
      await expect(card).toContainText("9.5");
      await expect(card).toContainText("CAD 149.99");
      await expect(card).toContainText("Runs large: 91.5% of fit-related returns were too big (n=118)");
      await expect(page.getByTestId("reviews-evidence")).toHaveText("Read 16 reviews, 1 flagged and excluded");
      await expect(page.getByTestId("purchase-rationale")).toContainText("runs large");
      await expect(page.getByTestId("bubble-bot").last()).toContainText("I haven't ordered anything yet");
    });

    // The trace: ALDO Data shares the fit signal and the (flagged) reviews, the personal agent proposes.
    for (const type of ["size_assist_requested", "fit_signal_shared", "size_requested", "reviews_shared", "size_recommended"]) {
      await expect(traceEvent(page, type).first()).toBeVisible();
    }
    await expect(traceEvent(page, "fit_signal_shared").first()).toContainText("derived from provided ALDO data");
    await expect(traceEvent(page, "reviews_shared").first()).toContainText("1 flagged");
    await expect(traceEvent(page, "size_assist_requested").first().getByTestId("actor-chip")).toHaveText("Personal agent");
    await expect(traceEvent(page, "fit_signal_shared").first().getByTestId("actor-chip")).toHaveText("ALDO Data");
    // Public fields only: the personal agent's private memory (retailers, typical size) is not in the trace.
    const traceText = (await trace(page).innerText()).toLowerCase();
    expect(traceText).not.toContain("retailer");
    expect(traceText).not.toContain("typical");
    // The run's tool calls sit in a collapsed group and expand on demand; private tools show no details.
    const group = page.getByTestId("trace-run").first();
    await expect(group.getByTestId("run-toggle")).toHaveAttribute("aria-expanded", "false");
    await group.getByTestId("run-toggle").click();
    await expect(group.getByTestId("run-toggle")).toHaveAttribute("aria-expanded", "true");
    await expect(group.locator('[data-tool="ask_aldo_size_assist"]')).toBeVisible();
    await expect(group.locator('[data-tool="recall_size_memory"]')).toContainText("Private to your personal agent");
    await expect(group.locator('[data-tool="recall_size_memory"]')).not.toContainText("10.5");

    // Confirm opens a sheet; Escape closes it and nothing is ordered.
    await page.getByTestId("confirm-purchase-open").click();
    const dialog = page.getByTestId("purchase-dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("Confirm your purchase");
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(page.getByTestId("confirm-purchase-open")).toBeFocused();
    await expect(page.getByTestId("order-card")).toHaveCount(0);

    // The owner's click places the order; five seconds of "out for delivery", then the push banner.
    await page.getByTestId("confirm-purchase-open").click();
    await page.getByTestId("confirm-purchase").click();
    await expect(page.getByTestId("order-card")).toBeVisible();
    await expect(page.getByTestId("order-card")).toContainText("Order placed");
    await expect(page.getByTestId("order-id")).toHaveText(/^DEMO-A2A-[0-9A-F]{6}$/);
    await expect(page.getByTestId("delivery-status")).toHaveAttribute("data-state", "out");
    const push = page.getByTestId("push-banner");
    await expect(push).toContainText("Delivered: Toney, size 9.5", { timeout: 20_000 });
    await expect(page.getByTestId("delivery-status")).toHaveAttribute("data-state", "delivered");
    await expect(page.getByTestId("bubble-bot").last()).toContainText("was delivered");
    await expect(traceEvent(page, "size_confirmed").first()).toContainText("Confirmed by the owner");
    await expect(traceEvent(page, "delivered").first()).toContainText("Delivered");

    // ---- Act 2: return workflow, colour exchange in store ---------------------------------------------
    await page.getByTestId("quick-john-2").click();
    await expect(page.getByTestId("workflow-pill")).toContainText("Return");
    await expect(page.getByTestId("bubble-bot").last()).toContainText("reason");
    // While the ALDO agent waits for the reason, the usual reasons are one tap away; the owner can still type their own.
    const reasons = page.getByTestId("reason-chips");
    await expect(reasons).toBeVisible();
    await expect(reasons.getByRole("button")).toHaveText(["Changed my mind", "Damaged or defective", "Size too big", "Size too small", "Wrong colour", "Something else"]);
    await expect(reasons.getByTestId("reason-unworn")).toBeChecked();
    await expect(traceEvent(page, "handoff_to_returns").first()).toContainText("Personal agent hands over to the ALDO agent");
    await expect(traceEvent(page, "memory_consulted").first()).toContainText("Using your preferences");
    await expect(traceEvent(page, "memory_consulted").first().getByTestId("actor-chip")).toHaveText("ALDO agent");

    await page.getByTestId("quick-john-3").click();
    await expect(page.getByTestId("bubble-bot").last()).toContainText("Brown");
    await expect(page.getByTestId("reason-chips")).toHaveCount(0);
    await expect(traceEvent(page, "memory_consulted")).toHaveCount(1);
    await page.getByTestId("quick-john-4").click();
    // The bubble is model text on a real deployment ("5 minutes away") and scripted on the mock ("5 min away").
    await expect(page.getByTestId("bubble-bot").last()).toContainText(/5 min(ute)?s? away/);
    await page.getByTestId("quick-john-5").click();

    const quote = page.getByTestId("quote-card");
    await expect(quote).toBeVisible();
    await expect(page.getByTestId("typing")).toHaveCount(0);
    const primary = quote.getByTestId("primary-option");
    await expect(primary).toContainText("Exchange in store");
    await expect(primary).toContainText("black 9.5");
    await expect(primary).toContainText("brown 9.5");
    await expect(primary).toContainText("ALDO Sainte-Catherine");
    await expect(primary).toContainText("5 min away");
    // The real store's street address sits under its name.
    await expect(primary.getByTestId("store-address")).toContainText("1009 Sainte-Catherine St. West #1007");
    // Ready date: scripted "Today" on the mock; the deployed fixture says Sat Oct 17 (pickup_ready_days 1, builder section 8).
    await expect(primary).toContainText(IS_MOCK ? "Today" : /Sat Oct 17|Tomorrow/);
    await expect(primary).toContainText("Free");
    await expect(quote.getByTestId("confirm-option-open")).toHaveText("Confirm exchange");
    // Refund options stay reachable under "See other options".
    await quote.getByTestId("see-other-options").click();
    await expect(quote.getByTestId("see-other-options")).toHaveAttribute("aria-expanded", "true");
    await expect(quote.locator('[data-option-type="return_in_store"]')).toBeVisible();
    await expect(quote.locator('[data-option-type="return_mail"]')).toBeVisible();
    await expect(traceEvent(page, "quote_created").first()).toContainText("Options ready");
    await expect(page.getByTestId("txn-card")).toHaveCount(0);

    await quote.getByTestId("confirm-option-open").click();
    const approve = page.getByTestId("approve-dialog");
    await expect(approve).toBeVisible();
    await expect(approve).toContainText("Confirm your exchange");
    await expect(approve).toContainText("black 9.5 to brown 9.5");
    await page.keyboard.press("Escape");
    await expect(approve).toHaveCount(0);
    await expect(page.getByTestId("txn-card")).toHaveCount(0);
    await quote.getByTestId("confirm-option-open").click();
    await page.getByTestId("confirm-approve").click();

    const txn = page.getByTestId("txn-card");
    await expect(txn).toBeVisible();
    await expect(txn).toContainText("Exchange confirmed");
    await expect(txn.getByTestId("txn-id")).toHaveText(/^DEMO-RMA-[0-9A-F]{6}$/);
    await expect(txn.getByTestId("store-notice")).toContainText("ALDO Store Desk notified");
    await expect(txn.getByTestId("store-notice")).toContainText("5 min away");
    await expect(page.getByTestId("bubble-bot").last()).toContainText(/store.*notified|notified.*store|(told|informed|alerted|let).{0,30}store/is);
    await expect(page.getByTestId("workflow-pill")).toContainText("Purchase");
    // The return workflow's public milestones sit in the trace next to the A2A events.
    await expect(trace(page).getByText("Policy decision: eligible")).toBeVisible();
    await expect(trace(page)).toContainText("Rules: window.ok");
    await expect(trace(page)).toContainText("Owner confirmed the chosen option");
    for (const type of ["transaction_created", "store_notified", "handback"]) await expect(traceEvent(page, type).first()).toBeVisible();
    await expect(traceEvent(page, "store_notified").first().getByTestId("actor-chip")).toHaveText("ALDO Store Desk");
    await expect(traceEvent(page, "handback").first()).toContainText("Back to the personal agent");
    // ALDO sends a size update once the personal agent's memory is read (the UI does that right after the exchange).
    await expect(traceEvent(page, "memory_updated").first()).toContainText("size 9.5 fits", { timeout: 30_000 });
    await expect(traceEvent(page, "memory_updated").first()).toContainText("provisional");

    // ---- The personal agent's memory sheet ----------------------------------------------------------------------------
    await page.getByTestId("memory-open").click();
    const memory = page.getByTestId("memory-dialog");
    await expect(memory).toBeVisible();
    await expect(memory).toContainText("What your personal agent remembers");
    await expect(memory.getByTestId("memory-typical")).toContainText("10.0");
    await expect(memory.getByTestId("memory-entries").locator("li")).toHaveCount(3);
    await expect(memory.getByTestId("memory-fit")).toContainText("Size 9.5 fits");
    await expect(memory.getByTestId("memory-fit")).toContainText("provisional");
    await expect(memory).not.toContainText(/synthetic/i);
    await page.keyboard.press("Escape");
    await expect(memory).toHaveCount(0);

    await onMock(async () => {
      await expect(txn).toContainText("brown 9.5");
      await expect(page.getByTestId("txn-next-step")).toContainText("Pick up brown 9.5 at ALDO Sainte-Catherine today");
    });
    await expectNaming(page);
  });
});

test.describe("return reasons as one-tap answers", () => {
  /** Buy and receive the Toney, ask to return it, and wait until the personal agent has asked for the reason. */
  async function askToReturn(page: Page, request: Parameters<typeof resetDemo>[0]): Promise<void> {
    await resetDemo(request, "default");
    await signInAs(page, "John");
    await expect(page.getByTestId("reason-chips")).toHaveCount(0);
    await page.getByTestId("quick-john-1").click();
    await expect(page.getByTestId("purchase-card")).toHaveAttribute("data-status", "proposed");
    await page.getByTestId("confirm-purchase-open").click();
    await page.getByTestId("confirm-purchase").click();
    await expect(page.getByTestId("push-banner")).toContainText("Delivered", { timeout: 25_000 });
    await page.getByTestId("quick-john-2").click();
    await expect(page.getByTestId("bubble-bot").last()).toContainText("reason");
    await expect(page.getByTestId("reason-chips")).toBeVisible();
    await expect(page.getByTestId("typing")).toHaveCount(0);
  }

  test("'Something else' leaves the box to the owner; a tap sends that reason with the wear answer", async ({ page, request }) => {
    test.setTimeout(300_000);
    await askToReturn(page, request);
    const reasons = page.getByTestId("reason-chips");
    await shot(page, "john-reason-chips");

    // "Something else": no message is sent, the message box gets the focus and says what to type.
    await reasons.getByTestId("reason-other").click();
    await expect(page.getByTestId("chat-input")).toBeFocused();
    await expect(page.getByTestId("chat-input")).toHaveAttribute("placeholder", "Type your reason");
    await expect(page.getByTestId("bubble-user")).toHaveCount(2); // the purchase request and "I want to return these."

    // A tap sends that reason (with "they're unworn", the answer the ALDO agent would ask for) and the chips go away.
    await reasons.getByTestId("reason-changed-mind").click();
    await expect(page.getByTestId("bubble-user").last()).toContainText("I changed my mind, and they're unworn.");
    await expect(page.getByTestId("typing")).toHaveCount(0, { timeout: 90_000 });
    await expect(page.getByTestId("send-error")).toHaveCount(0);
    await expect(page.getByTestId("reason-chips")).toHaveCount(0);
    await expect(page.getByTestId("chat-input")).toHaveAttribute("placeholder", "Message your personal agent");
    await expect(page.getByTestId("bubble-bot").last()).not.toHaveText("");
    // The real bot must not ask the wear question again: it offers an exchange or a refund (live probe, 5 of 5 lines).
    if (!IS_MOCK) await expect(page.getByTestId("bubble-bot").last()).not.toContainText(/\b(worn|wear|wore)\b[^.!?]*\?/i);
  });

  test("with the toggle off the wear answer is left out; the size chips carry the direction", async ({ page, request }) => {
    test.skip(!IS_MOCK, "the line is checked on the mock; the real bot's answer to it is covered by the test above");
    test.setTimeout(120_000);
    await askToReturn(page, request);
    const reasons = page.getByTestId("reason-chips");
    await reasons.getByTestId("reason-unworn").uncheck();
    await expect(reasons.getByTestId("reason-unworn")).not.toBeChecked();
    await reasons.getByTestId("reason-size-too-small").click();
    await expect(page.getByTestId("bubble-user").last()).toHaveText(/The size is too small\.$/);
    await expect(page.getByTestId("bubble-user").last()).not.toContainText("unworn");
  });
});

test.describe("David (Platinum): the bundle return", () => {
  test("three of ten items by mail with the Platinum perk, then the second bundle pays the fee", async ({ page, request }) => {
    await resetDemo(request, "default");
    await signInAs(page, "David");
    await expect(page.getByTestId("chat-sub")).toContainText("Platinum member");

    await page.getByTestId("quick-david-1").click();
    await expect(page.getByTestId("workflow-pill")).toContainText("Return");
    // Addendum 11.2: the personal agent shows the 10 items (numbered) and asks which ones and why. This was finding F2 (the hand-off
    // reply was one short sentence and listed nothing); the runner now composes the numbered list, so it is a hard check.
    await expect(page.getByTestId("bubble-bot").last().locator("li")).toHaveCount(10);
    await expect(page.getByTestId("bubble-bot").last()).toContainText(/which (three|ones|items)/i);
    await expect(traceEvent(page, "handoff_to_returns").first()).toBeVisible();

    await page.getByTestId("quick-david-2").click();
    const quote = page.getByTestId("quote-card");
    await expect(quote).toBeVisible();
    await expect(page.getByTestId("typing")).toHaveCount(0);
    await expect(quote).toContainText("Your return bundle");
    const primary = quote.getByTestId("primary-option");
    await expect(primary.getByTestId("bundle-items").locator("li")).toHaveCount(3);
    // Each of the three rows shows its real ALDO photo, decoded (a missing file would come back as the app's own page, not an image).
    await expect
      .poll(() =>
        primary
          .getByTestId("bundle-items")
          .locator("img")
          .evaluateAll((imgs) => imgs.length === 3 && imgs.every((el) => (el as HTMLImageElement).complete && (el as HTMLImageElement).naturalWidth > 0)),
      )
      .toBe(true);
    await expect(primary).toContainText("Return by mail");
    await expect(primary.getByTestId("benefit-note")).toContainText("Platinum perk 1 of 1 this month");
    await expect(primary.getByTestId("option-fee")).toContainText("CAD 0.00");
    await expect(primary.getByTestId("option-fee")).toContainText("CAD 5.95");
    // Refund total of the three lines: CAD 419.97 on the deployed fixtures (17999 + 10999 + 12999, handoff section 4);
    // the mock's own price list gives 429.97 (finding F3).
    await expect(primary.getByTestId("option-refund")).toHaveText(IS_MOCK ? "CAD 429.97" : "CAD 419.97");
    await expect(quote.getByTestId("confirm-option-open")).toHaveText("Confirm bundle return");
    await onMock(async () => {
      await expect(primary).toContainText("Brando boot");
      await expect(primary).toContainText("Oscar sneaker");
      await expect(primary).toContainText("Patten loafer");
      await expect(page.getByTestId("bubble-bot").last()).toContainText("no ALDO store within reach");
    });

    await quote.getByTestId("confirm-option-open").click();
    const approve = page.getByTestId("approve-dialog");
    await expect(approve).toContainText("Confirm your return");
    await expect(approve.getByRole("list", { name: "Items in this return" }).locator("li")).toHaveCount(3);
    await approve.getByTestId("confirm-approve").click();

    const txn = page.getByTestId("txn-card").first();
    await expect(txn).toBeVisible();
    await expect(txn).toContainText("Return label created for your bundle");
    await expect(txn.getByTestId("txn-id")).toHaveText(/^DEMO-RET-[0-9A-F]{6}$/);
    await expect(txn.getByTestId("txn-items").locator("li")).toHaveCount(3);
    await expect(traceEvent(page, "benefit_applied").first()).toContainText("Saved CAD 5.95");
    await expect(traceEvent(page, "benefit_applied").first()).toContainText("1 of 1 this month");
    await expect(traceEvent(page, "transaction_created").first()).toContainText("Return label created");
    await expect(page.getByTestId("workflow-pill")).toContainText("Purchase");

    // The optional beat: the perk is used for this month, the standard fee applies, and no argument changes it.
    await page.getByTestId("quick-david-3").click();
    // A deployment asks for the reason of the second return (the stage script gives none); the phone offers it as the next chip (F5).
    if (!IS_MOCK) await page.getByTestId("quick-david-3b").click();
    const second = page.getByTestId("quote-card").last();
    await expect(second)
      .toContainText("Your options")
      .catch(async (err: Error) => {
        // Model text decides this step on a deployment: say what the bot answered instead of only "not found".
        const said = await page.getByTestId("bubble-bot").evaluateAll((els) => els.slice(-3).map((e) => (e as HTMLElement).innerText.replace(/\s+/g, " ").slice(0, 400)));
        throw new Error(`${err.message}\nthe last bot messages were:\n${said.map((s, i) => `  [${i}] ${s}`).join("\n")}`);
      });
    await expect(page.getByTestId("typing")).toHaveCount(0);
    await expect(second.getByTestId("benefit-note")).toHaveCount(0);
    await expect(second.getByTestId("option-fee")).toContainText("CAD 5.95");
    await expect(second.getByTestId("option-refund")).toHaveText("CAD 94.04");
    await expect(page.getByTestId("bubble-bot").last()).toContainText(/(already|been) used|used (up|this month)|no (more|longer)/i);
    await page.getByTestId("quick-david-4").click();
    await expect(page.getByTestId("bubble-bot").last()).toContainText(/can(no|')t|unable|not able|won't|isn't possible|not something/i);
    await expect(page.getByTestId("quote-card")).toHaveCount(2);
    // The refusal is not just words: the fee on the card is still the standard one.
    await expect(page.getByTestId("quote-card").last().getByTestId("option-fee")).toContainText("CAD 5.95");
    await expectNaming(page);
  });
});

test.describe("recoveries and navigation", () => {
  test("an expired proposal is refused, the personal agent is asked again, and nothing was ordered", async ({ page, request }) => {
    test.skip(!IS_MOCK, "uses mock-only controls");
    await resetDemo(request, "default");
    await signInAs(page, "John");
    await page.getByTestId("quick-john-1").click();
    await expect(page.getByTestId("purchase-card")).toHaveAttribute("data-status", "proposed");
    await expect(page.getByTestId("confirm-purchase-open")).toBeEnabled();
    const requestId = await latestSizeRequestId(page);
    expect(requestId).toMatch(/^SZR-[0-9A-F]{6}$/);
    await mockPost(request, "/__mock/expire-purchase", { size_request_id: requestId });

    await page.getByTestId("confirm-purchase-open").click();
    await page.getByTestId("confirm-purchase").click();
    await expect(page.getByTestId("purchase-error")).toContainText("expired");
    await page.getByTestId("purchase-error").getByRole("button", { name: "Ask your personal agent again" }).click();
    await expect(page.getByTestId("purchase-dialog")).toHaveCount(0);
    await expect(page.getByTestId("bubble-user")).toHaveCount(2);
    await expect(page.getByTestId("purchase-card")).toHaveCount(2);
    await expect(page.getByTestId("purchase-card").first()).toHaveAttribute("data-status", "replaced");
    await expect(page.getByTestId("purchase-card").last()).toHaveAttribute("data-status", "proposed");
    await expect(page.getByTestId("order-card")).toHaveCount(0);
  });

  test("expired options are refused, fresh options replace them, and the old card is marked replaced", async ({ page, request }) => {
    test.skip(!IS_MOCK, "uses mock-only controls");
    await resetDemo(request, "default");
    await signInAs(page, "David");
    await page.getByTestId("quick-david-1").click();
    await page.getByTestId("quick-david-2").click();
    const first = page.getByTestId("quote-card");
    await expect(first).toBeVisible();
    await expect(page.getByTestId("typing")).toHaveCount(0);
    const quoteId = (await first.locator(".msg-card-head .mono").innerText()).trim();
    await mockPost(request, "/__mock/expire-quote", { quote_id: quoteId });

    await first.getByTestId("confirm-option-open").click();
    await page.getByTestId("confirm-approve").click();
    await expect(page.getByTestId("approve-error")).toContainText("expired");
    await page.getByTestId("approve-error").getByRole("button", { name: "Ask for new options" }).click();
    await expect(page.getByTestId("approve-dialog")).toHaveCount(0);
    await expect(page.getByTestId("quote-card")).toHaveCount(2);
    await expect(page.getByTestId("typing")).toHaveCount(0);
    await expect(page.getByTestId("quote-card").first()).toHaveAttribute("data-status", "replaced");
    const second = page.getByTestId("quote-card").last();
    await expect(second).toHaveAttribute("data-status", "open");
    await expect(second.getByTestId("confirm-option-open")).toBeEnabled();
    await expect(second.getByTestId("benefit-note")).toContainText("Platinum perk 1 of 1 this month");
  });

  test("a reload keeps the session; Cases is a separate read-only page; Switch returns to the first screen", async ({ page, request }) => {
    await resetDemo(request, "default");
    await signInAs(page, "John");
    await page.reload();
    await expect(page.getByTestId("chat-screen")).toBeVisible();
    await page.goto("/#/cases");
    await expect(page.getByTestId("cases-view")).toBeVisible();
    await expect(page.getByTestId("cases-empty")).toBeVisible();
    await expect(page.getByTestId("proto-banner")).toHaveText(STRIP);
    await page.getByRole("link", { name: "Back to personal agent" }).click();
    await expect(page.getByTestId("chat-screen")).toBeVisible();
    await page.getByTestId("switch-customer").click();
    await expect(page.getByTestId("persona-picker")).toBeVisible();
    await page.getByTestId("persona-DEMO-CUST-DAVID").click();
    await expect(page.getByTestId("chat-sub")).toContainText("Platinum member");
  });
});

test.describe("routes of the single-chat contract (addendum 11.5)", () => {
  test("personas carry tier fields, a message answers with agent and mode, reviews need a delegated token", async ({ request }) => {
    test.skip(!IS_MOCK, "uses mock-only controls");
    await resetDemo(request, "default");
    const personas = (await (await request.get("/api/demo/personas")).json()) as { personas: Record<string, string>[] };
    expect(personas.personas.find((p) => p.customer_id === "DEMO-CUST-JOHN")).toMatchObject({ tier: "silver", tier_label: "Silver member", location_label: "Bronfman Building, Montreal" });
    expect(personas.personas.find((p) => p.customer_id === "DEMO-CUST-DAVID")).toMatchObject({ tier: "platinum", tier_label: "Platinum member" });

    const session = (await (await request.post("/api/demo/sessions", { data: { customer_id: "DEMO-CUST-JOHN" } })).json()) as { token: string };
    const auth = { Authorization: `Bearer ${session.token}` };
    const conv = (await (await request.post("/api/personal-agent/conversations", { headers: auth, data: {} })).json()) as { conv_id: string };
    const info = await (await request.get(`/api/personal-agent/conversations/${conv.conv_id}`, { headers: auth })).json();
    expect(info).toMatchObject({ conv_id: conv.conv_id, mode: "bot", returns_order_id: null });
    const sent = await request.post(`/api/personal-agent/conversations/${conv.conv_id}/messages`, { headers: auth, data: { text: "Hello" } });
    expect(sent.status()).toBe(202);
    expect(await sent.json()).toMatchObject({ agent: "personal", mode: "bot", run_id: expect.stringMatching(/^PRUN-/) });

    // The reviews route takes a delegated token only; flagged reviews are marked, never removed from the response.
    const refused = await request.post("/api/gateway/reviews", { headers: auth, data: { product_query: "Men 001" } });
    expect(refused.status()).toBe(403);
    const delegated = (await (await request.post(`${MOCK_URL}/__mock/delegated-token`, { data: { customer_id: "DEMO-CUST-JOHN" } })).json()) as { token: string };
    const reviews = await request.post("/api/gateway/reviews", { headers: { Authorization: `Bearer ${delegated.token}` }, data: { product_query: "Men 001" } });
    expect(reviews.status()).toBe(200);
    const body = (await reviews.json()) as { n_reviews: number; provenance: string; reviews: { review_id: string; flags: string[] }[] };
    expect(body.n_reviews).toBe(16);
    expect(body.provenance).toBe("synthetic");
    expect(body.reviews.filter((r) => r.flags.includes("instruction_like_text")).map((r) => r.review_id)).toEqual(["REV-INJECT-1"]);
    // A delegated token is refused on every other route.
    const other = await request.get("/api/orders", { headers: { Authorization: `Bearer ${delegated.token}` } });
    expect(other.status()).toBe(403);
  });
});

test.describe("regression customers and admin controls", () => {
  test("Ava: stessy purchase, confirm, delivery, half-size swap through the same chat", async ({ page, request }) => {
    test.skip(!IS_MOCK, "uses the scripted mock");
    await resetDemo(request, "dc_stockout");
    await signInAs(page, "Ava", "?admin=1");
    await page.getByTestId("quick-ava-1").click();
    await expect(page.getByTestId("purchase-card")).toHaveAttribute("data-status", "proposed");
    await expect(page.getByTestId("confirm-purchase-open")).toBeEnabled();
    await page.getByTestId("confirm-purchase-open").click();
    await page.getByTestId("confirm-purchase").click();
    await expect(page.getByTestId("push-banner")).toContainText("Delivered: Stessy2.0, size 8.0", { timeout: 20_000 });
    await page.getByTestId("quick-ava-2").click();
    const quote = page.getByTestId("quote-card");
    await expect(quote).toBeVisible();
    await expect(page.getByTestId("typing")).toHaveCount(0);
    await expect(quote.getByTestId("primary-option")).toContainText("Exchange in store");
    await expect(quote.getByTestId("primary-option")).toContainText("Meets your deadline");
    await quote.getByTestId("confirm-option-open").click();
    await page.getByTestId("confirm-approve").click();
    await expect(page.getByTestId("txn-card")).toContainText("Exchange confirmed");
    await expect(traceEvent(page, "memory_updated").first()).toContainText("8.0 to 8.5", { timeout: 30_000 });
    // Cross-customer attempts are denied by the system, whatever the message says.
    await page.getByTestId("quick-ava-3").click();
    await expect(page.getByTestId("a2a-trace")).not.toContainText("DEMO-1006");
    await expect(page.getByTestId("bubble-bot").last()).toContainText("isn't on your account");
  });

  test("admin controls reset the demo; a wrong secret is rejected and the secret is not stored", async ({ page, request }) => {
    await resetDemo(request, "default");
    await page.goto("/?admin=1");
    await expect(page.getByTestId("admin-panel")).toBeVisible();
    const secret = page.getByLabel("Admin secret");
    await expect(secret).toHaveAttribute("type", "password");
    await secret.fill("definitely-wrong");
    await page.getByTestId("reset-default").click();
    await expect(page.getByRole("alert").first()).toBeVisible();
    await secret.fill(ADMIN_SECRET);
    await page.getByTestId("reset-dc_stockout").click();
    await expect(page.getByTestId("admin-status")).toContainText("Reset to dc_stockout complete");
    const stored = await page.evaluate(() => JSON.stringify({ ...sessionStorage }) + JSON.stringify({ ...localStorage }));
    expect(stored).not.toContain(ADMIN_SECRET);
  });
});
