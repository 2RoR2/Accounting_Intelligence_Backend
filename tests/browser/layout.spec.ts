import { test, expect, type Page } from "@playwright/test";
const E2E_PASSWORD = process.env.E2E_PASSWORD ?? "DemoPass!2026";
async function signIn(page: Page, email: string) {
  await page.goto("/login");
  await expect(page.getByLabel("Demo account", { exact: true })).toHaveCount(0);
  await page.getByLabel("Company email *", { exact: true }).fill(email);
  await page.getByLabel("Password *", { exact: true }).fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Continue with email" }).click();
  await page.getByRole("link", { name: /Continue to dashboard/ }).click();
  const dashboard = page.getByRole("heading", {
    name: /Platform overview|Company overview|Welcome back/,
  });
  const forced = page.getByRole("heading", { name: "Choose a new password" });
  await expect(dashboard.or(forced)).toBeVisible();
  if (await forced.isVisible()) {
    await page.getByLabel("New password", { exact: true }).fill("WorkspacePassword!");
    await page
      .getByLabel("Confirm new password", { exact: true })
      .fill("WorkspacePassword!");
    await page.getByRole("button", { name: "Save password" }).click();
  }
  await expect(dashboard).toBeVisible();
}
test("desktop and mobile layouts, navigation, and tenant isolation", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: /Less paperwork/ }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("landing-desktop.png"),
    fullPage: true,
  });
  await signIn(page, "admin@saic.example");
  await page.screenshot({
    path: testInfo.outputPath("admin-desktop.png"),
    fullPage: true,
  });
  for (const route of [
    "approvals",
    "tenants",
    "users",
    "processing",
    "performance",
    "records",
    "audit",
    "settings",
  ]) {
    await page.goto(`/admin/${route}`);
    await expect(page.locator("main h1").first()).toBeVisible();
    await expect(
      page.getByText("We couldn’t complete that request."),
    ).toHaveCount(0);
  }
  await page.goto("/admin/records");
  await page.getByRole("button", { name: "View records" }).first().click();
  await page.getByRole("button", { name: "Support View" }).first().click();
  await page
    .getByLabel("Support reason *")
    .fill("Investigating invoice support ticket");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByText("READ-ONLY SUPPORT VIEW", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Close dialog" }).click();
  await signIn(page, "alice@abc.example");
  await page.screenshot({
    path: testInfo.outputPath("company-desktop.png"),
    fullPage: true,
  });
  for (const route of [
    "documents",
    "upload",
    "exceptions",
    "records",
    "users",
    "validation-rules",
    "reports",
    "audit",
    "settings",
  ]) {
    await page.goto(`/company/${route}`);
    await expect(page.locator("main h1").first()).toBeVisible();
    await expect(
      page.getByText("We couldn’t complete that request."),
    ).toHaveCount(0);
  }
  await signIn(page, "rachel@abc.example");
  await page.goto("/workspace/documents/INVOICE_100002");
  await expect(page.getByLabel("Total Amount", { exact: true })).toHaveValue(
    "8480.00",
  );
  await page.screenshot({
    path: testInfo.outputPath("review-desktop.png"),
    fullPage: true,
  });
  await page.getByLabel("Total Amount", { exact: true }).fill("848.00");
  await page
    .getByRole("button", { name: "Save Total Amount", exact: true })
    .click();
  await expect(page.getByText(/Corrected: 848.00/)).toBeVisible();
  await page
    .getByRole("button", { name: "Save Reference / PO Number", exact: true })
    .click();
  await page
    .getByLabel("Correction note *")
    .fill("Verified total and PO against the source invoice.");
  await page.getByRole("button", { name: "Save & revalidate" }).click();
  await page.getByRole("button", { name: "Validation", exact: true }).click();
  await page
    .getByRole("button", { name: "Create standardised record" })
    .click();
  await page
    .getByRole("button", { name: "Standardised Record", exact: true })
    .click();
  await expect(
    page.getByText("STANDARDISED ACCOUNTING RECORD", { exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/workspace/dashboard");
  await expect(
    page.getByRole("heading", { name: "Welcome back, Rachel" }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("workspace-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("link", { name: "My Documents", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "My documents" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.goto("/workspace/documents/RECEIPT_100001");
  await expect(
    page.getByRole("heading", { name: "September-receipt.jpg", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("review-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await signIn(page, "sarah@xyz.example");
  await page.goto("/workspace/documents");
  await expect(page.getByText("INVOICE_100001", { exact: true })).toHaveCount(0);
  await page.goto("/workspace/documents/INVOICE_100001");
  await expect(
    page.getByText("Document not found in your workspace."),
  ).toBeVisible();
  expect(errors).toEqual([]);
});
