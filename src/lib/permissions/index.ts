import type { Database, Document, Role, User } from "@/types";
// Frontend filtering shapes the UI. Real tenant isolation must be enforced by the backend API.
export function requireRole(user: User, roles: Role[]) {
  const currentRole = String(user.role).toUpperCase();
  const allowedRoles = roles.map((role) => String(role).toUpperCase());
  if (!allowedRoles.includes(currentRole as Role))
    throw new Error("This action is not available for your role.");
}
export function canTenant(user: User, tenantId: string) {
  return user.role === "SUPER_ADMIN" || user.tenantId === tenantId;
}
export function canDocument(user: User, doc: Document) {
  return (
    canTenant(user, doc.tenantId) &&
    (user.role !== "ACCOUNTANT" ||
      doc.ownerId === user.id ||
      doc.assignedTo === user.id)
  );
}
export function assertTenant(user: User, tenantId: string) {
  if (!canTenant(user, tenantId))
    throw new Error("Company not available in this workspace.");
}
export function assertActive(db: Database, user: User) {
  const current = db.users.find((u) => u.id === user.id);
  if (!current || current.status !== "ACTIVE")
    throw new Error(
      "This account is inactive. Please sign in with an active account.",
    );
  if (
    current.tenantId &&
    db.tenants.find((t) => t.id === current.tenantId)?.status !== "ACTIVE"
  )
    throw new Error("This company is suspended.");
}
export const roleHome = (role: Role) =>
  role === "SUPER_ADMIN"
    ? "/admin/dashboard"
    : role === "LOCAL_ADMIN"
      ? "/company/dashboard"
      : "/workspace/dashboard";
