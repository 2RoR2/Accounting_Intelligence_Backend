import type { User } from "@/types";
import { ApiError, get, post } from "./client";

export type BackendUser = {
  id: string;
  name: string;
  email: string;
  role: string;
  tenantId: string | null;
  tenantCode?: string | null;
  tenantName?: string;
  status: string;
  mustChangePassword: boolean;
  lastLogin?: string;
};

export const AUTH_IDENTITY_CONTRACT = "canonical-tenant-uuid-v1";

const mapUser = (user: BackendUser): User => ({
  id: user.id,
  name: user.name,
  email: user.email,
  role: user.role as User["role"],
  tenantId: user.tenantId,
  tenantCode: user.tenantCode ?? null,
  status: user.status as User["status"],
  mustChangePassword: user.mustChangePassword,
  invitedBy: "",
  invitedAt: "",
  lastLogin: user.lastLogin ?? "",
  tenantName: user.tenantName ?? "",
});

export async function getAccounts() {
  return [] as User[];
}
export async function login(email: string, password: string) {
  if (!password) throw new Error("Enter a password to continue.");
  const response = await post<BackendUser | { mfaRequired: boolean; mfaMethod?: string; enrollmentRequired?: boolean; totpSecret?: string; otpauthUrl?: string }>("/api/auth/login", { email, password });
  if (!("id" in response)) return { mfaRequired: true, mfaMethod: response.mfaMethod ?? "authenticator", enrollmentRequired: response.enrollmentRequired ?? false, totpSecret: response.totpSecret ?? "", otpauthUrl: response.otpauthUrl ?? "" };
  return { mfaRequired: false, user: mapUser(response) };
}
export async function verifyMfa(code: string) {
  if (!/^\d{6}$/.test(code)) throw new Error("Enter the six-digit verification code.");
  return mapUser(await post<BackendUser>("/api/auth/verify-mfa", { code }));
}
export async function recoverAuthenticator(email: string, password: string) {
  const response = await post<{ mfaRequired: boolean; enrollmentRequired: boolean; totpSecret: string; otpauthUrl: string }>("/api/auth/recover-authenticator", { email, password });
  return response;
}
export async function generateBackupCodes() {
  return post<{ codes: string[]; generatedAt: string }>("/api/auth/backup-codes");
}
export async function resendMfa() {
  try {
    return await post<{ message: string; expiresIn: number }>("/api/auth/resend-mfa");
  } catch (error) {
    // The MFA challenge is short-lived and the first request can fail while
    // the frontend proxy is waking up. Retry once without creating a second
    // code when the first request already reached the API.
    if (error instanceof Error && error.message.startsWith("Unable to reach the backend API")) {
      await new Promise((resolve) => globalThis.setTimeout(resolve, 400));
      return post<{ message: string; expiresIn: number }>("/api/auth/resend-mfa");
    }
    throw error;
  }
}

export type MfaRecoveryRequest = {
  id: string;
  requester_id: string;
  target_user_id: string;
  target_email: string;
  target_name: string;
  reason: string;
  status: string;
  created_at: string;
};

export const getMfaRecoveryRequests = () =>
  get<MfaRecoveryRequest[]>("/api/admin/mfa-recovery-requests");

export const approveMfaRecovery = (requestId: string) =>
  post<{ success: boolean }>(`/api/admin/mfa-recovery-requests/${encodeURIComponent(requestId)}/approve`);

export const requestLostAuthenticatorRecovery = (reason: string) =>
  post<{ id: string; status: string; createdAt: string }>(
    "/api/auth/mfa-recovery-requests/lost-authenticator",
    { reason },
  );
export async function getCurrentUser() {
  try {
    const response = await get<BackendUser>("/api/auth/me");
    return mapUser(response);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}
export async function logout() {
  await post("/api/auth/logout");
}
export const passwordChecks = (password: string) => [
  password.length >= 8,
  /[A-Z]/.test(password),
  /[a-z]/.test(password),
  /[0-9]/.test(password),
  /[^A-Za-z0-9]/.test(password),
];
export async function activateAccount(
  token: string,
  password: string,
  confirmation: string,
) {
  if (!passwordChecks(password).every(Boolean) || password !== confirmation)
    throw new Error(
      "Meet all password requirements and confirm the same password.",
    );
  return post("/api/auth/activate", { token, password, confirmation }); /*
  return mutate((db) => {
    const user = db.users.find((u) => u.id === userId);
    if (!user || !["INVITED", "INVITATION_EXPIRED"].includes(user.status))
      throw new Error("Select a pending invitation.");
    const tenant = db.tenants.find((t) => t.id === user.tenantId);
    if (!tenant || tenant.status !== "ACTIVE")
      throw new Error("Company is not active.");
    if (
      user.status === "INVITATION_EXPIRED" &&
      db.users.filter(
        (u) =>
          u.tenantId === tenant.id && ["ACTIVE", "INVITED"].includes(u.status),
      ).length >= tenant.accountLimit
    )
      throw new Error("No account seats available.");
    user.status = "ACTIVE";
    if (user.role === "ACCOUNTANT") user.mustChangePassword = true;
    db.invitations
      .filter((i) => i.userId === userId)
      .forEach((i) => (i.status = "ACTIVE"));
    return user;
  }); */
}
export async function changeOwnPassword(
  user: User,
  password: string,
  confirmation: string,
) {
  if (!passwordChecks(password).every(Boolean) || password !== confirmation)
    throw new Error("Meet all password requirements and match both passwords.");
  await post("/api/auth/change-password", { password, confirmation });
  return { ...user, mustChangePassword: false };
}
export async function requestPasswordReset(email: string) {
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))
    throw new Error("Enter a valid company email.");
  return post<{ message: string }>("/api/auth/forgot-password", { email });
}
export async function verifyResetCode(email: string, code: string) {
  if (!/^\d{6}$/.test(code)) throw new Error("Enter the six-digit verification code.");
  return post<{ success: boolean }>("/api/auth/verify-reset-code", { email, code });
}
export async function resetPassword(
  password: string,
  confirmation: string,
) {
  if (!passwordChecks(password).every(Boolean) || password !== confirmation)
    throw new Error("Meet all password requirements and match both passwords.");
  return post<{ success: boolean }>("/api/auth/reset-password", { password, confirmation });
}
