"use client";
import Link from "next/link";
import { CheckCircle2, Landmark, Sparkles, UploadCloud } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { Badge, Modal, PageHeader, Panel, SearchInput } from "@/components/common/ui";
import { useResource } from "@/features/common/hooks/use-resource";
import {
  confirmBankImport,
  decideMatch,
  discardBankImportPreview,
  inspectBankStatement,
  getMoneyTransactions,
  previewBankImport,
  getReconciliationPeriods,
  createReconciliationPeriod,
  completeReconciliationPeriod,
  type BankImportPreview,
} from "@/lib/api/money.service";
import type { BankReconciliationCandidate, BankMoneyTransaction, User } from "@/types";
import { decimalDisplay as money } from "@/lib/api/document-contract";

type ImportColumn = "posted_date" | "value_date" | "description" | "reference" | "debit" | "credit" | "currency";
const IMPORT_COLUMNS: Array<{ key: ImportColumn; label: string; required: boolean }> = [
  { key: "posted_date", label: "Posted date", required: true },
  { key: "value_date", label: "Value date", required: false },
  { key: "description", label: "Description", required: true },
  { key: "reference", label: "Reference", required: false },
  { key: "debit", label: "Debit (Money Out)", required: true },
  { key: "credit", label: "Credit (Money In)", required: true },
  { key: "currency", label: "Currency", required: false },
];

function csvHeaders(text: string): string[] {
  const firstLine = text.replace(/^\uFEFF/, "").split(/\r?\n/, 1)[0] ?? "";
  const headers: string[] = [];
  let value = "";
  let quoted = false;
  for (let i = 0; i < firstLine.length; i += 1) {
    const char = firstLine[i];
    if (char === '"' && quoted && firstLine[i + 1] === '"') { value += '"'; i += 1; }
    else if (char === '"') quoted = !quoted;
    else if (char === "," && !quoted) { headers.push(value.trim()); value = ""; }
    else value += char;
  }
  headers.push(value.trim());
  if (quoted || headers.some((header) => !header) || new Set(headers).size !== headers.length) {
    throw new Error("The CSV needs one header row with unique, non-empty column names.");
  }
  return headers;
}

function detectDateFormat(text: string, dateHeader: string): "iso" | "dmy" | "mdy" | "" {
  const lines = text.replace(/^\uFEFF/, "").split(/\r?\n/).filter(Boolean);
  if (lines.length < 2) return "";
  const headers = lines[0].split(",").map((item) => item.trim().replace(/^"|"$/g, ""));
  const dateIndex = headers.indexOf(dateHeader);
  if (dateIndex < 0) return "";
  const value = (lines[1].split(",")[dateIndex] ?? "").replace(/^"|"$/g, "").trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return "iso";
  if (/^\d{1,2}\/\d{1,2}\/\d{4}$/.test(value)) return Number(value.split("/")[0]) > 12 ? "dmy" : "dmy";
  return "";
}

function guessColumn(headers: string[], key: ImportColumn): string {
  const normalized = (header: string) => header.toLowerCase().replace(/[^a-z]/g, "");
  const matches: Record<ImportColumn, string[]> = {
    posted_date: ["date", "transactiondate", "posteddate", "bookingdate"],
    value_date: ["valuedate"],
    description: ["description", "details", "narrative", "transactiondetails", "payee", "merchant"],
    reference: ["reference", "ref", "transactionreference"],
    debit: ["debit", "withdrawal", "moneyout"],
    credit: ["credit", "deposit", "moneyin"],
    currency: ["currency", "ccy"],
  };
  return headers.find((header) => matches[key].includes(normalized(header))) ?? "";
}

function headerMatchesField(header: string, key: ImportColumn): boolean {
  const value = header.toLowerCase().replace(/[^a-z]/g, "");
  const terms: Record<ImportColumn, string[]> = {
    posted_date: ["date", "transactiondate", "posteddate", "bookingdate"],
    value_date: ["valuedate"],
    description: ["description", "details", "narrative", "transactiondetails", "payee", "merchant"],
    reference: ["reference", "ref", "transactionreference"],
    debit: ["debit", "withdrawal", "moneyout", "payment", "amount"],
    credit: ["credit", "deposit", "moneyin", "receipt", "amount"],
    currency: ["currency", "ccy"],
  };
  return terms[key].some((term) => value === term || value.includes(term));
}

