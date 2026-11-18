"use client";
import Link from "next/link";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, CircleX, FileCheck2, RefreshCw } from "lucide-react";
import type { CurrencyReview, EvidenceStatus, ExtractedField, SourceEvidence, User } from "@/types";
import { AiAssistPanel } from "./ai-assist-panel";
import { MissingLineForm } from "./missing-line-form";
import { CURRENCY_SUPPORT, sourceCheckReason, aiAssistOutcomeLabel, alternativeCandidates, candidateText, dateReadings, dollarCurrencyChoices, fieldGroup, processingSteps, validationRank, type FieldGroup, type ProcessingTrace } from "@/lib/api/document-contract";
import { useResource } from "@/features/common/hooks/use-resource";
import {
  acceptValidationReview,
  addExtractedLineItem,
  retryDocumentProcessing,
  getDocument,
  getExceptions,
  getExtraction,
  getValidationReport,
  revalidateDocument,
  updateExtractedField,
  updateExtractedLineItem,
} from "@/lib/api/document.service";
import { getAuditLogs } from "@/lib/api/audit.service";
import {
  getStandardisedRecords,
  standardiseDocument,
} from "@/lib/api/record.service";
import {
  Badge,
  DataTable,
  ErrorState,
  Field,
  LoadingState,
  PageHeader,
  Panel,
  Toast,
  formatDate,
} from "@/components/common/ui";
import { ApiError } from "@/lib/api/client";
import { canEditDocument, canDecideReview } from "@/lib/permissions/document-review";
import { decimalDisplay as money, fieldLabel, reviewableFinding, sourceKind, standardisationAvailable, validationState, validationSummary } from "@/lib/api/document-contract";
import { TransactionAnomalyVisual } from "@/features/common/documents/transaction-anomaly-visual";
import { DocumentViewer, SourceEvidenceList } from "@/components/common/documents/document-viewer";
import { DocumentDetailPreview } from "@/features/common/documents/invoice-detail-preview";
import { RecordDetail } from "@/features/common/records/records";
import {
  addDocumentComment,
  createCustomer,
  createSupplier,
  decideDocumentReview,
  getDocumentKindWorkflow,
  getDocumentReviews,
  getDocumentComments,
  saveDocumentDraft,
  submitDocumentReview,
  type DocumentComment,
} from "@/lib/api/workflow.service";


const EVIDENCE_STATUS: Record<EvidenceStatus, string> = {
  source_backed: "Source evidence found",
  ambiguous_candidate: "Source candidate, not selected",
  missing: "Not found in source",
  conflicting: "Source candidates conflict",
  human_corrected: "Reviewed correction",
  model_source_verified: "Model candidate, printed beside its label. Not applied",
  model_suggested: "Model candidate, role not verified. Not applied",
  unsupported: "No usable source evidence",
};

/** A reason is required and goes on the audit log. It applies to this revision only, and local-admin approval still follows. */
function ValidationReviewForm({ rule, busy, onSubmit }: { rule: string; busy: boolean; onSubmit: (note: string) => void }) {
  const [note, setNote] = useState("");
  const inputId = useId();
  const enough = note.trim().length >= 10;
  return <div className="validation-review-form">
    <label htmlFor={inputId}>{rule === "EX-010" ? "Why is this amount acceptable?" : "How do you know the tax treatment is right?"}</label>
    <textarea id={inputId} rows={2} value={note} disabled={busy} onChange={e => setNote(e.target.value)} />
    <button type="button" className="btn small" disabled={busy || !enough} onClick={() => onSubmit(note.trim())}>Record review</button>
    <small>Recorded with your name. A later correction reopens it, and a local admin must still approve.</small>
  </div>;
}

function ProcessingPanel({ trace, type, status, labelFor }: { trace?: ProcessingTrace; type?: string; status?: string; labelFor: (key: string) => string }) {
  const steps = processingSteps(trace);
  if (!steps.length) return null;
  return <details className="review-field-group processing-panel"><summary><span>Technical processing details</span><small>Diagnostics</small></summary>
    <ul>
      <li>Detected type: {type ?? "not recorded"}{status && status !== "classified" ? " (needs review)" : ""}</li>
      {steps.map(step => <li key={step}>{step}</li>)}
      {trace?.qwen?.fields?.map(item => <li key={item.field}>{labelFor(item.field)}: {item.outcome ? aiAssistOutcomeLabel(item.outcome) : "no outcome recorded"}{sourceCheckReason(item.source_check) ? ` (${sourceCheckReason(item.source_check)})` : ""}</li>)}
    </ul>
  </details>;
}

