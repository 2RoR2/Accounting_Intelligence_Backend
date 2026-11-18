"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { User } from "@/types";
import { createChartAccount, createJournal, getAccountingPeriods, getChartAccounts, getLedgerJournals, reverseJournal, type AccountingPeriod, type ChartAccount, type Journal, type LedgerAccount } from "@/lib/api/ledger.service";

type AccountingMode = "deemed" | "accounts" | "journals" | "contras" | "fixed-assets";
type LocalRow = { id: string; date: string; reference: string; description: string; amount: string; status: string };

const fallbackRows: LocalRow[] = [
  { id: "DP-00002", date: "31/08/2026", reference: "-", description: "Deemed payment", amount: "150.00", status: "Ready" },
  { id: "DP-00001", date: "28/02/2026", reference: "-", description: "Deemed payment", amount: "106.92", status: "Ready" },
];

const headingCopy: Record<AccountingMode, string> = {
  deemed: "Invoices subject to the SST 12-month deemed paid rule.",
  accounts: "A list of accounts in your company’s general ledger.",
  journals: "Manual adjustments made to your chart of accounts.",
  contras: "Contras between customers and suppliers.",
  "fixed-assets": "Depreciated until 31/10/2026.",
};

const titleFor = (mode: AccountingMode) => ({ deemed: "Deemed Payments", accounts: "Chart of Accounts", journals: "Journal Entries", contras: "Contras", "fixed-assets": "Fixed Assets" })[mode];

