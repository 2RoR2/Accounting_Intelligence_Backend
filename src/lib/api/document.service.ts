import type {
  Document,
  AiAssistResult,
  AiAssistState,
  Extraction,
  RecordCategory,
  User,
  ValidationResult,
  ValidationRules,
  Exception,
} from "@/types";
import type { DocumentKind } from "@/types";
import { requireRole } from "../permissions";
import { api, get } from "./client";
import { mapDocument, mapExtraction, mapValidationReport, type ExtractionResponse, type ValidationReport, type CorrectionResponse, type RevalidationResponse } from "./document-contract";
import { correctLineItem, type LineFieldKey } from "./workflow.service";
export type DocumentSearch = {
  q?: string;
  status?: string;
  date?: string;
  amount?: string;
  business?: string;
};
export type DemoCatalogDocument = Document & { demoCode: string; sampleId: string; benchmarkRevision: string; sourceSha256: string; goldManifestSha256: string };
export type DemoAccuracyPreview = { datasetId: string; status: "NOT_RUN" | string; documentCount: number; accuracy: number | null; recall: number | null; precision: number | null; coverage: number | null; breakdown: unknown[] };
export async function getDemoCatalog(user: User, datasetId: string) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  return get<{ datasetId: string; documents: DemoCatalogDocument[] }>(`/api/demo-datasets/${encodeURIComponent(datasetId)}/catalog`);
}
export async function getDemoAccuracyPreview(user: User, datasetId: string) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  return get<DemoAccuracyPreview>(`/api/demo-datasets/${encodeURIComponent(datasetId)}/accuracy-preview`);
}
export async function getDocuments(user: User, filters: DocumentSearch = {}) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value?.trim()) params.set(key, value.trim());
  });
  const query = params.toString();
  return (await get<Array<Document & { kind?: string }>>(`/api/documents${query ? `?${query}` : ""}`)).map(mapDocument);
}
export async function getDocument(user: User, documentId: string) {
  return mapDocument(await get<Document & { kind?: string }>(`/api/documents/${documentId}`));
}
export async function getExtraction(user: User, documentId: string) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  return mapExtraction(await get<ExtractionResponse>("/api/documents/" + documentId + "/extraction"));
}
export function validateExtraction(
  extraction: Extraction,
  rules: ValidationRules,
  otherExtractions: Extraction[],
): ValidationResult[] {
  const f = extraction.fields;
  const results: ValidationResult[] = [];
  const add = (
    enabled: boolean,
    rule: string,
    pass: boolean,
    message: string,
  ) => {
    if (enabled)
      results.push({
        rule,
        result: pass ? "PASS" : "FAIL",
        message: pass ? "Check passed." : message,
      });
  };
  add(
    rules.supplier,
    "Supplier Required",
    !!f.supplier?.value?.trim(),
    "Supplier name is required.",
  );
  add(
    rules.invoice,
    "Invoice Number Required",
    !!f.invoice?.value?.trim(),
    "Invoice number is required.",
  );
  add(
    rules.date,
    "Invoice Date",
    /^\d{4}-\d{2}-\d{2}$/.test(f.date?.value ?? "") &&
      !Number.isNaN(Date.parse(f.date?.value ?? "")),
    "Enter a valid invoice date.",
  );
  const values = ["subtotal", "tax", "discount", "total"].map((k) =>
    f[k]?.value?.trim() === "" ? NaN : Number(f[k]?.value),
  );
  add(
    rules.total,
    "Total Calculation",
    values.every((v) => Number.isFinite(v) && v >= 0) &&
      Math.abs(values[0] + values[1] - values[2] - values[3]) < 0.01,
    "Subtotal + tax − discount does not equal total.",
  );
  add(
    rules.duplicate,
    "Duplicate Invoice",
    !otherExtractions.some(
      (e) =>
        e.documentId !== extraction.documentId &&
        e.fields.invoice?.value === f.invoice?.value &&
        e.fields.supplier?.value === f.supplier?.value,
    ),
    "An invoice with this number and supplier already exists.",
  );
  const low = Object.values(f).filter(
    (field) => (field.confidence ?? 0) < rules.threshold && !field.correctedBy,
  );
  results.push({
    rule: "Confidence Check",
    result: low.length ? "WARNING" : "PASS",
    message: low.length
      ? `${low.map((f) => f.label).join(", ")} below ${rules.threshold}%. Review recommended.`
      : "All fields meet the confidence threshold or have been reviewed.",
  });
  return results;
}
export async function getValidationResults(user: User, documentId: string) {
  return (await getValidationReport(user, documentId)).results;
}
export async function getValidationReport(user: User, documentId: string) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  return mapValidationReport(await get<ValidationReport>("/api/documents/" + documentId + "/validation"));
}
export async function getAiAssist(user: User, documentId: string) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  return get<AiAssistState>("/api/documents/" + documentId + "/ai-assist");
}
export async function requestAiAssist(user: User, documentId: string, fields?: string[]) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  return api<AiAssistResult>("/api/documents/" + documentId + "/ai-assist", { method: "POST", body: JSON.stringify({ fields: fields ?? null }) });
}
export async function decideAiAssist(user: User, documentId: string, decision: { field: string; decision: "accepted" | "rejected"; attemptId?: string }) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  return api<{ recorded: boolean }>("/api/documents/" + documentId + "/ai-assist/decision", { method: "POST", body: JSON.stringify(decision) });
}
export async function updateExtractedField(user: User, documentId: string, key: string, value: string, expectedContentRevision: number) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  if (!Number.isInteger(expectedContentRevision) || expectedContentRevision < 1) throw new Error("Refresh the document to obtain its current revision.");
  return api<CorrectionResponse>("/api/documents/" + documentId + "/extraction/" + encodeURIComponent(key), {
    method: "PATCH", body: JSON.stringify({ value, expectedContentRevision }),
  });
}
export async function updateExtractedLineItem(user: User, documentId: string, lineId: string, key: string, value: string, expectedContentRevision: number) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  if (!Number.isInteger(expectedContentRevision) || expectedContentRevision < 1) throw new Error("Refresh the document to obtain its current revision.");
  if (!["description", "quantity", "unit_price", "amount"].includes(key)) throw new Error("Unsupported line-item field.");
  return correctLineItem(documentId, lineId, key as LineFieldKey, value, expectedContentRevision);
}
export type MissingLineInput = { description: string; quantity: string | null; unitPrice: string | null; amount: string; sourcePage: number; sourceNote: string; expectedContentRevision: number };
export async function addExtractedLineItem(documentId: string, input: MissingLineInput) {
  return api<{ id: string; contentRevision: number }>(`/api/documents/${documentId}/line-items`, { method: "POST", body: JSON.stringify(input) });
}
export async function revalidateDocument(user: User, documentId: string, note = "") {
  void note;
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  return api<RevalidationResponse>("/api/documents/" + documentId + "/revalidate", { method: "POST" });
}

