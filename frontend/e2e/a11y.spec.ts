import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { SHOT_DIR, resetDemo, shot, signInAs, tabTo } from "./helpers";

// Accessibility specs of TEST_HANDOFF layer L4: an axe scan of every phone screen the two stage demos pass through, and a
// keyboard-only path through John's purchase confirmation. Run against the real site with
//   cd backend && ../scripts/with_aws_env.sh env SECRETS_ARN=<arn> uv run --no-sync python ../scripts/test_run_e2e_deployed.py -g a11y
// (the mock also works: leave E2E_TARGET unset). Serious and critical axe violations fail the test; ALL screens are scanned
// even after a failure (expect.soft), and every finding is appended to docs/eval/screenshots/v2/axe-v2.log.

const LOG = join(SHOT_DIR, "axe-v2.log");

function log(line: string): void {
  mkdirSync(dirname(LOG), { recursive: true });
  appendFileSync(LOG, `${new Date().toISOString()} ${line}\n`);
  console.log(line);
}

/** Axe on the current page. Serious/critical violations fail softly (the scan goes on); others are logged only. */
async function axeSoft(page: Page, label: string): Promise<void> {
  await page.waitForTimeout(400); // let the sheet / banner animations settle
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]).analyze();
  const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  const other = results.violations.filter((v) => v.impact !== "serious" && v.impact !== "critical");
  const fmt = (v: (typeof results.violations)[number]) =>
    `${v.id} [${v.impact}] ${v.nodes.length} node(s): ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`;
  // For colour-contrast also say which pair failed (foreground on background, ratio), so the log is actionable.
  const detail = (v: (typeof results.violations)[number]) =>
    v.id !== "color-contrast"
      ? ""
      : v.nodes
          .slice(0, 3)
          .map((n) => {
            const d = (n.any[0]?.data ?? {}) as { fgColor?: string; bgColor?: string; contrastRatio?: number; fontSize?: string };
            return `    ${n.target.join(" ")} fg=${d.fgColor} bg=${d.bgColor} ratio=${d.contrastRatio} size=${d.fontSize}`;
          })
          .join("\n");
  log(`axe ${label}: ${blocking.length} serious/critical, ${other.length} moderate/minor (${results.passes.length} rules passed)`);
  for (const v of blocking) log(`  BLOCKING ${fmt(v)}${detail(v) ? `\n${detail(v)}` : ""}`);
  for (const v of other) log(`  note ${fmt(v)}`);
  expect.soft(blocking.map(fmt), `axe serious/critical violations on ${label}`).toEqual([]);
}

/** The focused element draws a visible focus indicator (outline or ring). */
async function expectFocusVisible(page: Page, what: string): Promise<void> {
  const visible = await page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el || el === document.body) return false;
    const cs = getComputedStyle(el);
    return (cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0) || (cs.boxShadow !== "none" && cs.boxShadow !== "");
  });
  expect.soft(visible, `${what} shows a visible focus indicator`).toBeTruthy();
}

