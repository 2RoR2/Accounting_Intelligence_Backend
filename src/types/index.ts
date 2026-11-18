export * from "./audit";
export * from "./auth";
export * from "./dashboard";
export * from "./document";
export * from "./exception";
export * from "./extraction";
export * from "./record";
export * from "./tenant";
export * from "./validation";
export * from "./money";

import type { AuditLog } from "./audit";
import type { Invitation, User } from "./auth";
import type { Document } from "./document";
import type { Exception } from "./exception";
import type { Extraction } from "./extraction";
import type { StandardisedRecord } from "./record";
import type { AccountLimitRequest, CompanyRequest, Tenant } from "./tenant";
import type { ValidationRules } from "./validation";

/** Shape persisted by the local application repository. */
export interface Database {
  version: number;
  users: User[];
  tenants: Tenant[];
  requests: CompanyRequest[];
  limitRequests: AccountLimitRequest[];
  invitations: Invitation[];
  documents: Document[];
  extractions: Extraction[];
  exceptions: Exception[];
  records: StandardisedRecord[];
  audit: AuditLog[];
  rules: Record<string, ValidationRules>;
  settings: Record<string, string>;
}
