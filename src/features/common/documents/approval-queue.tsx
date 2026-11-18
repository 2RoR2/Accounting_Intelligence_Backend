"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { CheckCircle2, RotateCcw, XCircle, ExternalLink, FileText } from "lucide-react";
import type { Extraction, User } from "@/types";
import { useResource } from "@/features/common/hooks/use-resource";
import { getDocuments, getExtraction, getValidationReport } from "@/lib/api/document.service";
import { canDecideReview } from "@/lib/permissions/document-review";
import { decimalDisplay } from "@/lib/api/document-contract";
import { decideDocumentReview, getDocumentReviews } from "@/lib/api/workflow.service";
import {
  Badge,
  ErrorState,
  Field,
  LoadingState,
  PageHeader,
  Panel,
  SearchInput,
  Toast,
  formatDate,
} from "@/components/common/ui";

type Change = { label: string; from: string; to: string };

export function ApprovalQueuePage({ user }: { user: User }) {
  const [search, setSearch] = useState("");
  const [note, setNote] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [message, setMessage] = useState("");

  const { data, error, loading, refresh } = useResource(async () => {
    const docs = await getDocuments(user, { status: "IN_REVIEW" });
    return Promise.all(docs.map(async (doc) => {
      const [extraction, reviews, validation] = await Promise.all([
        getExtraction(user, doc.id).catch(() => null),
        getDocumentReviews(doc.id).catch(() => null),
        getValidationReport(user, doc.id).catch(() => null),
      ]);
      return { doc, extraction, reviews, validation };
    }));
  }, `approval-queue:${user.id}`);
  useEffect(() => {
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void refresh(); }, 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function decide(documentId: string, decision: "approved" | "rejected" | "returned", revision: number) {
    setBusyId(documentId);
    setMessage("");
    try {
      await decideDocumentReview(documentId, decision, note.trim() || defaultNote(decision), revision);
      setMessage(decision === "approved" ? "Document approved." : decision === "returned" ? "Document returned for revalidation." : "Document rejected.");
      setNote("");
      await refresh();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Decision failed. Refresh and try again.");
    } finally {
      setBusyId(null);
    }
  }

  if (loading) return <LoadingState />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;

  const rows = data.filter(({ doc }) =>
    `${doc.code ?? ""} ${doc.name} ${doc.supplier ?? ""} ${doc.customer ?? ""}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );

  return (
    <div className="approval-queue-page">
      <PageHeader
        eyebrow="REVIEW"
        title="Approval queue"
        description="Each item shows the file the accountant edited and the changes they made."
      />
      {message && <Toast message={message} />}
      <Panel title="Waiting for approval" subtitle={`${rows.length} document${rows.length === 1 ? "" : "s"} in review`}>
        <div className="filter-bar">
          <SearchInput value={search} onChange={setSearch} placeholder="Search code, file, supplier, or customer…" />
        </div>
        <div className="panel-body" style={{ paddingTop: 0 }}>
          <Field label="Decision note · Optional (applied to the next action)">
            <textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="Add a short note for the audit trail…" rows={2} />
          </Field>
        </div>
        {rows.length === 0 ? (
          <div className="panel-body"><p>No documents are waiting for approval.</p></div>
        ) : (
          <div className="approval-list">
            {rows.map(({ doc, extraction, reviews, validation }) => {
              const changes = changeList(extraction);
              const submitted = reviews?.submissions?.[0];
              const revision = doc.contentRevision ?? extraction?.contentRevision ?? 1;
              const busy = busyId === doc.id;
              const canApprove = canDecideReview(user, doc, reviews) && validation?.validationStatus === "PASS" && validation.coverageComplete !== false;
              return (
                <article className="approval-card" key={doc.id}>
                  <header>
                    <div>
                      <strong>{doc.code || doc.name}</strong>
                      <Link href={`/company/documents/${doc.id}`}><FileText size={14} /> {doc.name}</Link>
                    </div>
                    <Badge>{(doc.kind || doc.type || "DOCUMENT").replaceAll("_", " ")}</Badge>
                  </header>
                  <dl>
                    <div><dt>File</dt><dd>{doc.name}</dd></div>
                    <div><dt>Party</dt><dd>{doc.supplier || doc.customer || "—"}</dd></div>
                    <div><dt>Amount</dt><dd>{decimalDisplay(doc.totalAmount ?? extraction?.fields.total?.value, extraction?.fields.currency?.value)}</dd></div>
                    <div><dt>Submitted</dt><dd>{submitted ? `${formatDate(submitted.createdAt)}${submitted.note ? ` · ${submitted.note}` : ""}` : formatDate(doc.createdAt)}</dd></div>
                  </dl>
                  <div className="approval-changes">
                    <h3>Changes made</h3>
                    {changes.length === 0 ? (
                      <p>No field or line edits were recorded. The accountant submitted the extracted values as they were.</p>
                    ) : (
                      <ul>
                        {changes.map((change, index) => (
                          <li key={`${index}-${change.label}`}>
                            <strong>{change.label}</strong>
                            <span>{change.from || "empty"}</span>
                            <em>→</em>
                            <span>{change.to || "empty"}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                  <div className="row-actions">
                    <button type="button" className="btn primary small" disabled={busy || !revision || !canApprove} onClick={() => decide(doc.id, "approved", revision)}><CheckCircle2 size={14} /> Approve</button>
                    <button type="button" className="btn small" disabled={busy} onClick={() => decide(doc.id, "returned", revision)}><RotateCcw size={14} /> Return</button>
                    <button type="button" className="btn small danger" disabled={busy} onClick={() => decide(doc.id, "rejected", revision)}><XCircle size={14} /> Reject</button>
                    <Link className="btn small" href={`/company/documents/${doc.id}`}><ExternalLink size={14} /> Open file</Link>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}

function changeList(extraction: Extraction | null): Change[] {
  if (!extraction) return [];
  const fieldChanges = Object.values(extraction.fields ?? {})
    .filter((field) => field.correctedBy)
    .map((field) => ({
      label: field.label || "Field",
      from: field.rawValue || field.normalizedValue || field.original || "",
      to: field.currentValue || field.value || "",
    }));
  const lineChanges = (extraction.lineCorrections ?? []).map((item) => ({
    label: `Line · ${item.field_key.replaceAll("_", " ")}`,
    from: item.old_value || "",
    to: item.new_value || "",
  }));
  return [...fieldChanges, ...lineChanges];
}

function defaultNote(decision: "approved" | "rejected" | "returned") {
  if (decision === "approved") return "Approved from the approval queue.";
  if (decision === "returned") return "Returned for revalidation from the approval queue.";
  return "Rejected from the approval queue.";
}
