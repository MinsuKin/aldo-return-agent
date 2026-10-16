import { expect, test, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { SHOT_DIR } from "./helpers";

// Visual inspection of the deployed site's STATIC screens only: no messages, no confirm clicks, and never
// /api/demo/reset. We only LOAD each screen and screenshot it at four settings. Run against the real site:
//   cd backend && ../scripts/with_aws_env.sh env SECRETS_ARN=<arn> DEMO_BASE_URL=https://<domain> \
//     uv run --no-sync python ../scripts/test_run_e2e_deployed.py visual.spec.ts
// Output: docs/eval/screenshots/v2/visual/<screen>-<viewport>-<scheme>.png  (SHOT_DIR is .../v2; we add visual/).

const OUT = join(SHOT_DIR, "visual");

type Setting = { viewport: { width: number; height: number }; scheme: "light" | "dark"; tag: string };

// 1440x900 light, 1440x900 dark, 1000x900 (stacked), 390x844 (phone). Tag = <viewport>-<scheme>.
const SETTINGS: Setting[] = [
  { viewport: { width: 1440, height: 900 }, scheme: "light", tag: "1440x900-light" },
  { viewport: { width: 1440, height: 900 }, scheme: "dark", tag: "1440x900-dark" },
  { viewport: { width: 1000, height: 900 }, scheme: "light", tag: "1000x900-light" },
  { viewport: { width: 390, height: 844 }, scheme: "light", tag: "390x844-light" },
];

async function save(page: Page, screen: string, tag: string): Promise<void> {
  const path = join(OUT, `${screen}-${tag}.png`);
  mkdirSync(dirname(path), { recursive: true });
  await page.screenshot({ path, fullPage: false });
}

// Each screen is reached by LOADING a URL only (plus, for the chat screens, a single persona pick which is a sign-in,
// not a demo message). Nothing is sent, confirmed, or reset.
const SCREENS: { name: string; open: (page: Page) => Promise<void> }[] = [
  {
    name: "persona-picker",
    open: async (page) => {
      await page.goto("/");
      await expect(page.getByTestId("persona-picker")).toBeVisible();
    },
  },
  {
    name: "john-chat",
    open: async (page) => {
      await page.goto("/");
      await page.getByTestId("persona-DEMO-CUST-JOHN").click();
      await expect(page.getByTestId("chat-screen")).toBeVisible();
    },
  },
  {
    name: "david-chat",
    open: async (page) => {
      await page.goto("/");
      await page.getByTestId("persona-DEMO-CUST-DAVID").click();
      await expect(page.getByTestId("chat-screen")).toBeVisible();
    },
  },
  {
    name: "admin-picker",
    open: async (page) => {
      await page.goto("/?admin=1");
      await expect(page.getByTestId("persona-picker")).toBeVisible();
    },
  },
  {
    name: "cases",
    open: async (page) => {
      // /#/cases is a read-only page that needs a session, so sign in first (a persona pick, not a demo message),
      // then navigate. No message is sent, nothing is confirmed, and the demo is never reset.
      await page.goto("/");
      await page.getByTestId("persona-DEMO-CUST-JOHN").click();
      await expect(page.getByTestId("chat-screen")).toBeVisible();
      await page.goto("/#/cases");
      await expect(page.getByTestId("cases-view")).toBeVisible();
    },
  },
];

for (const setting of SETTINGS) {
  test.describe(`visual @ ${setting.tag}`, () => {
    test.use({ viewport: setting.viewport, colorScheme: setting.scheme });
    for (const screen of SCREENS) {
      test(`${screen.name}`, async ({ page }) => {
        await screen.open(page);
        await page.waitForTimeout(500); // let banners / layout settle
        await save(page, screen.name, setting.tag);
        // Finding F12: at 390 px the stacked layout was 6 px wider than the viewport. No screen may scroll sideways.
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow, `${screen.name} @ ${setting.tag}: horizontal page overflow in px`).toBeLessThanOrEqual(0);
      });
    }
  });
}
