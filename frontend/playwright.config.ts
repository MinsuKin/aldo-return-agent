import { defineConfig, devices } from "@playwright/test";

// Functional specs for the phone UI. By default they start (or reuse) the local mock API on port 3001 and
// the Vite dev server on port 5173; point them at a deployment instead with
//   E2E_BASE_URL=https://<cloudfront-domain> E2E_TARGET=deployed E2E_ADMIN_SECRET=... npx playwright test
const external = !!process.env.E2E_BASE_URL;

export default defineConfig({
  testDir: "./e2e",
  timeout: Number(process.env.E2E_TEST_TIMEOUT_MS ?? 240_000),
  expect: { timeout: Number(process.env.E2E_EXPECT_TIMEOUT_MS ?? 45_000) },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  outputDir: "./test-results",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    viewport: { width: 1440, height: 900 },
    trace: "off",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: external
    ? undefined
    : [
        {
          command: "node mock/server.ts",
          url: "http://127.0.0.1:3001/__mock/health",
          reuseExistingServer: true,
          timeout: 30_000,
          env: { MOCK_TIME_SCALE: process.env.MOCK_TIME_SCALE ?? "0.25", MOCK_QUIET: "1" },
        },
        { command: "npx vite", url: "http://localhost:5173", reuseExistingServer: true, timeout: 60_000 },
      ],
});
