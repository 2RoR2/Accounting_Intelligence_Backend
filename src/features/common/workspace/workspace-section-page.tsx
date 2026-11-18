"use client";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { AlertTriangle, Camera, CheckCircle2, Clock3, MoreHorizontal, Plus, RefreshCw, UploadCloud } from "lucide-react";
import type { User } from "@/types";
import { useResource } from "@/features/common/hooks/use-resource";
import { getDocuments, getExceptions, revalidateDocument, retryDocumentProcessing } from "@/lib/api/document.service";
import { getNotifications, markNotificationRead, getAuthSessions } from "@/lib/api/workflow.service";
import { getSettings, saveSettings } from "@/lib/api/settings.service";
import { Badge, DataTable, ErrorState, LoadingState, PageHeader, Panel } from "@/components/common/ui";

const titleFor = (value: string) => value.replace(/^view-/, "").split("-").filter(Boolean).map((word) => word[0].toUpperCase() + word.slice(1)).join(" ") || "Workspace";

export function WorkspaceSectionPage({ user, section }: { user: User; section: string }) {
  const routeView = section.replace(/^view-/, "");
  const searchParams = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const [inlineDocumentTab, setInlineDocumentTab] = useState<"all-documents" | "credit-notes" | "debit-notes">(
    requestedTab === "credit-notes" || requestedTab === "debit-notes" ? requestedTab : "all-documents",
  );
  useEffect(() => {
    if (requestedTab === "credit-notes" || requestedTab === "debit-notes") setInlineDocumentTab(requestedTab);
    else if (["documents", "all-documents"].includes(routeView)) setInlineDocumentTab("all-documents");
  }, [requestedTab, routeView]);
  const view = ["documents", "all-documents"].includes(routeView) ? inlineDocumentTab : routeView;
  const [busyId, setBusyId] = useState("");
  const [actionError, setActionError] = useState("");
  const [newMenuOpen, setNewMenuOpen] = useState(false);
  const [utilityTab, setUtilityTab] = useState("");
  const [settings, setSettings] = useState({ timezone: "Asia/Kuala_Lumpur", notifications: "all", sessionTimeoutMinutes: 30, defaultDocumentView: "all" as "all" | "exceptions" | "review", emailAssignmentNotifications: true, processingStatusNotifications: true, uploadMaxSizeMb: 20, allowCameraUploads: true });
  const [settingsMessage, setSettingsMessage] = useState("");
  const [noteSearch, setNoteSearch] = useState("");
  const [noteStatus, setNoteStatus] = useState("");
  const [noteCustomer, setNoteCustomer] = useState("");
  const [noteStart, setNoteStart] = useState("");
  const [noteEnd, setNoteEnd] = useState("");
  const [notificationsData, setNotificationsData] = useState<Awaited<ReturnType<typeof getNotifications>>>([]);
  const [sessionsData, setSessionsData] = useState<Awaited<ReturnType<typeof getAuthSessions>>>([]);
  const { data, error, loading, refresh } = useResource(
    async () => {
      const documents = await getDocuments(user);
      let exceptions = [] as Awaited<ReturnType<typeof getExceptions>>;
      try {
        exceptions = await getExceptions(user);
      } catch {
        // The document list remains useful when the exceptions service is unavailable.
      }
      return { documents, exceptions };
    },
    `${user.id}:${view}`,
  );
  const documents = useMemo(() => {
    if (!data) return [];
    if (["sales-invoices", "sales-records"].includes(view)) return data.documents.filter((item) => item.type === "INVOICE");
    if (["purchase-bills", "purchase-records"].includes(view)) return data.documents.filter((item) => item.type === "BILL");
    if (view === "receipts") return data.documents.filter((item) => item.type === "RECEIPT");
    if (["credit-notes", "credit-notes-applied", "credit-notes-outstanding"].includes(view)) return data.documents.filter((item) => item.type === "CREDIT_NOTE");
    if (["debit-notes", "debit-notes-applied", "debit-notes-outstanding"].includes(view)) return data.documents.filter((item) => item.type === "DEBIT_NOTE");
    if (view === "debit-notes") return data.documents.filter((item) => item.type === "DEBIT_NOTE");
    if (view === "processing-queue") return data.documents.filter((item) => ["QUEUED", "PROCESSING"].includes(item.status));
    if (view === "failed-processing") return data.documents.filter((item) => item.status === "FAILED");
    if (["my-review-queue", "pending-approval"].includes(view)) return data.documents.filter((item) => ["IN_REVIEW", "EXCEPTION"].includes(item.status));
    if (view === "returned-for-correction") return data.documents.filter((item) => item.status === "EXCEPTION");
    return data.documents;
  }, [data, view]);
  const exceptions = useMemo(() => {
    if (!data) return [];
    if (view === "blocking-errors") return data.exceptions.filter((item) => ["HIGH", "BLOCKING"].includes(String(item.severity).toUpperCase()));
    if (view === "warnings") return data.exceptions.filter((item) => String(item.severity).toUpperCase() === "MEDIUM");
    if (view === "resolved-exceptions") return data.exceptions.filter((item) => String(item.status).toUpperCase() === "RESOLVED");
    return data.exceptions;
  }, [data, view]);
  const notificationView = view === "notifications";
  const settingsView = view === "settings";
  useEffect(() => {
    if (notificationView) void getNotifications().then(setNotificationsData).catch(() => setNotificationsData([]));
    if (settingsView) { void getAuthSessions().then(setSessionsData).catch(() => setSessionsData([])); void getSettings(user).then(setSettings).catch(() => undefined); }
  }, [notificationView, settingsView]);
  if (loading) return <LoadingState variant="table" />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;
  const exceptionView = ["exceptions", "all-exceptions", "blocking-errors", "warnings", "resolved-exceptions"].includes(view);
  const documentView = !exceptionView;
  const creditNoteView = ["credit-notes", "credit-notes-applied", "credit-notes-outstanding"].includes(routeView);
  const debitNoteView = ["debit-notes", "debit-notes-applied", "debit-notes-outstanding"].includes(routeView);
  const noteView = creditNoteView || debitNoteView;
  const noteRows = documents.filter((row) =>
    (!noteSearch || `${row.name} ${row.customer ?? ""}`.toLowerCase().includes(noteSearch.toLowerCase())) &&
    (!noteCustomer || row.customer === noteCustomer) &&
    (!noteStatus || (noteStatus === "Outstanding" ? row.status !== "COMPLETED" : row.status === noteStatus.toUpperCase())) &&
    (!noteStart || row.createdAt.slice(0, 10) >= noteStart) && (!noteEnd || row.createdAt.slice(0, 10) <= noteEnd),
  );
  const rows = exceptionView ? exceptions : documents;
  const processingView = view === "processing-queue" || view === "failed-processing";
  async function processDocument(documentId: string, status: "EXTRACTED" | "QUEUED") {
    setBusyId(documentId);
    setActionError("");
    try {
      if (status === "EXTRACTED") await revalidateDocument(user, documentId);
      else await retryDocumentProcessing(user, documentId);
      refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Unable to update processing status.");
    } finally {
      setBusyId("");
    }
  }
  if (notificationView || settingsView) {
    const title = notificationView ? "Notifications" : "Settings";
    const description = notificationView ? "Review requests, failures, assignments and important system messages." : "Session/security settings where appropriate, upload policies and display preferences.";
    const tabs = notificationView ? ["Inbox", "Unread", "Preferences"] : ["General", "Security", "Document Settings"];
    const activeTab = utilityTab || tabs[0];
    const visibleNotifications = activeTab === "Unread" ? notificationsData.filter((item) => !item.read) : notificationsData;
    const persistSettings = async () => { setSettingsMessage(""); try { await saveSettings(user, settings); setSettingsMessage("Settings saved."); } catch (e) { setSettingsMessage(e instanceof Error ? e.message : "Unable to save settings."); } };
    return <><PageHeader title={title} description={description} /><Panel title={title}>
      <div className="workspace-tabs" role="tablist" aria-label={`${title} tabs`}>{tabs.map((tab) => <button type="button" className={activeTab === tab ? "active" : ""} key={tab} onClick={() => setUtilityTab(tab)}>{tab}</button>)}</div>
      {notificationView ? activeTab === "Preferences" ? <div className="empty-state"><strong>Notification preferences</strong><p>Notification preferences are managed in Settings.</p></div> : <DataTable rows={visibleNotifications} rowKey={(row) => row.id} columns={[{ label: "Message", render: (row) => <strong>{row.title}</strong> }, { label: "Details", render: (row) => row.message }, { label: "Status", render: (row) => <Badge>{row.read ? "Read" : "Unread"}</Badge> }, { label: "Action", render: (row) => row.read ? <span>—</span> : <button type="button" className="btn small" onClick={() => markNotificationRead(row.id).then(() => setNotificationsData((items) => items.map((item) => item.id === row.id ? { ...item, read: true } : item)))}>Mark as read</button> }]} /> : <><div className="settings-form-grid"><label>Session timeout<select value={settings.sessionTimeoutMinutes} onChange={(event) => setSettings((s) => ({ ...s, sessionTimeoutMinutes: Number(event.target.value) }))}><option value="15">15 minutes</option><option value="30">30 minutes</option><option value="60">60 minutes</option></select></label><label>Default document view<select value={settings.defaultDocumentView} onChange={(event) => setSettings((s) => ({ ...s, defaultDocumentView: event.target.value as "all" | "exceptions" | "review" }))}><option value="all">All Documents</option><option value="exceptions">Exceptions</option><option value="review">My Review Queue</option></select></label><label className="settings-toggle"><input type="checkbox" checked={settings.emailAssignmentNotifications} onChange={(event) => setSettings((s) => ({ ...s, emailAssignmentNotifications: event.target.checked }))} /> Email notifications for assignments</label><label className="settings-toggle"><input type="checkbox" checked={settings.processingStatusNotifications} onChange={(event) => setSettings((s) => ({ ...s, processingStatusNotifications: event.target.checked }))} /> Show processing status updates</label><button type="button" className="btn primary" onClick={() => void persistSettings()}>Save settings</button>{settingsMessage && <p role="status">{settingsMessage}</p>}</div><h3>Active sessions</h3><DataTable rows={sessionsData} rowKey={(row) => row.id} columns={[{ label: "Session", render: (row) => row.id.slice(0, 8) }, { label: "Expires", render: (row) => row.expiresAt }, { label: "Status", render: () => <Badge>Active</Badge> }]} /></>}
    </Panel></>;
  }
  const documentTabs = [
    ["All Documents", "all-documents"],
    ["Sales Invoices", "sales-invoices"],
    ["Purchase Bills", "purchase-bills"],
    ["Receipts", "receipts"],
    ["Credit Notes", "credit-notes"],
    ["Debit Notes", "debit-notes"],
  ];
  if (noteView) return <>
    <PageHeader title={creditNoteView ? "Credit Notes" : "Debit Notes"} description={creditNoteView ? "Credit notes issued to your customers." : "Debit notes issued to your customers."} actions={<div className="new-document-menu"><Link className="btn primary new-document-main" href="/workspace/upload"><Plus size={18} /> New</Link><button type="button" className="btn primary new-document-more" aria-label="New document options" onClick={() => setNewMenuOpen((open) => !open)}><MoreHorizontal size={20} /></button>{newMenuOpen && <div className="new-document-menu-popover"><Link href="/workspace/upload"><UploadCloud size={16} /> Upload document</Link><Link href="/workspace/upload?source=camera"><Camera size={16} /> Use camera</Link></div>}</div>} />
    <Panel title={`${creditNoteView ? "Credit Notes" : "Debit Notes"} (${rows.length})`}>
      <div className="workspace-tabs credit-note-tabs"><Link className={view === (creditNoteView ? "credit-notes" : "debit-notes") ? "active" : ""} href={`/workspace/documents/view-${creditNoteView ? "credit-notes" : "debit-notes"}`}>All</Link><Link className={view === (creditNoteView ? "credit-notes-applied" : "debit-notes-applied") ? "active" : ""} href={`/workspace/documents/view-${creditNoteView ? "credit-notes-applied" : "debit-notes-applied"}`}>Applied</Link><Link className={view === (creditNoteView ? "credit-notes-outstanding" : "debit-notes-outstanding") ? "active" : ""} href={`/workspace/documents/view-${creditNoteView ? "credit-notes-outstanding" : "debit-notes-outstanding"}`}>Outstanding</Link></div>
       <div className="credit-note-filter-grid"><label>Date Range<input type="date" value={noteStart} onChange={(e) => setNoteStart(e.target.value)} /><input type="date" value={noteEnd} onChange={(e) => setNoteEnd(e.target.value)} /></label><label>Search<input placeholder="Search" value={noteSearch} onChange={(e) => setNoteSearch(e.target.value)} /></label><label>Customer<select value={noteCustomer} onChange={(e) => setNoteCustomer(e.target.value)}><option value="">All</option>{[...new Set(documents.map((row) => row.customer).filter(Boolean))].map((customer) => <option key={customer} value={customer ?? ""}>{customer}</option>)}</select></label><label>Status<select value={noteStatus} onChange={(e) => setNoteStatus(e.target.value)}><option value="">All but Void</option><option value="Applied">Applied</option><option value="Outstanding">Outstanding</option></select></label><label>Email Tracker<select defaultValue=""><option>All</option><option>Sent</option><option>Not sent</option></select></label><label>Tag<select defaultValue=""><option>All</option></select></label><button className="btn primary" type="button" onClick={() => undefined}>Search</button></div>
      <DataTable rows={noteRows} rowKey={(row) => row.id} columns={[{ label: "No.", render: (row) => <strong>{row.name}</strong> }, { label: "Date", render: (row) => row.createdAt.slice(0, 10) }, { label: "Customer", render: (row) => row.customer ?? "—" }, { label: "Amount", render: (row) => `RM ${(row.totalAmount ?? 0).toLocaleString("en-MY", { minimumFractionDigits: 2 })}` }, { label: "Balance", render: () => "RM 0.00" }, { label: "Status", render: (row) => <Badge>{row.status}</Badge> }, { label: "Action", render: (row) => <Link className="btn small" href={`/workspace/documents/${row.id}`}>View</Link> }]} />
    </Panel>
  </>;
  return <>
    <PageHeader title={view === "all-documents" ? "All Documents" : view === "credit-notes" ? "Credit Notes" : view === "debit-notes" ? "Debit Notes" : view === "processing-queue" ? "Processing queue" : view === "failed-processing" ? "Failed processing" : titleFor(section)} description={processingView ? (view === "processing-queue" ? "Track documents moving through extraction and validation." : "Review documents that need another processing attempt.") : documentView ? "Manage your accounting document submissions and review their processing status." : `Current ${titleFor(section).toLowerCase()} data from the accounting workspace.`} actions={documentView ? <div className="new-document-menu">
      <Link className="btn primary new-document-main" href="/workspace/upload"><Plus size={18} /> New</Link>
      <button type="button" className="btn primary new-document-more" aria-label="New document options" aria-expanded={newMenuOpen} onClick={() => setNewMenuOpen((open) => !open)}><MoreHorizontal size={20} /></button>
      {newMenuOpen && <div className="new-document-menu-popover">
        <Link href="/workspace/upload" onClick={() => setNewMenuOpen(false)}><UploadCloud size={16} /> Upload document</Link>
        <Link href="/workspace/upload?source=camera" onClick={() => setNewMenuOpen(false)}><Camera size={16} /> Use camera</Link>
      </div>}
    </div> : undefined} />
    <Panel title={`${view === "all-documents" ? "All Documents" : view === "credit-notes" ? "Credit Notes" : view === "debit-notes" ? "Debit Notes" : titleFor(section)} (${rows.length})`}>
      {view === "processing-queue" && <div className="processing-summary">
        <div className="processing-summary-card">
          <span className="processing-summary-icon processing-summary-icon-progress"><Clock3 size={20} /></span>
          <div><span className="processing-summary-label">Active jobs</span><strong>{documents.length}</strong><span className="processing-summary-detail">Documents in progress</span></div>
        </div>
        <div className="processing-summary-card">
          <span className="processing-summary-icon processing-summary-icon-next"><CheckCircle2 size={20} /></span>
          <div><span className="processing-summary-label">Next step</span><strong>Validate</strong><span className="processing-summary-detail">Review extracted values when ready</span></div>
        </div>
      </div>}
      {view === "failed-processing" && <div className="processing-summary processing-summary-warning">
        <div className="processing-summary-card">
          <span className="processing-summary-icon processing-summary-icon-warning"><AlertTriangle size={20} /></span>
          <div><span className="processing-summary-label">Failed jobs</span><strong>{documents.length}</strong><span className="processing-summary-detail">Need attention</span></div>
        </div>
        <div className="processing-summary-card">
          <span className="processing-summary-icon processing-summary-icon-recovery"><RefreshCw size={20} /></span>
          <div><span className="processing-summary-label">Recommended action</span><strong>Re-detect</strong><span className="processing-summary-detail">Retry extraction from the original file</span></div>
        </div>
      </div>}
      {actionError && <div className="notice danger">{actionError}</div>}
      {documentView && <>
        <div className="workspace-tabs" role="tablist" aria-label="Document categories">
          {documentTabs.map(([label, slug]) => {
            const inlineTab = slug === "credit-notes" || slug === "debit-notes" || slug === "all-documents";
            return inlineTab ? (
              <button key={slug} type="button" className={view === slug ? "active" : ""} onClick={() => setInlineDocumentTab(slug as "all-documents" | "credit-notes" | "debit-notes")}>{label}</button>
            ) : (
              <Link key={slug} className={view === slug ? "active" : ""} href={`/workspace/documents/view-${slug}`}>{label}</Link>
            );
          })}
        </div>
        <div className="filter-bar workspace-filter-bar"><input aria-label="Start date" type="date" /><input aria-label="End date" type="date" /><select aria-label="Status" defaultValue=""><option value="">All statuses</option><option>COMPLETED</option><option>IN_REVIEW</option><option>EXCEPTION</option><option>PROCESSING</option><option>FAILED</option></select><button className="btn small" type="button">Search</button></div>
      </>}
      {exceptionView ? <DataTable rows={exceptions} rowKey={(row) => row.id} columns={[{ label: "Type", render: (row) => <strong>{row.type}</strong> }, { label: "Document", render: (row) => row.documentId }, { label: "Severity", render: (row) => <Badge>{row.severity}</Badge> }, { label: "Status", render: (row) => <Badge>{row.status}</Badge> }]} /> : <DataTable rows={documents} rowKey={(row) => row.id} columns={[{ label: "Reference", render: (row) => <strong>{row.name}</strong> }, { label: "Business", render: (row) => row.supplier ?? row.customer ?? "—" }, { label: "Type", render: (row) => row.type }, { label: "Status", render: (row) => <Badge>{row.status}</Badge> }, { label: "Action", render: (row) => view === "processing-queue" ? <button className="btn small" disabled={busyId === row.id} onClick={() => processDocument(row.id, "EXTRACTED")}>{busyId === row.id ? "Validating…" : "Validate"}</button> : view === "failed-processing" ? <button className="btn small" disabled={busyId === row.id} onClick={() => processDocument(row.id, "QUEUED")}>{busyId === row.id ? "Queuing…" : "Re-detect"}</button> : <Link className="btn small" href={`/workspace/documents/${row.id}`}>Review</Link> }]} />}
    </Panel>
  </>;
}