function autoMapColumns(headers: string[]): Record<ImportColumn, string> {
  const result = {} as Record<ImportColumn, string>;
  const used = new Set<string>();
  for (const { key } of IMPORT_COLUMNS) {
    const candidates = headers.filter((header) => headerMatchesField(header, key));
    const match = guessColumn(headers, key) || (candidates.length === 1 ? candidates[0] : "");
    result[key] = match && !used.has(match) ? match : "";
    if (result[key]) used.add(result[key]);
  }
  const amountHeader = headers.find((header) => /^(amount|value|transactionamount)$/i.test(header.replace(/[^a-z]/gi, "")));
  if (amountHeader) {
    // A signed Amount column is intentionally mapped to both semantic sides;
    // the backend then uses the sign to populate exactly one side per row.
    result.debit = amountHeader;
    result.credit = amountHeader;
  }
  return result;
}

export function ReconciliationPage({ user }: { user: User }) {
  const base = user.role === "LOCAL_ADMIN" ? "/company" : "/workspace";
  const fileRef = useRef<HTMLInputElement>(null);
  const { data, loading, error, refresh } = useResource(
    () => Promise.all([getMoneyTransactions(user, "money_in"), getMoneyTransactions(user, "money_out")]),
    "reconciliation",
  );
  const { data: periods, refresh: refreshPeriods } = useResource(() => getReconciliationPeriods(), "bank-reconciliation-periods");
  const [busy, setBusy] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importHeaders, setImportHeaders] = useState<string[]>([]);
  const [importMapping, setImportMapping] = useState<Record<ImportColumn, string>>({
    posted_date: "", value_date: "", description: "", reference: "", debit: "", credit: "", currency: "",
  });
  const [accountRef, setAccountRef] = useState("");
  const [dateFormat, setDateFormat] = useState<"" | "iso" | "dmy" | "mdy">("");
  const [defaultCurrency, setDefaultCurrency] = useState("MYR");
  const [importPreview, setImportPreview] = useState<BankImportPreview | null>(null);
  const [periodForm, setPeriodForm] = useState({ bankAccountRef: "", startsOn: "", endsOn: "", openingBalance: "", closingBalance: "", bookBalance: "", statementAdjustment: "0", bookAdjustment: "0", adjustmentEvidence: "" });
  const [activeTab, setActiveTab] = useState<"transactions" | "period">("transactions");
  const [transactionSearch, setTransactionSearch] = useState("");
  const [transactionView, setTransactionView] = useState<"all" | "money_in" | "money_out">("all");
  const [accountFilter, setAccountFilter] = useState("");
  const [periodFilter, setPeriodFilter] = useState("");
  const [selectedRow, setSelectedRow] = useState<BankMoneyTransaction | null>(null);
  const [reviewRow, setReviewRow] = useState<BankMoneyTransaction | null>(null);
  // Show every confirmed bank row. Rows without an exact candidate still need
  // to be visible so the user can see what was imported and review it.
  const rows = useMemo(() => (data ?? []).flat(), [data]);
  const visibleRows = useMemo(() => rows.filter((row) => {
    const text = `${row.description} ${row.reference ?? ""} ${row.postedDate}`.toLowerCase();
    return (!transactionSearch || text.includes(transactionSearch.toLowerCase())) && (transactionView === "all" || row.direction === transactionView) && (!accountFilter || row.bankAccountRef === accountFilter) && (!periodFilter || (row.postedDate ? row.postedDate.startsWith(periodFilter) : false));
  }), [rows, transactionSearch, transactionView, accountFilter, periodFilter]);
  const totals = useMemo(() => visibleRows.reduce((result, row) => {
    const currency = row.currency ?? "MYR";
    const bucket = result[currency] ?? { moneyIn: 0, moneyOut: 0 };
    const amount = Number(row.amount ?? 0);
    if (row.direction === "money_in") bucket.moneyIn += amount; else bucket.moneyOut += amount;
    result[currency] = bucket;
    return result;
  }, {} as Record<string, { moneyIn: number; moneyOut: number }>), [visibleRows]);
  const primaryCurrency = Object.keys(totals)[0] ?? "MYR";
  const primaryTotals = totals[primaryCurrency] ?? { moneyIn: 0, moneyOut: 0 };
  const reviewCandidate = reviewRow?.candidates?.find((candidate) => candidate.status === "proposed")
    ?? reviewRow?.candidates?.find((candidate) => candidate.status === "confirmed")
    ?? reviewRow?.candidates?.[0]
    ?? null;

  async function onImport(file: File | undefined) {
    if (!file) return;
    setActionError("");
    if (!/\.(csv|xlsx|pdf)$/i.test(file.name)) {
      setActionError("Choose a CSV, Excel (.xlsx), or PDF bank statement.");
      return;
    }
    try {
      let headers: string[];
      let detectedDate = "" as "" | "iso" | "dmy" | "mdy";
      if (/\.csv$/i.test(file.name)) {
        const text = await file.text();
        headers = csvHeaders(text);
        const detectedDateHeader = headers.find((header) => headerMatchesField(header, "posted_date"));
        detectedDate = detectedDateHeader ? detectDateFormat(text, detectedDateHeader) : "";
      } else {
        const inspected = await inspectBankStatement(file);
        headers = inspected.headers;
      }
      setImportFile(file);
      setImportHeaders(headers);
      setImportMapping(autoMapColumns(headers));
      setImportPreview(null);
      setAccountRef("");
      setDateFormat(detectedDate);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Unable to inspect the bank statement.");
    }
  }

  async function prepareImport() {
    if (!importFile || !dateFormat || !accountRef.trim()) return;
    setBusy(true);
    setActionError("");
    try {
      const effectiveMapping = { ...importMapping };
      for (const { key } of IMPORT_COLUMNS) {
        if (!effectiveMapping[key]) {
          const candidates = importHeaders.filter((header) => headerMatchesField(header, key));
          if (candidates.length === 1) effectiveMapping[key] = candidates[0];
        }
      }
      const amountHeader = importHeaders.find((header) => /^(amount|value|transactionamount)$/i.test(header.replace(/[^a-z]/gi, "")));
      if (amountHeader) {
        // A single signed Amount column is one source field for both sides.
        // The backend uses its sign to populate exactly one side per row.
        effectiveMapping.debit = amountHeader;
        effectiveMapping.credit = amountHeader;
      }
      const missing = IMPORT_COLUMNS.filter(({ key, required }) => required && !effectiveMapping[key]).map(({ label }) => label);
      if (missing.length) throw new Error(`Choose a column for: ${missing.join(", ")}.`);
      const mapping = Object.fromEntries(Object.entries(effectiveMapping).filter(([, header]) => header)) as Record<string, string>;
      setImportPreview(await previewBankImport(user, importFile, accountRef.trim(), mapping, dateFormat, defaultCurrency));
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Unable to preview the bank statement.");
    } finally {
      setBusy(false);
    }
  }

  async function finishImport(confirm: boolean) {
    if (!importPreview) return;
    setBusy(true);
    setActionError("");
    try {
      if (confirm) {
        const result = await confirmBankImport(user, importPreview.importId);
        setNotice(`Imported ${result.rowCount} bank rows. Credits went to Money In and debits to Money Out. ${result.candidatesProposed} exact matches need review.`);
      } else {
        await discardBankImportPreview(user, importPreview.importId);
        setNotice("Bank import preview discarded.");
      }
      setImportFile(null);
      setImportPreview(null);
      await refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Unable to complete the bank import.");
    } finally {
      setBusy(false);
    }
  }

  async function decide(row: BankMoneyTransaction, candidate: BankReconciliationCandidate, decision: "confirm" | "reject") {
    setBusyId(candidate.id); setNotice(""); setActionError("");
    try {
      await decideMatch(user, candidate.id, decision, candidate.revision, decision === "reject" ? "Rejected during reconciliation review" : undefined);
      setNotice(decision === "confirm" ? "Match confirmed." : "Candidate rejected.");
      await refresh();
      setReviewRow(null);
      setSelectedRow(null);
    } catch (e) { setActionError(e instanceof Error ? e.message : "Unable to save reconciliation decision."); }
    finally { setBusyId(null); }
  }

  async function createPeriod() {
    setBusy(true); setActionError("");
    try { await createReconciliationPeriod(periodForm); setNotice("Reconciliation period created."); setPeriodForm({ bankAccountRef: "", startsOn: "", endsOn: "", openingBalance: "", closingBalance: "", bookBalance: "", statementAdjustment: "0", bookAdjustment: "0", adjustmentEvidence: "" }); await refreshPeriods(); }
    catch (e) { setActionError(e instanceof Error ? e.message : "Unable to create reconciliation period."); }
    finally { setBusy(false); }
  }

  return <div className="reconciliation-page">
    <PageHeader
      eyebrow="BANKING"
      title="Bank reconciliation"
      description="Review and match imported bank transactions to your accounting records."
      actions={
        <>
          <input ref={fileRef} hidden type="file" accept=".csv,.xlsx,.pdf,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/pdf" onChange={(e) => { void onImport(e.target.files?.[0]); e.target.value = ""; }} />
          <button className="btn primary" type="button" disabled={busy} onClick={() => fileRef.current?.click()}><UploadCloud size={16} /> Upload bank statement</button>
        </>
      }
    />
    <div className="bank-reconciliation-toolbar">
      <label><span>Account</span><select value={accountFilter} onChange={(event) => setAccountFilter(event.target.value)}><option value="">All bank accounts</option>{[...new Set(rows.map((row) => row.bankAccountRef).filter(Boolean))].map((account) => <option key={String(account)} value={String(account)}>{account}</option>)}</select></label>
      <label><span>Period</span><select value={periodFilter} onChange={(event) => setPeriodFilter(event.target.value)}><option value="">All periods</option>{[...new Set(rows.map((row) => row.postedDate ? row.postedDate.slice(0, 7) : "").filter(Boolean))].map((period) => <option key={period} value={period}>{period}</option>)}</select></label>
    </div>
    <div className="bank-reconciliation-tabs"><button className={activeTab === "transactions" ? "active" : ""} onClick={() => setActiveTab("transactions")}>Transactions</button><button className={activeTab === "period" ? "active" : ""} onClick={() => setActiveTab("period")}>Period reconciliation</button></div>
    {activeTab === "transactions" && <div className="bank-reconciliation-summary">
      <div><span>Money In</span><strong>{money(primaryTotals.moneyIn.toFixed(2), primaryCurrency)}</strong></div>
      <div><span>Money Out</span><strong>{money(primaryTotals.moneyOut.toFixed(2), primaryCurrency)}</strong></div>
      <div><span>Net movement</span><strong>{money((primaryTotals.moneyIn - primaryTotals.moneyOut).toFixed(2), primaryCurrency)}</strong></div>
    </div>}
    {notice && <div className="notice success" role="status">{notice}</div>}
    {actionError && <p className="error-text" role="alert">{actionError}</p>}
    {activeTab === "period" && <Panel title="Period reconciliation" subtitle="Enter the statement balances for one bank account. The checks below determine whether sign-off is available.">
      <div className="period-form">
        <div className="period-form-section"><div className="period-form-heading"><span>Statement</span><small>Identify the account and statement dates</small></div><div className="period-form-grid period-form-grid-statement">
          {([['bankAccountRef','Bank account'],['startsOn','Start date'],['endsOn','End date'],['openingBalance','Opening balance'],['closingBalance','Statement closing']] as const).map(([key,label]) => <label key={key}>{label}<input type={key.includes('On') ? 'date' : 'text'} value={periodForm[key]} onChange={(e) => setPeriodForm((p) => ({ ...p, [key]: e.target.value }))} /></label>)}
        </div></div>
        <div className="period-form-section"><div className="period-form-heading"><span>Book comparison</span><small>Use the book balance at the statement closing date</small></div><div className="period-form-grid period-form-grid-balances">
          {([['bookBalance','Book balance'],['statementAdjustment','Statement adjustment'],['bookAdjustment','Book adjustment']] as const).map(([key,label]) => <label key={key}>{label}<input type="text" value={periodForm[key]} onChange={(e) => setPeriodForm((p) => ({ ...p, [key]: e.target.value }))} /></label>)}
        </div></div>
        <label className="period-evidence">Adjustment evidence<textarea value={periodForm.adjustmentEvidence} onChange={(e) => setPeriodForm((p) => ({ ...p, adjustmentEvidence:e.target.value }))} placeholder="Required for timing or other adjustments" /></label>
        <div className="period-form-actions"><button className="btn primary" type="button" disabled={busy || !periodForm.bankAccountRef || !periodForm.startsOn || !periodForm.endsOn} onClick={() => void createPeriod()}>Create period</button></div>
      </div>
      {(periods ?? []).map((period) => <div key={period.id} className="reconciliation-card" style={{ marginTop:12 }}><div className="reconciliation-card-heading"><strong>{period.bankAccountRef} · {period.startsOn} to {period.endsOn}</strong><Badge>{period.status.toUpperCase()}</Badge></div><div className="reconciliation-card-grid"><div><span>Statement check</span><strong>{period.statementDifference} difference</strong></div><div><span>Adjusted balance check</span><strong>{period.unexplainedDifference} unexplained</strong></div><div><span>Unaccounted lines</span><strong>{period.unaccountedLines}</strong></div></div>{period.status === 'open' && <button className="btn primary" type="button" disabled={busy || period.statementDifference !== '0.00' || period.unexplainedDifference !== '0.00' || period.unaccountedLines !== 0} onClick={async () => { setBusy(true); try { await completeReconciliationPeriod(period.id); await refreshPeriods(); setNotice('Reconciliation completed.'); } catch (e) { setActionError(e instanceof Error ? e.message : 'Unable to complete reconciliation.'); } finally { setBusy(false); } }}>Complete reconciliation</button>}</div>)}
    </Panel>}
    {activeTab === "transactions" && <Panel title="Transactions" subtitle="Imported from bank statements. Review and reconcile each transaction.">
      <div className="bank-transaction-toolbar"><SearchInput value={transactionSearch} onChange={setTransactionSearch} placeholder="Search transactions" /><div className="bank-transaction-filters">{([['all','All'],['money_in','Money In'],['money_out','Money Out']] as const).map(([value,label]) => <button key={value} className={transactionView === value ? "active" : ""} onClick={() => setTransactionView(value)}>{label}</button>)}</div><span className="bank-transaction-count">{visibleRows.length} transactions</span></div>
      <div className="bank-transaction-workspace"><div className="bank-transaction-list">{visibleRows.map((row) => <button className={`bank-transaction-row ${selectedRow?.id === row.id ? "selected" : ""}`} key={row.id} onClick={() => setSelectedRow(row)}><span>{row.postedDate}</span><strong>{row.description || "Bank transaction"}</strong><b className={row.direction === "money_in" ? "in" : "out"}>{row.direction === "money_in" ? "+" : "-"}{money(row.amount, row.currency)}</b><Badge>{row.status === "needs_review" ? "NEEDS REVIEW" : row.status === "matched" ? "MATCHED" : "UNRECONCILED"}</Badge></button>)}</div><aside className="bank-transaction-detail">{selectedRow ? <><div className="bank-detail-heading"><h3>{selectedRow.description || "Bank transaction"}</h3><Badge>{selectedRow.direction === "money_in" ? "MONEY IN" : "MONEY OUT"}</Badge></div><p>{selectedRow.postedDate} · {selectedRow.reference || "No reference"}</p><div className="bank-detail-amount"><span>Amount</span><strong>{money(selectedRow.amount, selectedRow.currency)}</strong></div><div className="bank-detail-actions"><button className="btn primary" onClick={() => setReviewRow(selectedRow)}>Open review</button><span>{selectedRow.status === "matched" ? "Matched" : "Awaiting transaction decision"}</span></div></> : <div className="bank-detail-empty"><Landmark size={28} /><strong>Select a transaction</strong><span>Choose a bank line to review its amount, reference, and matching options.</span></div>}</aside></div>
    </Panel>}
    {false && <>
    <div className="reconciliation-stats">
      <div><span>Needs review</span><strong>{rows.length}</strong><small>Proposed matches</small></div>
      <div><span>Banking workflow</span><strong><CheckCircle2 size={22} /> Ready</strong><small>Confirm or reject each match</small></div>
      <div><span>Next step</span><strong><Landmark size={22} /> Upload</strong><small>Bring in a bank statement</small></div>
    </div>
    <Panel title="Transactions to review" subtitle={user.role === "LOCAL_ADMIN" ? "All bank statement lines uploaded for this company." : "Review each bank line and confirm a match only when the bank row and record agree."}>
      {loading && <p className="loading-state">Loading reconciliation candidates…</p>}
      {error && <p className="error-text">{error}</p>}
      {!loading && !error && rows.length === 0 && <div className="reconciliation-empty"><div className="reconciliation-empty-icon"><Sparkles size={24} /></div><h3>No matches need review yet</h3><p>Upload a bank statement to compare transactions against standardised records.</p></div>}
      {!loading && rows.length > 0 && <div className="reconciliation-list">
        {rows.map((row) => {
          const candidates = (row.candidates ?? []).filter((candidate) => candidate.status === "proposed");
          if (candidates.length === 0) return <div className="reconciliation-card" key={row.id}>
            <div className="reconciliation-card-heading"><div><strong>{row.description || "Bank transaction"}</strong><span>{row.postedDate} · {row.direction === "money_in" ? "Money In" : "Money Out"}</span></div><Badge>{row.status === "needs_review" ? "NEEDS REVIEW" : row.status === "matched" ? "MATCHED" : "UNRECONCILED"}</Badge></div>
            <div className="reconciliation-card-grid"><div><span>Bank amount</span><strong>{row.currency ?? ""} {row.amount ?? "—"}</strong></div><div><span>Reference</span><strong>{row.reference || "—"}</strong></div><div><span>Import status</span><strong>{row.status === "needs_review" ? "Needs review" : "Awaiting transaction decision"}</strong></div></div>
          </div>;
          return candidates.map((candidate) => <div className="reconciliation-card" key={candidate.id}>
          <div className="reconciliation-card-heading"><div><strong>{row.description || "Bank transaction"}</strong><span>{row.postedDate} · {row.direction === "money_in" ? "Money In" : "Money Out"}</span></div><Badge>PROPOSED</Badge></div>
          <div className="reconciliation-card-grid">
            <div><span>Bank amount</span><strong>{row.currency ?? ""} {row.amount ?? "—"}</strong></div>
            <div><span>Record</span><strong>{candidate.recordId ? <Link href={`${base}/records/${candidate.recordId}`}>{candidate.recordCode}</Link> : candidate.recordCode}</strong></div>
            <div><span>Original document</span><strong>{candidate.documentId ? <Link href={`${base}/documents/${candidate.documentId}`}>{candidate.documentCode || "Open document"}</Link> : "—"}</strong></div>
            <div><span>Candidate amount</span><strong>{candidate.currency ?? ""} {candidate.amount ?? "—"}</strong></div>
            <div><span>Reason</span><strong>{candidate.reasons.join(", ") || "Suggested match"}</strong></div>
          </div>
          <div className="reconciliation-actions">
            <button className="btn primary" disabled={busyId === candidate.id} onClick={() => void decide(row, candidate, "confirm")}>Confirm match</button>
            <button className="btn" disabled={busyId === candidate.id} onClick={() => void decide(row, candidate, "reject")}>Reject</button>
          </div>
        </div>);
        })}
      </div>}
    </Panel>
    </>}

    {reviewRow && (
      <Modal title="Review reconciliation match" onClose={() => setReviewRow(null)}>
        <div className="panel-body reconciliation-review-modal">
          <div className="reconciliation-review-summary">
            <div>
              <span>Bank transaction</span>
              <strong>{reviewRow.description || "Bank transaction"}</strong>
              <small>{reviewRow.postedDate} · {reviewRow.reference || "No reference"}</small>
            </div>
            <div>
              <span>{reviewRow.direction === "money_in" ? "Money In" : "Money Out"}</span>
              <strong>{money(reviewRow.amount, reviewRow.currency)}</strong>
              <small>{reviewRow.matchStatus === "matched" ? "Matched" : "Awaiting match decision"}</small>
            </div>
          </div>

          {reviewCandidate ? <section className="reconciliation-match-record">
            <div className="transaction-review-section-heading">
              <h3>Suggested standardised record</h3>
              <Badge>{reviewCandidate.status === "confirmed" ? "MATCHED" : "PROPOSED"}</Badge>
            </div>
            <div className="reconciliation-match-record-grid">
              <div><span>Record</span><strong>{reviewCandidate.recordCode}</strong></div>
              <div><span>Category</span><strong>{reviewCandidate.category.replace(/_/g, " ")}</strong></div>
              <div><span>Reference</span><strong>{reviewCandidate.reference || "—"}</strong></div>
              <div><span>Party</span><strong>{reviewCandidate.party || "—"}</strong></div>
              <div><span>Record amount</span><strong>{money(reviewCandidate.amount, reviewCandidate.currency)}</strong></div>
              <div><span>Difference</span><strong>{money(reviewCandidate.amountDelta, reviewCandidate.currency)}</strong></div>
              <div className="reconciliation-match-record-wide"><span>Match evidence</span><strong>{reviewCandidate.reasons.join(", ") || "Suggested match"}</strong></div>
            </div>
            <div className="reconciliation-review-links">
              <Link className="btn small" href={`${base}/records/${reviewCandidate.recordId}`}>View standardised record</Link>
              {reviewCandidate.documentId && <Link className="btn small" href={`${base}/documents/${reviewCandidate.documentId}`}>View original document</Link>}
            </div>
          </section> : <section className="reconciliation-match-record">
            <div className="transaction-review-section-heading"><h3>No suggested standardised record</h3><Badge>UNMATCHED</Badge></div>
            <p className="reconciliation-review-empty-copy">This bank row is available for review, but no approved standardised record has been suggested yet.</p>
          </section>}

          {reviewCandidate && reviewCandidate.status === "proposed" && <div className="reconciliation-review-actions">
            <button className="btn primary" type="button" disabled={busyId === reviewCandidate.id} onClick={() => void decide(reviewRow, reviewCandidate, "confirm")}>Confirm match</button>
            <button className="btn" type="button" disabled={busyId === reviewCandidate.id} onClick={() => void decide(reviewRow, reviewCandidate, "reject")}>Reject match</button>
          </div>}
        </div>
      </Modal>
    )}

    {importFile && (
      <Modal title={importPreview ? "Review bank statement import" : "Map bank statement columns"} onClose={() => {
        if (busy) return;
        if (importPreview) setActionError("Choose Confirm import or Discard preview to close this review.");
        else setImportFile(null);
      }}>
        <div className="panel-body" style={{ maxHeight: "70vh", overflowY: "auto" }}>
          {!importPreview ? <>
            <p>Upload a CSV, Excel, or PDF statement once. Credits are separated into Money In and debits into Money Out.</p>
            <p><strong>File:</strong> {importFile.name}</p>
            <label style={{ display: "block", margin: "12px 0" }}>Bank account label
              <input value={accountRef} onChange={(event) => setAccountRef(event.target.value)} maxLength={64} placeholder="Main current account" style={{ display: "block", width: "100%", marginTop: 4 }} />
            </label>
            <label style={{ display: "block", margin: "12px 0" }}>Date format
              <select value={dateFormat} onChange={(event) => setDateFormat(event.target.value as typeof dateFormat)} style={{ display: "block", width: "100%", marginTop: 4 }}>
                <option value="">Choose date format</option><option value="iso">YYYY-MM-DD</option><option value="dmy">DD/MM/YYYY</option><option value="mdy">MM/DD/YYYY</option>
              </select>
            </label>
            <label style={{ display: "block", margin: "12px 0" }}>Default currency
              <input value={defaultCurrency} onChange={(event) => setDefaultCurrency(event.target.value.toUpperCase())} maxLength={3} placeholder="MYR" style={{ display: "block", width: "100px", marginTop: 4 }} />
            </label>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 10 }}>
              {IMPORT_COLUMNS.map(({ key, label, required }) => {
                const availableHeaders = importHeaders.filter((header) => headerMatchesField(header, key));
                const selected = importMapping[key];
                const singleHeader = availableHeaders.length === 1 ? availableHeaders[0] : "";
                const displayValue = selected || singleHeader || (!required ? "Not mapped" : "Choose column");
                return <label key={key}>{label}{required ? " *" : " (optional)"}
                  {availableHeaders.length <= 1 ? <input value={displayValue} readOnly aria-label={`${label} detected column`} style={{ display: "block", width: "100%", marginTop: 4, background: "#f5f8fc" }} /> :
                    <select value={selected} onChange={(event) => setImportMapping((current) => ({ ...current, [key]: event.target.value }))} style={{ display: "block", width: "100%", marginTop: 4 }}>
                      <option value="">{required ? "Choose column" : "Not mapped"}</option>
                      {availableHeaders.map((header) => <option key={header} value={header} disabled={Object.entries(importMapping).some(([otherKey, value]) => otherKey !== key && value === header && !((key === "debit" || key === "credit") && (otherKey === "debit" || otherKey === "credit")))}>{header}</option>)}
                    </select>}
                </label>;
              })}
            </div>
            <div className="modal-actions" style={{ marginTop: 18 }}>
              <button className="btn" type="button" disabled={busy} onClick={() => setImportFile(null)}>Cancel</button>
              <button className="btn primary" type="button" disabled={busy || !accountRef.trim() || !dateFormat} onClick={() => void prepareImport()}>{busy ? "Reading statement…" : "Preview rows"}</button>
            </div>
          </> : <>
            <p>{importPreview.rowCount} new rows; {importPreview.duplicateRowsSkipped} duplicates skipped; {importPreview.needsReviewCount} rows need review. Confirming separates the rows into Money In and Money Out.</p>
            {importPreview.sampleRows.length > 0 && <div className="bank-import-preview-scroll"><table className="bank-import-preview-table"><thead><tr><th>Row</th><th>Date</th><th>Description</th><th>Debit</th><th>Credit</th><th>Currency</th><th>Goes to</th></tr></thead><tbody>
              {importPreview.sampleRows.map((row) => <tr key={row.sourceRow}><td>{row.sourceRow}</td><td>{row.postedDate ?? "—"}</td><td>{row.description || "—"}</td><td>{row.debit ?? "—"}</td><td>{row.credit ?? "—"}</td><td>{row.currency ?? "—"}</td><td>{row.direction === "money_in" ? "Money In" : "Money Out"}</td></tr>)}
            </tbody></table></div>}
            <div className="modal-actions" style={{ marginTop: 18 }}>
              <button className="btn" type="button" disabled={busy} onClick={() => void finishImport(false)}>Discard preview</button>
              <button className="btn primary" type="button" disabled={busy || !importPreview.canConfirm} onClick={() => void finishImport(true)}>{busy ? "Importing…" : "Confirm import"}</button>
            </div>
          </>}
        </div>
      </Modal>
    )}
  </div>;
}
