import { expect, test, type Page } from "@playwright/test";
import { IS_MOCK, axeScan, firstApprove, latestSizeRequestId, mockPost, resetDemo, shot, signInAs, tabTo, waitAldoIdle } from "./helpers";

// One Playwright pass over the three-column demo. Runs against the mock API by default and,
// with E2E_TARGET=deployed, against the real site (mock-only controls are skipped there).

const BANNER = "Prototype, simulated ALDO data, not official policy";
const PA_REGION = "ALDO bot, your personal agent";
const ALDO_REGION = "ALDO Returns Agent";

const a2aRow = (page: Page, type: string) => page.locator(`[data-testid="a2a-trace"] [data-event-type="${type}"]`);

/** Run ALDO bot to its proposal and wait until the purchase card can be confirmed. */
async function proposeViaBot(page: Page): Promise<void> {
  await page.getByTestId("suggested-pa-order").click();
  await expect(page.getByTestId("purchase-card")).toHaveAttribute("data-status", "proposed");
  await expect(page.getByTestId("pa-working")).toHaveCount(0);
  await expect(page.getByTestId("confirm-purchase-open")).toBeEnabled();
}

test.describe("sign-in and layout", () => {
  test("sign-in screen: banner, label, personas, axe", async ({ page, request }) => {
    await resetDemo(request, "dc_stockout");
    await page.goto("/");
    await expect(page.getByTestId("proto-banner")).toHaveText(BANNER);
    await expect(page.getByRole("heading", { name: "Demo sign-in, synthetic customer" })).toBeVisible();
    for (const name of ["Ava", "Ben", "Chloe"]) {
      await expect(page.getByRole("button", { name: `Sign in as ${name}` })).toBeVisible();
    }
    await shot(page, "01-sign-in");
    await axeScan(page, "sign-in");
  });

  test("workspace: three columns, names, banner persists, token kept in sessionStorage", async ({ page, request }) => {
    await resetDemo(request, "dc_stockout");
    await signInAs(page, "Ava");
    await expect(page.getByTestId("proto-banner")).toHaveText(BANNER);
    await expect(page.getByTestId("session")).toContainText("Demo sign-in, synthetic customer: Ava");
    const cols = [
      page.getByRole("region", { name: PA_REGION, exact: true }),
      page.getByRole("region", { name: "A2A trace", exact: true }),
      page.getByRole("region", { name: ALDO_REGION, exact: true }),
    ];
    const boxes = [];
    for (const c of cols) {
      await expect(c).toBeVisible();
      boxes.push(await c.boundingBox());
    }
    // Side by side at >= 1200 px: increasing x, same top edge.
    expect(boxes[0]!.x).toBeLessThan(boxes[1]!.x);
    expect(boxes[1]!.x).toBeLessThan(boxes[2]!.x);
    expect(Math.abs(boxes[0]!.y - boxes[2]!.y)).toBeLessThan(4);

    // The personal agent is ALDO bot everywhere, never "Muse".
    await expect(page.locator("body")).not.toContainText(/muse/i);
    await expect(page.getByRole("heading", { name: PA_REGION })).toBeVisible();
    await expect(page.getByRole("heading", { name: ALDO_REGION, exact: true })).toBeVisible();

    await expect(page.getByTestId("memory-card")).toContainText("Synthetic demo memory");
    await expect(page.getByTestId("memory-typical")).toContainText("7.5");
    await expect(page.getByTestId("memory-entries").locator("li")).toHaveCount(3);
    await expect(page.getByTestId("suggested-pa-order")).toContainText("Order me the ALDO Stessy heels for the wedding Saturday.");
    for (const id of ["hero-wedding", "final-sale-mind", "ignore-policy"]) await expect(page.getByTestId(`suggested-${id}`)).toBeVisible();
    await expect(page.getByTestId("orders")).toContainText("Simulated data");
    await expect(page.getByTestId("order-DEMO-1001")).toContainText("Stessy2.0");
    await expect(page.getByTestId("order-DEMO-1001")).toContainText("CAD 129.99");

    const token = await page.evaluate(() => sessionStorage.getItem("aldo.demo.session"));
    expect(token).toBeTruthy();
    await shot(page, "02-workspace");
    await axeScan(page, "workspace");

    // Reload keeps the session (sessionStorage) and shows the workspace again.
    await page.reload();
    await expect(page.getByTestId("workspace")).toBeVisible();
  });

  test("narrow screens stack the columns", async ({ page, request }) => {
    await resetDemo(request, "dc_stockout");
    await page.setViewportSize({ width: 1000, height: 900 });
    await signInAs(page, "Ava");
    const a = await page.getByRole("region", { name: PA_REGION, exact: true }).boundingBox();
    const b = await page.getByRole("region", { name: "A2A trace", exact: true }).boundingBox();
    const c = await page.getByRole("region", { name: ALDO_REGION, exact: true }).boundingBox();
    expect(Math.abs(a!.x - b!.x)).toBeLessThan(4);
    expect(Math.abs(b!.x - c!.x)).toBeLessThan(4);
    expect(a!.y).toBeLessThan(b!.y);
    expect(b!.y).toBeLessThan(c!.y);
    await expect(page.getByTestId("proto-banner")).toBeVisible();
    await shot(page, "03-stacked-narrow", { fullPage: true });
    await axeScan(page, "workspace-narrow");
  });
});

