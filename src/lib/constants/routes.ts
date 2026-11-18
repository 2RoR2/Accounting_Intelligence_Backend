import type { Role } from "@/types";

export const PUBLIC_ROUTES = new Set(["login", "welcome", "request-access", "activate", "forgot-password", "reset-password", "help"]);
export const WORKSPACE_AREA: Record<Role, string> = { SUPER_ADMIN: "admin", LOCAL_ADMIN: "company", ACCOUNTANT: "workspace" };
export const WORKSPACE_PAGES: Record<string, ReadonlySet<string>> = {
  admin: new Set(["dashboard", "approvals", "tenants", "users", "processing", "performance", "records", "audit", "settings", "profile"]),
  company: new Set([
  "dashboard", "documents", "receipts", "upload", "exceptions",
  "approvals", "contacts", "reconciliation",
  "records", "users", "validation-overview", "validation-rules", "reports", "audit", "evaluation", "accounting", "invoices", "credit-notes", "debit-notes", "bills", "purchase-credit-notes", "purchase-debit-notes", "bank-money-in", "bank-money-out", "bank-transfers", "bank-accounts", "bank-feeds", "accounting-deemed-payments", "accounting-chart", "accounting-trial-balance", "company-profile", "company-settings", "contact-detail",
  "settings", "profile", "money-in", "money-out", "ledger", "accounting-journal-entries"
]),
  workspace: new Set([
    "dashboard", "evaluation", "documents", "receipts", "upload", "exceptions", "contacts", "records", "reports", "settings", "help", "profile",
    "money-in", "money-out", "reconciliation", "ledger", "accounting", "validation-overview",
    "invoices", "credit-notes", "debit-notes", "bills", "purchase-credit-notes", "purchase-debit-notes",
    "bank-money-in", "bank-money-out", "bank-transfers", "bank-accounts", "bank-feeds",
    "accounting-deemed-payments", "accounting-journal-entries", "accounting-chart", "accounting-trial-balance", "flow-digests", "company-profile", "company-settings",
    "store-orders", "store-catalogue", "store-coupons", "store-content", "store-settings", "minipos-settings",
    "contact-detail"
  ]),
};
const DETAIL_PAGES = new Set(["documents", "records", "tenants"]);
export const isPublicRoute = (path: string[]) => path.length === 1 && PUBLIC_ROUTES.has(path[0]);
export function isAllowedWorkspaceRoute(path: string[], role: Role) {
  const [area, page, id] = path;
  const validPage = WORKSPACE_PAGES[area]?.has(page) === true;
  const validDetail = !id || id.startsWith("view-") || DETAIL_PAGES.has(page) || (page === "evaluation" && id === "effort-study");
  return path.length <= 3 && area === WORKSPACE_AREA[role] && validPage && validDetail;
}