/** A suggestion to confirm, never evidence. Saving is the reviewer's explicit decision and is audited on the server. */
function CurrencyReviewPanel({ review, readonly, saving, onConfirm }: { review: CurrencyReview; readonly: boolean; saving: boolean; onConfirm: (code: string) => void }) {
  const confirm = (code: string) => !readonly && <button type="button" className="btn small" disabled={saving} aria-label={`Confirm ${code} as this document's currency`} onClick={() => onConfirm(code)}>Confirm {code}</button>;
  if (!review.needed) {
    if (!review.decision) return null;
    const { decision } = review;
    return <div className="shoebox-field-candidates" aria-label="Currency decision">
      <small><strong>Confirmed as {decision.value}</strong>{decision.by ? ` by ${decision.by}` : ""}{decision.at ? ` on ${decision.at.slice(0, 10)}` : ""}. This is a person&apos;s decision, not a value read from the document.</small>
      {!!decision.selectedBasis?.length && <ul>{decision.selectedBasis.map((clue, index) => <li key={index}>{clue.text}</li>)}</ul>}
    </div>;
  }
  return <div className="shoebox-field-candidates" aria-label="Currency review">
    {review.printedSymbol && <p><small>The document prints only &quot;{review.printedSymbol.symbol}&quot;{review.printedSymbol.text ? ` (${review.printedSymbol.text})` : ""}, which does not name a currency.</small></p>}
    {review.proposals.map(proposal => <div key={proposal.currency} data-currency-proposal={proposal.currency}>
      <p><strong>{proposal.rank === 1 ? "Proposed" : "Also possible"}: {proposal.currency}</strong> <small>Not verified. {CURRENCY_SUPPORT[proposal.support]}.</small></p>
      <ul>{proposal.clues.map((clue, index) => <li key={index}>{clue.name}: &quot;{clue.text}&quot; ({clue.why}{clue.page ? `, page ${clue.page}` : ""})</li>)}</ul>
      {confirm(proposal.currency)}
    </div>)}
    {review.proposals.length === 0 && <p><small>Nothing on the document or in your contact records points to one currency.</small></p>}
    {review.disagreement && <p role="status"><small>These clues point to different currencies. Check the document before choosing.</small></p>}
    {review.conflicts.map((item, index) => <p key={index}><small>{item.currency} is not proposed: {item.name} (&quot;{item.text}&quot;) points to it, but the document prints {review.printedSymbol?.symbol}.</small></p>)}
    {review.alternatives.length > 0 && !readonly && <p><small>If the document or supplier shows another dollar currency: </small>{review.alternatives.map(code => <span key={code}>{confirm(code)} </span>)}</p>}
    <p><small>{review.statement}</small></p>
  </div>;
}

/** Another document may have added this contact since this result was stored: then only revalidation is needed. */
const addedOrExisting = (request: Promise<unknown>) => request.catch(e => { if (!(e instanceof ApiError && e.status === 409)) throw e; });

function FormField({ field, fieldKey, onSave, onFocus, readonly, selected, onDirtyChange, currencyReview }: {
  field: ExtractedField; fieldKey?: string; onSave: (value: string) => Promise<void>; onFocus?: () => void; readonly: boolean; selected?: boolean;
  onDirtyChange?: (label: string, dirty: boolean) => void; currencyReview?: CurrencyReview | null;
}) {
  const inputId = useId();
  const [value, setValue] = useState(field.value ?? "");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const candidates = alternativeCandidates(field);
  const readings = !field.correctedBy && field.value == null && (fieldKey === "date" || fieldKey === "due")
    ? candidates.source.flatMap(item => dateReadings(item.value ?? item.raw_text)).slice(0, 2) : [];
  const currencies = !field.correctedBy && !currencyReview ? dollarCurrencyChoices(fieldKey ?? "", field) : [];
  const currencyOptions = Object.keys(CURRENCY_SUPPORT);
  useEffect(() => { if (!dirty) setValue(field.value ?? ""); }, [field.value, dirty]);
  useEffect(() => {
    onDirtyChange?.(field.label, dirty);
    return () => onDirtyChange?.(field.label, false);
  }, [dirty, field.label, onDirtyChange]);
  /** Fills the input only. Nothing is stored until the reviewer presses Save. */
  const fillFromCandidate = (candidate: string) => { setValue(candidate); setDirty(true); setConflict(false); setError(""); };
  return <div className={`shoebox-field${selected ? " selected" : ""}`} onFocusCapture={onFocus}>
    <div className="shoebox-field-heading"><label className="shoebox-label" htmlFor={inputId}>{field.label}</label>
      {onFocus && <button type="button" className="text-link" aria-label={`View source for ${field.label}`} aria-pressed={!!selected} onClick={onFocus}>View source</button>}
    </div>
    <span className="shoebox-field-state">{currencyReview?.decision && field.correctedBy ? "Confirmed by a person, not read from the document" : field.correctedBy ? "Reviewed correction" : field.evidenceStatus ? EVIDENCE_STATUS[field.evidenceStatus] : "Source state: " + (field.state ?? "unverified")}</span>
    <input id={inputId} list={fieldKey === "currency" ? `${inputId}-currency-options` : undefined} aria-label={field.label} value={value} disabled={readonly || saving}
      onFocus={onFocus} onChange={e => { setValue(e.target.value); setDirty(true); setConflict(false); setError(""); }} />
    {fieldKey === "currency" && <datalist id={`${inputId}-currency-options`}>{currencyOptions.map(code => <option key={code} value={code} />)}</datalist>}
    {selected && <div className="shoebox-mobile-evidence"><strong>Selected source evidence</strong>
      {field.evidence?.length ? field.evidence.map((item, index) => <div key={index}><strong>Source page {item.page ?? "not provided"}</strong><p>{item.text ?? "Source text not provided"}</p></div>) : <p>Source evidence not available.</p>}
    </div>}
    <details className="shoebox-field-metadata"><summary>Raw values and extraction details</summary>
      <dl><dt>Raw source value</dt><dd>{field.rawValue ?? "Not provided"}</dd>
        <dt>Extracted value</dt><dd>{field.original ?? "Not provided"}</dd>
        <dt>Saved current value</dt><dd>{field.value ?? "Not provided"}</dd>
        <dt>Method</dt><dd>{field.method ?? "Not provided"}</dd>
        <dt>Confidence</dt><dd>{field.confidence == null ? "Not measured" : String(field.confidence)}</dd>
        {field.ocrEngineScore && <><dt>OCR engine score</dt><dd>{field.ocrEngineScore.min === field.ocrEngineScore.max ? field.ocrEngineScore.min.toFixed(3) : `${field.ocrEngineScore.min.toFixed(3)} to ${field.ocrEngineScore.max.toFixed(3)}`} (text recognition only, not field correctness)</dd></>}
        {field.correctedBy && <><dt>Corrected by</dt><dd>{field.correctedBy}</dd><dt>Corrected at</dt><dd>{field.correctedAt ?? "Not provided"}</dd></>}
      </dl>
    </details>
    {!field.correctedBy && candidates.source.length > 0 && <div className="shoebox-field-candidates"><small>Printed in source, not selected:</small>
      <ul>{candidates.source.map((item, index) => <li key={index}>{candidateText(item)}
        {!readonly && item.value != null && readings.length === 0 && <button type="button" className="text-link" onClick={() => fillFromCandidate(item.value!)}>Use this value</button>}</li>)}</ul></div>}
    {!field.correctedBy && candidates.model.length > 0 && <div className="shoebox-field-candidates"><small>Model suggestion, not verified as an accounting fact. Not applied:</small>
      <ul>{candidates.model.map((item, index) => <li key={index}>{candidateText(item)}
        {!readonly && item.value != null && <button type="button" className="text-link" onClick={() => fillFromCandidate(item.value!)}>Use this value</button>}</li>)}</ul>
      {field.value != null && <small>Parser value: {field.value}. The parser value is kept unless you change it.</small>}</div>}
    {!readonly && readings.length > 0 && <div className="shoebox-field-candidates"><small>Day and month order is not clear. Check the document, then choose:</small>
      <ul>{readings.map(item => <li key={item.value}>{item.label}: {item.value} <button type="button" className="text-link" onClick={() => fillFromCandidate(item.value)}>Use this reading</button></li>)}</ul></div>}
    {currencyReview && <CurrencyReviewPanel review={currencyReview} readonly={readonly} saving={saving} onConfirm={async code => {
      setSaving(true); setError("");
      try { await onSave(code); setDirty(false); }
      catch (e) { setError((e as Error).message); setConflict(e instanceof ApiError && e.status === 409); }
      finally { setSaving(false); }
    }} />}
    {!readonly && currencies.length > 0 && <div className="shoebox-field-candidates"><small>&quot;$&quot; does not name a currency. If the document or supplier shows which one, choose:</small>
      <ul>{currencies.map(code => <li key={code}><button type="button" className="text-link" onClick={() => fillFromCandidate(code)}>{code}</button></li>)}</ul></div>}
    {field.confirmationRequired && field.evidenceStatus !== "source_backed" && <small>Accountant confirmation required</small>}
    {field.warnings?.map((warning, index) => <p role="status" key={warning.code + index}>
      {warning.reviewed ? "Reviewed source warning" : "Extraction warning"}: {warning.message}
    </p>)}
    {!readonly && <button type="button" className="btn small" aria-label={"Save " + field.label} disabled={!dirty || saving || conflict} onClick={async () => {
      setSaving(true); setError("");
      try { await onSave(value); setDirty(false); }
      catch (e) { setError((e as Error).message); setConflict(e instanceof ApiError && e.status === 409); }
      finally { setSaving(false); }
    }}>{saving ? "Saving..." : "Save field"}</button>}
    {error && <p role="alert">{error}{conflict ? " Latest data refreshed; your draft is retained. Edit again after reviewing the saved value." : ""}</p>}
  </div>;
}

