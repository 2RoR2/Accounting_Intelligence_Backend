export type Role = "SUPER_ADMIN" | "LOCAL_ADMIN" | "ACCOUNTANT";
export type UserStatus = "INVITED" | "ACTIVE" | "DISABLED" | "INVITATION_EXPIRED";
export interface User { id: string; name: string; email: string; role: Role; tenantId: string | null; tenantCode?: string | null; tenantName?: string; status: UserStatus; invitedBy: string; invitedAt: string; lastLogin: string; avatar?: string; mustChangePassword?: boolean; }
export interface Invitation { id: string; userId: string; tenantId: string; invitedBy: string; createdAt: string; status: UserStatus; }