test.describe("a11y: axe scan of the phone screens", () => {
  test("John: picker, chat, proposal, purchase dialog, order and push banner, quote, approve dialog, transaction, memory sheet, cases page", async ({ page, request }) => {
    test.setTimeout(300_000);
    await resetDemo(request, "default");
    await page.goto("/");
    await expect(page.getByTestId("persona-picker")).toBeVisible();
    await axeSoft(page, "first screen (persona picker)");
    await page.getByTestId("persona-DEMO-CUST-JOHN").click();
    await expect(page.getByTestId("chat-screen")).toBeVisible();
    await axeSoft(page, "John: empty chat");

    await page.getByTestId("quick-john-1").click();
    await expect(page.getByTestId("purchase-card")).toHaveAttribute("data-status", "proposed");
    await expect(page.getByTestId("typing")).toHaveCount(0);
    await axeSoft(page, "John: purchase proposal card and trace");
    await shot(page, "a11y-john-1-proposal");

    await page.getByTestId("confirm-purchase-open").click();
    await expect(page.getByTestId("purchase-dialog")).toBeVisible();
    await axeSoft(page, "John: purchase confirmation dialog");
    await shot(page, "a11y-john-2-dialog");
    await page.getByTestId("confirm-purchase").click();
    await expect(page.getByTestId("order-card")).toContainText("Order placed");
    await axeSoft(page, "John: order placed");
    await expect(page.getByTestId("push-banner")).toContainText("Delivered", { timeout: 20_000 });
    await axeSoft(page, "John: delivered push banner");
    await shot(page, "a11y-john-3-delivered");

    await page.getByTestId("quick-john-2").click();
    // While the ALDO agent waits for the reason, the usual reasons are one tap away (the owner's polish note).
    await expect(page.getByTestId("reason-chips")).toBeVisible();
    await expect(page.getByTestId("typing")).toHaveCount(0);
    await axeSoft(page, "John: reason chips after 'I want to return these'");
    await shot(page, "a11y-john-3b-reasons");
    for (const n of [3, 4, 5]) await page.getByTestId(`quick-john-${n}`).click();
    const quote = page.getByTestId("quote-card");
    await expect(quote).toBeVisible();
    await expect(page.getByTestId("typing")).toHaveCount(0);
    await axeSoft(page, "John: exchange options card");
    await quote.getByTestId("see-other-options").click();
    await axeSoft(page, "John: exchange card with the other options open");
    await shot(page, "a11y-john-4-quote");
    await quote.getByTestId("confirm-option-open").click();
    await expect(page.getByTestId("approve-dialog")).toBeVisible();
    await axeSoft(page, "John: exchange confirmation dialog");
    await page.getByTestId("confirm-approve").click();
    await expect(page.getByTestId("txn-card")).toContainText("Exchange confirmed");
    await axeSoft(page, "John: exchange confirmed card and trace");
    await shot(page, "a11y-john-5-confirmed");

    await page.getByTestId("memory-open").click();
    await expect(page.getByTestId("memory-dialog")).toBeVisible();
    await axeSoft(page, "John: memory sheet");
    await page.keyboard.press("Escape");
    await page.goto("/#/cases");
    await expect(page.getByTestId("cases-view")).toBeVisible();
    await axeSoft(page, "Cases page");
  });

  test("David: bundle options card, bundle confirmation dialog, confirmed bundle", async ({ page, request }) => {
    test.setTimeout(240_000);
    await resetDemo(request, "default");
    await signInAs(page, "David");
    await page.getByTestId("quick-david-1").click();
    await expect(page.getByTestId("workflow-pill")).toContainText("Return");
    // The first reply lists the ten items, numbered (addendum 11.2): scan and keep a picture of that list.
    await expect(page.getByTestId("bubble-bot").last().locator("li")).toHaveCount(10);
    await expect(page.getByTestId("typing")).toHaveCount(0);
    await axeSoft(page, "David: the ten items listed");
    await shot(page, "a11y-david-0-items");
    await page.getByTestId("quick-david-2").click();
    const quote = page.getByTestId("quote-card");
    await expect(quote).toBeVisible();
    await expect(page.getByTestId("typing")).toHaveCount(0);
    await axeSoft(page, "David: bundle options card");
    await shot(page, "a11y-david-1-bundle");
    await quote.getByTestId("confirm-option-open").click();
    await expect(page.getByTestId("approve-dialog")).toBeVisible();
    await axeSoft(page, "David: bundle confirmation dialog (items list)");
    await page.getByTestId("confirm-approve").click();
    await expect(page.getByTestId("txn-card").first()).toContainText("Return label created");
    await axeSoft(page, "David: bundle confirmed card and trace");
    await shot(page, "a11y-david-2-confirmed");
  });
});

test.describe("a11y: operator panel", () => {
  test("admin mode: regression customers and the demo controls", async ({ page }) => {
    await page.goto("/?admin=1");
    await expect(page.getByTestId("admin-panel")).toBeVisible();
    await axeSoft(page, "Admin mode: regression customers and demo controls");
    await shot(page, "a11y-admin-picker");
  });
});

test.describe("a11y: keyboard only", () => {
  test("John's purchase from the persona picker to the placed order, with Tab, Enter and Escape only", async ({ page, request }) => {
    test.setTimeout(240_000);
    await resetDemo(request, "default");
    await page.goto("/");
    // Skip link first, then the persona buttons.
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused();
    await expectFocusVisible(page, "the skip link");
    const john = page.getByTestId("persona-DEMO-CUST-JOHN");
    await tabTo(page, john);
    await expectFocusVisible(page, "John's persona button");
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("chat-screen")).toBeVisible();

    // The chat log is a polite live region; the quick reply is reachable and sends with Enter.
    await expect(page.getByTestId("chat-log")).toHaveAttribute("aria-live", "polite");
    const quick = page.getByTestId("quick-john-1");
    await tabTo(page, quick);
    await expectFocusVisible(page, "the quick reply chip");
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("purchase-card")).toHaveAttribute("data-status", "proposed");
    await expect(page.getByTestId("typing")).toHaveCount(0);

    // Confirm opens the sheet: focus moves in, Tab wraps inside, the page behind is unreachable, Escape returns focus.
    const open = page.getByTestId("confirm-purchase-open");
    await tabTo(page, open);
    await expectFocusVisible(page, "Confirm purchase");
    await page.keyboard.press("Enter");
    const dialog = page.getByTestId("purchase-dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute("aria-modal", "true");
    for (let i = 0; i < 8; i++) {
      const inside = await dialog.evaluate((el) => el.contains(document.activeElement));
      expect(inside, `focus stays inside the dialog after ${i} Tab presses`).toBeTruthy();
      await page.keyboard.press("Tab");
    }
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(open).toBeFocused();
    await expect(page.getByTestId("order-card")).toHaveCount(0);

    // Reopen and place the order with the keyboard.
    await page.keyboard.press("Enter");
    await expect(dialog).toBeVisible();
    const confirm = page.getByTestId("confirm-purchase");
    await tabTo(page, confirm);
    await expectFocusVisible(page, "Confirm and place order");
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("order-card")).toContainText("Order placed");
    await expect(page.getByTestId("order-id")).toHaveText(/^DEMO-A2A-[0-9A-F]{6}$/);
    await shot(page, "a11y-keyboard-order-placed");
    // Once delivered, the next quick reply is reachable by keyboard too.
    await expect(page.getByTestId("push-banner")).toContainText("Delivered", { timeout: 20_000 });
    await tabTo(page, page.getByTestId("quick-john-2"));
    await expectFocusVisible(page, "the next quick reply");
  });
});
