import type { Document, User } from "@/types";
import { requireRole } from "../permissions";
import { mapRecord, mapSupportView, type SupportViewResponse, type RecordResponse } from "./document-contract";
import { api, get } from "./client";
export async function standardiseDocument(user: User, documentId: string) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  const record = await api<{ id: string }>(`/api/records/from-document/${documentId}`, { method: "POST" });
  if (!record || typeof record.id !== "string" || !record.id.trim()) {
    throw new Error("The server did not confirm a standardised record. Refresh records before trying again.");
  }
  return record.id;
}
export async function getStandardisedRecords(user: User) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  return (await get<RecordResponse[]>("/api/records")).map(mapRecord);
}
export async function getOutstandingRecordIds(user: User, category: "accounts_receivable" | "accounts_payable", overdue = false) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  const rows = await get<Array<{ id: string }>>(`/api/ledger/outstanding-records?category=${category}&overdue=${overdue}`);
  return rows.map((row) => row.id);
}
export async function getRecordMetadata(user: User, tenantId?: string) {
  requireRole(user, ["SUPER_ADMIN"]);
  return get<Array<{ id: string; documentId: string; tenantId: string; createdAt: string; category: string; validation: string; status: string; document?: Document }>>(`/api/records/metadata${tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ""}`);
}
export async function getStandardisedRecord(user: User, recordId: string) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  const record = await get<RecordResponse>(`/api/records/${encodeURIComponent(recordId)}`);
  return record && !Array.isArray(record) && record.id ? mapRecord(record) : undefined;
}
export async function openSupportView(
  user: User,
  recordId: string,
  reason: string,
) {
  requireRole(user, ["SUPER_ADMIN"]);
  if (reason.trim().length < 5)
    throw new Error("Provide a support reason of at least 5 characters.");
  return mapSupportView(await api<SupportViewResponse>(`/api/records/${recordId}/support-view`, { method: "POST", body: JSON.stringify({ reason }) }));
}
export function downloadCsv(filename: string, rows: string[][]) {
  const csv = rows
    .map((row) =>
      row
        .map(
          (value) =>
            `"${(/^[=+@\-\t\r]/.test(value) ? "'" : "") + value.replaceAll('"', '""')}"`,
        )
        .join(","),
    )
    .join("\r\n");
  const url = URL.createObjectURL(
    new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
