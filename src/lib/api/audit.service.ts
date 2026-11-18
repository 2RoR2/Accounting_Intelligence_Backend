import type { AuditLog, User } from "@/types";
import { get } from "./client";
/** resources: only entries for these (a document's id, name or record id), so its history is complete. */
export async function getAuditLogs(user: User, resources?: string[]) {
  void user;
  const wanted = (resources ?? []).filter(Boolean);
  return get<AuditLog[]>(wanted.length ? `/api/audit?${wanted.map((value) => `resource=${encodeURIComponent(value)}`).join("&")}` : "/api/audit");
}
