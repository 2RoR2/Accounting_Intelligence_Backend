import { validateBankMatchingInputs } from "./bank-contract";
import { api, get } from "./client";

export type InboxDocument = {
  id: string;
  code: string;
  name: string;
  status: string;
  assignedTo: string | null;
  priority: string;
  dueAt: string;
};

export type DocumentBatch = {
  id: string;
  name: string;
  status: string;
  totalDocuments: number;
  processedDocuments: number;
  failedDocuments?: number;
  documents?: Array<{ id: string; code: string; name: string; status: string }>;
};

export type DocumentComment = {
  id: string;
  comment: string;
  author: string;
  pageNumber?: number | null;
  fieldKey?: string | null;
  resolved?: boolean;
  createdAt: string;
};

export type DocumentReconciliation = {
  id: string;
  sourceDocumentId: string;
  sourceCode: string;
  relatedDocumentId: string;
  relatedCode: string;
  relationType: "credit_for" | "debit_for" | "adjustment_of" | "related_to";
  amount?: number | null;
  note: string;
  createdAt: string;
};

export const getDocumentReconciliations = (documentId: string) =>
  get<DocumentReconciliation[]>(`/api/documents/${documentId}/reconciliations`);

export const reconcileDocument = (documentId: string, relatedDocumentId: string, relationType: DocumentReconciliation["relationType"], amount?: number, note = "") =>
  api(`/api/documents/${documentId}/reconciliations`, {
    method: "POST",
    body: JSON.stringify({ relatedDocumentId, relationType, amount: amount === undefined ? undefined : amount.toFixed(2), note }),
  });

export type { BankAccountingRecord, BankTransaction } from "./bank-contract";

export type BankReconciliationResult = {
  results: Array<{
    accounting_record_id: string;
    status: "MATCHED" | "UNMATCHED" | "REVIEW_REQUIRED";
    matched_bank_transaction_id: string | null;
    candidates: string[];
    reason: string | null;
  }>;
  used_bank_transaction_ids: string[];
};

export async function runBankReconciliation(accountingRecords: unknown, bankTransactions: unknown) {
  const body = validateBankMatchingInputs(accountingRecords, bankTransactions);
  return api<BankReconciliationResult>("/api/bank-reconciliation", { method: "POST", body: JSON.stringify(body) });
}

export const getDocumentInbox = (status?: string, assignedTo?: string) => {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (assignedTo) params.set("assignedTo", assignedTo);
  const query = params.toString();
  return get<InboxDocument[]>(`/api/accountant/inbox${query ? `?${query}` : ""}`);
};

export const createDocumentBatch = (name: string, documentIds: string[]) =>
  api<DocumentBatch>("/api/accountant/batches", {
    method: "POST",
    body: JSON.stringify({ name, documentIds }),
  });

export const getDocumentBatch = (batchId: string) =>
  get<DocumentBatch>(`/api/accountant/batches/${batchId}`);

