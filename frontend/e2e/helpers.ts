import AxeBuilder from "@axe-core/playwright";
import { expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

/** "mock" (default) runs everything, including mock-only controls and scripted wording; "deployed" skips those. */
export const IS_MOCK = (process.env.E2E_TARGET ?? "mock") === "mock";
export const MOCK_URL = process.env.MOCK_URL ?? "http://127.0.0.1:3001";
export const ADMIN_SECRET = process.env.E2E_ADMIN_SECRET ?? "mock-admin";
export const SHOT_DIR = process.env.E2E_SHOT_DIR ?? join(process.cwd(), "..", "docs", "eval", "screenshots", "ui-v2");

export async function shot(page: Page, name: string, opts: { fullPage?: boolean } = {}): Promise<void> {
  mkdirSync(SHOT_DIR, { recursive: true });
  await page.screenshot({ path: join(SHOT_DIR, `${name}.png`), fullPage: opts.fullPage ?? false });
}

/** Axe scan; fails on serious or critical violations and logs everything else for visibility. */
export async function axeScan(page: Page, label: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]).analyze();
  const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  const other = results.violations.filter((v) => v.impact !== "serious" && v.impact !== "critical");
  const fmt = (v: (typeof results.violations)[number]) =>
    `${v.id} [${v.impact}] ${v.nodes.length} node(s): ${v.nodes
      .slice(0, 3)
      .map((n) => n.target.join(" "))
      .join(" | ")}`;
  console.log(`axe ${label}: ${blocking.length} serious/critical, ${other.length} moderate/minor`);
  for (const v of other) console.log(`  note: ${fmt(v)}`);
  expect(blocking.map(fmt), `axe serious/critical violations on ${label}`).toEqual([]);
}

export async function resetDemo(request: APIRequestContext, variant: "default" | "dc_stockout" | "all_stockout"): Promise<void> {
  const res = await request.post("/api/demo/reset", { headers: { "X-Demo-Admin": ADMIN_SECRET }, data: { variant } });
  expect(res.ok(), `reset ${variant} -> ${res.status()}`).toBeTruthy();
}

/** Pick a customer on the phone's first screen ("Who is using the personal agent?"). */
export async function signInAs(page: Page, name: string, query = ""): Promise<void> {
  await page.goto(`/${query}`);
  await page.getByRole("button", { name: new RegExp(`^${name}\\b`) }).click();
  await expect(page.getByTestId("chat-screen")).toBeVisible();
}

/** Press Tab until `target` has focus (a keyboard-only way to reach a control). */
export async function tabTo(page: Page, target: Locator, maxTabs = 150): Promise<void> {
  for (let i = 0; i < maxTabs; i++) {
    const focused = await target.evaluate((el) => el === document.activeElement).catch(() => false);
    if (focused) return;
    await page.keyboard.press("Tab");
  }
  throw new Error(`Could not reach ${target} with ${maxTabs} Tab presses`);
}

export async function mockPost(request: APIRequestContext, path: string, data: unknown): Promise<void> {
  const res = await request.post(`${MOCK_URL}${path}`, { data });
  expect(res.ok(), `${path} -> ${res.status()}`).toBeTruthy();
}

/** The size_request_id of the newest size_recommended event on the A2A feed (read with the page's own session). */
export async function latestSizeRequestId(page: Page): Promise<string> {
  return page.evaluate(async () => {
    const raw = sessionStorage.getItem("aldo.demo.session");
    const token = raw ? (JSON.parse(raw) as { token: string }).token : "";
    const res = await fetch("/api/a2a/events?after=0", { headers: { Authorization: `Bearer ${token}` } });
    const body = (await res.json()) as { events: { type: string; payload: { size_request_id?: string } }[] };
    return body.events.filter((e) => e.type === "size_recommended").at(-1)?.payload.size_request_id ?? "";
  });
}

/** Run assertions that depend on the mock's scripted text and data; skipped against a real deployment. */
export async function onMock(fn: () => Promise<void>): Promise<void> {
  if (IS_MOCK) await fn();
}
