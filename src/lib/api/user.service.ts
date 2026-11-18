import type { User, UserStatus } from "@/types";
import { assertTenant, requireRole } from "../permissions";
import { passwordChecks } from "./auth.service";
import { api, get } from "./client";

export async function updateOwnProfile(user: User, name: string, avatar?: string) {
  if (name.trim().length < 2) throw new Error("Enter a valid display name.");
  if (avatar && !avatar.startsWith("data:image/")) throw new Error("Select a valid profile image.");
  return api<User>("/api/auth/profile", { method: "PATCH", body: JSON.stringify({ name: name.trim(), avatar: avatar ?? "" }) }); /*
  return mutate((db) => {
    const target = db.users.find((candidate) => candidate.id === user.id);
    if (!target) throw new Error("Profile was not found.");
    target.name = name.trim();
    target.avatar = avatar;
    log(db, user, "PROFILE_UPDATED", target.email, "Name or profile image updated.");
    return target;
  }); */
}

export async function updateAccountantCredentials(user: User, userId: string, name: string, password: string) {
  requireRole(user, ["LOCAL_ADMIN"]);
  if (name.trim().length < 2) throw new Error("Enter a valid accountant name.");
  if (password && !passwordChecks(password).every(Boolean))
    throw new Error("Use 8+ characters with uppercase, lowercase, number, and symbol.");
  return api<User>(`/api/users/${userId}/credentials`, { method: "PATCH", body: JSON.stringify({ name: name.trim(), password }) });
  /*
  return mutate((db) => {
    const target = db.users.find((candidate) => candidate.id === userId);
    if (!target || target.tenantId !== user.tenantId || target.role !== "ACCOUNTANT")
      throw new Error("Account is not manageable in this workspace.");
    target.name = name.trim();
    if (password) target.mustChangePassword = true;
    log(db, user, "ACCOUNTANT_CREDENTIALS_UPDATED", target.email, password ? "Name updated and password reset recorded." : "Name updated.");
    return target;
  }); */
}
export async function getTenantUsers(user: User, tenantId?: string) {
  requireRole(user, ["SUPER_ADMIN", "LOCAL_ADMIN", "ACCOUNTANT"]);
  if (tenantId) assertTenant(user, tenantId);
  return get<User[]>(`/api/users${tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ""}`);
}
export async function inviteAccountant(
  user: User,
  name: string,
  email: string,
) {
  requireRole(user, ["LOCAL_ADMIN"]);
  if (!name.trim() || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))
    throw new Error("Enter a full name and valid company email.");
  return api<User>("/api/users/invitations", { method: "POST", body: JSON.stringify({ name: name.trim(), email }) });
  /*
  return mutate((db) => {
    if (db.users.some((u) => u.email.toLowerCase() === email.toLowerCase()))
      throw new Error("This company email is already registered.");
    const invited: User = {
      id: id("USR"),
      name,
      email,
      role: "ACCOUNTANT",
      tenantId: tenant.id,
      status: "INVITED",
      invitedBy: user.name,
      invitedAt: now(),
      lastLogin: "—",
    };
    db.users.push(invited);
    db.invitations.push({
      id: id("INV"),
      userId: invited.id,
      tenantId: tenant.id,
      invitedBy: user.name,
      createdAt: now(),
      status: "INVITED",
    });
    log(db, user, "ACCOUNTANT_INVITED", email, "Invitation created.");
    return invited;
  }); */
}
export async function changeUserStatus(
  user: User,
  userId: string,
  status: UserStatus,
) {
  requireRole(user, ["LOCAL_ADMIN"]);
  await api(`/api/users/${userId}/status`, { method: "PATCH", body: JSON.stringify({ status: status.toLowerCase() }) });
  return;
  /*
  mutate((db) => {
    const target = db.users.find((u) => u.id === userId);
    if (
      !target ||
      target.tenantId !== user.tenantId ||
      target.role !== "ACCOUNTANT"
    )
      throw new Error("Account is not manageable in this workspace.");
    const tenant = db.tenants.find((t) => t.id === user.tenantId)!;
    if (
      ["ACTIVE", "INVITED"].includes(status) &&
      !["ACTIVE", "INVITED"].includes(target.status) &&
      usage(tenant.id) >= tenant.accountLimit
    )
      throw new Error("Account limit reached.");
    target.status = status;
    db.invitations
      .filter((i) => i.userId === userId)
      .forEach((i) => (i.status = status));
    log(db, user, "USER_STATUS_CHANGED", target.email, status);
  }); */
}
export const disableUser = (user: User, id: string) =>
  changeUserStatus(user, id, "DISABLED");
export async function resendInvitation(user: User, userId: string) {
  await api(`/api/users/${userId}/resend-invitation`, { method: "POST" });
  return;
  /*
  await changeUserStatus(user, userId, "INVITED");
  mutate((db) => {
    const target = db.users.find((u) => u.id === userId)!;
    target.invitedAt = now();
    log(db, user, "INVITATION_RESENT", target.email);
  }); */
}
