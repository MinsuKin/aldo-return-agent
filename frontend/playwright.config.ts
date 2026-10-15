import { defineConfig, devices } from "@playwright/test";

// The same specs run against the local mock (default) and the deployed site:
//   E2E_BASE_URL=https://<cloudfront-domain> E2E_TARGET=deployed E2E_ADMIN_SECRET=... \
//   E2E_SHOT_DIR=../docs/eval/screenshots E2E_EXPECT_TIMEOUT_MS=120000 E2E_TEST_TIMEOUT_MS=900000 \
//   npx playwright test
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
    viewport: { width: 1600, height: 1000 },
    trace: "off",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1600, height: 1000 } } }],
});
