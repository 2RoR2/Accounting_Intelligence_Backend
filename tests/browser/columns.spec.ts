import { test, expect } from "@playwright/test";
import { createHmac } from "node:crypto";
const E2E_PASSWORD = process.env.E2E_PASSWORD ?? "DemoPass!2026";

function totp(secret: string) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const char of secret.replace(/\s/g, "").toUpperCase()) bits += alphabet.indexOf(char).toString(2).padStart(5, "0");
  const bytes = Buffer.alloc(Math.floor(bits.length / 8));
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(bits.slice(i * 8, i * 8 + 8), 2);
  const counter = Math.floor(Date.now() / 1000 / 30);
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(BigInt(counter));
  const digest = createHmac("sha1", bytes).update(message).digest();
  const offset = digest[digest.length - 1] & 15;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1000000).padStart(6, "0");
}

test("email sign-in and distinct administrative columns", async ({ page }, testInfo) => {
  await page.goto("/login");
  await expect(page.getByLabel("Demo account", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Company email *", { exact: true })).toHaveValue(
    "",
  );
  await page
    .getByLabel("Company email *", { exact: true })
    .fill("platform.demo@saic.example");
  await page.getByLabel("Password *", { exact: true }).fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Continue with email" }).click();
  if (await page.getByRole("heading", { name: "Verify your sign-in" }).isVisible().catch(() => false)) {
    const secret = await page.getByLabel("Manual TOTP setup key", { exact: true }).inputValue();
    const code = totp(secret);
    for (let i = 0; i < code.length; i++) {
      const input = page.getByLabel(`Authenticator digit ${i + 1}`, { exact: true });
      await input.fill(code[i]);
      await expect(input).toHaveValue(code[i]);
    }
    await page.getByRole("button", { name: "Verify and continue" }).click();
  }
  await page.getByRole("link", { name: /Continue to dashboard/ }).click();
  await expect(
    page.getByRole("heading", { name: "Platform overview" }),
  ).toBeVisible();
  await page.goto("/admin/approvals");
  const table = page.getByRole("table").first();
  for (const name of [
    "Company",
    "Registration number",
    "Contact person",
    "Company email",
  ]) {
    await expect(
      table.getByRole("columnheader", { name, exact: true }),
    ).toBeVisible();
  }
  const meridian = table
    .getByRole("row")
    .filter({ hasText: "Meridian Group Sdn Bhd" });
  await expect(meridian.getByRole("cell").nth(0)).toHaveText(
    "Meridian Group Sdn Bhd",
  );
  await expect(meridian.getByRole("cell").nth(1)).toHaveText("202601002100");
  await expect(meridian.getByRole("cell").nth(2)).toHaveText("Lim Wei Jun");
  await expect(meridian.getByRole("cell").nth(3)).toHaveText(
    "wei@meridian.example",
  );
  const colors = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    return ["--navy-deep", "--blue-brand", "--blue-light"].map((name) =>
      root.getPropertyValue(name).trim(),
    );
  });
  expect(colors).toEqual(["#061433", "#022fa2", "#eaf5ff"]);
  await page.screenshot({
    path: testInfo.outputPath("approvals-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("approvals-mobile.png"),
    fullPage: true,
  });
  await page.goto("/login");
  await page.screenshot({
    path: testInfo.outputPath("login-mobile.png"),
    fullPage: true,
  });
});