/** Settles one reviewable finding with a reason. Recorded on the audit log for this revision; approval is still separate. */
export async function acceptValidationReview(user: User, documentId: string, rule: string, note: string, expectedContentRevision: number) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  return api<RevalidationResponse>("/api/documents/" + documentId + "/validation-review", { method: "POST", body: JSON.stringify({ rule, note, expectedContentRevision }) });
}

export async function retryDocumentProcessing(user: User, documentId: string) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  const result = await api<{ id: string; jobId: string; queueJobId?: string | null; jobStatus: string; status: string; dispatchAccepted: boolean }>(`/api/documents/${documentId}/retry`, { method: "POST" });
  const scheduled = result?.jobStatus === "PENDING" && result.status === "QUEUED" && result.dispatchAccepted === false && result.queueJobId == null;
  if (!result || result.id !== documentId || !result.jobId || !result.status || !result.jobStatus || (!scheduled && result.dispatchAccepted !== true) || (result.queueJobId != null && typeof result.queueJobId !== "string")) throw new Error("Retry was not confirmed by a supported protocol. Refresh the document before trying again.");
  return result;
}
export async function getExceptions(user: User, documentId?: string) {
  void user;
  return get<Exception[]>(documentId ? `/api/exceptions?documentId=${encodeURIComponent(documentId)}` : "/api/exceptions");
}
export async function resolveException(
  user: User,
  documentId: string,
  note: string,
) {
  if (!note.trim())
    throw new Error("Add a correction note before revalidating.");
  const exception = (await getExceptions(user)).find((item) => item.documentId === documentId);
  if (!exception) throw new Error("Exception was not found.");
  await api(`/api/exceptions/${exception.id}/resolve`, {
    method: "PATCH",
    body: JSON.stringify({ note }),
  });
  return revalidateDocument(user, documentId, note);
}
export async function assignException(
  user: User,
  exceptionId: string,
  userId: string,
) {
  requireRole(user, ["LOCAL_ADMIN"]);
  await api(`/api/exceptions/${exceptionId}/assign`, {
    method: "PATCH",
    body: JSON.stringify({ userId }),
  });
}
export async function uploadDocument(
  user: User,
  file: File,
  scenario: string,
  category: RecordCategory = "ACCOUNTS_PAYABLE",
  kind: DocumentKind = "INVOICE",
) {
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  if (!/\.(pdf|png|jpe?g)$/i.test(file.name))
    throw new Error(
      "Upload a PDF, PNG or JPEG invoice, bill, receipt, credit note or debit note; unknown content requires review.",
    );
  if (file.size === 0) throw new Error("This file is empty.");
  if (file.size > 20 * 1024 * 1024)
    throw new Error("Maximum file size is 20 MB.");
  const body = new FormData();
  body.append("file", file, file.name);
  void scenario;
  const params = new URLSearchParams({kind: kind.toLowerCase(), category: category.toLowerCase()});
  return mapDocument(await api<Document>("/api/documents?" + params, { method: "POST", body }));
}
export async function advanceProcessing(
  user: User,
  documentId: string,
  status: "QUEUED" | "PROCESSING" | "EXTRACTED" | "FAILED",
) {
  if (status === "EXTRACTED") return revalidateDocument(user, documentId);
  return retryDocumentProcessing(user, documentId); /*
  mutate((db) => {
    const doc = documentFor(db, user, documentId, true);
    doc.status = status;
    log(db, user, `DOCUMENT_${status}`, doc.name, "Simulated processing.");
  }); */
  if (status === "EXTRACTED") await revalidateDocument(user, documentId);
}
export async function getValidationRules(user: User) {
  // Accountants need read access to the active policy while reviewing a
  // document. Updates remain restricted to local admins below.
  requireRole(user, ["LOCAL_ADMIN", "ACCOUNTANT"]);
  return get<ValidationRules>("/api/validation-rules");
}
export async function updateValidationRules(
  user: User,
  rules: ValidationRules,
) {
  requireRole(user, ["LOCAL_ADMIN"]);
  if (rules.threshold < 0 || rules.threshold > 100)
    throw new Error("Threshold must be between 0 and 100.");
  await api("/api/validation-rules", { method: "PUT", body: JSON.stringify(rules) });
}
