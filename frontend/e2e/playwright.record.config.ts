import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// Recording variant of playwright.config.ts: SAME mock/external webServer logic (keyed on E2E_BASE_URL) and the same
// baseURL rule, but captures video for every test at a fixed 1440x900. Used to produce the demo recordings; the final
// recordings against the real site are made by the parent, so this config is validated against the mock only:
//   cd frontend && npx playwright test -c e2e/playwright.record.config.ts -g 'John|David' --repeat-each=2
const external = !!process.env.E2E_BASE_URL;

// This config lives in frontend/e2e, but the mock/vite commands and their relative paths are written as the base
// playwright.config.ts does — i.e. relative to the frontend root. Pin the webServer cwd there so they resolve the same.
const FRONTEND_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

export default defineConfig({
  testDir: ".",
  testMatch: "demo.spec.ts",
  timeout: Number(process.env.E2E_TEST_TIMEOUT_MS ?? 240_000),
  expect: { timeout: Number(process.env.E2E_EXPECT_TIMEOUT_MS ?? 45_000) },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  outputDir: "../test-results-rec",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    viewport: { width: 1440, height: 900 },
    trace: "off",
    video: "on",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: external
    ? undefined
    : [
        {
          command: "node mock/server.ts",
          cwd: FRONTEND_ROOT,
          url: "http://127.0.0.1:3001/__mock/health",
          reuseExistingServer: true,
          timeout: 30_000,
          env: { MOCK_TIME_SCALE: process.env.MOCK_TIME_SCALE ?? "0.25", MOCK_QUIET: "1" },
        },
        { command: "npx vite", cwd: FRONTEND_ROOT, url: "http://localhost:5173", reuseExistingServer: true, timeout: 60_000 },
      ],
});
