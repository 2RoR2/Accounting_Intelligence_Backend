"use client";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  FileText,
  Plus,
  MoreHorizontal,
  UploadCloud,
  Camera,
  Trash2,
} from "lucide-react";
import type { DocumentKind, RecordCategory, User } from "@/types";
import { useResource } from "@/features/common/hooks/use-resource";
import {
  getDocuments,
  getDocument,
  uploadDocument,
} from "@/lib/api/document.service";
import { getProcessingJob } from "@/lib/api/processing.service";
import { ApiError } from "@/lib/api/client";
import {
  Badge,
  DataTable,
  ErrorState,
  Field,
  Filter,
  LoadingState,
  PageHeader,
  Panel,
  SearchInput,
  formatDate,
} from "@/components/common/ui";
import { CameraCapture } from "@/features/common/documents/camera-capture";
import { cancelAccountantBatch, createDocumentBatch } from "@/lib/api/workflow.service";

const statusLabel: Record<string, string> = {
  UPLOADED: "Uploaded",
  QUEUED: "Queued",
  PROCESSING: "Processing",
  EXTRACTED: "Needs Review",
  VALIDATING: "Validating",
  EXCEPTION: "Needs Review",
  CORRECTED: "Corrected",
  IN_REVIEW: "Needs Approval",
  VALIDATED: "Approved",
  STANDARDISED: "Standardised",
  COMPLETED: "Standardised",
  FAILED: "Failed",
};

function nextAction(status: string) {
  if (["UPLOADED", "QUEUED", "PROCESSING"].includes(status)) return "Processing in progress";
  if (["EXTRACTED", "EXCEPTION", "CORRECTED"].includes(status)) return "Review and correct fields";
  if (status === "IN_REVIEW") return "Awaiting approval";
  if (status === "VALIDATED") return "Create standardised record";
  if (["STANDARDISED", "COMPLETED"].includes(status)) return "Ready for export";
  if (status === "FAILED") return "Review error and retry";
  return "Open document";
}

