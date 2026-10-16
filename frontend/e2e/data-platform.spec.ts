import { expect, test } from "@playwright/test";
import { ADMIN_SECRET, IS_MOCK, MOCK_URL, resetDemo, shot, signInAs } from "./helpers";

// The two-way door of the AWS data platform (docs/data-platform/DESIGN.md), seen from the admin panel (?admin=1):
// three modes, "static" is today's demo, "platform" serves the dataset analysed on AWS and says so in the trace.
// The mock emulates the admin routes and the trace fields; the live site is checked with the same steps once the
// pipeline has run (E2E_TARGET=deployed).

const trace = (page: import("@playwright/test").Page) => page.getByTestId("a2a-trace");

async function setMode(request: import("@playwright/test").APIRequestContext, mode: string): Promise<void> {
  const res = await request.post(`${MOCK_URL}/api/demo/data-platform`, { headers: { "X-Demo-Admin": ADMIN_SECRET }, data: { mode } });
  expect(res.ok()).toBeTruthy();
}

test.describe("data platform door", () => {
  test.skip(!IS_MOCK, "the live run of the door is done by the evaluation after the pipeline has produced the curated document");

  test("the admin panel shows the mode, flips it, and the trace names the AWS source only in platform mode", async ({ page, request, browser }) => {
    test.setTimeout(120_000);
    await resetDemo(request, "default");
    await setMode(request, "static");

    await page.goto("/?admin=1");
    const panel = page.getByTestId("admin-panel");
    await expect(panel).toBeVisible();
    // Everything needs the admin secret; the mode buttons need a status first.
    await expect(page.getByTestId("data-platform-check")).toBeDisabled();
    await page.getByLabel("Admin secret").fill(ADMIN_SECRET);
    await expect(page.getByTestId("data-mode-platform")).toBeDisabled();
    await page.getByTestId("data-platform-check").click();
    await expect(page.getByTestId("data-platform-status")).toContainText("Source: static");
    await expect(page.getByTestId("data-platform-status")).toContainText("AWS data ready");
    await expect(page.getByTestId("data-mode-static")).toHaveAttribute("aria-pressed", "true");

    await page.getByTestId("data-mode-platform").click();
    await expect(page.getByTestId("data-platform-status")).toContainText("Source: platform");
    await expect(page.getByTestId("data-mode-platform")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("data-mode-static")).toHaveAttribute("aria-pressed", "false");
    await shot(page, "admin-data-platform");

    // John buys the Toney: the fit signal row of the trace names where it came from.
    await page.getByTestId("persona-DEMO-CUST-JOHN").click();
    await page.getByTestId("quick-john-1").click();
    await expect(page.getByTestId("purchase-card")).toHaveAttribute("data-status", "proposed");
    const fit = trace(page).locator('[data-event-type="fit_signal_shared"]').first();
    await expect(fit).toContainText("S3 + Athena");
    await expect(fit).toContainText("mock-que"); // the first 8 characters of the Athena query id
    await shot(page, "trace-data-platform");

    // Back to static (admin API, as the panel would): the next fit signal shows no AWS source.
    await setMode(request, "static");
    await resetDemo(request, "default");
    const fresh = await browser.newContext(); // the first page keeps its session; a new visitor starts from the first screen
    const page2 = await fresh.newPage();
    await signInAs(page2, "John");
    await page2.getByTestId("quick-john-1").click();
    await expect(page2.getByTestId("purchase-card")).toHaveAttribute("data-status", "proposed");
    const staticFit = trace(page2).locator('[data-event-type="fit_signal_shared"]').first();
    await expect(staticFit).toBeVisible();
    await expect(staticFit).not.toContainText("S3 + Athena");
    await fresh.close();
  });

  test("a wrong admin secret is refused and the mode is unchanged", async ({ page, request }) => {
    await setMode(request, "static");
    await page.goto("/?admin=1");
    await page.getByLabel("Admin secret").fill("not-the-secret");
    await page.getByTestId("data-platform-check").click();
    await expect(page.getByRole("alert")).toBeVisible(); // the app words a 401 the same way for every admin call
    const status = await request.get(`${MOCK_URL}/api/demo/data-platform`, { headers: { "X-Demo-Admin": ADMIN_SECRET } });
    expect((await status.json()).mode).toBe("static");
  });
});