export interface DocumentReviews {
  submissions: Array<{ id: string; submittedBy: string; decision: string; note: string; createdAt: string }>;
  decisions: Array<{ id: string; reviewedBy: string; decision: string; note: string; approvedRevision: number | null; createdAt: string }>;
}
export const getDocumentReviews = (documentId: string) => get<DocumentReviews>(`/api/documents/${documentId}/reviews`);
function requireRevision(revision: number) {
  if (!Number.isInteger(revision) || revision < 1) throw new Error("Refresh the document to obtain its current revision.");
}
export async function submitDocumentReview(documentId: string, decision: "submitted", note: string, expectedContentRevision: number) {
  requireRevision(expectedContentRevision);
  const result = await api<{ documentId: string; decision: string; status: string }>(`/api/documents/${documentId}/review`, {
    method: "POST", body: JSON.stringify({ decision, note, expectedContentRevision }),
  });
  if (!result || result.documentId !== documentId || result.decision !== decision || result.status !== "IN_REVIEW") throw new Error("The server did not confirm review submission. Refresh before trying again.");
  return result;
}
export async function decideDocumentReview(documentId: string, decision: "approved" | "rejected" | "returned", note: string, expectedContentRevision: number) {
  requireRevision(expectedContentRevision);
  const result = await api<{ documentId: string; decision: string; reviewedBy: string; status: string }>(`/api/documents/${documentId}/review/decision`, {
    method: "POST", body: JSON.stringify({ decision, note, expectedContentRevision }),
  });
  const status = { approved: "VALIDATED", rejected: "EXCEPTION", returned: "IN_REVIEW" }[decision];
  if (!result || result.documentId !== documentId || result.decision !== decision || !result.reviewedBy || result.status !== status) throw new Error("The server did not confirm the review decision. Refresh before trying again.");
  return result;
}
export type LineFieldKey = "description" | "quantity" | "unit_price" | "amount";
export interface LineCorrectionResponse { lineItemId: string; fieldKey: LineFieldKey; value: string; correctedBy: string; contentRevision: number; }
export async function correctLineItem(documentId: string, lineItemId: string, fieldKey: LineFieldKey, value: string, expectedContentRevision: number) {
  requireRevision(expectedContentRevision);
  const result = await api<LineCorrectionResponse>(`/api/documents/${documentId}/line-items/${lineItemId}/${fieldKey}`, {
    method: "PATCH", body: JSON.stringify({ value, expectedContentRevision }),
  });
  if (!result || result.lineItemId !== lineItemId || result.fieldKey !== fieldKey || typeof result.value !== "string" || !result.correctedBy || !Number.isInteger(result.contentRevision) || result.contentRevision <= expectedContentRevision) {
    throw new Error("The server did not confirm the line correction. Refresh authoritative values before trying again.");
  }
  return result;
}

export const requestRecordAmendment = (recordId: string, reason: string) =>
  api<{ id: string; recordId: string; status: string; createdAt: string }>(`/api/records/${recordId}/amendments`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });

export const getDocumentComments = (documentId: string) =>
  get<DocumentComment[]>(`/api/documents/${documentId}/comments`);

export const addDocumentComment = (documentId: string, comment: string, pageNumber?: number, fieldKey?: string) =>
  api<{ id: string; comment: string; createdAt: string }>(`/api/documents/${documentId}/comments`, {
    method: "POST",
    body: JSON.stringify({ comment, pageNumber, fieldKey }),
  });