export function DocumentList({ user, initialTab = "all" }: { user: User; initialTab?: string }) {
  const base = user.role === "LOCAL_ADMIN" ? "/company" : "/workspace";
  const searchParams = useSearchParams();
  const selectedTab = searchParams.get("tab") || initialTab;
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [date, setDate] = useState("");
  const [amount, setAmount] = useState("");
  const [business, setBusiness] = useState("");
  const [newMenuOpen, setNewMenuOpen] = useState(false);
  const { data, error, loading, refresh } = useResource(
    () => getDocuments(user, { q: search, status, date, amount, business }),
    `${user.id}:${search}:${status}:${date}:${amount}:${business}`,
  );
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (data?.some((document) => ["QUEUED", "PROCESSING"].includes(document.status))) refresh();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [data, refresh]);
  const visibleDocuments = useMemo(() => {
    if (!data) return [];
    const typeByTab: Record<string, string | undefined> = {
      "sales-invoices": "INVOICE",
      "purchase-bills": "BILL",
      receipts: "RECEIPT",
      "credit-notes": "CREDIT_NOTE",
      "debit-notes": "DEBIT_NOTE",
    };
    const sourceDocuments = data.filter((document) => !/\.json$/i.test(document.name));
    return typeByTab[selectedTab] ? sourceDocuments.filter((document) => document.type === typeByTab[selectedTab]) : sourceDocuments;
  }, [data, selectedTab]);
  const documentTabs = [["All Documents", "all"], ["Sales Invoices", "sales-invoices"], ["Purchase Bills", "purchase-bills"], ["Receipts", "receipts"], ["Credit Notes", "credit-notes"], ["Debit Notes", "debit-notes"]];
  if (loading) return <LoadingState variant="table" />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;
  return (
    <>
      <PageHeader
        title={
          user.role === "ACCOUNTANT" ? "My documents" : "Company documents"
        }
        description="Follow every document from upload through standardisation."
        actions={
          <div className="new-document-menu">
            <Link className="btn primary new-document-main" href={`${base}/upload`}>
              <Plus size={18} /> New
            </Link>
            <button
              type="button"
              className="btn primary new-document-more"
              aria-label="New document options"
              aria-expanded={newMenuOpen}
              onClick={() => setNewMenuOpen((open) => !open)}
            >
              <MoreHorizontal size={20} />
            </button>
            {newMenuOpen && (
              <div className="new-document-menu-popover">
                <Link href={`${base}/upload`} onClick={() => setNewMenuOpen(false)}>
                  <UploadCloud size={16} /> Upload document
                </Link>
                <Link href={`${base}/upload?source=camera`} onClick={() => setNewMenuOpen(false)}>
                  <Camera size={16} /> Use camera
                </Link>
              </div>
            )}
          </div>
        }
      />
      <Panel>
        <div className="workspace-tabs document-list-tabs" role="tablist" aria-label="Document categories">
          {documentTabs.map(([label, tab]) => <Link key={tab} className={selectedTab === tab ? "active" : ""} href={tab === "all" ? `${base}/documents` : `${base}/documents?tab=${tab}`} role="tab" aria-selected={selectedTab === tab}>{label}</Link>)}
        </div>
        <div className="filter-bar">
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search documents…"
          />
          <Filter
            label="Statuses"
            value={status}
            onChange={setStatus}
            options={[
              "UPLOADED",
              "QUEUED",
              "PROCESSING",
              "EXCEPTION",
              "CORRECTED",
              "IN_REVIEW",
              "VALIDATED",
              "STANDARDISED",
              "COMPLETED",
              "FAILED",
            ]}
          />
          <input aria-label="Business" value={business} onChange={(e) => setBusiness(e.target.value)} placeholder="Supplier or customer" />
          <input aria-label="Amount" type="number" min="0" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="Exact total" />
          <input
            aria-label="Uploaded on"
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
          />
        </div>
        <DataTable
          rows={visibleDocuments}
          rowKey={(d) => d.id}
          columns={[
            {
              label: "Document",
              render: (d) => (
                <Link
                  href={`${base}/documents/${d.id}`}
                  className="document-link"
                >
                  <span className="file-icon">
                    <FileText size={19} />
                  </span>
                  <span>
                    <strong>{d.name}</strong>
                    <small>{d.id}</small>
                  </span>
                </Link>
              ),
            },
            { label: "Type", render: (d) => d.type },
            { label: "Uploaded", render: (d) => formatDate(d.createdAt) },
            {
              label: "Processing",
              render: (d) => (d.status === "FAILED" ? "—" : `${d.seconds} sec`),
            },
            { label: "Status", render: (d) => <div><Badge>{statusLabel[d.status] ?? d.status}</Badge><small>{nextAction(d.status)}</small></div> },
            {
              label: "Action",
              render: (d) => (
                <Link className="text-link" href={`${base}/documents/${d.id}`}>
                  Review <ArrowRight size={14} />
                </Link>
              ),
            },
          ]}
        />
      </Panel>
    </>
  );
}
export function UploadPage({ user }: { user: User }) {
  const input = useRef<HTMLInputElement>(null);
  const searchParams = useSearchParams();
  const [files, setFiles] = useState<File[]>([]);
  const [kind, setKind] = useState<DocumentKind>("INVOICE");
  const [category, setCategory] = useState<RecordCategory>("ACCOUNTS_PAYABLE");
  const [dragging, setDragging] = useState(false);
  const [stage, setStage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [sending, setSending] = useState(false);
  const [camera, setCamera] = useState(() => searchParams.get("source") === "camera");
  const [batch, setBatch] = useState<{ id: string; name: string; status: string } | null>(null);
  const [completedDocumentId, setCompletedDocumentId] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);
  const base = user.role === "LOCAL_ADMIN" ? "/company" : "/workspace";
  const allowedExtensions = ["pdf", "png", "jpg", "jpeg"];
  const addFiles = (incoming: File[]) => {
    const invalid = incoming.find((file) => {
      const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
      return file.size === 0 || file.size > 20 * 1024 * 1024 || !allowedExtensions.includes(extension);
    });
    if (invalid) {
      const extension = invalid.name.split(".").pop()?.toLowerCase() ?? "file";
      setError(invalid.size === 0 ? `${invalid.name} is empty.` : invalid.size > 20 * 1024 * 1024 ? `${invalid.name} is larger than 20 MB.` : `.${extension} files are not supported.`);
      return;
    }
    setError("");
    setCompletedDocumentId(null);
    // Selecting a file only prepares the queue; processing progress starts when upload begins.
    setProgress(0);
    setFiles((current) => [...current, ...incoming.filter((file) => !current.some((item) => item.name === file.name && item.size === file.size))]);
  };
  async function start() {
    if (!files.length) {
      setError("Choose at least one file.");
      return;
    }
    setProgress(5);
    setBusy(true);
    setError("");
    try {
      const documentIds: string[] = [];
      let processingContinues = false;
      for (const file of files) {
        setStage(`Uploading ${file.name}`);
        setSending(true);
        const doc = await uploadDocument(user, file, "real", category, kind) as Awaited<ReturnType<typeof uploadDocument>> & { jobId?: string };
        setSending(false);
        documentIds.push(doc.id);
        setProgress(25);
        setCompletedDocumentId(doc.id);
        if (!doc.jobId) throw new Error(`The backend did not return a processing job for ${file.name}.`);
        let terminal = false;
        let consecutivePollErrors = 0;
        const pollingDeadline = Date.now() + 300_000;
        while (!terminal && Date.now() < pollingDeadline) {
          try {
            const job = await getProcessingJob(doc.jobId);
            consecutivePollErrors = 0;
            setStage(`${file.name} · ${job.status.toLowerCase()}`);
            const jobStatus = job.status.toUpperCase();
            const waiting = ["PENDING", "DISPATCHED", "QUEUED"].includes(jobStatus);
            setProgress(waiting ? 25 : jobStatus === "PROCESSING" ? 60 : ["SUCCEEDED", "FAILED", "CANCELLED"].includes(jobStatus) ? 100 : 25);
            terminal = ["SUCCEEDED", "FAILED", "CANCELLED"].includes(jobStatus);
            if (waiting) { processingContinues = true; break; }
            if (["FAILED", "CANCELLED"].includes(jobStatus)) {
              throw new Error(`PROCESSING_TERMINAL_FAILURE:${job.lastError || `${file.name} processing failed.`}`);
            }
          } catch (error) {
            if (error instanceof Error && error.message.startsWith("PROCESSING_TERMINAL_FAILURE:")) {
              throw new Error(error.message.slice("PROCESSING_TERMINAL_FAILURE:".length));
            }
            const status = error instanceof ApiError ? error.status : undefined;
            const transient = status === undefined || status === 429 || (status >= 500 && status <= 599) || error instanceof TypeError || error instanceof DOMException;
            if (!transient) throw error;
            consecutivePollErrors += 1;
            if (consecutivePollErrors >= 3) {
              throw new Error(`${file.name} processing status could not be refreshed after several attempts.`);
            }
            setStage(`${file.name} · reconnecting to processing status…`);
          }
          if (!terminal) {
            const remaining = pollingDeadline - Date.now();
            if (remaining <= 0) break;
            const backoff = consecutivePollErrors ? Math.min(4000, 1000 * 2 ** (consecutivePollErrors - 1)) : 1000;
            await new Promise((resolve) => window.setTimeout(resolve, Math.min(backoff, remaining)));
          }
        }
        if (!terminal) processingContinues = true;
        const actual = await getDocument(user, doc.id);
        setProgress(actual.status === "PROCESSING" ? 60 : actual.status === "QUEUED" ? 25 : 100);
        setCompletedDocumentId(doc.id);
      }
      if (documentIds.length > 1) setBatch(await createDocumentBatch(`${kind} upload`, documentIds));
      setStage(processingContinues ? "Processing continues in the backend. Open the document to follow progress; do not upload it again." : "Extraction finished. Open the document to review the real results.");
      if (!processingContinues) setProgress(100);
    } catch (e) {
      setError((e as Error).message);
      setProgress(0);
      setStage("");
    } finally {
      setSending(false);
      setBusy(false);
    }
  }
  return (
    <>
      <div className="upload-reference-page">
      <PageHeader
        title="Upload documents"
        description="Add your files. We’ll extract the details for your review."
      />
      <div className="upload-workflow-steps" aria-label="Upload workflow"><div className="is-active"><b>1</b><strong>Select files</strong></div><i /><div><b>2</b><strong>Confirm details</strong></div><i /><div><b>3</b><strong>Upload & process</strong></div></div>
      <div className="upload-layout">
        <Panel
          title="Add your documents"
          subtitle="Select invoices, bills, or receipts"
        >
          <div className="panel-body">
            {error && <ErrorState message={error} dismissible onClose={() => setError("")} />}
            <div className="upload-sources">
              <button type="button" className="btn" disabled={busy} onClick={() => input.current?.click()}>
                <UploadCloud size={16} /> Upload from device
              </button>
              <button type="button" className="btn primary" disabled={busy} onClick={() => setCamera(true)}>
                <Camera size={16} /> Use camera
              </button>
            </div>
            {camera ? (
              <CameraCapture
                onClose={() => setCamera(false)}
                onConfirm={(file) => {
                  addFiles([file]);
                  setCamera(false);
                  setError("");
                }}
              />
            ) : (
            <button
              type="button"
              className={`drop-zone ${dragging ? "dragging" : ""}`}
              tabIndex={0}
              aria-label="Upload area. Press Ctrl+V to paste a file or image."
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onPaste={(e) => {
                if (busy) return;
                const pastedFiles = Array.from(e.clipboardData.files);
                if (pastedFiles.length) {
                  e.preventDefault();
                  addFiles(pastedFiles);
                }
              }}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                if (!busy) addFiles(Array.from(e.dataTransfer.files));
              }}
              onClick={() => input.current?.click()}
              disabled={busy}
            >
              <span className="upload-icon">
                <UploadCloud size={30} />
              </span>
              <strong>Drag, drop, paste, or browse your documents</strong>
              <span>
                Press Ctrl+V for a copied image or file, or <b>browse files</b>
              </span>
              <small>
                PDF, PNG, JPG/JPEG · Maximum 20 MB per
                file
              </small>
            </button>
            )}
            {batch && <div className="notice success">Batch {batch.name} · {batch.status} <button type="button" className="btn small" onClick={() => cancelAccountantBatch(batch.id).then(() => setBatch({ ...batch, status: "CANCELLED" }))}>Cancel pending batch</button></div>}
            <input
              ref={input}
              hidden
              multiple
              type="file"
              accept=".pdf,.png,.jpg,.jpeg"
              onChange={(e) => addFiles(Array.from(e.target.files ?? []))}
            />
            {progress > 0 && <div className="upload-progress" role="status" aria-label={`Processing ${progress}%`}>
              <div className="upload-progress-title"><strong>Upload file</strong><span>{stage || "Select file type to start processing"}</span></div>
              <div className="upload-progress-file"><FileText size={28} /><div><strong>{files[0]?.name || "Accounting document"}</strong><small>{files[0] ? `${(files[0].size / 1024).toFixed(1)} KB` : "Document"} · {sending ? "Uploading" : "Uploaded"}</small></div><button type="button" className="upload-progress-delete" aria-label="Remove uploaded file" disabled={busy} onClick={() => setFiles([])}><Trash2 size={17} /></button></div>
              <div className="upload-progress-row"><div className="upload-progress-track"><span style={{ width: `${progress}%` }} /></div><span>{progress}%</span></div>
              <div className="upload-progress-type"><label htmlFor="upload-progress-kind">File type <b>*</b></label><select id="upload-progress-kind" value={kind} disabled={busy} onChange={(e) => setKind(e.target.value as DocumentKind)}><option value="INVOICE">Invoice</option><option value="BILL">Bill</option><option value="RECEIPT">Receipt</option><option value="CREDIT_NOTE">Credit note</option><option value="DEBIT_NOTE">Debit note</option></select></div>
              {completedDocumentId && <Link className="btn small" href={`${base}/documents/${completedDocumentId}`}>View extracted output</Link>}
            </div>}
            {!progress && files.length > 0 && <div className="upload-queue-header"><strong>Ready to upload</strong><span>{files.length} file{files.length === 1 ? "" : "s"}</span></div>}
            {!progress && files.map((file, i) => (
              <div className="selected-file" key={`${file.name}-${i}`}>
                <FileText size={19} />
                <div>
                  <strong>{file.name}</strong>
                  <small>{(file.size / 1024).toFixed(1)} KB</small>
                </div>
                <button
                  disabled={busy}
                  aria-label={`Remove ${file.name}`}
                  className="text-link"
                  onClick={() => setFiles(files.filter((_, j) => i !== j))}
                >
                  Remove
                </button>
              </div>
            ))}
            {!progress && <div className="upload-form-grid">
            <Field
              label="Document type"
              hint="Stored as a unique code for that type, such as INVOICE_100123, BILL_100123, or RECEIPT_100123."
            >
              <select
                value={kind}
                disabled={busy}
                onChange={(e) => setKind(e.target.value as DocumentKind)}
              >
                <option value="INVOICE">Invoice</option>
                <option value="BILL">Bill</option>
                <option value="RECEIPT">Receipt</option>
                <option value="CREDIT_NOTE">Credit note</option>
                <option value="DEBIT_NOTE">Debit note</option>
              </select>
            </Field>
            </div>}
            {!progress && <Field label="Accounting category" hint="Confirm AP/AR direction; source kind and payment method do not determine it.">
              <select value={category} disabled={busy} onChange={e => setCategory(e.target.value as RecordCategory)}>
                <option value="ACCOUNTS_PAYABLE">Accounts Payable | Received from supplier</option>
                <option value="ACCOUNTS_RECEIVABLE">Accounts Receivable | Sent to customer</option>
              </select>
            </Field>}

            {stage && !progress && (
              <div className="processing-banner" role="status">
                <span className="pulse-dot" />
                {stage} {busy ? `· ${files.length} file${files.length === 1 ? "" : "s"} in batch` : ""}
              </div>
            )}
            {!progress && <button
              className="btn primary full"
              disabled={busy || !files.length}
              onClick={start}
            >
              <UploadCloud size={17} />
              {busy ? "Processing documents…" : "Upload and process"}
            </button>}
          </div>
        </Panel>
        <Panel title="What happens next">
          <div className="panel-body upload-guide">
            {[
              ["01", "Upload", "Add your invoice, bill, or receipt."],
              ["02", "Extract", "Review the fields returned by the AI extraction service."],
              ["03", "Validate", "Review backend checks and extraction warnings."],
              ["04", "Standardise", "Resolve exceptions; validation and explicit human approval are required."],
            ].map(([n, t, d], i) => (
              <div key={n} className={`step-item${i === 0 ? " is-active" : ""}`}>
                <span className="step-dot" />
                <div className="step-body">
                  <strong>{t}</strong>
                  <p>{d}</p>
                </div>
              </div>
            ))}
          </div>
        </Panel>
      </div>
      <div className="upload-reference-note"><strong>For clearer results</strong><span>Keep all document edges visible.</span><span>Use sharp, well-lit images.</span><span>Upload complete documents such as full invoices or receipts.</span></div>
      </div>
    </>
  );
}
