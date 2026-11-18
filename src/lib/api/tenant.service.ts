import type { AccountLimitRequest, CompanyRequest, Tenant, User } from "@/types";
import { assertTenant, requireRole } from "../permissions";
import { api, get } from "./client";
export async function getTenants(user: User) {
  void user;
  return get<Tenant[]>("/api/tenants");
}
export async function getTenantById(user: User, tenantId: string) {
  assertTenant(user, tenantId);
  return (await getTenants(user)).find((t) => t.id === tenantId);
}
export async function getCompanyRequests(user: User) {
  requireRole(user, ["SUPER_ADMIN"]);
  return get<CompanyRequest[]>("/api/company-requests");
}
export async function requestEnterpriseAccess(
  input: Omit<CompanyRequest, "id" | "status" | "submittedAt">,
) {
  if (
    !input.company.trim() ||
    !input.registration.trim() ||
    !input.contact.trim() ||
    !Number.isInteger(input.accounts) ||
    input.accounts < 1 ||
    input.accounts > 1000
  )
    throw new Error("Complete all required fields; request 1–1,000 accounts.");
  if (
    ![input.email, ...input.employees].every((e) =>
      /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e),
    )
  )
    throw new Error("Enter valid company and employee emails.");
  return api<CompanyRequest>("/api/company-requests", { method: "POST", body: JSON.stringify({ company: input.company, registration: input.registration, contact: input.contact, email: input.email, phone: input.phone, accounts: input.accounts, employees: input.employees, notes: input.notes }) }); /*
  return mutate((db) => {
    if (
      db.requests.some(
        (r) =>
          r.email.toLowerCase() === input.email.toLowerCase() &&
          r.status === "PENDING",
      ) ||
      db.users.some((u) => u.email.toLowerCase() === input.email.toLowerCase())
    )
      throw new Error("This email already has an account or pending request.");
    const request = {
      ...input,
      id: id("REQ"),
      status: "PENDING" as const,
      submittedAt: now(),
    };
    db.requests.unshift(request);
    return request;
  }); */
}
export async function approveCompanyRequest(
  user: User,
  requestId: string,
  limit: number,
) {
  requireRole(user, ["SUPER_ADMIN"]);
  const result = await api<{ tenantId: string }>(`/api/company-requests/${requestId}/approve`, { method: "POST", body: JSON.stringify({ limit }) });
  return result.tenantId; /*
  return mutate((db) => {
    assertActive(db, user);
    const request = db.requests.find((r) => r.id === requestId);
    if (!request || request.status !== "PENDING")
      throw new Error("Request has already been reviewed.");
    if (!Number.isInteger(limit) || limit < 1)
      throw new Error("Account limit must be a positive whole number.");
    const tenantId = id("TENANT");
    const adminId = id("USR");
    db.tenants.push({
      id: tenantId,
      name: request.company,
      registration: request.registration,
      adminEmail: request.email,
      accountLimit: limit,
      status: "ACTIVE",
      createdAt: now(),
    });
    db.users.push({
      id: adminId,
      name: request.contact,
      email: request.email,
      role: "LOCAL_ADMIN",
      tenantId,
      status: "INVITED",
      invitedBy: user.name,
      invitedAt: now(),
      lastLogin: "—",
    });
    db.invitations.push({
      id: id("INV"),
      userId: adminId,
      tenantId,
      invitedBy: user.name,
      createdAt: now(),
      status: "INVITED",
    });
    db.rules[tenantId] = { ...defaultRules };
    request.status = "APPROVED";
    log(
      db,
      user,
      "COMPANY_APPROVED",
      request.company,
      `Local Administrator activation invitation created for ${request.email}.`,
      tenantId,
    );
    return tenantId;
  }); */
}
export async function rejectCompanyRequest(
  user: User,
  requestId: string,
  reason: string,
) {
  requireRole(user, ["SUPER_ADMIN"]);
  if (!reason.trim()) throw new Error("A rejection reason is required.");
  await api(`/api/company-requests/${requestId}/reject`, { method: "POST", body: JSON.stringify({ reason }) }); /*
  mutate((db) => {
    const r = db.requests.find((r) => r.id === requestId);
    if (!r || r.status !== "PENDING")
      throw new Error("Request already reviewed.");
    r.status = "REJECTED";
    r.rejectionReason = reason;
    log(db, user, "COMPANY_REJECTED", r.company, reason);
  }); */
}
export async function updateAccountLimit(
  user: User,
  tenantId: string,
  limit: number,
  reason: string,
) {
  requireRole(user, ["SUPER_ADMIN"]);
  if (!reason.trim()) throw new Error("Please provide a reason.");
  await api(`/api/tenants/${tenantId}/limit`, { method: "PATCH", body: JSON.stringify({ limit, reason }) }); /*
  mutate((db) => {
    db.tenants.find((t) => t.id === tenantId)!.accountLimit = limit;
    log(
      db,
      user,
      "ACCOUNT_LIMIT_UPDATED",
      tenantId,
      `${limit} seats. ${reason}`,
      tenantId,
    );
  }); */
}
export async function setTenantStatus(user: User, tenantId: string) {
  requireRole(user, ["SUPER_ADMIN"]);
  await api(`/api/tenants/${tenantId}/status`, { method: "PATCH" }); /*
  mutate((db) => {
    const t = db.tenants.find((t) => t.id === tenantId)!;
    t.status = t.status === "ACTIVE" ? "SUSPENDED" : "ACTIVE";
    log(db, user, "TENANT_STATUS_CHANGED", t.name, t.status, tenantId);
  }); */
}
export async function requestAccountLimit(
  user: User,
  requested: number,
  reason: string,
) {
  requireRole(user, ["LOCAL_ADMIN"]);
  const tenant = (await getTenantById(user, user.tenantId!))!;
  if (
    !Number.isInteger(requested) ||
    requested <= tenant.accountLimit ||
    !reason.trim()
  )
    throw new Error("Enter a larger whole-number limit and a reason.");
  await api("/api/account-limit-requests", { method: "POST", body: JSON.stringify({ requested, reason }) });
}
export async function getAccountLimitRequests(user: User) {
  void user;
  return get<AccountLimitRequest[]>("/api/account-limit-requests");
}
export async function reviewAccountLimit(
  user: User,
  requestId: string,
  approve: boolean,
  reason: string,
) {
  requireRole(user, ["SUPER_ADMIN"]);
  if (!reason.trim()) throw new Error("Please provide a reason.");
  await api(`/api/account-limit-requests/${requestId}/review`, { method: "POST", body: JSON.stringify({ approve, reason }) });
}
