import { createHmac, randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";

// Everything goes through the real n8n workflow: the page posts a sample to n8n's
// webhook, n8n calls the API, and the review page shows what happened.
const N8N = "http://localhost:5690/webhook";
const MOCK = "http://127.0.0.1:4041";
const CAL_SECRET = "local-cal-secret";

async function send(page: Page, title: string) {
  await page.goto("/");
  await page.locator(".sample", { hasText: title }).click();
  await page.getByRole("button", { name: "Send through n8n" }).click();
  await expect(page).toHaveURL(/\/leads\/[0-9a-f-]{36}$/);
}

const status = (page: Page) => page.getByTestId("status");

test("a hot booking goes through n8n to HubSpot and a Slack alert", async ({ page, request }) => {
  await send(page, "Operations director books a demo for 140 vans");
  await expect(status(page)).toHaveText("Synced");
  await expect(page.getByTestId("tier")).toContainText("hot");
  await expect(page.getByTestId("hubspot")).toContainText("Deal");
  await expect(page.getByTestId("rules")).toContainText("booked_meeting");
  await expect(page.getByRole("region", { name: "Company website" })).toContainText("BrightPath Logistics");
  await expect(page.getByTestId("audit")).toContainText(/alert sent/i);
  const slack = await (await request.get(`${MOCK}/slack/messages`)).json();
  expect(slack.at(-1).text).toContain("Hot lead: *Maria Santos*");
});

test("an unsure lead waits for a person, who edits and approves it", async ({ page }) => {
  await send(page, "A message that tries to set its own score");
  await expect(status(page)).toHaveText("Needs review");
  await expect(page.getByRole("region", { name: "Needs your decision" })).toContainText(
    "The message tries to tell the scorer what to do.",
  );
  await page.getByLabel("nurture").check();
  await page.getByLabel("Score", { exact: true }).fill("30");
  await page.getByLabel("Note for the CRM (optional)").fill("Browsing only; keep in nurture.");
  await page.getByRole("button", { name: "Approve and sync" }).click();
  await expect(page.getByText("Saved. n8n picked the lead up again.")).toBeVisible();
  await expect(status(page)).toHaveText("Synced");
  await expect(page.getByTestId("tier")).toContainText("cold · 30/100");
  await expect(page.getByTestId("audit")).toContainText(/approved/i);
  await expect(page.getByTestId("audit")).toContainText("demo visitor");
});

test("a rejected lead never reaches the CRM", async ({ page }) => {
  await send(page, "A competitor fishing for prices");
  await expect(status(page)).toHaveText("Needs review");
  await page.getByRole("button", { name: "Reject" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Reject" }).click();
  await expect(status(page)).toHaveText("Rejected");
  await expect(page.getByText("Not sent to the CRM.")).toBeVisible();
});

test("spam is discarded without a review", async ({ page }) => {
  await send(page, "SEO agency pitch");
  await expect(status(page)).toHaveText("Discarded");
  await expect(page.getByTestId("tier")).toContainText("spam");
});

test("robots.txt is respected when reading the company website", async ({ page }) => {
  await send(page, "Moving company books a demo");
  await expect(status(page)).toHaveText("Synced");
  await expect(page.getByTestId("site-status")).toContainText("robots.txt asks LeadLoomBot to stay out");
});

test("a redelivered booking maps to the same lead, and a forged one is refused", async ({ request }) => {
  const body = JSON.stringify({
    triggerEvent: "BOOKING_CREATED",
    payload: {
      uid: randomUUID(),
      startTime: "2026-12-01T09:00:00Z",
      attendees: [{ email: "ops@redelivery-test.example", name: "Rita Ops" }],
      additionalNotes: "We run 60 vans and want a demo next month.",
    },
  });
  const sign = (secret: string) => createHmac("sha256", secret).update(body).digest("hex");
  const post = (secret: string) =>
    request.post(`${N8N}/leads/booking`, {
      data: body,
      headers: { "Content-Type": "application/json", "X-Cal-Signature-256": sign(secret) },
    });
  const first = await post(CAL_SECRET);
  const again = await post(CAL_SECRET);
  expect(first.status()).toBe(201);
  expect(again.status()).toBe(200);
  const [a, b] = [await first.json(), await again.json()];
  expect(b).toEqual({ lead_id: a.lead_id, duplicate: true });
  const forged = await post("not-the-secret");
  expect(forged.status()).toBe(401);
});

test("demo leads stay in the browser that sent them; reviewers see everything", async ({ page, browser }) => {
  await send(page, "SEO agency pitch");
  const url = page.url();
  const stranger = await browser.newPage();
  expect((await stranger.goto(url))?.status()).toBe(404);
  await stranger.goto("/signin");
  await stranger.getByLabel("Reviewer key").fill("local-reviewer-key");
  await stranger.getByRole("button", { name: "Sign in" }).click();
  await expect(stranger.getByRole("heading", { name: "Leads" })).toBeVisible();
  expect((await stranger.goto(url))?.status()).toBe(200);
});

test("a failed HubSpot sync can be sent back through n8n", async ({ page, request }) => {
  // More failures than n8n's 3 tries of the API's 4 attempts each.
  await request.post(`${MOCK}/hubspot/_fail`, { data: { match: "/objects/notes", status: 503, times: 50 } });
  try {
    await send(page, "Grocery delivery firm asks for pricing for 60 vehicles");
    await expect(status(page)).toHaveText("Sync failed");
    // Let n8n finish its own retries first: three failed sync calls in the audit log.
    await expect
      .poll(
        async () => {
          await page.reload();
          return page.getByTestId("audit").getByText("sync failed").count();
        },
        { timeout: 60_000, intervals: [2_000] },
      )
      .toBe(3);
  } finally {
    await request.delete(`${MOCK}/hubspot/_fail`);
  }
  await page.getByRole("button", { name: "Retry through n8n" }).click();
  await expect(page.getByText("Sent back through n8n.")).toBeVisible();
  await expect(status(page)).toHaveText("Synced");
});

test("sending a sample shows that it is working and cannot be sent twice", async ({ page }) => {
  await page.goto("/");
  await page.locator(".sample", { hasText: "SEO agency pitch" }).click();
  await expect(page.getByLabel("Message", { exact: true })).toHaveValue(/page 1 results/);
  // Runs after the page's own submit handler, and keeps what it saw across the navigation.
  await page.evaluate(() => {
    document.querySelector(".composer")!.addEventListener("submit", () => {
      const button = document.querySelector<HTMLButtonElement>(".composer button[type=submit]")!;
      const note = document.querySelector<HTMLElement>(".composer .sending")!;
      const state = { disabled: button.disabled, label: button.textContent!.trim(), noteShown: !note.hidden };
      sessionStorage.setItem("busy", JSON.stringify(state));
    });
  });
  await page.getByRole("button", { name: "Send through n8n" }).click();
  await expect(page).toHaveURL(/\/leads\/[0-9a-f-]{36}$/);
  const busy = JSON.parse((await page.evaluate(() => sessionStorage.getItem("busy"))) ?? "null");
  expect(busy).toEqual({ disabled: true, label: "Sending through n8n…", noteShown: true });
});

test("signing out asks first", async ({ page }) => {
  await page.goto("/signin");
  await page.getByLabel("Reviewer key").fill("local-reviewer-key");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("button", { name: "Sign out" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Sign out?");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByText("Reviewer", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Sign out" }).click();
  await dialog.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("link", { name: "Reviewer sign-in" })).toBeVisible();
});

test("on a phone the choice controls line up in even rows", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const rows = (selector: string) =>
    page.locator(selector).evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().top)));
  await page.goto("/");
  expect(new Set(await rows(".seg label")).size).toBe(1);
  await send(page, "A message that tries to set its own score");
  await expect(status(page)).toHaveText("Needs review");
  const routes = await rows(".decision .seg label");
  expect([...new Set(routes)].map((top) => routes.filter((t) => t === top).length)).toEqual([3, 3]);
});

test("opening Try it starts waking n8n", async ({ page }) => {
  const wake = page.waitForRequest((r) => r.url().endsWith("/n8n/wake") && r.method() === "POST");
  await page.goto("/");
  await wake;
});
