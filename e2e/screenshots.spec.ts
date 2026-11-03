import { expect, test, type Page } from "@playwright/test";

// README screenshots: SCREENSHOTS=1 npx playwright test e2e/screenshots.spec.ts
test.skip(!process.env.SCREENSHOTS, "Set SCREENSHOTS=1 to refresh docs/screenshots.");
test.use({ viewport: { width: 1280, height: 900 } });

const shot = (page: Page, name: string) => page.screenshot({ path: `docs/screenshots/${name}.png`, fullPage: true });

async function send(page: Page, title: string) {
  await page.goto("/");
  await page.locator("form.sample", { hasText: title }).getByRole("button", { name: "Send through n8n" }).click();
  await expect(page).toHaveURL(/\/leads\/[0-9a-f-]{36}$/);
}

for (const scheme of ["light", "dark"] as const) {
  test(`pages in ${scheme} mode`, async ({ page }) => {
    const suffix = scheme === "dark" ? "-dark" : "";
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto("/");
    await shot(page, `home${suffix}`);

    await send(page, "Operations director books a demo for 140 vans");
    await expect(page.getByTestId("status")).toHaveText("Synced");
    await page.goto(page.url()); // drop the refresh-time flash
    await shot(page, `lead-synced${suffix}`);

    await send(page, "A message that tries to set its own score");
    await expect(page.getByTestId("status")).toHaveText("Needs review");
    await shot(page, `lead-review${suffix}`);

    await send(page, "SEO agency pitch");
    await expect(page.getByTestId("status")).toHaveText("Discarded");
    await page.goto("/leads");
    await shot(page, `leads${suffix}`);
  });
}

test.describe("n8n", () => {
  test.use({ deviceScaleFactor: 2 });

  test("the n8n workflow", async ({ page }) => {
    await page.goto("http://localhost:5690/signin");
    await page.getByLabel(/email/i).fill("owner@leadloom.local");
    await page.getByLabel(/password/i).fill("LeadLoom-local-1");
    await page.getByRole("button", { name: /sign in/i }).click();
    await page.goto("http://localhost:5690/workflow/LeadLoomIntake01");
    await expect(page.getByText("Wait for a reviewer")).toBeVisible();
    const checklist = page.getByRole("dialog").filter({ hasText: "Production Checklist" });
    if (await checklist.isVisible()) {
      await checklist.getByTitle("Close").or(checklist.getByLabel("Close")).first().click();
      await expect(checklist).toBeHidden();
    }
    await page.getByRole("button", { name: "Zoom to Fit" }).click();
    await page.waitForTimeout(1000);
    await page.screenshot({ path: "docs/screenshots/n8n-workflow.png" });
  });
});
