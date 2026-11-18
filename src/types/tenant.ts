/** Tenant.id is the canonical UUID. Display codes never grant access. */
export type TenantStatus = "ACTIVE" | "SUSPENDED";
export type RequestStatus = "PENDING" | "APPROVED" | "REJECTED";
export interface Tenant { id: string; name: string; registration: string; adminEmail: string; accountLimit: number; status: TenantStatus; createdAt: string; }
export interface CompanyRequest { id: string; company: string; registration: string; contact: string; email: string; phone: string; accounts: number; employees: string[]; notes: string; submittedAt: string; status: RequestStatus; rejectionReason?: string; }
export interface AccountLimitRequest { id: string; tenantId: string; current: number; requested: number; reason: string; status: RequestStatus; }