test.describe("three-act flow", () => {
  test("proposal, owner confirmation, delivery, exchange approval, memory update", async ({ page, request }) => {
    await resetDemo(request, "dc_stockout");
    await signInAs(page, "Ava");

    // ---- Act 1: ALDO bot proposes, the owner confirms -----------------------------------------
    await page.getByTestId("suggested-pa-order").click();
    await expect(page.getByTestId("pa-transcript")).toContainText("Order me the ALDO Stessy heels for the wedding Saturday.");
    await expect(a2aRow(page, "fit_signal_shared")).toBeVisible();
    await expect(a2aRow(page, "fit_signal_shared")).toContainText("runs small");
    await expect(a2aRow(page, "fit_signal_shared")).toContainText("79.9%");
    await expect(a2aRow(page, "fit_signal_shared")).toContainText("n=582");
    await expect(a2aRow(page, "fit_signal_shared")).toContainText("derived from provided data");
    await shot(page, "04-act1-live-a2a-trace");

    await expect(a2aRow(page, "size_recommended")).toBeVisible();
    const types = () => page.locator('[data-testid="a2a-trace"] [data-testid="trace-a2a"]').evaluateAll((els) => els.map((e) => e.getAttribute("data-event-type")));
    expect(await types()).toEqual(["size_assist_requested", "fit_signal_shared", "size_requested", "size_recommended"]);

    // Every A2A event shows who sent it, by display name; the technical id stays in data-actor.
    const badges = page.locator('[data-testid="a2a-trace"] [data-testid="trace-a2a"] [data-testid="actor-badge"]');
    expect(await badges.evaluateAll((els) => els.map((e) => e.getAttribute("data-actor")))).toEqual(["personal_agent", "aldo_gateway", "aldo_gateway", "personal_agent"]);
    expect(await badges.allInnerTexts()).toEqual(["ALDO bot", "ALDO Gateway", "ALDO Gateway", "ALDO bot"]);
    // Public fields only: the private memory (retailer labels, typical size) is not in the A2A feed.
    const a2aText = (await page.getByTestId("a2a-trace").innerText()).toLowerCase();
    expect(a2aText).not.toContain("retailer");
    expect(a2aText).not.toContain("typical");

    // The proposal is rendered from the server's record, and nothing is ordered yet.
    const card = page.getByTestId("purchase-card");
    await expect(card).toHaveAttribute("data-status", "proposed");
    await expect(page.getByTestId("pa-working")).toHaveCount(0);
    await expect(page.getByTestId("pa-transcript")).toContainText("I haven't ordered anything yet");
    await expect(card).toContainText("Purchase proposal from ALDO bot");
    const facts = page.getByTestId("purchase-facts");
    await expect(facts).toContainText("Stessy2.0, black");
    await expect(facts).toContainText("8.0");
    await expect(facts).toContainText("Pick up in store at ALDO Demo Store Montreal");
    await expect(facts).toContainText("Sat, Oct 17, 2026");
    await expect(facts).toContainText("Meets your deadline");
    await expect(facts).toContainText("CAD 129.99");
    await expect(facts).toContainText("Runs small: 79.9% of fit-related returns were too small (n=582)");
    await expect(facts).toContainText("derived from provided data");
    await expect(page.getByTestId("purchase-rationale")).toContainText("runs small per ALDO signal");
    await expect(page.getByTestId("purchase-countdown")).toContainText("Proposal expires in");
    await expect(page.locator('[data-testid^="order-DEMO-A2A-"]')).toHaveCount(0);
    await expect(page.locator('[data-testid^="simulate-delivery-"]')).toHaveCount(0);
    await card.scrollIntoViewIfNeeded();
    await shot(page, "05-act1-purchase-proposal");
    await axeScan(page, "purchase-proposal");

    // Confirm purchase opens a dialog first; Escape closes it without calling the API.
    await page.getByTestId("confirm-purchase-open").click();
    const dialog = page.getByTestId("purchase-dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("Confirm your purchase");
    await expect(dialog).toContainText("Stessy2.0, black");
    await expect(dialog).toContainText("Pick up in store at ALDO Demo Store Montreal");
    await expect(dialog).toContainText("CAD 129.99");
    await expect(dialog).toContainText("Simulated data");
    await shot(page, "06-purchase-confirmation-dialog");
    await axeScan(page, "purchase-dialog");
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(page.getByTestId("confirm-purchase-open")).toBeFocused();
    await expect(card).toHaveAttribute("data-status", "proposed");
    await expect(page.locator('[data-testid^="order-DEMO-A2A-"]')).toHaveCount(0);

    // Reopen and confirm: now the simulated order exists.
    await page.getByTestId("confirm-purchase-open").click();
    await page.getByTestId("confirm-purchase").click();
    await expect(card).toHaveAttribute("data-status", "decided");
    await expect(card).toContainText("Simulated order placed");
    await expect(page.getByTestId("purchase-order-id")).toHaveText(/^DEMO-A2A-[0-9A-F]{6}$/);
    await expect(page.getByTestId("purchase-next-step")).toContainText("Pick up size 8.0 at ALDO Demo Store Montreal");
    await expect(a2aRow(page, "size_confirmed")).toContainText("Owner confirmed");
    await expect(a2aRow(page, "size_confirmed")).toContainText("Simulated order DEMO-A2A-");
    expect(await types()).toEqual(["size_assist_requested", "fit_signal_shared", "size_requested", "size_recommended", "size_confirmed"]);
    const agentOrder = page.locator('[data-testid^="order-DEMO-A2A-"]');
    await expect(agentOrder).toBeVisible();
    await expect(agentOrder).toContainText("Placed by ALDO bot");
    await expect(agentOrder).toContainText("size 8.0");
    await shot(page, "07-act1-order-placed");

    // ---- Demo control: simulate delivery ------------------------------------------------------
    await page.locator('[data-testid^="simulate-delivery-"]').click();
    await expect(agentOrder).toContainText("Delivered Fri, Oct 16, 2026");
    await expect(page.getByTestId("suggested-still-tight")).toContainText("They're still slightly tight. Can I swap for a half size up before the wedding Saturday?");
    await expect(page.getByTestId("suggested-hero-wedding")).toHaveCount(0);

    // ---- Act 2: return flow with the ALDO Returns Agent --------------------------------------
    await page.getByTestId("suggested-still-tight").click();
    await expect(page.getByTestId("aldo-working")).toBeVisible();
    await expect(page.getByTestId("aldo-trace").locator('[data-testid="trace-tool"]').first()).toBeVisible();
    await shot(page, "08-act2-live-tool-trace");

    await expect(page.getByTestId("options")).toBeVisible();
    await waitAldoIdle(page);
    const cards = page.locator('[data-testid^="option-card-"]');
    expect(await cards.count()).toBeGreaterThanOrEqual(4);
    // Store pickup first and it meets the Saturday deadline; a refund option is still visible.
    await expect(cards.first()).toHaveAttribute("data-option-type", "exchange_store_pickup");
    await expect(cards.first()).toContainText("Meets your deadline");
    await expect(cards.first()).toContainText("ALDO Demo Store Montreal");
    await expect(cards.first()).toContainText("size 8.5");
    await expect(cards.first()).toContainText("Simulated data");
    await expect(page.locator('[data-option-type="return_in_store"]')).toBeVisible();
    await expect(page.locator('[data-option-type="return_mail"]')).toContainText("CAD 5.95");
    await expect(page.locator('[data-option-type="exchange_ship_from_store"]')).toContainText("After your deadline");
    await page.getByTestId("options").scrollIntoViewIfNeeded();
    await shot(page, "09-option-cards");
    await axeScan(page, "option-cards");

    // Approve opens a dialog first; Escape closes it without calling the API.
    await firstApprove(page).click();
    const approve = page.getByTestId("approve-dialog");
    await expect(approve).toBeVisible();
    await expect(approve).toContainText("Exchange, pick up in store");
    await expect(approve).toContainText("ALDO Demo Store Montreal");
    await expect(approve).toContainText("size 8.5");
    await expect(approve).toContainText("Simulated data");
    await shot(page, "10-confirmation-dialog");
    await axeScan(page, "confirmation-dialog");
    await page.keyboard.press("Escape");
    await expect(approve).toHaveCount(0);
    await expect(firstApprove(page)).toBeFocused();
    await expect(page.getByTestId("confirmation-panel")).toHaveCount(0);

    // Reopen and confirm.
    await firstApprove(page).click();
    await page.getByTestId("confirm-approve").click();
    const panel = page.getByTestId("confirmation-panel");
    await expect(panel).toBeVisible();
    await expect(panel).toContainText("Simulated exchange created");
    await expect(page.getByTestId("txn-id")).toHaveText(/^DEMO-RMA-[0-9A-F]{6}$/);
    await expect(page.getByTestId("txn-state")).toContainText("completed");
    await expect(page.getByTestId("txn-next-step")).toContainText("Pick up size 8.5 at ALDO Demo Store Montreal");
    await expect(panel).toContainText("simulated exchange/return created");
    await expect(panel).not.toContainText(/(has been|was|is|been) (refunded|shipped)/i);
    await expect(page.getByTestId("run-metrics").first()).toBeVisible();
    await panel.scrollIntoViewIfNeeded();
    await shot(page, "11-confirmation-panel");

    // ---- Write-back: memory card updates (provisional), A2A feed shows memory_updated ---------
    await expect(a2aRow(page, "memory_updated").first()).toContainText("8.0 to 8.5");
    await expect(a2aRow(page, "memory_updated").first()).toContainText("provisional");
    const update = page.getByTestId("memory-update");
    await expect(update).toHaveCount(1);
    await expect(update).toContainText("Stessy2.0");
    await expect(update).toContainText("8.0 to 8.5");
    await expect(update).toContainText("provisional");
    await shot(page, "12-memory-updated");
    await axeScan(page, "workspace-after-exchange");

    // Demo control: simulate pickup confirms the memory.
    await page.getByTestId("simulate-pickup").click();
    await expect(update).toContainText("confirmed");
    await expect(page.getByTestId("simulate-pickup")).toBeDisabled();
    await shot(page, "13-memory-confirmed");
  });

  test("a double click on Confirm does not create a second transaction", async ({ page, request }) => {
    test.skip(!IS_MOCK, "uses mock-only controls");
    await resetDemo(request, "dc_stockout");
    await signInAs(page, "Ava");
    await page.getByTestId("suggested-hero-wedding").click();
    await expect(page.getByTestId("options")).toBeVisible();
    await waitAldoIdle(page);
    await firstApprove(page).click();
    await page.getByTestId("confirm-approve").dblclick();
    await expect(page.getByTestId("confirmation-panel")).toBeVisible();
    await expect(page.getByTestId("aldo-trace").locator('[data-testid="trace-transaction"]')).toHaveCount(1);
    await expect(page.locator('[data-testid="aldo-trace"] [data-testid="trace-run"]')).toHaveCount(2);
  });
});

test.describe("purchase confirmation errors", () => {
  test("an expired proposal and a stock change show clear messages and recover", async ({ page, request }) => {
    test.skip(!IS_MOCK, "uses mock-only controls");
    await resetDemo(request, "dc_stockout");
    await signInAs(page, "Ava");
    await proposeViaBot(page);

    // REQUEST_EXPIRED: the server says the proposal lapsed when the owner confirms.
    const requestId = await latestSizeRequestId(page);
    expect(requestId).toMatch(/^SZR-[0-9A-F]{6}$/);
    await mockPost(request, "/__mock/expire-purchase", { size_request_id: requestId });
    await page.getByTestId("confirm-purchase-open").click();
    await page.getByTestId("confirm-purchase").click();
    await expect(page.getByTestId("purchase-error")).toContainText("expired");
    await shot(page, "14-purchase-request-expired");
    await axeScan(page, "purchase-error-dialog");
    await page.getByTestId("purchase-error").getByRole("button", { name: "Ask ALDO bot again" }).click();
    await expect(page.getByTestId("purchase-dialog")).toHaveCount(0);
    // ALDO bot makes a fresh proposal; nothing was ordered in between.
    await expect(page.getByTestId("pa-transcript").getByText("Order me the ALDO Stessy heels for the wedding Saturday.")).toHaveCount(2);
    await expect(page.locator('[data-testid^="order-DEMO-A2A-"]')).toHaveCount(0);
    await expect(page.getByTestId("purchase-card")).toHaveAttribute("data-status", "proposed");
    await expect(page.getByTestId("pa-working")).toHaveCount(0);

    // STOCK_CHANGED: the proposed pair sold out at the store before the owner confirmed.
    await mockPost(request, "/__mock/set-atp", { sku: "DEMO-SKU-HERO-BLACK-080", location_id: "DEMO-STORE-MTL-01", atp: 0 });
    await page.getByTestId("confirm-purchase-open").click();
    await page.getByTestId("confirm-purchase").click();
    await expect(page.getByTestId("purchase-error")).toContainText("Stock changed");
    await shot(page, "15-purchase-stock-changed");
    await page.getByTestId("purchase-error").getByRole("button", { name: "Ask ALDO bot again" }).click();
    // A new proposal in another size; it can be confirmed.
    await expect(page.getByTestId("pa-transcript").getByText("Order me the ALDO Stessy heels for the wedding Saturday.")).toHaveCount(3);
    await expect(page.getByTestId("pa-working")).toHaveCount(0);
    await expect(page.getByTestId("purchase-card")).toHaveAttribute("data-status", "proposed");
    await expect(page.getByTestId("purchase-facts")).toContainText("8.5");
    await page.getByTestId("confirm-purchase-open").click();
    await page.getByTestId("confirm-purchase").click();
    await expect(page.getByTestId("purchase-card")).toHaveAttribute("data-status", "decided");
    await expect(page.locator('[data-testid^="order-DEMO-A2A-"]')).toHaveCount(1);
  });

  test("a different option confirmed elsewhere is reported as a conflict", async ({ page, request }) => {
    test.skip(!IS_MOCK, "uses mock-only controls");
    await resetDemo(request, "default"); // the DC has stock, so two options were offered
    await signInAs(page, "Ava");
    await proposeViaBot(page);
    const requestId = await latestSizeRequestId(page);
    await mockPost(request, "/__mock/confirm-other", { size_request_id: requestId });
    await page.getByTestId("confirm-purchase-open").click();
    await page.getByTestId("confirm-purchase").click();
    await expect(page.getByTestId("purchase-error")).toContainText("different option was already confirmed");
    await shot(page, "16-purchase-idempotency-conflict");
    await page.getByRole("button", { name: "Cancel" }).click();
    // The card re-reads the server record, which now shows the order the other client placed.
    await expect(page.getByTestId("purchase-card")).toHaveAttribute("data-status", "decided");
    await expect(page.locator('[data-testid^="order-DEMO-A2A-"]')).toHaveCount(1);
  });
});

test.describe("escalation, denial, errors", () => {
  test("final-sale defect opens a case that shows in the Cases tab", async ({ page, request }) => {
    await resetDemo(request, "dc_stockout");
    await signInAs(page, "Ava");
    await page.getByTestId("order-DEMO-1002").click();
    await page.getByLabel("Message to the ALDO Returns Agent").fill("The heel broke after two days. I know it is final sale but it is defective.");
    await page.getByRole("button", { name: "Send" }).last().click();
    await expect(page.getByTestId("case-panel")).toBeVisible();
    await expect(page.getByTestId("case-id")).toHaveText(/^DEMO-CASE-[0-9A-F]{6}$/);
    await expect(page.getByTestId("aldo-transcript")).toContainText("Your case ID is DEMO-CASE-");
    await expect(page.getByTestId("aldo-trace").locator('[data-tool="escalate_case"]')).toBeVisible();
    await expect(page.locator('[data-testid^="option-card-"]')).toHaveCount(0);
    await page.getByTestId("case-panel").scrollIntoViewIfNeeded();
    await shot(page, "17-case-created");
    const caseId = (await page.getByTestId("case-id").innerText()).trim();

    await page.getByRole("link", { name: "Cases", exact: true }).first().click();
    await expect(page.getByTestId("cases-view")).toBeVisible();
    await expect(page.getByTestId("cases-view")).toContainText(caseId);
    await expect(page.getByTestId("proto-banner")).toHaveText(BANNER);
    await shot(page, "18-cases");
    await axeScan(page, "cases");
  });

  test("final-sale change of mind is refused with no options", async ({ page, request }) => {
    await resetDemo(request, "dc_stockout");
    await signInAs(page, "Ava");
    await page.getByTestId("suggested-final-sale-mind").click();
    await expect(page.getByTestId("order-DEMO-1002").locator("input")).toBeChecked();
    await expect(page.getByTestId("aldo-transcript")).toContainText("final sale");
    await waitAldoIdle(page);
    await expect(page.locator('[data-testid^="option-card-"]')).toHaveCount(0);
    await expect(page.getByTestId("aldo-trace")).toContainText("final_sale.no_return");
  });

  test("another customer's order is denied and the policy is unchanged", async ({ page, request }) => {
    await resetDemo(request, "dc_stockout");
    await signInAs(page, "Ava");
    await page.getByTestId("suggested-ignore-policy").click();
    const denied = page.getByTestId("aldo-trace").locator('[data-tool="get_order_context"]');
    await expect(denied).toContainText("NOT_AUTHORIZED");
    await expect(denied).toContainText("belongs to another customer");
    await expect(page.getByTestId("aldo-trace").locator(".code-amber")).toBeVisible();
    await expect(page.getByTestId("aldo-transcript")).toContainText("isn't on your account");
    await waitAldoIdle(page);
    await expect(page.locator('[data-testid^="option-card-"]')).toHaveCount(0);
    await expect(page.getByTestId("confirmation-panel")).toHaveCount(0);
    await shot(page, "19-denied-cross-customer");
  });

  test("expired quote and conflicting approval show clear messages", async ({ page, request }) => {
    test.skip(!IS_MOCK, "uses mock-only controls");
    await resetDemo(request, "dc_stockout");
    await signInAs(page, "Ava");
    await page.getByTestId("suggested-hero-wedding").click();
    await expect(page.getByTestId("options")).toBeVisible();
    await waitAldoIdle(page);
    const firstQuote = (await page.getByTestId("options").locator(".card-head .mono").innerText()).trim();

    // QUOTE_EXPIRED: the server says the quote lapsed when the customer confirms.
    await mockPost(request, "/__mock/expire-quote", { quote_id: firstQuote });
    await firstApprove(page).click();
    await page.getByTestId("confirm-approve").click();
    const err = page.getByTestId("approve-error");
    await expect(err).toContainText("expired");
    await shot(page, "20-quote-expired");
    await err.getByRole("button", { name: "Ask for new options" }).click();
    await expect(page.getByTestId("approve-dialog")).toHaveCount(0);
    await expect(page.getByTestId("aldo-transcript")).toContainText("You asked for fresh options.");
    await expect(page.getByTestId("options").locator(".card-head .mono")).not.toHaveText(firstQuote);
    await waitAldoIdle(page);
    await expect(firstApprove(page)).toBeEnabled();

    // IDEMPOTENCY_CONFLICT: another client approved a different option on this quote first.
    const secondQuote = (await page.getByTestId("options").locator(".card-head .mono").innerText()).trim();
    const lastCardId = (await page.locator('[data-testid^="option-card-"]').last().getAttribute("data-testid")) ?? "";
    await mockPost(request, "/__mock/approve-other", { quote_id: secondQuote, option_id: lastCardId.replace("option-card-", "") });
    await firstApprove(page).click();
    await page.getByTestId("confirm-approve").click();
    await expect(page.getByTestId("approve-error")).toContainText("different option was already approved");
    await shot(page, "21-idempotency-conflict");
    await axeScan(page, "approve-error-dialog");
    await page.getByRole("button", { name: "Cancel" }).click();
    // After the conflict the quote is re-read: all Approve buttons are locked and one card says Approved.
    await expect(page.locator('[data-testid^="option-card-"]', { hasText: "Approved" })).toHaveCount(1);
    await expect(firstApprove(page)).toBeDisabled();
  });
});

test.describe("keyboard and admin", () => {
  test("keyboard-only: sign in, send a suggested prompt, approve and confirm a return", async ({ page, request }) => {
    await resetDemo(request, "dc_stockout");
    await page.goto("/");
    // Sign in with Tab + Enter only.
    await tabTo(page, page.getByRole("button", { name: "Sign in as Ava" }));
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("workspace")).toBeVisible();
    // Reach the ALDO Returns Agent's first suggested prompt by Tab and send it with Enter.
    await tabTo(page, page.getByTestId("suggested-hero-wedding"));
    await expect(page.getByTestId("suggested-hero-wedding")).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("options")).toBeVisible();
    await waitAldoIdle(page);
    // Open the approve dialog with the keyboard.
    await tabTo(page, firstApprove(page));
    await page.keyboard.press("Enter");
    const dialog = page.getByTestId("approve-dialog");
    await expect(dialog).toBeVisible();
    // Focus stays inside the dialog while tabbing through it.
    const confirm = page.getByTestId("confirm-approve");
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press("Tab");
      expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
    }
    await tabTo(page, confirm, 6);
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("confirmation-panel")).toContainText("Simulated exchange created");
    // Visible focus: the focused control has a non-zero outline.
    await tabTo(page, page.getByTestId("simulate-pickup"));
    const outline = await page.getByTestId("simulate-pickup").evaluate((el) => getComputedStyle(el).outlineWidth);
    expect(parseFloat(outline)).toBeGreaterThanOrEqual(2);
  });

  test("keyboard-only: ask ALDO bot, then confirm the purchase", async ({ page, request }) => {
    await resetDemo(request, "dc_stockout");
    await page.goto("/");
    await tabTo(page, page.getByRole("button", { name: "Sign in as Ava" }));
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("workspace")).toBeVisible();
    await tabTo(page, page.getByTestId("suggested-pa-order"));
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("purchase-card")).toHaveAttribute("data-status", "proposed");
    await expect(page.getByTestId("confirm-purchase-open")).toBeEnabled();
    await tabTo(page, page.getByTestId("confirm-purchase-open"));
    await page.keyboard.press("Enter");
    const dialog = page.getByTestId("purchase-dialog");
    await expect(dialog).toBeVisible();
    for (let i = 0; i < 5; i++) {
      await page.keyboard.press("Tab");
      expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
    }
    await tabTo(page, page.getByTestId("confirm-purchase"), 6);
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("purchase-card")).toContainText("Simulated order placed");
  });

  test("admin controls at ?admin=1 reset the demo; wrong secret is rejected", async ({ page, request }) => {
    await resetDemo(request, "default");
    await page.goto("/?admin=1");
    await expect(page.getByTestId("admin-panel")).toBeVisible();
    await expect(page.getByTestId("proto-banner")).toHaveText(BANNER);
    const secret = page.getByLabel("Admin secret");
    await expect(secret).toHaveAttribute("type", "password");
    await shot(page, "22-admin-reset");
    await axeScan(page, "admin");

    await secret.fill("definitely-wrong");
    await page.getByTestId("reset-default").click();
    await expect(page.getByRole("alert").filter({ hasText: /session|sign in|authenticated|Admin/i }).first()).toBeVisible();

    await secret.fill(process.env.E2E_ADMIN_SECRET ?? "mock-admin");
    await page.getByTestId("reset-dc_stockout").click();
    await expect(page.getByTestId("admin-status")).toContainText("Reset to dc_stockout complete");
    // The secret is not persisted anywhere.
    const stored = await page.evaluate(() => JSON.stringify({ ...sessionStorage }) + JSON.stringify({ ...localStorage }));
    expect(stored).not.toContain(process.env.E2E_ADMIN_SECRET ?? "mock-admin");
  });
});