export function DocumentReview({
  user,
  documentId,
}: {
  user: User;
  documentId: string;
}) {
  const base = user.role === "LOCAL_ADMIN" ? "/company" : "/workspace";
  const { data, error, loading, refresh } = useResource(
    async () => {
      const [doc, extraction] = await Promise.all([
        getDocument(user, documentId),
        getExtraction(user, documentId),
      ]);
      const validationReport = ["UPLOADED","QUEUED","PROCESSING","FAILED"].includes(doc.status) ? null : await getValidationReport(user, documentId);
      const optional = async <T,>(request: Promise<T>, fallback: T) => {
        try {
          return await Promise.race([
            request,
            new Promise<T>((resolve) => window.setTimeout(() => resolve(fallback), 4000)),
          ]);
        } catch {
          return fallback;
        }
      };
      // This document's rows only. The tenant-wide lists took about a second each and the audit
      // list stops at the newest 500 entries, which dropped an older document's history.
      const recordsRequest = getStandardisedRecords(user);
      const [exceptions, records, audit, comments, workflow, reviews] = await Promise.all([
        optional(getExceptions(user, documentId).then((items) => items.filter((e) => e.documentId === documentId)), []),
        optional(recordsRequest, []),
        optional(recordsRequest.catch(() => []).then((items) =>
          getAuditLogs(user, [documentId, doc.name, items.find((r) => r.documentId === documentId)?.id ?? ""])), []),
        optional(getDocumentComments(documentId), []),
        optional(getDocumentKindWorkflow(documentId), { kind: "", fields: {}, overdue: false, duplicates: [], relatedNotes: [] }),
        optional(getDocumentReviews(documentId), null),
      ]);
      return { doc, extraction, validation: validationReport?.results ?? [], validationReport, exceptions, records, audit, comments, workflow, reviews };
    },
    `${user.id}:${documentId}`,
  );
  const [panel, setPanel] = useState<"review" | "validation" | "record" | "activity">("review");
  const [fieldFilter, setFieldFilter] = useState<"attention" | "all" | "reviewed">("attention");
  const [unsaved, setUnsaved] = useState<string[]>([]);
  const [assistSource, setAssistSource] = useState<{ label: string; evidence: SourceEvidence[] } | undefined>();
  const markDirty = useCallback((label: string, isDirty: boolean) => setUnsaved(current =>
    isDirty ? (current.includes(label) ? current : [...current, label]) : current.filter(item => item !== label)), []);
  const [note, setNote] = useState("");
  const [failure, setFailure] = useState("");
  const [toast, setToast] = useState("");
  const [busy, setBusy] = useState(false);
  const [comment, setComment] = useState("");
  const [highlightedField, setHighlightedField] = useState<string | undefined>();
  const [selectedLine, setSelectedLine] = useState<{ index: number; key: string; label: string }>();
  const validationHeadingRef = useRef<HTMLDivElement>(null);

  async function run(op: () => Promise<unknown>, message: string, propagate = false) {
    setBusy(true);
    setFailure("");
    setToast(""); // An earlier success must not stay on screen beside a later failure.
    try {
      await op();
      await refresh();
      setToast(message); // Only after the saved value has been read back.
    } catch (e) {
      setFailure((e as Error).message);
      if (e instanceof ApiError && e.status === 409) await refresh();
      if (propagate) throw e;
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!data || !["UPLOADED","QUEUED","PROCESSING"].includes(data.doc.status)) return;
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [data, refresh]);

  if (loading) return <LoadingState variant="review" />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;

  const extraction = data.extraction ?? { documentId, fields: {}, lineItems: [] };
  const kind = sourceKind(extraction, data.doc.type);
  const sourceReadonly = ["COMPLETED","STANDARDISED"].includes(data.doc.status) || !extraction.contentRevision || kind === "UNKNOWN";
  const canStandardise = standardisationAvailable(data.doc.status, kind, data.validationReport);
  const workflow = data.workflow ?? { overdue: false, duplicates: [], relatedNotes: [] };
  const validation = data.validation ?? [];
  const summary = validationSummary(validation);
  const failedFields = validation.filter(item => item.result === "FAIL" && item.field).map(item => item.field as string);
  const fieldsIn = (group: FieldGroup) => Object.entries(extraction.fields).filter(([key, field]) => {
    if (fieldFilter === "reviewed" && !field.correctedBy) return false;
    if (fieldFilter === "attention" && fieldGroup(key, field, failedFields) !== "attention") return false;
    return fieldGroup(key, field, failedFields) === group;
  });
  const allFieldCount = Object.keys(extraction.fields).length;
  const attentionFieldCount = Object.entries(extraction.fields).filter(([key, field]) => fieldGroup(key, field, failedFields) === "attention").length;
  const reviewedFieldCount = Object.values(extraction.fields).filter(field => !!field.correctedBy).length;
  const sourceBackedFieldCount = Object.entries(extraction.fields).filter(([key, field]) => fieldGroup(key, field, failedFields) === "source_backed").length;
  const missingFieldCount = Object.entries(extraction.fields).filter(([key, field]) => fieldGroup(key, field, failedFields) === "not_printed").length;
  const renderField = ([key, field]: [string, ExtractedField]) => (
    <FormField
      key={key}
      fieldKey={key}
      field={{ ...field, label: fieldLabel(kind, key, field.label) }}
      readonly={readonly}
      currencyReview={key === "currency" ? extraction.currencyReview : undefined}
      selected={!selectedLine && highlightedField === key}
      onDirtyChange={markDirty}
      onFocus={() => { setAssistSource(undefined); setSelectedLine(undefined); setHighlightedField(key); }}
      onSave={(value) =>
        run(
          () => updateExtractedField(user, documentId, key, value, data.extraction.contentRevision!),
          "Correction saved with a new revision.", true,
        )
      }
    />
  );
  const modelSuggestions = Object.values(extraction.fields).filter(field => alternativeCandidates(field).model.length > 0).length;
  // A receivable's counterparty is the customer, even before the company has a customer list.
  const receivable = data.doc.category?.toUpperCase() === "ACCOUNTS_RECEIVABLE";
  const newSupplier = extraction.fields.supplier?.value ?? extraction.fields.merchant?.value ?? extraction.fields.issuer?.value;
  // Revalidating resets status to extracted/exception, which would silently undo a submission or approval.
  const canRevalidate = !["IN_REVIEW", "VALIDATED"].includes(data.doc.status);
  const blockingFindings = summary.blocking;
  const validationStatus = data.validationReport?.validationStatus;
  const viewIssues = () => {
    setPanel("validation");
    window.setTimeout(() => validationHeadingRef.current?.focus(), 0);
  };
  const records = data.records ?? [];
  const comments = data.comments ?? [];
  const audit = data.audit ?? [];
  const record = records.find((r) => r.documentId === documentId);
  const readonly = sourceReadonly || !canEditDocument(user, data.doc, !!record);
  const failed = data.doc.status === "FAILED";
  const pending = ["UPLOADED", "QUEUED", "PROCESSING"].includes(data.doc.status);
  const extracting = data.doc.status === "PROCESSING";

  return (
    <>
      <PageHeader
        eyebrow="DOCUMENT REVIEW"
        title={data.doc.name}
        description={`${data.doc.id} · Uploaded ${formatDate(data.doc.createdAt)}`}
        actions={
          <>
            <Badge>{data.doc.status}</Badge>
            <Link href={`${base}/documents`} className="btn">
              All documents
            </Link>
          </>
        }
      />

      {failure && <ErrorState message={failure} />}

      {!failed && !pending && (
        <DocumentDetailPreview doc={data.doc} extraction={extraction} />
      )}

      {failed || pending ? (
        <section className={`document-processing-card ${failed ? "is-failed" : "is-pending"}`}>
          <div className="document-processing-head">
            <div><span className="document-processing-kicker">DOCUMENT WORKFLOW</span><h2>{failed ? "Processing needs attention" : extracting ? "Extracting your document" : "Waiting for processing"}</h2><p>{failed ? "The source could not be extracted. Review the processing error before retrying." : extracting ? "A worker is reading the source and preparing the fields for review." : "Your document is stored. Extraction will start when a worker claims its queued job."}</p></div><Badge>{data.doc.status}</Badge>
          </div>
          <div className="document-processing-steps"><div className="is-complete"><CheckCircle2 size={18} /><span>Uploaded</span></div><div className={extracting ? "is-current" : ""}><RefreshCw size={18} /><span>{failed ? "Extraction failed" : extracting ? "Extracting fields" : "Queued for extraction"}</span></div><div><FileCheck2 size={18} /><span>Ready for review</span></div></div>
          <div className="document-processing-footer"><span>{failed ? "Review the error and retry when ready." : "You can leave this page and return to check its progress."}</span><button className="btn primary" disabled={busy || pending} onClick={() => run(() => retryDocumentProcessing(user, documentId), "Extraction retry queued.")}><RefreshCw size={16} /> {failed ? "Retry extraction" : extracting ? "Extraction in progress" : "Waiting for a worker"}</button></div>
        </section>
      ) : (
        <>
          <div className="review-toolbar">
            <button className={panel === "review" ? "active" : ""} onClick={() => setPanel("review")}>Review</button>
            <button className={panel === "validation" ? "active" : ""} onClick={() => setPanel("validation")}>Validation</button>
            <button className={panel === "record" ? "active" : ""} onClick={() => setPanel("record")}>Standardised Record</button>
            <button className={panel === "activity" ? "active" : ""} onClick={() => setPanel("activity")}>Activity</button>
          </div>

          {panel === "review" && (
            <div className="shoebox-split">
              <div className="shoebox-doc">
                <DocumentViewer name={data.doc.name} extraction={extraction} highlightedField={highlightedField}
                  selectedSource={assistSource ? assistSource : selectedLine ? { label: selectedLine.label, evidence: extraction.lineItems[selectedLine.index]?.evidence?.[selectedLine.key] ?? [] }
                    : highlightedField ? { label: fieldLabel(kind, highlightedField, extraction.fields[highlightedField]?.label), evidence: extraction.fields[highlightedField]?.evidence ?? [] } : undefined} />
              </div>
              <div className="shoebox-form">
                <div className="shoebox-form-head">
                  <div className="review-form-title-row"><span>{kind === "UNKNOWN" ? "Unknown / Needs Review" : kind[0] + kind.slice(1).toLowerCase() + " fields"}</span><small>{allFieldCount} fields · revision {extraction.contentRevision ?? "—"}</small></div>
                  <div className="review-filter-bar" role="toolbar" aria-label="Field filters">
                    <button type="button" className={fieldFilter === "attention" ? "active" : ""} onClick={() => setFieldFilter("attention")}>Needs attention <b>{attentionFieldCount}</b></button>
                    <button type="button" className={fieldFilter === "all" ? "active" : ""} onClick={() => setFieldFilter("all")}>All fields <b>{allFieldCount}</b></button>
                    <button type="button" className={fieldFilter === "reviewed" ? "active" : ""} onClick={() => setFieldFilter("reviewed")}>Reviewed <b>{reviewedFieldCount}</b></button>
                  </div>
                </div>
                {kind === "UNKNOWN" && (Object.keys(extraction.fields).length > 0 || extraction.lineItems.length > 0) &&
                  <p role="status">Provisional source reads are shown below. Document type and field roles remain unresolved; these candidates are not approved accounting values.</p>}
                {(workflow.overdue || workflow.duplicates.length > 0 || workflow.relatedNotes.length > 0) && (
                  <div className="panel-body">
                    {workflow.overdue && <Badge>Overdue</Badge>}
                    {workflow.duplicates.length > 0 && <p>Possible duplicate document: {workflow.duplicates.map((d) => d.code).join(", ")}</p>}
                    {workflow.relatedNotes.length > 0 && <p>Related notes: {workflow.relatedNotes.map((d) => d.code).join(", ")}</p>}
                  </div>
                )}
                <ProcessingPanel trace={extraction.raw?.processing_trace as ProcessingTrace | undefined} type={extraction.classification?.value} status={extraction.classification?.status}
                  labelFor={(key) => fieldLabel(kind, key, extraction.fields[key]?.label)} />
                <AiAssistPanel user={user} documentId={documentId} contentRevision={extraction.contentRevision} readonly={readonly}
                  labelFor={(key) => fieldLabel(kind, key, extraction.fields[key]?.label)}
                  onShowSource={(label, evidence) => { setSelectedLine(undefined); setAssistSource({ label, evidence }); }}
                  onSaved={refresh} />
                {Object.keys(extraction.fields).length === 0 && <p>No accounting fields available. Review the original source.</p>}
                {Object.keys(extraction.fields).length > 0 && <div role="status" className="review-focus-summary">
                  <strong className="review-summary-title">Review status</strong>
                  <ul className="review-summary-list">
                    <li className="is-warning"><b>{attentionFieldCount}</b><span>{attentionFieldCount === 1 ? "field needs" : "fields need"} your decision</span></li>
                    <li className="is-info"><b>{sourceBackedFieldCount}</b><span>have source evidence or a human correction</span></li>
                    <li className="is-danger"><b>{missingFieldCount}</b><span>have no value selected</span></li>
                    <li className="is-neutral"><b>{modelSuggestions}</b><span>{modelSuggestions === 1 ? "field has" : "fields have"} a Qwen suggestion; none are applied automatically</span></li>
                    {data.validationReport && <li className="is-validation"><b>Validation</b><span>{summary.passed} passed · {summary.failed} failed · {summary.notChecked} not checked</span></li>}
                  </ul>
                </div>}
                {fieldsIn("attention").length > 0 && <details open className="review-field-group review-priority-group"><summary><span>Needs your attention</span><b>{fieldsIn("attention").length}</b></summary><p className="review-section-help">Fields with missing, conflicting, or uncertain values. Compare each one with the source document.</p><div className="shoebox-fields" aria-label="Fields that need your decision">{fieldsIn("attention").map(renderField)}</div></details>}
                {fieldsIn("source_backed").length > 0 && <details className="review-field-group"><summary><span>Source-backed and reviewed fields</span><b>{fieldsIn("source_backed").length}</b></summary>
                  <div className="shoebox-fields">{fieldsIn("source_backed").map(renderField)}</div></details>}
                {fieldsIn("not_printed").length > 0 && <details open={data.validationReport?.validationStatus !== "PASS" || undefined} className="review-field-group"><summary><span>No value selected</span><b>{fieldsIn("not_printed").length}</b></summary>
                  <div className="shoebox-fields">{fieldsIn("not_printed").map(renderField)}</div></details>}
                {!!extraction.warnings?.length && <div aria-label="Extraction warnings"><strong>Extraction warnings</strong>
                  <p>Source uncertainty is separate from accounting validation.</p>
                  {extraction.warnings.map((warning, index) => <p role="status" key={warning.code + index}>{warning.message}</p>)}
                </div>}
                {extraction.lineItems.length > 0 && (
                  <div className="shoebox-lines">
                    <strong>{kind === "BILL" ? "Charges / line items" : kind === "RECEIPT" ? "Receipt items" : "Line items"}</strong>
                    <DataTable
                      rows={extraction.lineItems}
                      rowKey={(r) => r.id ?? r.description ?? "line"}
                      columns={[
                        { label: "Description", render: (r) => r.description ?? "Not provided" },
                        { label: "Qty", render: (r) => r.quantity ?? "Not provided" },
                        { label: "Unit", render: (r) => money(r.unitPrice, extraction.fields.currency?.value) },
                        { label: "Amount", render: (r) => money(r.amount, extraction.fields.currency?.value) },
                      ]}
                    />
                    <details open={kind === "UNKNOWN" || undefined}><summary>{kind === "UNKNOWN" ? "Provisional line candidates" : "Correct line items"}</summary>
                      <p>{kind === "UNKNOWN" ? "Source candidates are retained for inspection. No line value has been selected while the document type is unresolved." : "Save only values supported by the original document. Missing cells stay empty until confirmed."}</p>
                      {extraction.lineItems.map((line, index) => <div className="shoebox-fields" key={line.id ?? index}>
                        {([['description', 'description', 'Description'], ['quantity', 'quantity', 'Quantity'], ['unit_price', 'unitPrice', 'Unit price'], ['amount', 'amount', 'Amount']] as const).map(([key, property, label]) => {
                          const originalRows = extraction.raw?.line_items as Array<Record<string, {value?: string | null}>> | undefined;
                          const original = originalRows?.[index]?.[key]?.value ?? null;
                          const correction = extraction.lineCorrections?.find(item => item.line_item_id === line.id && item.field_key === key);
                          const guidance = line.guidance?.[key];
                          return <FormField key={key} readonly={readonly || !line.id}
                            selected={selectedLine?.index === index && selectedLine.key === key}
                            onFocus={() => { setHighlightedField(undefined); setSelectedLine({ index, key, label: `Line ${index + 1} ${label}` }); }}
                            field={{label: `Line ${index + 1} ${label}`, value: line[property] == null ? null : String(line[property]),
                              original, rawValue: line.evidence?.[key]?.map(e => e.text).filter(Boolean).join(" | ") || null,
                              confidence: null, state: line.fieldStates?.[key], evidence: line.evidence?.[key],
                              candidates: line.candidates?.[key],
                              method: line.methods?.[key] ?? "Method not provided",
                              correctedBy: correction?.corrected_by, correctedAt: correction?.corrected_at,
                              confirmationRequired: !correction, warnings: guidance?.warnings?.map(w => ({...w, reviewed: !!correction}))}}
                            onDirtyChange={markDirty}
                            onSave={value => run(() => updateExtractedLineItem(user, documentId, line.id!, key, value, extraction.contentRevision!), "Line correction saved with a new revision.", true)} />;
                        })}
                      </div>)}
                      {!!extraction.lineCorrections?.length && <details><summary>Line correction history</summary>
                        {extraction.lineCorrections.map((item, index) => <p key={index}>{item.field_key}: {item.old_value ?? "Not provided"} → {item.new_value ?? "Not provided"} | {item.corrected_by} | {item.corrected_at}</p>)}
                      </details>}
                    </details>
                    <details><summary>Line item source evidence</summary>{extraction.lineItems.map((line, i) => <div key={line.id ?? i}><strong>Line {i + 1}</strong>{Object.entries(line.evidence ?? {}).map(([key, evidence]) => <div key={key}><p>{key}</p><SourceEvidenceList evidence={evidence} documentId={documentId} /></div>)}</div>)}</details>
                  </div>
                )}
                {!readonly && canRevalidate && <MissingLineForm revision={data.doc.contentRevision ?? 1} disabled={busy || unsaved.length !== 0}
                  onSave={input => run(() => addExtractedLineItem(documentId, input), "Printed item saved and revalidated.", true)} />}
                {(
                  <details open className="shoebox-actions review-action-drawer">
                    <summary><span>Review actions</span><small>{unsaved.length ? `${unsaved.length} unsaved change${unsaved.length === 1 ? "" : "s"}` : "Save or submit this review"}</small></summary>
                    <Field label="Review note · Optional">
                        <textarea disabled={readonly}
                        value={note}
                        onChange={(e) => setNote(e.target.value)}
                        placeholder="Add context for the approver or explain a correction…"
                      />
                    </Field>
                    <div className="review-action-bar">
                      <div className={`review-save-state ${unsaved.length ? "is-dirty" : "is-saved"}`} role="status">
                        <span aria-hidden="true" />{unsaved.length ? `Unsaved changes: ${unsaved.length}` : "All changes saved"}
                      </div>
                      {unsaved.length > 0 && <p className="review-unsaved-hint">Revalidation uses saved values only. Save changes before revalidating.</p>}
                      <div className="review-action-buttons">
                        <button className="btn" disabled={readonly || busy} onClick={() => run(() => saveDocumentDraft(documentId), "Draft saved.")}>Save draft</button>
                        {canRevalidate && <button className="btn primary" disabled={readonly || busy || unsaved.length > 0} onClick={() => run(() => revalidateDocument(user, documentId), "Review saved and revalidated.")}><CheckCircle2 size={16} /> Save & revalidate</button>}
                        {user.role === "ACCOUNTANT" && canRevalidate && <button className={`btn ${validationStatus === "PASS" && blockingFindings === 0 ? "primary" : "secondary"}`} disabled={readonly || busy || validationStatus !== "PASS" || blockingFindings > 0 || unsaved.length > 0} onClick={() => run(() => submitDocumentReview(documentId, "submitted", note || "Ready for approval", data.doc.contentRevision ?? 1), "Submitted for approval.")}>Submit for approval</button>}
                      </div>
                    </div>
                    {user.role === "ACCOUNTANT" && canRevalidate && blockingFindings > 0 && <div className="review-blocking-notice" role="status"><strong>{blockingFindings} {blockingFindings === 1 ? "issue blocks" : "issues block"} submission</strong><span>Review the validation findings to continue.</span><button type="button" className="text-link" onClick={viewIssues}>View issues</button></div>}
                    {user.role === "LOCAL_ADMIN" && canRevalidate && <p role="status">An Accountant must submit this document for approval. A different Local Admin can then approve it.</p>}
                    {data.doc.status === "IN_REVIEW" && <p role="status">Submitted for independent Local Admin approval. Corrections require a new submission.</p>}
                    {canDecideReview(user, data.doc, data.reviews) && data.validationReport?.validationStatus === "PASS" && data.validationReport.coverageComplete !== false && (
                      <button className="btn primary" disabled={busy} onClick={() => run(() => decideDocumentReview(documentId, "approved", note || "Approved", data.doc.contentRevision ?? 1), "Document approved.")}>
                        Approve document
                      </button>
                    )}
                  </details>
                )}
              </div>
            </div>
          )}

          {panel === "validation" && (
            <Panel title="Validation centre" subtitle="Confirm every extracted value before this document moves to standardisation.">
              {data.validationReport && <div className="validation-overview">
                <div className={`validation-score ${summary.failed ? "has-fail" : ""}`}><Badge>{data.validationReport.validationStatus}</Badge><strong>{summary.blocking}</strong><span>blocking findings</span></div>
                <div className="validation-overview-copy"><strong>Review required before standardisation</strong><p>Confirm every extracted value. Human approval is still required; this is not an extraction accuracy score.</p></div>
                <div className="validation-stat is-pass"><strong>{summary.passed}</strong><span>Passed</span></div>
                <div className="validation-stat is-fail"><strong>{summary.failed}</strong><span>Failed</span></div>
                <div className="validation-stat is-pending"><strong>{summary.notChecked}</strong><span>Not checked</span></div>
                <div className="validation-stat"><strong>{summary.notApplicable}</strong><span>Not applicable</span></div>
              </div>}
              {data.validationReport?.transactionAnomaly && <TransactionAnomalyVisual result={data.validationReport.transactionAnomaly} />}
              <div className="validation-results-head" ref={validationHeadingRef} tabIndex={-1}><div><strong>Rule results</strong><span>{validation.length} backend findings at the current revision</span></div><Badge>{data.doc.status}</Badge></div>
              <DataTable
                rows={[...validation].sort((a, b) => validationRank(a) - validationRank(b))}
                rowKey={(v) => JSON.stringify([v.rule, v.field ?? null, validation.indexOf(v)])}
                columns={[
                  { label: "Check", render: (v) => <div className="validation-rule"><span className={`validation-icon ${v.result.toLowerCase()}`}>{v.result === "PASS" ? <CheckCircle2 size={16} /> : v.result === "WARNING" ? <AlertTriangle size={16} /> : <CircleX size={16} />}</span><strong>{v.rule}</strong></div> },
                  { label: "Result", render: (v) => <><Badge>{validationState(v)}</Badge>{v.preventsApproval && <small> Blocks standardisation</small>}</> },
                  { label: "Field", render: (v) => v.field ?? "Not provided" },
                  { label: "Severity", render: (v) => v.severity ?? "Not provided" },
                  { label: "What to do", render: (v) => <div>{v.causeLabel && <small>{v.causeLabel}</small>} <span className={v.result === "FAIL" ? "validation-message fail-text" : "validation-message"}>{v.message}</span>
                    {v.rule === "EX-008" && (v.field === "customer" || receivable) && !readonly && user.role === "LOCAL_ADMIN" && extraction.fields.customer?.value && <p><button type="button" className="btn small" disabled={busy} onClick={() => run(async () => { await addedOrExisting(createCustomer(extraction.fields.customer!.value!)); await revalidateDocument(user, documentId); }, "Customer added and document revalidated.")}>Add {extraction.fields.customer.value} as a customer</button></p>}
                    {reviewableFinding(v) && !readonly && canRevalidate && extraction.contentRevision && <ValidationReviewForm rule={v.rule} busy={busy} onSubmit={note => run(() => acceptValidationReview(user, documentId, v.rule, note, extraction.contentRevision!), "Review recorded and document revalidated.")} />}
                    {v.rule === "EX-008" && v.field !== "customer" && !receivable && !readonly && user.role === "LOCAL_ADMIN" && newSupplier && <p><button type="button" className="btn small" disabled={busy} onClick={() => run(async () => { await addedOrExisting(createSupplier(newSupplier)); await revalidateDocument(user, documentId); }, "Supplier added and document revalidated.")}>Add {newSupplier} as a supplier</button></p>}
                    {v.rule === "EX-008" && !readonly && user.role !== "LOCAL_ADMIN" && <p className="validation-hint">Only a local admin can add this name to the {v.field === "customer" || receivable ? "customer" : "supplier"} list. Ask them to add it from this document; it is then revalidated. Approval also needs a local admin.</p>}
                    {v.duplicate_of && <p><Link href={`${base}/documents/${encodeURIComponent(v.duplicate_of)}`}>Related document: {v.duplicate_of}</Link></p>}
                    {v.vendor_candidate && <p>Vendor suggestion: {v.vendor_candidate.vendor_name ?? "Not provided"}<br />Similarity: {v.vendor_candidate.similarity ?? "Not provided"} (name matching, not extraction confidence)<br />Method: {v.vendor_candidate.score_method ?? "Not provided"}</p>}
                  </div> },
                ]}
              />
              <div className="panel-body actions validation-actions">
                {!readonly && canRevalidate && (
                  <button
                    disabled={busy || unsaved.length > 0}
                    className="btn"
                    onClick={() => run(() => revalidateDocument(user, documentId), "Document revalidated.")}
                  >
                    <RefreshCw size={15} /> Revalidate
                  </button>
                )}
                <button
                  disabled={busy || !canStandardise}
                  className="btn primary"
                  onClick={() => run(async () => { await standardiseDocument(user, documentId); setPanel("record"); }, "Standardised accounting record created.")}
                >
                  <FileCheck2 size={16} /> Create standardised record
                </button>
              </div>
            </Panel>
          )}

          {panel === "record" && (
            record ? <RecordDetail record={record} /> : <Panel title="Standardised record" subtitle="The accounting record is created only after the extracted values pass validation and you confirm the document.">
              <div className="panel-body record-empty-state">
                <FileCheck2 size={28} aria-hidden="true" />
                <strong>No standardised record yet</strong>
                <p>Review and correct the extracted values, then revalidate the document. Once the blocking checks pass, create the standardised record here.</p>
                <button type="button" className="btn primary" disabled={busy || !canStandardise} onClick={() => run(async () => { await standardiseDocument(user, documentId); setPanel("record"); }, "Standardised accounting record created.")}>
                  <FileCheck2 size={16} /> Create standardised record
                </button>
                {!canStandardise && <small>Complete the blocking validation findings before creating the record.</small>}
              </div>
            </Panel>
          )}

          {panel === "activity" && (
            <>
            <Panel title="Document activity">
              <div className="panel-body">
                <Field label="Correction comment">
                  <textarea
                    value={comment}
                    onChange={(e) => setComment(e.target.value)}
                    placeholder="Add context for the next reviewer…"
                  />
                </Field>
                <button
                  className="btn primary"
                  disabled={busy || !comment.trim()}
                  onClick={() =>
                    run(
                      async () => {
                        await addDocumentComment(documentId, comment.trim());
                        setComment("");
                      },
                      "Comment added.",
                    )
                  }
                >
                  Add comment
                </button>
              </div>
              {comments.length > 0 && (
                <DataTable
                  rows={comments as DocumentComment[]}
                  rowKey={(c) => c.id}
                  columns={[
                    { label: "Comment", render: (c) => c.comment },
                    { label: "Author", render: (c) => c.author },
                    { label: "Created", render: (c) => new Date(c.createdAt).toLocaleString("en-MY") },
                  ]}
                />
              )}
              <DataTable
                rows={audit.filter(
                  (a) => a.resource === data.doc.name || a.resource === documentId || a.resource === record?.id,
                )}
                rowKey={(a) => a.id}
                columns={[
                  { label: "Timestamp", render: (a) => new Date(a.timestamp).toLocaleString("en-MY") },
                  { label: "User", render: (a) => a.user },
                  { label: "Action", render: (a) => a.action.replaceAll("_", " ") },
                  { label: "Details", render: (a) => a.details },
                ]}
              />
            </Panel>
            </>
          )}
        </>
      )}
      <Toast message={toast} />
    </>
  );
}
