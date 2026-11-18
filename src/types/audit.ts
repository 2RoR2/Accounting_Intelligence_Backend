import type { Role } from "./auth";
export interface AuditLog { id: string; tenantId: string | null; user: string; role: Role | "SYSTEM"; action: string; resource: string; details: string; timestamp: string; }