export function AccountingPage({ user, mode }: { user: User; mode: AccountingMode }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [tab, setTab] = useState("All");
  const [query, setQuery] = useState("");
  const [accounts, setAccounts] = useState<LedgerAccount[]>([]);
  const [journals, setJournals] = useState<Journal[]>([]);
  const [rows, setRows] = useState<LocalRow[]>(fallbackRows);
  const [loading, setLoading] = useState(mode === "accounts" || mode === "journals");
  const [dialog, setDialog] = useState<"new" | "view" | "reverse" | null>(null);
  const [selected, setSelected] = useState<LedgerAccount | Journal | LocalRow | null>(null);
  const [notice, setNotice] = useState("");
  const [form, setForm] = useState({ name: "", code: "", description: "", amount: "0", accountClass: "asset" });
  const base = user.role === "LOCAL_ADMIN" ? "/company" : "/workspace";
  const isAccounts = mode === "accounts";
  const isJournals = mode === "journals";
  const canManageJournals = user.role === "LOCAL_ADMIN" || user.role === "SUPER_ADMIN";
  const title = titleFor(mode);
  const isNew = searchParams.get("new") === "1";

  const notify = (message: string) => {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 3200);
  };

  useEffect(() => {
    let active = true;
    if (!isAccounts && !isJournals) return () => { active = false; };
    setLoading(true);
    const load = async () => {
      try {
        if (isAccounts) {
          const result = await getChartAccounts();
          if (active) setAccounts(result.map((account) => ({ id: account.id, code: account.code, name: account.name, accountType: account.accountClass, isActive: account.isActive })));
        } else {
          const result = await getLedgerJournals();
          if (active) setJournals(result);
        }
      } catch {
        if (active) {
          if (isAccounts) setAccounts([]);
          notify("Could not load Chart of Accounts from the backend. Check the API and database, then retry.");
        }
      } finally {
        if (active) setLoading(false);
      }
    };
    void load();
    return () => { active = false; };
  }, [isAccounts, isJournals]);

  const visibleAccounts = useMemo(() => {
    const source = accounts;
    const normalized = query.trim().toLowerCase();
    return source.filter((account) => {
      const tabMatches = tab === "All" || account.accountType.toLowerCase().includes(tab.toLowerCase().replace("liabilities", "liability"));
      const textMatches = !normalized || `${account.code} ${account.name} ${account.accountType}`.toLowerCase().includes(normalized);
      return tabMatches && textMatches;
    });
  }, [accounts, query, tab]);

  const visibleJournals = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return (journals.length ? journals : []).filter((journal) => `${journal.id} ${journal.description} ${journal.sourceType}`.toLowerCase().includes(normalized));
  }, [journals, query]);

  const visibleLocalRows = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return rows.filter((row) => `${row.id} ${row.description} ${row.reference}`.toLowerCase().includes(normalized));
  }, [query, rows]);

  const openNew = () => {
    if (isJournals && !canManageJournals) {
      notify("Journal entries are read-only for your role. Ask an administrator for posting access.");
      return;
    }
    setForm({ name: "", code: "", description: "", amount: "0", accountClass: "asset" });
    const route = mode === "journals" ? "accounting-journal-entries" : mode === "contras" ? "accounting-contras" : mode === "accounts" ? "accounting-chart" : `accounting-${mode}`;
    router.push(`${base}/${route}?new=1`);
  };

  const saveDraft = async (event: React.FormEvent) => {
    event.preventDefault();
    const id = form.code || `${mode === "journals" ? "JE" : "DRAFT"}-${String(Date.now()).slice(-5)}`;
    if (isAccounts) {
      try {
        const created = await createChartAccount({ code: form.code || id, name: form.name || "New account", accountClass: form.accountClass });
        setAccounts((current) => [...current, { id: created.id, code: created.code, name: created.name, accountType: created.accountClass }]);
      } catch (error) {
        notify(error instanceof Error ? error.message : "The account could not be saved.");
        return;
      }
    } else if (isJournals) {
      setJournals((current) => [...current, { id, periodId: "draft", sourceType: "manual", status: "DRAFT", date: new Date().toISOString().slice(0, 10), description: form.description || "Manual journal draft", totalDebit: form.amount || "0", totalCredit: form.amount || "0", idempotencyKey: id }]);
    } else {
      setRows((current) => [{ id, date: new Date().toLocaleDateString("en-GB"), reference: "-", description: form.description || title, amount: form.amount || "0", status: "Draft" }, ...current]);
    }
    setDialog(null);
    notify(`${title} draft saved.`);
  };

  const reverseSelectedJournal = async () => {
    if (!canManageJournals) {
      notify("You do not have permission to reverse journals.");
      setDialog(null);
      return;
    }
    if (!selected || !("sourceType" in selected)) return;
    try {
      await reverseJournal(selected.id);
      setJournals((current) => current.map((journal) => journal.id === selected.id ? { ...journal, status: "REVERSED" } : journal));
      notify("Journal reversed and a reversal entry was recorded.");
    } catch (error) {
      notify(error instanceof Error ? error.message : "Unable to reverse this journal.");
    } finally {
      setDialog(null);
    }
  };

  const openView = (value: LedgerAccount | Journal | LocalRow) => {
    setSelected(value);
    setDialog("view");
  };


  if (isNew && mode === "journals" && !canManageJournals) {
    router.replace(`${base}/accounting-journal-entries`);
    return null;
  }
  if (isNew && (mode === "accounts" || mode === "journals" || mode === "contras")) return <AccountingNewPage mode={mode} form={form} setForm={setForm} onSubmit={saveDraft} onCancel={() => router.push(`${base}/${mode === "journals" ? "accounting-journal-entries" : mode === "contras" ? "accounting-contras" : "accounting-chart"}`)} />;

  return (
    <div className="accounting-page">
      <div className="bukku-breadcrumb">⌂ Home <span>/</span> ▧ Accounting <span>/</span> <strong>{title}</strong></div>
      <div className="accounting-heading"><h1>{title} <small>{headingCopy[mode]}</small></h1><div className="workflow-actions"><button type="button" className="btn primary" onClick={openNew}>＋ New</button></div></div>
      <section className="accounting-card">
        {isAccounts && <div className="accounting-tabs">{["All", "Assets", "Liabilities", "Equity", "Income", "Expenses"].map((item) => <button type="button" className={tab === item ? "active" : ""} onClick={() => setTab(item)} key={item}>{item}</button>)}</div>}
        <div className="accounting-filters"><label>Search<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search" /></label>{!isAccounts && <><label>Date Range<input placeholder="Start Date     →  End Date" /></label><label>Status<select><option>All but Void</option><option>Ready</option><option>Void</option></select></label></>}<button type="button" className="round-action" onClick={() => notify("Filters applied.")}>⌕</button><button type="button" className="round-action" onClick={() => { setQuery(""); setTab("All"); notify("Filters cleared."); }}>↩</button></div>
        {loading && <div className="table-state">Loading ledger data…</div>}
        {isAccounts ? <table className="accounting-table"><thead><tr><th>Code</th><th>Account</th><th>System Account</th><th>Balance</th><th>Action</th></tr></thead><tbody>{visibleAccounts.map((account) => <tr key={account.id}><td>{account.code}</td><td>{account.name}</td><td><span className="system-pill">{account.accountType || "-"}</span></td><td>RM&nbsp;—</td><td><button type="button" className="row-view" onClick={() => openView(account)}>View</button><button type="button" className="row-more" onClick={() => notify(`${account.name} account actions are available in the detail view.`)}>...</button></td></tr>)}</tbody></table> : isJournals ? <table className="accounting-table"><thead><tr><th>No.</th><th>Date</th><th>Reference No.</th><th>Description</th><th>Amount</th><th>Status</th><th>Action</th></tr></thead><tbody>{visibleJournals.map((journal) => <tr key={journal.id}><td>{journal.id}</td><td>{journal.date}</td><td>{journal.sourceId || "-"}</td><td>{journal.description}</td><td>RM&nbsp;{journal.totalDebit}</td><td><span className={`status-pill ${journal.status === "REVERSED" ? "warning" : "valid"}`}>{journal.status}</span></td><td><button type="button" className="row-view" onClick={() => openView(journal)}>View</button><button type="button" className="row-more" onClick={() => { setSelected(journal); setDialog("reverse"); }}>...</button></td></tr>)}</tbody></table> : <table className="accounting-table"><thead><tr><th>No.</th><th>Date</th><th>Reference No.</th><th>Description</th><th>Amount</th><th>Status</th><th>Action</th></tr></thead><tbody>{visibleLocalRows.map((row) => <tr key={row.id}><td>{row.id}</td><td>{row.date}</td><td>{row.reference}</td><td>{row.description}</td><td>RM&nbsp;{row.amount}</td><td><span className="status-pill valid">{row.status}</span></td><td><button type="button" className="row-view" onClick={() => openView(row)}>View</button><button type="button" className="row-more" onClick={() => notify(`${row.id} actions are available after opening the record.`)}>...</button></td></tr>)}</tbody></table>}
        {!loading && ((isAccounts && !visibleAccounts.length) || (isJournals && !visibleJournals.length) || (!isAccounts && !isJournals && !visibleLocalRows.length)) && <div className="table-state">No records match the current filters.</div>}
      </section>
      {dialog && <div className="modal-backdrop" onMouseDown={() => setDialog(null)}><div className="quotation-modal" role="dialog" onMouseDown={(event) => event.stopPropagation()}><div className="modal-title"><h2>{dialog === "new" ? `New ${title}` : dialog === "reverse" ? "Reverse journal" : "Record details"}</h2><button type="button" onClick={() => setDialog(null)}>×</button></div>{dialog === "new" ? <form onSubmit={saveDraft}><label>Name / description<input required value={isAccounts ? form.name : form.description} onChange={(event) => setForm({ ...form, ...(isAccounts ? { name: event.target.value } : { description: event.target.value }) })} /></label><label>Reference / code<input value={form.code} onChange={(event) => setForm({ ...form, code: event.target.value })} /></label><label>Amount<input type="number" min="0" step="0.01" value={form.amount} onChange={(event) => setForm({ ...form, amount: event.target.value })} /></label><div className="modal-actions"><button type="button" className="btn" onClick={() => setDialog(null)}>Cancel</button><button type="submit" className="btn primary">Save as Draft</button></div></form> : dialog === "reverse" ? <><p>Reversal creates an auditable counter-entry and keeps the original journal in history.</p><div className="modal-actions"><button type="button" className="btn" onClick={() => setDialog(null)}>Cancel</button><button type="button" className="btn danger" onClick={() => void reverseSelectedJournal()}>Reverse journal</button></div></> : <><div className="quotation-detail">{selected && "code" in selected ? <><span>Code<strong>{selected.code}</strong></span><span>Account<strong>{selected.name}</strong></span><span>Type<strong>{selected.accountType || "Custom"}</strong></span></> : selected && "sourceType" in selected ? <><span>Journal<strong>{selected.id}</strong></span><span>Status<strong>{selected.status}</strong></span><span>Description<strong>{selected.description}</strong></span></> : selected && <><span>Reference<strong>{selected.id}</strong></span><span>Status<strong>{selected.status}</strong></span><span>Amount<strong>RM {selected.amount}</strong></span></>}</div><div className="modal-actions"><button type="button" className="btn" onClick={() => setDialog(null)}>Close</button>{selected && "sourceType" in selected && selected.status !== "REVERSED" && <button type="button" className="btn" onClick={() => setDialog("reverse")}>Reverse</button>}</div></>}</div></div>}
      {notice && <div className="workflow-toast" role="status">{notice}</div>}
    </div>
  );
}

