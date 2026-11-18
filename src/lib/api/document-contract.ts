import type { CurrencyProposal, Document, StandardisedRecord, AiAssistOutcome, AiAssistResult, AiAssistResultField, ExtractedField, Extraction, FieldCandidate, LineItem, ValidationResult, TransactionAnomaly } from "@/types";
export interface ExtractionResponse {
  currencyReview?: Extraction["currencyReview"];
  lineCorrections?: Extraction["lineCorrections"];
  warnings?: Extraction["warnings"]; documentId: string; contentRevision?: number; raw?: Record<string, unknown>; classification?: Extraction["classification"];
  fields?: Array<Partial<ExtractedField> & { key: string }>;
  lineItems?: Array<Partial<LineItem> & { unit_price?: string | null }>;
}
export interface ValidationReport {
  documentId: string; contentRevision: number; status: string; validationStatus: string;
  reviewRequired: boolean; accountingApprovalPerformed: boolean;
  coverageComplete?: boolean; validationPassed?: boolean; notApplicable?: number; passed: number; warnings: number; failed: number; blocking: boolean; results: ValidationResult[];
  transactionAnomaly?: TransactionAnomaly | null;
}
/** POST revalidate returns errors/checks; GET validation supplies the full report. */
export interface RevalidationResponse {
  documentId: string; contentRevision: number; status: string; validationStatus: string;
  reviewRequired: boolean; accountingApprovalPerformed: boolean; coverageComplete: boolean;
  errors: Array<{ code: string; field: string; message: string; severity?: string }>;
  checks: ValidationResult[];
}
export interface CorrectionResponse { key: string; value: string; correctedBy: string; contentRevision: number; }
export function mapDocument(value: Document & { kind?: string }): Document {
  return { ...value, type: value.kind ?? value.type };
}
export function mapExtraction(value: ExtractionResponse): Extraction {
  return {
    currencyReview: value.currencyReview ?? null,
    lineCorrections: value.lineCorrections ?? [], warnings: value.warnings ?? [], documentId: value.documentId, contentRevision: value.contentRevision, raw: value.raw,
    classification: value.classification ?? value.raw?.document_type as Extraction["classification"],
    fields: Object.fromEntries((value.fields ?? []).map(field => [field.key, {
      ...field, label: field.label ?? field.key, value: field.value ?? null,
      original: field.normalizedValue ?? field.original ?? null,
      confidence: field.confidence ?? null, evidence: field.evidence ?? [], candidates: field.candidates ?? [],
    }])),
    lineItems: (value.lineItems ?? []).map(line => ({
      ...line, description: line.description ?? null, quantity: line.quantity ?? null,
      unitPrice: line.unit_price ?? line.unitPrice ?? null, amount: line.amount ?? null,
    })),
  };
}
/** Keeps printed source candidates apart from model suggestions. Nothing here is applied. */
export function fieldCandidates(field: Pick<ExtractedField, "candidates">) {
  const seen = new Set<string>();
  const shown = ((field.candidates ?? []) as FieldCandidate[]).filter(item => {
    const key = `${item.source === "ai" ? "ai" : "source"}|${item.value ?? ""}|${item.raw_text ?? ""}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return item.value != null || !!item.raw_text;
  });
  return { source: shown.filter(item => item.source !== "ai"), model: shown.filter(item => item.source === "ai") };
}
/** A printed reading equal to the selected value is not an alternative, so it is not offered as one. */
export function alternativeCandidates(field: Pick<ExtractedField, "candidates" | "value">) {
  const { source, model } = fieldCandidates(field);
  return { source: source.filter(item => item.value == null || item.value !== field.value), model };
}
/** Both readings of a printed numeric date whose day and month order cannot be told. Nothing here picks one. */
export function dateReadings(printed?: string | null) {
  const match = printed?.match(/(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)/);
  if (!match) return [];
  const [first, second, year] = [Number(match[1]), Number(match[2]), match[3]];
  if (first > 12 || second > 12 || first === second) return [];
  const iso = (month: number, day: number) => `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  return [{ label: "Month first", value: iso(first, second) }, { label: "Day first", value: iso(second, first) }];
}
export const CURRENCY_SUPPORT: Record<CurrencyProposal["support"], string> = {
  printed_on_document: "Printed on the document, not selected",
  supplier_records: "From this supplier's approved records",
  document_context: "Inferred from context only",
};
/** "$" names no currency. These fill the input for the reviewer to confirm; none is assumed. */
const DOLLAR_CURRENCIES = ["USD", "SGD", "AUD", "CAD", "NZD", "HKD"];
export function dollarCurrencyChoices(key: string, field: Pick<ExtractedField, "candidates" | "value">) {
  const printsDollar = fieldCandidates(field).source.some(item => item.value == null && item.raw_text?.includes("$"));
  return key === "currency" && field.value == null && printsDollar ? DOLLAR_CURRENCIES : [];
}
const FIELD_ALIASES: Record<string, string> = { invoice_date: "date", bill_date: "date", transaction_date: "date", invoice_number: "invoice",
  receipt_number: "receipt", bill_reference: "reference", supplier_name: "supplier", merchant_name: "merchant", total_amount: "total", tax_amount: "tax" };
export type FieldGroup = "attention" | "source_backed" | "not_printed";
/** Orders review only. Every field stays visible and editable, and approval rules are not involved. */
export function fieldGroup(key: string, field: Pick<ExtractedField, "evidenceStatus" | "correctedBy">, failedFields: Iterable<string>): FieldGroup {
  if ([...failedFields].some(name => (FIELD_ALIASES[name] ?? name) === key)) return field.correctedBy ? "source_backed" : "attention";
  if (field.correctedBy || field.evidenceStatus === "source_backed") return "source_backed";
  return field.evidenceStatus === "missing" || field.evidenceStatus === "ambiguous_candidate" ? "not_printed" : "attention";
}
export interface ProcessingTrace {
  text_lines?: number | null; ocr_lines?: number | null; runner?: string | null; runner_reason?: string | null; stages?: string[];
  final_route?: string | null; retry?: string | null;
  visual_input?: { kind?: string; page?: number; width?: number; height?: number; prompt_tokens?: number | null } | null;
  qwen?: { attempted?: boolean; status?: string | null; fields?: Array<{ field: string; outcome?: AiAssistOutcome; source_check?: string | null }> };
}
const FINAL_ROUTE: Record<string, string> = {
  DOCUMENT_TYPE_REVIEW: "Document type needs review",
  ACCOUNTANT_ACTION_REQUIRED: "Accountant action required",
  MODEL_CANDIDATES_FOR_REVIEW: "Model suggestions waiting for review",
  ACCOUNTANT_CONFIRMATION: "Ready for accountant confirmation",
};
/** Plain statements of what actually ran. Absent facts are said to be unknown, never assumed. */
export function processingSteps(trace?: ProcessingTrace | null) {
  if (!trace) return [];
  const reading = trace.text_lines == null ? "Docling: line count not recorded"
    : trace.ocr_lines ? `Docling with RapidOCR: ${trace.ocr_lines} of ${trace.text_lines} text lines came from OCR`
      : `Docling: ${trace.text_lines} text lines from the PDF text layer, no OCR needed`;
  const workflow = trace.runner === "langgraph" ? `LangGraph workflow: ${trace.stages?.length ?? 0} stages ran`
    : trace.runner === "plain" ? `Plain router: ${trace.stages?.length ?? 0} stages ran${trace.runner_reason ? ` (${trace.runner_reason})` : ""}`
      : "Workflow runner not recorded";
  const asked = trace.qwen?.fields ?? [];
  const model = asked.length ? `Qwen2.5-VL asked about ${asked.length} ${asked.length === 1 ? "field" : "fields"}`
    : trace.qwen?.attempted ? "Qwen2.5-VL was called but returned nothing usable"
      : trace.qwen?.status === "failed" ? "Qwen2.5-VL could not be used; the parser result stands" : "Qwen2.5-VL not called: no field qualified for recovery";
  const steps = [reading, workflow, model];
  const image = trace.visual_input;
  if (image?.width && image.height) steps.push(`Image sent to the model: page ${image.page ?? 1}, ${image.width} x ${image.height} px${image.prompt_tokens ? `, ${image.prompt_tokens} prompt tokens with the image` : ""}`);
  if (trace.retry) steps.push(trace.retry === "retry_used" ? "One targeted retry was used" : "One targeted retry ran; the first result was kept");
  if (trace.final_route) steps.push(`Route: ${FINAL_ROUTE[trace.final_route] ?? trace.final_route}`);
  return steps;
}
/** Sort key only: what blocks approval first, passed checks last. No result is hidden or changed. */
export const validationRank = (item: Pick<ValidationResult, "result" | "preventsApproval">) =>
  item.result === "FAIL" ? (item.preventsApproval ? 0 : 2) : item.preventsApproval ? 1 : item.result === "NOT_APPLICABLE" ? 5 : item.result === "PASS" ? 4 : 3;
const SOURCE_CHECK_TEXT: Record<string, string> = {
  source_role_verified: "printed beside a matching label; confirm it",
  source_text_match: "text found in source; role not verified",
  source_near_match: "read from the image; differs slightly from the OCR text",
};
/** Why a model answer was held back or rejected, in the words a reviewer needs. */
export const sourceCheckReason = (check?: string | null) => ({
  source_text_match: "found on the document, but nothing ties it to this field",
  source_near_match: "the image and the OCR text differ slightly",
  not_found_in_source: "not found on the document, so it was rejected",
  date_order_not_verifiable: "day and month could be either way round, so it was rejected",
  placeholder_not_a_value: "the document prints a placeholder here, not a value",
} as Record<string, string>)[check ?? ""] ?? null;
const AI_ASSIST_OUTCOME: Record<AiAssistOutcome, string> = {
  PARSER_PRESERVED: "Agrees with current value",
  SOURCE_VERIFIED_CANDIDATE: "Source-checked candidate",
  UNVERIFIED_MODEL_SUGGESTION: "Unverified suggestion",
  PARSER_MODEL_CONFLICT: "Differs from current value",
  NO_RECOVERY: "No supported value",
  MODEL_FAILURE: "Model answer unusable",
};
export const aiAssistOutcomeLabel = (outcome: AiAssistOutcome) => AI_ASSIST_OUTCOME[outcome];
/** Only a value printed on the document can be offered for saving. */
export const aiAssistCanApply = (row: AiAssistResultField) => row.candidate != null
  && ["SOURCE_VERIFIED_CANDIDATE", "UNVERIFIED_MODEL_SUGGESTION", "PARSER_MODEL_CONFLICT"].includes(row.outcome);
export function modelIdentity(result: Pick<AiAssistResult, "model" | "model_digest" | "page" | "seconds">) {
  return [result.model, result.model_digest ? `digest ${result.model_digest.replace(/^sha256:/, "").slice(0, 12)}` : null,
    result.page ? `page ${result.page}` : null, result.seconds != null ? `${result.seconds} s` : null].filter(Boolean).join(" · ");
}

/** One line for a reviewer: the value, the label it was printed under, and where. */
export function candidateText(item: FieldCandidate) {
  const source = (item.evidence ?? []).find(entry => entry.printed_label) ?? item.evidence?.[0];
  const details = [
    source?.printed_label ? `printed as ${source.printed_label}` : null,
    source?.printed_value ? `reads "${source.printed_value}"` : null,
    source?.page ? `page ${source.page}` : null,
    source?.extraction_method ?? null,
    item.model ?? null,
    item.source_check ? SOURCE_CHECK_TEXT[item.source_check] ?? null : null,
  ].filter(Boolean);
  const value = item.value ?? item.raw_text ?? "";
  return details.length ? `${value} (${details.join(", ")})` : value;
}
/** Exact display only. No financial calculation or assumed currency. */
export function decimalDisplay(value: string | number | null | undefined, currency?: string | null): string {
  if (value === null || value === undefined || value === "") return "Not provided";
  const text = String(value);
  if (!/^-?[0-9]+(?:\.[0-9]+)?$/.test(text)) return "Invalid amount";
  const [whole, fraction] = text.split(".");
  const formatted = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (fraction === undefined ? "" : "." + fraction);
  return currency ? currency + " " + formatted : formatted;
}

export type RecordResponse = Omit<StandardisedRecord, "extraction"> & { extraction?: Extraction; snapshot?: string | {
  contentRevision?: number; fieldPredictions?: ExtractionResponse["fields"];
  lineItems?: ExtractionResponse["lineItems"]; extraction?: Record<string, unknown>;
} };
export function mapRecord(record: RecordResponse): StandardisedRecord {
  const applyApprovedValues = (extraction: Extraction): StandardisedRecord => {
    const fields = { ...extraction.fields };
    const extracted = (keys: string[]) => keys.map((key) => fields[key]?.value).find((value) => value !== undefined && value !== null && value !== "");
    const values: Record<string, string | null | undefined> = {
      supplier: record.supplier ?? extracted(["supplier", "supplier_name", "merchant"]),
      customer: record.customer ?? extracted(["customer", "buyer", "buyer_name"]),
      invoice: record.invoiceNumber ?? extracted(["invoice", "invoice_number"]),
      reference: record.referenceNumber ?? extracted(["reference", "reference_number", "bill_reference", "receipt"]),
      date: record.invoiceDate ?? extracted(["date", "invoice_date", "bill_date", "transaction_date"]),
      due_date: record.dueDate ?? extracted(["due_date"]),
      total: record.totalAmount ?? extracted(["total", "total_amount", "amount_due", "total_including_tax"]),
      currency: record.currency ?? extracted(["currency"]),
    };
    for (const [key, value] of Object.entries(values)) {
      if (value !== undefined && value !== null && value !== "") fields[key] = { ...(fields[key] ?? {}), key, label: fields[key]?.label ?? key, value } as typeof fields[string];
    }
    return { ...record, extraction: { ...extraction, fields } };
  };
  if (record.extraction) return applyApprovedValues(mapExtraction(record.extraction as unknown as ExtractionResponse));
  // PostgreSQL JSON may arrive as an object, while older exports stored the
  // snapshot as a JSON string. Accept both forms and the legacy snake_case
  // keys so approved records remain readable after a backend/schema upgrade.
  const snapshot = typeof record.snapshot === "string"
    ? (() => { try { return JSON.parse(record.snapshot) as Exclude<NonNullable<RecordResponse["snapshot"]>, string>; } catch { return {}; } })()
    : (record.snapshot ?? {}) as Exclude<NonNullable<RecordResponse["snapshot"]>, string>;
  const legacy = snapshot as Exclude<NonNullable<RecordResponse["snapshot"]>, string> & {
    field_predictions?: ExtractionResponse["fields"];
    line_items?: ExtractionResponse["lineItems"];
    reviewed_fields?: Record<string, string | null>;
  };
  const fields = snapshot.fieldPredictions ?? legacy.field_predictions ??
    Object.entries(legacy.reviewed_fields ?? {}).map(([key, value]) => ({ key, value }));
  const lineItems = snapshot.lineItems ?? legacy.line_items ?? [];
  return applyApprovedValues(mapExtraction({
    documentId: record.documentId, contentRevision: snapshot.contentRevision,
    fields, lineItems, raw: snapshot.extraction,
  }));
}

/** Support response omits category, validation and createdAt; never synthesize them. */
export type SupportViewResponse = Pick<RecordResponse, "id" | "documentId" | "tenantId" | "status" | "snapshot">;
export type SupportViewRecord = Pick<StandardisedRecord, "id" | "documentId" | "tenantId" | "status"> & { extraction: Extraction };
export function mapSupportView(record: SupportViewResponse): SupportViewRecord {
  const snapshot = typeof record.snapshot === "string" ? (() => { try { return JSON.parse(record.snapshot) as Exclude<NonNullable<RecordResponse["snapshot"]>, string>; } catch { return {}; } })() : (record.snapshot ?? {}) as Exclude<NonNullable<RecordResponse["snapshot"]>, string>;
  return { ...record, extraction: mapExtraction({
    documentId: record.documentId, contentRevision: snapshot.contentRevision,
    fields: snapshot.fieldPredictions, lineItems: snapshot.lineItems, raw: snapshot.extraction,
  }) };
}

/** Source classification overrides stored intake kind, including explicit unknown. */
export function sourceKind(extraction: Pick<Extraction, "classification">, intake?: string): string {
  if (extraction.classification?.value !== undefined) {
    const kind = extraction.classification.value.toUpperCase();
    return ["INVOICE", "BILL", "RECEIPT"].includes(kind) ? kind : "UNKNOWN";
  }
  return intake?.toUpperCase() ?? "UNKNOWN";
}

/** Human-readable labels aligned with backend short storage keys (FIELD_KEYS). */
export function fieldLabel(kind: string, key: string, fallback?: string): string {
  const shared: Record<string, string> = {
    currency: "Currency",
    subtotal: "Subtotal",
    tax: "Tax",
    discount: "Discount",
    total: "Total",
    customer: "Customer",
    due: "Due date",
    supplier: "Supplier",
    invoice: "Invoice number",
    date: "Document date",
    reference: "Reference",
    amount_due: "Amount due",
    amount_paid: "Amount paid",
    allowances: "Allowances",
    charges: "Charges",
    prepaid: "Prepaid amount",
    rounding: "Rounding",
    total_excluding_tax: "Total excluding tax",
    total_including_tax: "Total including tax",
    tendered_amount: "Tendered amount",
    change_given: "Change given",
    outstanding_amount: "Outstanding amount",
    payment_terms: "Payment terms",
    received_date: "Received date",
    expense_category: "Expense category",
    payment_reference: "Payment reference",
    purchase_order_number: "Purchase order number",
    goods_receipt_reference: "Goods receipt reference",
    supplier_address: "Supplier address",
    customer_address: "Customer address",
    supplier_tax_id: "Supplier tax ID",
    customer_tax_id: "Customer tax ID",
    merchant_address: "Merchant address",
    account_number: "Account number",
    billing_period: "Billing period",
    transaction_time: "Transaction time",
    payment_method: "Payment method",
  };
  const profiles: Record<string, Record<string, string>> = {
    INVOICE: {
      supplier: "Supplier",
      invoice: "Invoice number",
      date: "Invoice date",
      customer: "Customer",
    },
    BILL: {
      supplier: "Issuer",
      reference: "Bill reference",
      account_number: "Account number",
      billing_period: "Billing period",
      date: "Bill date",
      total: "Bill total",
      amount_due: "Amount due",
      amount_paid: "Amount paid",
      charges: "Current charges",
    },
    RECEIPT: {
      supplier: "Merchant",
      reference: "Receipt reference",
      date: "Transaction date",
      transaction_time: "Transaction time",
      payment_method: "Payment method",
    },
  };
  return profiles[kind]?.[key] ?? shared[key] ?? fallback ?? key.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

export function validationState(result: ValidationResult): string {
  return result.checkStatus === "NOT_CHECKED" ? "NOT_CHECKED" : result.result;
}

/** GET validation returns stored statuses (PASSED, FAILED, NOT_CHECKED, WARNING); the UI displays PASS, FAIL, WARNING plus checkStatus. An unrecognised value is never shown as a pass. */
export function normalizeValidationResult(raw: Omit<ValidationResult, "result"> & { result?: string | null }): ValidationResult {
  const value = String(raw.result ?? "").toUpperCase();
  if (value === "NOT_APPLICABLE" || raw.checkStatus === "NOT_APPLICABLE") return { ...raw, result: "NOT_APPLICABLE", checkStatus: "NOT_APPLICABLE" };
  const notChecked = value === "NOT_CHECKED" || raw.checkStatus === "NOT_CHECKED";
  const result = notChecked ? "WARNING" : value === "PASS" || value === "PASSED" ? "PASS" : value === "FAIL" || value === "FAILED" ? "FAIL" : "WARNING";
  return { ...raw, result, checkStatus: notChecked ? "NOT_CHECKED" : raw.checkStatus };
}
export function mapValidationReport(report: ValidationReport): ValidationReport {
  return { ...report, results: (report.results ?? []).map(normalizeValidationResult) };
}
export interface ValidationSummary { passed: number; failed: number; notChecked: number; notApplicable: number; warnings: number; blocking: number; }
/** Only these findings can be settled by an accountant with a recorded reason, and only while they are open. */
export const reviewableFinding = (item: Pick<ValidationResult, "rule" | "result" | "checkStatus" | "preventsApproval">) =>
  (item.rule === "EX-010" && item.result === "FAIL") || (item.rule === "TAX-STATUS" && item.checkStatus === "NOT_CHECKED" && item.preventsApproval === true);
/** Counts come from the listed rule results, so the summary always agrees with the table. */
export function validationSummary(results: ValidationResult[]): ValidationSummary {
  const notChecked = results.filter(item => item.checkStatus === "NOT_CHECKED").length;
  return {
    passed: results.filter(item => item.result === "PASS").length,
    failed: results.filter(item => item.result === "FAIL").length,
    notChecked,
    notApplicable: results.filter(item => item.result === "NOT_APPLICABLE").length,
    warnings: results.filter(item => item.result === "WARNING").length - notChecked,
    blocking: results.filter(item => item.result === "FAIL" || item.preventsApproval === true).length,
  };
}

/** Document reference by kind: the extractor stores bills and receipts under their own keys. */
export function documentReference(fields: Extraction["fields"]): string | null {
  for (const key of ["reference", "bill_reference", "bill_number", "receipt_number", "receipt_reference"]) {
    const value = fields[key]?.value;
    if (value) return value;
  }
  return null;
}

export interface CurrencyTotal { currency: string | null; total: string; records: number; }
/** Exact per-currency totals. Mixed currencies are never summed; amounts use integer arithmetic. */
export function escapeMarkup(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export function isPlainNumber(value: string): boolean {
  return /^-?\d+(\.\d+)?$/.test(value);
}

export function currencyTotals(records: Array<{ currency?: string | null; total?: string | null }>): CurrencyTotal[] {
  const groups = new Map<string | null, string[]>();
  for (const { currency, total } of records) {
    if (total == null || !/^-?[0-9]+(?:\.[0-9]+)?$/.test(total)) continue;
    groups.set(currency ?? null, [...(groups.get(currency ?? null) ?? []), total]);
  }
  return [...groups.entries()].map(([currency, totals]) => {
    const scale = Math.max(2, ...totals.map(value => value.split(".")[1]?.length ?? 0));
    const sum = totals.reduce((acc, value) => {
      const negative = value.startsWith("-");
      const [whole, fraction = ""] = value.replace("-", "").split(".");
      const units = BigInt(whole + fraction.padEnd(scale, "0"));
      return acc + (negative ? -units : units);
    }, 0n);
    const digits = (sum < 0n ? -sum : sum).toString().padStart(scale + 1, "0");
    const total = (sum < 0n ? "-" : "") + digits.slice(0, -scale) + "." + digits.slice(-scale);
    return { currency, total, records: totals.length };
  }).sort((a, b) => (a.currency ?? "").localeCompare(b.currency ?? ""));
}
export function standardisationAvailable(status: string, kind: string, report: ValidationReport | null): boolean {
  return status === "VALIDATED" && kind !== "UNKNOWN" && report?.validationStatus === "PASS"
    && report.coverageComplete !== false && !report.blocking;
}

/** Decimal field keys that should use decimalDisplay. */
export const MONEY_KEYS = new Set([
  "subtotal", "tax", "discount", "total", "amount_due", "amount_paid",
  "allowances", "charges", "prepaid", "rounding", "total_excluding_tax",
  "total_including_tax", "tendered_amount", "change_given", "outstanding_amount",
]);
