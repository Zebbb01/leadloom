import { defineConfig, devices } from "@playwright/test";

// One local stack for every test: embedded PostgreSQL, the mock model/HubSpot/Slack,
// n8n with the committed workflow, and the API (scripts/dev.py). Each test gets a
// fresh browser context, so its demo leads live in their own workspace.
export default defineConfig({
  testDir: "e2e",
  timeout: 120_000,
  expect: { timeout: 45_000 },
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: { baseURL: "http://localhost:8400", trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "uv run python -m scripts.dev",
    url: "http://127.0.0.1:8400/health",
    reuseExistingServer: !process.env.CI,
    timeout: 600_000,
  },
});