function AccountingNewPage({ mode, form, setForm, onSubmit, onCancel }: { mode: "accounts" | "journals" | "contras"; form: { name: string; code: string; description: string; amount: string; accountClass: string }; setForm: (value: { name: string; code: string; description: string; amount: string; accountClass: string }) => void; onSubmit: (event: React.FormEvent) => void | Promise<void>; onCancel: () => void }) {
  const isAccount = mode === "accounts";
  const isJournal = mode === "journals";
  const title = isAccount ? "Account" : isJournal ? "Journal Entry" : "Contra";
  const [items, setItems] = useState([{ account: "", description: "", debit: "", credit: "" }]);
  const [periods, setPeriods] = useState<AccountingPeriod[]>([]);
  const [journalAccounts, setJournalAccounts] = useState<ChartAccount[]>([]);
  const [journalDate] = useState(new Date().toISOString().slice(0, 10));
  const [formError, setFormError] = useState("");
  const addItem = () => setItems((current) => [...current, { account: "", description: "", debit: "", credit: "" }]);
  const updateItem = (index: number, key: "account" | "description" | "debit" | "credit", value: string) => setItems((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, [key]: value } : item));
  const tabs = isAccount ? [] : ["General", "Items", ...(mode === "contras" ? ["Sales Invoices", "Purchase Bills"] : []), "Additional Info", "Attachments", "Controls"];
  const scrollTo = (id: string) => document.getElementById(`accounting-new-${id.toLowerCase().replaceAll(" ", "-")}`)?.scrollIntoView({ behavior: "smooth" });
  useEffect(() => {
    if (!isJournal) return;
    void Promise.all([getAccountingPeriods(), getChartAccounts()]).then(([loadedPeriods, loadedAccounts]) => {
      setPeriods(loadedPeriods.filter((period) => period.status === "open"));
      setJournalAccounts(loadedAccounts.filter((account) => account.isActive));
    }).catch((error) => setFormError(error instanceof Error ? error.message : "Unable to load open accounting periods and accounts."));
  }, [isJournal]);
  const submitNew = async (event: React.FormEvent) => {
    if (!isJournal) { await onSubmit(event); return; }
    event.preventDefault();
    setFormError("");
    const amount = Number(form.amount);
    const dateInput = document.querySelector<HTMLInputElement>(".quotation-detail-card input[type='date']");
    const effectiveJournalDate = dateInput?.value || journalDate;
    const debitAccount = journalAccounts.find((account) => account.id === items[0]?.account || account.code === items[0]?.account)?.id;
    const creditAccount = journalAccounts.find((account) => account.id === items[1]?.account || account.code === items[1]?.account)?.id;
    if (!periods[0]) { setFormError("Create or open an accounting period before posting a journal."); return; }
    if (!Number.isFinite(amount) || amount <= 0) { setFormError("Enter a positive journal amount."); return; }
    if (!debitAccount || !creditAccount || debitAccount === creditAccount) { setFormError("Select two different accounts for the debit and credit lines."); return; }
    try {
      await createJournal({ periodId: periods[0].id, idempotencyKey: `manual:${crypto.randomUUID()}`, sourceType: "manual", journalDate: effectiveJournalDate, description: form.description.trim(), lines: [{ accountId: debitAccount, debit: amount.toFixed(2), description: items[0]?.description || form.description }, { accountId: creditAccount, credit: amount.toFixed(2), description: items[1]?.description || form.description }] });
      onCancel();
    } catch (error) { setFormError(error instanceof Error ? error.message : "The journal could not be posted."); }
  };
  useEffect(() => {
    if (!isJournal) return;
    const formElement = document.querySelector<HTMLFormElement>(".quotation-detail-card");
    if (!formElement) return;
    const handleSubmit = (event: Event) => { event.preventDefault(); event.stopImmediatePropagation(); void submitNew(event as unknown as React.FormEvent); };
    formElement.addEventListener("submit", handleSubmit, true);
    return () => formElement.removeEventListener("submit", handleSubmit, true);
  }, [isJournal, journalAccounts, periods, items, form, journalDate]);
  useEffect(() => {
    if (!isJournal) return;
    const formElement = document.querySelector<HTMLFormElement>(".quotation-detail-card");
    if (!formElement) return;
    let banner = formElement.querySelector<HTMLElement>("[data-journal-error]");
    if (!banner) {
      banner = document.createElement("div");
      banner.dataset.journalError = "true";
      banner.setAttribute("role", "alert");
      banner.style.cssText = "margin:12px 0;padding:10px 12px;border:1px solid #f3a6a6;border-radius:6px;background:#fff1f1;color:#9b1c1c;font-size:13px;";
      formElement.prepend(banner);
    }
    banner.textContent = formError;
    banner.hidden = !formError;
  }, [isJournal, formError]);
  if (isAccount) return <div className="quotation-detail-page"><div className="bukku-breadcrumb">⌂ Home <span>/</span> ▧ Accounting <span>/</span> Chart of Accounts <span>/</span> <strong>New Account</strong></div><div className="quotation-detail-heading"><h1>New Account</h1><button type="button" onClick={onCancel}>Back</button></div><form className="quotation-detail-card" onSubmit={onSubmit}><section className="detail-section"><div className="detail-section-title"><h2>General Info</h2><small>Create an account in the general ledger.</small></div><div className="detail-fields"><label>Account Code<input value={form.code} onChange={(event) => setForm({ ...form, code: event.target.value })} placeholder="Auto Generated" /><small>Recommended code: 1000 to 1499</small></label><label>Name<input required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="Sales Income" /></label><label className="wide">Description<textarea value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} placeholder="Description of the account" /></label><label>Currency<select defaultValue="MYR"><option>MYR - Malaysian Ringgit</option><option>USD - US Dollar</option></select></label></div></section><section className="detail-section"><div className="detail-section-title"><h2>Categorisation</h2><small>Classify the account for reports and posting.</small></div><div className="detail-fields"><label>System Type<select defaultValue=""><option value="">Select System Type</option><option>Bank & Cash</option><option>Accounts Receivable</option><option>Income</option><option>Expense</option></select></label><label>Type<select defaultValue="Current Assets"><option>Current Assets</option><option>Non-current Assets</option><option>Liabilities</option><option>Equity</option><option>Income</option><option>Expenses</option></select></label><label>Parent Account<select defaultValue=""><option value="">Select Parent Account</option><option>1000 Cash and Bank</option></select></label><label>Classification<select defaultValue="Operating"><option>Operating</option><option>Investing</option><option>Financing</option></select></label></div></section><div className="detail-save"><button type="button" onClick={onCancel}>Cancel</button><button type="submit" className="primary">Save</button></div></form></div>;
  return <div className="quotation-detail-page"><div className="bukku-breadcrumb">⌂ Home <span>/</span> ▧ Accounting <span>/</span> {title}s <span>/</span> <strong>New {title}</strong></div><div className="quotation-detail-heading"><h1>New {title}</h1><button type="button" onClick={onCancel}>Back</button></div><form className="quotation-detail-card" onSubmit={onSubmit}><nav aria-label={`${title} form sections`}>{tabs.map((tab) => <button type="button" key={tab} className={tab === "General" ? "active" : ""} onClick={() => scrollTo(tab)}>{tab}</button>)}</nav><section className="detail-section" id="accounting-new-general"><div className="detail-section-title"><h2>General Info</h2><small>General information such as number, date and journal details.</small></div><div className="detail-fields"><label>No.<input readOnly value={isJournal ? "JE-[5DIGIT]" : "CT-[5DIGIT]"} /></label><label>Reference No.<input value={form.code} onChange={(event) => setForm({ ...form, code: event.target.value })} /></label><label>Date<input type="date" defaultValue="2026-11-18" /></label><label>Currency<select defaultValue="MYR"><option>MYR - Malaysian Ringgit</option><option>USD - US Dollar</option></select></label><label>Rate<input readOnly value="1" /></label><label>Description<input required value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label><label>Internal Note<input /></label><label>Tags<input placeholder="Select Tags" /></label>{!isJournal && <><label>Customer<input placeholder="Select Customer" /></label><label>Supplier / Employee<input placeholder="Select Contact" /></label></>}</div></section>{isJournal ? <section className="detail-section" id="accounting-new-items"><div className="detail-section-title"><h2>Items</h2><small>Journal lines must balance before posting.</small></div><div className="quotation-items"><table><thead><tr><th>Account</th><th>Description</th><th>Debit</th><th>Credit</th></tr></thead><tbody>{items.map((item, index) => <tr key={`journal-item-${index}`}><td><input value={item.account} onChange={(event) => updateItem(index, "account", event.target.value)} placeholder="Select Account" /></td><td><input value={item.description} onChange={(event) => updateItem(index, "description", event.target.value)} /></td><td><input type="number" min="0" step="0.01" value={item.debit} onChange={(event) => updateItem(index, "debit", event.target.value)} /></td><td><input type="number" min="0" step="0.01" value={item.credit} onChange={(event) => updateItem(index, "credit", event.target.value)} /></td></tr>)}</tbody></table><button type="button" className="primary" onClick={addItem}>＋ Item</button><div className="quotation-total"><div><span>Debit Total</span><strong>RM {items.reduce((total, item) => total + Number(item.debit || 0), 0).toFixed(2)}</strong></div><div><span>Credit Total</span><strong>RM {items.reduce((total, item) => total + Number(item.credit || 0), 0).toFixed(2)}</strong></div></div></div></section> : <><section className="detail-section" id="accounting-new-sales-invoices"><div className="detail-section-title"><h2>Sales Invoices</h2><small>Apply this contra to customer invoices.</small></div><div className="detail-empty">No data</div></section><section className="detail-section" id="accounting-new-purchase-bills"><div className="detail-section-title"><h2>Purchase Bills</h2><small>Apply this contra to supplier bills.</small></div><div className="detail-empty">No data</div></section></>}<section className="detail-section" id="accounting-new-additional-info"><div className="detail-section-title"><h2>Additional Info</h2><small>Additional information such as remarks.</small></div><div className="detail-fields"><label className="wide">Remarks<textarea placeholder="Remarks" /></label></div></section><section className="detail-section" id="accounting-new-attachments"><div className="detail-section-title"><h2>Attachments</h2><small>Supporting documents attached to the transaction.</small></div><div className="upload-box">＋<br />Click or drop files to upload<br /><small>or select from File Manager.</small></div></section><section className="detail-section" id="accounting-new-controls"><div className="detail-section-title"><h2>Controls</h2><small>Controls and statuses for the transaction.</small></div><div className="detail-fields"><label>Status<select defaultValue="Draft"><option>Draft</option><option>Pending Approval</option><option>Ready</option></select></label></div></section><div className="detail-save"><button type="button" onClick={onCancel}>Cancel</button><button type="submit" className="primary">Save</button></div></form></div>;
}