export const getSuppliers = () => get<Array<{ id: string; name: string; registrationNumber?: string; address?: string }>>("/api/suppliers");
export const createSupplier = (name: string) => api<{ id: string; name: string }>("/api/suppliers", { method: "POST", body: JSON.stringify({ name }) });
export const getDocumentTags = () => get<Array<{ id: string; name: string; color: string }>>("/api/document-tags");
export const createDocumentTag = (name: string, color = "#64748b") => api<{ id: string; name: string; color: string }>("/api/document-tags", { method: "POST", body: JSON.stringify({ name, color }) });
export const deleteDocumentTag = (tagId: string) => api<{ deleted: boolean; id: string }>(`/api/document-tags/${tagId}`, { method: "DELETE" });
export const tagDocument = (documentId: string, tagId: string) => api(`/api/documents/${documentId}/tags/${tagId}`, { method: "POST" });
export const getSavedFilters = () => get<Array<{ id: string; name: string; resource: string; definition: Record<string, unknown>; isShared: boolean }>>("/api/saved-filters");
export const createSavedFilter = (name: string, resource: "documents" | "exceptions" | "records", definition: Record<string, unknown>, isShared = false) => api("/api/saved-filters", { method: "POST", body: JSON.stringify({ name, resource, definition, isShared }) });
export const deleteSavedFilter = (filterId: string) => api<{ deleted: boolean; id: string }>(`/api/saved-filters/${filterId}`, { method: "DELETE" });
export const createReviewReminder = (documentId: string, assignedTo: string, remindAt: string) => api("/api/review-reminders", { method: "POST", body: JSON.stringify({ documentId, assignedTo, remindAt }) });
export const getNotifications = () => get<Array<{ id: string; type: string; title: string; message: string; resource?: string | null; read: boolean; createdAt: string }>>("/api/notifications");
export const markNotificationRead = (notificationId: string) => api<{ id: string; read: boolean; readAt: string }>(`/api/notifications/${notificationId}/read`, { method: "PATCH" });
export const getAuthSessions = () => get<Array<{ id: string; expiresAt: string; current: boolean }>>("/api/auth/sessions");
export const requestBackup = (backupType: "database" | "documents" | "full") => api("/api/backups", { method: "POST", body: JSON.stringify({ backupType }) });
export const getDocumentStatusHistory = (documentId: string) => get<Array<{ id: string; oldStatus: string | null; newStatus: string; reason: string; createdAt: string }>>(`/api/documents/${documentId}/status-history`);
export const createExport = (resource: "records" | "documents" | "exceptions", filters: Record<string, unknown> = {}, format: "csv" | "json" = "csv") => api("/api/exports", { method: "POST", body: JSON.stringify({ resource, filters, format }) });
export const getExports = () => get<Array<{ id: string; resource: string; format: string; status: string; rowCount: number; createdAt: string }>>("/api/exports");
export const downloadExport = (exportId: string) => `/api/exports/${exportId}/download`;
export const createDocumentVersion = (documentId: string, input: { storagePath: string; fileHashSha256: string; fileSizeBytes: number }) => api(`/api/documents/${documentId}/versions`, { method: "POST", body: JSON.stringify(input) });
export const getDocumentVersions = (documentId: string) => get<Array<{ id: string; versionNumber: number; storagePath: string; fileHashSha256: string; createdAt: string }>>(`/api/documents/${documentId}/versions`);
export const cancelAccountantBatch = (batchId: string) => api<{ id: string; status: string }>(`/api/accountant/batches/${batchId}/cancel`, { method: "POST" });
export const getWorkload = () => get<Array<{ id: string; documentId: string; documentCode: string; filename: string; assignedTo: string; assigneeName: string; priority: string; dueAt: string }>>("/api/workload");
export const downloadDocumentVersion = (documentId: string, versionId: string) => `/api/documents/${documentId}/versions/${versionId}/file`;
export const getPermissions = () => get<Array<{ key: string; description: string }>>("/api/permissions");
export const setUserPermission = (userId: string, permissionKey: string, granted: boolean) => api(`/api/users/${userId}/permissions`, { method: "PATCH", body: JSON.stringify({ permissionKey, granted }) });
export const getPlatformNotifications = () => get<Array<{ id: string; type: string; severity: string; title: string; message: string; createdAt: string }>>("/api/platform/notifications");
export const createPlatformNotification = (input: { notificationType: string; severity: "info" | "warning" | "critical"; title: string; message: string }) => api("/api/platform/notifications", { method: "POST", body: JSON.stringify(input) });
export const decideRecordAmendment = (amendmentId: string, decision: "approved" | "rejected") => api(`/api/record-amendments/${amendmentId}/decision`, { method: "POST", body: JSON.stringify({ decision }) });
export const getDocumentKindWorkflow = (documentId: string) => get<{ kind: string; fields: Record<string, string | null>; overdue: boolean; duplicates: Array<{ id: string; code: string; name: string; kind: string }>; relatedNotes: Array<{ id: string; relationType: string; amount?: number; code: string; kind: string }>}>(`/api/documents/${documentId}/kind-workflow`);
export const saveDocumentDraft = (documentId: string) => api(`/api/documents/${documentId}/draft`, { method: "POST" });
export const getCustomers = () => get<Array<{ id: string; name: string; registrationNumber?: string; address?: string }>>("/api/customers");
export const createCustomer = (name: string, registrationNumber?: string, address?: string) => api("/api/customers", { method: "POST", body: JSON.stringify({ name, registrationNumber, address }) });
export const downloadApprovedRecordsXlsx = () => `${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/api/exports/xlsx`;
export const bulkDocumentAction = (documentIds: string[], action: "assign" | "tag" | "export", userId?: string, tagId?: string) => api("/api/documents/bulk-action", { method: "POST", body: JSON.stringify({ documentIds, action, userId, tagId }) });
export const getCustomFields = () => get<Array<{ id: string; fieldKey: string; label: string; fieldType: string; options: string[]; required: boolean }>>("/api/custom-fields");
export const createCustomField = (input: { fieldKey: string; label: string; fieldType: string; options?: string[]; required?: boolean }) => api("/api/custom-fields", { method: "POST", body: JSON.stringify(input) });
