"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { User } from "@/types";
import {
  archiveERPCatalog,
  createERPCatalog,
  createERPDocument,
  listERPCatalog,
  listERPDocuments,
  transitionERPDocument,
  type ERPCatalogEntry,
  type ERPDocument,
} from "@/lib/api/erp.service";

type BankMode = "money-in" | "money-out" | "transfers" | "accounts" | "feeds";
type RowStatus = "Ready" | "Draft" | "Void";
type BankRow = { id: string; erpId?: string; date: string; contact?: string; from?: string; to?: string; amount: number; status: RowStatus; documentType?: string };
type Account = { id?: string; code: string; name: string; type: "Bank" | "Credit Card"; balance: string; active: boolean };
type BankForm = {
  code: string;
  name: string;
  type: "Bank" | "Credit Card";
  contact: string;
  amount: string;
  date: string;
  from: string;
  to: string;
  reference: string;
  description: string;
  note: string;
  tags: string;
  feeAmount: string;
  feeAccount: string;
};

const moneyInRows: BankRow[] = [
  { id: "OR-10098", date: "15/07/2026", contact: "Alpha Sdn Bhd", amount: 500, status: "Ready" },
  { id: "OR-10066", date: "01/02/2026", contact: "-", amount: 1000, status: "Ready" },
  { id: "DISB-0001", date: "06/12/2025", contact: "1Playground Group 123", amount: 5000, status: "Ready" },
  { id: "OR-10013", date: "13/06/2025", contact: "-", amount: 1050, status: "Ready" },
  { id: "OR-10012", date: "12/06/2025", contact: "ABC Sdn Bhd", amount: 1000, status: "Ready" },
];
const moneyOutRows: BankRow[] = [
  { id: "PV-12409", date: "08/11/2026", contact: "Equistal connection test supplier", amount: 1, status: "Ready" },
  { id: "PV-12406", date: "02/11/2026", contact: "ABC Sdn Bhd", amount: 500, status: "Ready" },
  { id: "PV-12405", date: "27/10/2026", contact: "ABC TESTING SDN BHD", amount: 30000, status: "Ready" },
];
const transferRows: BankRow[] = [
  { id: "FT-00010", date: "08/11/2026", from: "1000-00 Petty Cash", to: "1000-01 Bank Account", amount: 1, status: "Ready" },
  { id: "FT-00009", date: "24/10/2026", from: "1000-00 Petty Cash", to: "1000-01 Bank Account", amount: 1, status: "Ready" },
  { id: "FT-00006", date: "02/04/2026", from: "1000-01 Bank Account", to: "1000-00 Petty Cash", amount: 10000, status: "Ready" },
];
const initialAccounts: Account[] = [
  { code: "1113", name: "AAA", type: "Bank", balance: "0.00", active: true },
  { code: "1000-01", name: "Bank Account", type: "Bank", balance: "91,987,267.39", active: true },
  { code: "1116", name: "Cash", type: "Bank", balance: "51,910.00", active: true },
  { code: "3000", name: "Credit Card Accounts", type: "Credit Card", balance: "200.00", active: true },
  { code: "1000-00", name: "Petty Cash", type: "Bank", balance: "129,527.29", active: true },
];

function documentTypeFor(mode: BankMode) { return mode === "money-in" ? "money-in" : mode === "money-out" ? "money-out" : "bank-transfer"; }
function formatDate(value?: string | null) { if (!value) return new Date().toLocaleDateString("en-GB"); const date = new Date(`${value.slice(0, 10)}T00:00:00`); return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString("en-GB"); }
function statusFor(value: ERPDocument["status"]): RowStatus { return value === "ready" ? "Ready" : value === "void" ? "Void" : "Draft"; }
function fromDocument(row: ERPDocument): BankRow {
  const fields = row.fields ?? {};
  return { id: row.number, erpId: row.id, date: formatDate(row.documentDate), contact: row.contactName || "-", from: typeof fields.from === "string" ? fields.from : undefined, to: typeof fields.to === "string" ? fields.to : undefined, amount: Number(row.amount) || 0, status: statusFor(row.status), documentType: row.type };
}
function fromCatalog(row: ERPCatalogEntry): Account {
  const systemType = row.data.systemType === "Credit Card" ? "Credit Card" : "Bank";
  return { id: row.id, code: row.code || row.name, name: row.name, type: systemType, balance: typeof row.data.balance === "string" ? row.data.balance : "0.00", active: row.isActive };
}
const emptyForm = (): BankForm => ({ code: "", name: "", type: "Bank", contact: "", amount: "", date: new Date().toISOString().slice(0, 10), from: "", to: "", reference: "", description: "", note: "", tags: "", feeAmount: "", feeAccount: "" });

function bankRoute(mode: BankMode) {
  return mode === "money-in" ? "bank-money-in" : mode === "money-out" ? "bank-money-out" : mode === "transfers" ? "bank-transfers" : "bank-accounts";
}

export function BankPage({ user, mode }: { user: User; mode: BankMode }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const base = user.role === "LOCAL_ADMIN" ? "/company" : "/workspace";
  const isNew = searchParams.get("new") === "1";
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState<BankRow[]>(mode === "transfers" ? transferRows : mode === "money-out" ? moneyOutRows : moneyInRows);
  const [accounts, setAccounts] = useState<Account[]>(initialAccounts);
  const [accountTab, setAccountTab] = useState<"All" | "Bank" | "Credit Card">("All");
  const [menu, setMenu] = useState<string | null>(null);
  const [selected, setSelected] = useState<BankRow | Account | null>(null);
  const [dialog, setDialog] = useState<"new" | "view" | "reconcile" | null>(null);
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [connected, setConnected] = useState(false);
  const [newForm, setNewForm] = useState<BankForm>(emptyForm());
  const title = mode === "money-in" ? "Money In" : mode === "money-out" ? "Money Out" : mode === "transfers" ? "Transfers" : mode === "accounts" ? "Accounts" : "Bank Feeds";
  const isTransfer = mode === "transfers";
  const visibleRows = useMemo(() => rows.filter((row) => `${row.id} ${row.contact ?? ""} ${row.from ?? ""} ${row.to ?? ""}`.toLowerCase().includes(query.toLowerCase())), [rows, query]);
  const visibleAccounts = accounts.filter((account) => account.active && (accountTab === "All" || account.type === accountTab));

  useEffect(() => {
    let active = true;
    if (mode === "feeds") return () => { active = false; };
    setLoading(true);
    const load = mode === "accounts"
      ? listERPCatalog("bank-account").then((items) => { if (active) setAccounts(items.map(fromCatalog)); })
      : listERPDocuments(documentTypeFor(mode), { page: 1, pageSize: 100 }).then((page) => { if (active) setRows(page.items.map(fromDocument)); });
    void load.catch(() => { if (active) setNotice("The local API is unavailable; showing the built-in preview until it reconnects."); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [mode]);

  const close = () => { setDialog(null); setSelected(null); setMenu(null); setNewForm(emptyForm()); };
  const openNew = () => { setNewForm(emptyForm()); setSelected(null); router.push(`${base}/${bankRoute(mode)}?new=1`); };

  async function create(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      if (mode === "accounts") {
        const created = await createERPCatalog("bank-account", { name: newForm.name.trim(), code: newForm.code.trim() || undefined, data: { systemType: newForm.type, balance: "0.00" } });
        setAccounts((current) => [fromCatalog(created), ...current]);
        setNotice(`${created.name} was created in your local chart of accounts.`);
        close();
        router.push(`${base}/${bankRoute(mode)}`);
        return;
      }
      const created = await createERPDocument(documentTypeFor(mode), {
        contactName: isTransfer ? undefined : newForm.contact.trim() || undefined,
        documentDate: newForm.date,
        amount: Number(newForm.amount) || 0,
        status: "draft",
        description: newForm.description.trim() || (isTransfer ? `Transfer from ${newForm.from} to ${newForm.to}` : title),
        fields: {
          ...(isTransfer ? { from: newForm.from.trim(), to: newForm.to.trim() } : { account: mode === "money-in" ? newForm.to.trim() : newForm.from.trim() }),
          referenceNo: newForm.reference.trim(),
          internalNote: newForm.note.trim(),
          tags: newForm.tags.trim(),
          feeAmount: Number(newForm.feeAmount) || 0,
          feeAccount: newForm.feeAccount.trim(),
        },
      });
      const row = fromDocument(created);
      setRows((current) => [row, ...current]);
      setNotice(`${row.id} was saved as a Draft.`);
      close();
      router.push(`${base}/${bankRoute(mode)}`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "The record could not be saved.");
    } finally { setSaving(false); }
  }

  async function connectFeed() {
    setSaving(true);
    try {
      await createERPCatalog("bank-account", { name: "Maybank Feed", code: "MAYBANK-FEED", data: { provider: "Maybank", status: "pending_authorization" } });
      setConnected(true);
      setNotice("The Maybank feed request was saved. Complete provider authorisation before importing statements.");
    } catch (error) { setNotice(error instanceof Error ? error.message : "The feed request could not be saved."); }
    finally { setSaving(false); }
  }

  async function accountAction(account: Account, action: "view" | "deactivate" | "delete") {
    setMenu(null);
    if (action === "view" || !account.id) { setSelected(account); setDialog("view"); return; }
    try {
      await archiveERPCatalog("bank-account", account.id);
      setAccounts((current) => current.filter((item) => item.id !== account.id));
      setNotice(`${account.name} was ${action === "delete" ? "archived" : "deactivated"}.`);
    } catch (error) { setNotice(error instanceof Error ? error.message : "The account could not be updated."); }
  }

  async function rowAction(row: BankRow, action: "view" | "reconcile" | "void") {
    setMenu(null);
    setSelected(row);
    if (action === "view" || action === "reconcile") { setDialog(action); return; }
    try {
      if (row.erpId) await transitionERPDocument(documentTypeFor(mode), row.erpId, "void", "Voided from the local bank workflow");
      setRows((current) => current.map((item) => item.id === row.id ? { ...item, status: "Void" } : item));
      setNotice(`${row.id} was voided and retained in the audit trail.`);
    } catch (error) { setNotice(error instanceof Error ? error.message : "The transaction could not be voided."); }
  }

  async function markReviewed() {
    const row = selected && "id" in selected && !("name" in selected) ? selected : null;
    if (!row) return;
    try {
      if (row.erpId && row.status === "Draft") await transitionERPDocument(documentTypeFor(mode), row.erpId, "ready", "Reviewed and approved from the local bank workflow");
      setRows((current) => current.map((item) => item.id === row.id ? { ...item, status: "Ready" } : item));
      setNotice(`${row.id} was reviewed and marked Ready.`);
      close();
    } catch (error) { setNotice(error instanceof Error ? error.message : "The transaction could not be reviewed."); }
  }

  if (mode === "feeds") return <div className="bank-page"><div className="bukku-breadcrumb">⌂ Home <span>/</span> ▥ Bank <span>/</span> <strong>Feeds</strong></div><div className="bank-title"><h1>Bank Feeds <small>Connect your bank to streamline your statements seamlessly.</small></h1><button type="button" className="btn primary" onClick={() => router.push(`${base}/bank-accounts`)}>▥ Accounts</button></div><div className="feed-card"><div className="feed-logo">◎</div><h3>Maybank</h3><p>Securely save a provider connection request in your own workspace.</p><button type="button" className="btn primary" disabled={connected || saving} onClick={() => void connectFeed()}>{connected ? "Connection requested" : saving ? "Saving…" : "Connect"}</button></div>{notice && <div className="workflow-toast" role="status">{notice}</div>}</div>;

  if (isNew) return <BankNewPage mode={mode} form={newForm} setForm={setNewForm} onSubmit={create} onCancel={() => router.push(`${base}/${bankRoute(mode)}`)} saving={saving} />;

  if (mode === "accounts") return <div className="bank-page" aria-busy={loading}><div className="bukku-breadcrumb">⌂ Home <span>/</span> ▥ Bank <span>/</span> <strong>Accounts</strong></div><div className="bank-title"><h1>Accounts <small>Bank & credit card accounts of your company.</small></h1><button type="button" className="btn primary" onClick={openNew}>＋ New</button></div><section className="bank-card"><div className="bank-tabs">{(["All", "Bank", "Credit Card"] as const).map((item) => <button type="button" key={item} className={accountTab === item ? "active" : ""} onClick={() => setAccountTab(item)}>{item}</button>)}</div><table className="bank-table"><thead><tr><th>Code</th><th>Account</th><th>System Type</th><th>Statement Balance</th><th>Current Balance</th><th>Action</th></tr></thead><tbody>{visibleAccounts.map((row) => <tr key={row.id ?? row.code}><td>{row.code}</td><td>{row.name}</td><td>{row.type}</td><td>-</td><td>RM {row.balance}</td><td><button type="button" onClick={() => { setSelected(row); setDialog("reconcile"); }}>Reconcile</button><button type="button" onClick={() => setMenu(menu === row.code ? null : row.code)}>...</button>{menu === row.code && <div className="account-menu"><button type="button" onClick={() => void accountAction(row, "view")}>View</button><button type="button" onClick={() => void accountAction(row, "deactivate")}>Deactivate</button><button type="button" className="danger" onClick={() => void accountAction(row, "delete")}>Delete</button></div>}</td></tr>)}</tbody></table>{dialog && <BankDialog mode={mode} dialog={dialog} selected={selected} onClose={close} onSubmit={create} form={newForm} setForm={setNewForm} saving={saving} onReviewed={() => void markReviewed()} />}{notice && <div className="workflow-toast" role="status">{notice}</div>}</section></div>;

  return <div className="bank-page" aria-busy={loading}><div className="bukku-breadcrumb">⌂ Home <span>/</span> ▥ Bank <span>/</span> <strong>{title}</strong></div><div className="bank-title"><h1>{title} <small>{isTransfer ? "Transfer of funds between your bank / cash accounts." : mode === "money-in" ? "Other money received in your bank / cash accounts." : "Other money paid out from your bank / cash accounts."}</small></h1><button type="button" className="btn primary" onClick={openNew}>＋ New</button></div><section className="bank-card"><div className="bank-filters"><label>Date Range<input placeholder="Start Date     →     End Date" /></label><label>Search<input value={query} onChange={(event) => setQuery(event.target.value)} /></label><label>{isTransfer ? "Account" : mode === "money-in" ? "Deposit To" : "Pay From"}<input placeholder="All" /></label><label>Status<input placeholder="All but Void" /></label><button type="button" className="more-filters" onClick={() => { setQuery(""); setNotice("Bank filters cleared."); }}>Less Filters⌃</button></div><div className="bank-toolbar"><button type="button" onClick={() => window.print()} aria-label="Print">▣</button><button type="button" onClick={() => setNotice("Use the browser print dialog to save this list as PDF.")} aria-label="Export">⇩</button><span>1-{visibleRows.length} of {visibleRows.length} items　‹　1　›　30 / page</span></div><table className="bank-table"><thead><tr><th>No.</th><th>Date</th><th>{isTransfer ? "Transfer From" : "Contact"}</th>{isTransfer && <th>Transfer To</th>}<th>Amount</th><th>Status</th><th>Action</th></tr></thead><tbody>{visibleRows.map((row) => <tr key={row.erpId ?? row.id}><td>{row.id}</td><td>{row.date}</td><td>{isTransfer ? row.from : row.contact}</td>{isTransfer && <td>{row.to}</td>}<td>RM {row.amount.toLocaleString("en-MY", { minimumFractionDigits: 2 })}</td><td><span className={`quote-status ${row.status.toLowerCase()}`}>{row.status}</span></td><td><button type="button" onClick={() => void rowAction(row, "view")}>View</button><button type="button" onClick={() => setMenu(menu === row.id ? null : row.id)}>...</button>{menu === row.id && <div className="account-menu"><button type="button" onClick={() => void rowAction(row, "reconcile")}>Review / Reconcile</button><button type="button" onClick={() => void rowAction(row, "void")} disabled={row.status === "Void"}>Void</button></div>}</td></tr>)}</tbody></table>{dialog && <BankDialog mode={mode} dialog={dialog} selected={selected} onClose={close} onSubmit={create} form={newForm} setForm={setNewForm} saving={saving} onReviewed={() => void markReviewed()} />}{notice && <div className="workflow-toast" role="status">{notice}</div>}</section></div>;
}

function BankDialog({ mode, dialog, selected, onClose, onSubmit, form, setForm, saving, onReviewed }: { mode: BankMode; dialog: "new" | "view" | "reconcile"; selected: BankRow | Account | null; onClose: () => void; onSubmit: (event: FormEvent) => void; form: BankForm; setForm: (value: BankForm) => void; saving: boolean; onReviewed: () => void }) {
  const isAccount = mode === "accounts";
  return <div className="modal-backdrop" onMouseDown={onClose}><div className="quotation-modal" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}><div className="modal-title"><h2>{dialog === "new" ? (isAccount ? "New bank account" : "New bank transaction") : dialog === "reconcile" ? (isAccount ? "Reconcile account" : "Review transaction") : "View"}</h2><button type="button" onClick={onClose}>×</button></div>{dialog === "new" ? <form onSubmit={onSubmit}>{isAccount ? <><label>Code<input required value={form.code} onChange={(event) => setForm({ ...form, code: event.target.value })} placeholder="1000-02" /></label><label>Account name<input required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="Operating account" /></label><label>System type<select value={form.type} onChange={(event) => setForm({ ...form, type: event.target.value as BankForm["type"] })}><option>Bank</option><option>Credit Card</option></select></label></> : <>{mode !== "transfers" && <label>Contact<input value={form.contact} onChange={(event) => setForm({ ...form, contact: event.target.value })} placeholder="Customer or supplier" /></label>}{mode === "transfers" && <><label>Transfer from<input required value={form.from} onChange={(event) => setForm({ ...form, from: event.target.value })} placeholder="1000-00 Petty Cash" /></label><label>Transfer to<input required value={form.to} onChange={(event) => setForm({ ...form, to: event.target.value })} placeholder="1000-01 Bank Account" /></label></>}<label>Date<input required type="date" value={form.date} onChange={(event) => setForm({ ...form, date: event.target.value })} /></label><label>Amount<input required type="number" min="0.01" step="0.01" value={form.amount} onChange={(event) => setForm({ ...form, amount: event.target.value })} /></label></>}<div className="modal-actions"><button type="button" className="btn" onClick={onClose}>Cancel</button><button type="submit" className="btn primary" disabled={saving}>{saving ? "Saving…" : "Save as Draft"}</button></div></form> : <><p>{dialog === "reconcile" ? (isAccount ? "Review the statement balance before confirming this account." : "Review the transaction and confirm it before posting it as Ready.") : "Read-only local ERP summary."}</p><div className="quotation-detail">{selected && "name" in selected ? <><span>Account<strong>{selected.name}</strong></span><span>Code<strong>{selected.code}</strong></span><span>Balance<strong>RM {selected.balance}</strong></span></> : selected && <><span>Reference<strong>{selected.id}</strong></span><span>Amount<strong>RM {selected.amount.toFixed(2)}</strong></span><span>Status<strong>{selected.status}</strong></span></>}</div><div className="modal-actions"><button type="button" className="btn" onClick={onClose}>Close</button>{dialog === "reconcile" && !isAccount && <button type="button" className="btn primary" onClick={onReviewed}>Mark reviewed</button>}</div></>}</div></div>;
}

function BankNewPage({ mode, form, setForm, onSubmit, onCancel, saving }: { mode: BankMode; form: BankForm; setForm: (value: BankForm) => void; onSubmit: (event: FormEvent) => void; onCancel: () => void; saving: boolean }) {
  const isAccount = mode === "accounts";
  const isTransfer = mode === "transfers";
  const isMoneyIn = mode === "money-in";
  const title = isAccount ? "Account" : isTransfer ? "Transfer" : isMoneyIn ? "Money In" : "Money Out";
  const partyLabel = isMoneyIn ? "Customer" : "Supplier";
  const accountLabel = isMoneyIn ? "Deposit To" : "Pay From";
  const [activeTab, setActiveTab] = useState("general");
  const update = (key: keyof BankForm, value: string) => setForm({ ...form, [key]: value });
  const jump = (id: string) => {
    setActiveTab(id);
    document.getElementById(`bank-new-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const tabs = isAccount ? [] : ["general", isTransfer ? "transfer" : isMoneyIn ? "received" : "made", "additional", ...(isMoneyIn ? ["custom"] : []), "attachments", "controls"];
  const tabLabel: Record<string, string> = { general: "General Info", received: "Money Received", made: "Payment Made", transfer: "Transfer Info", additional: "Additional Info", custom: "Custom Fields", attachments: "Attachments", controls: "Controls" };

  if (isAccount) return <div className="quotation-detail-page"><div className="bukku-breadcrumb">⌂ Home <span>/</span> ▥ Bank <span>/</span> Accounts <span>/</span> <strong>New Account</strong></div><div className="quotation-detail-heading"><h1>New Account</h1><button type="button" onClick={onCancel}>Back</button></div><form className="quotation-detail-card" onSubmit={onSubmit}><section className="detail-section" id="bank-new-general"><div className="detail-section-title"><h2>General Info</h2><small>Create a bank or cash account for this workspace.</small></div><div className="detail-fields"><label>Type<select value={form.type} onChange={(event) => update("type", event.target.value)}><option value="Bank">Bank / Cash</option><option value="Credit Card">Credit Card</option></select></label><label>Currency<select defaultValue="MYR"><option>MYR - Malaysian Ringgit</option><option>USD - US Dollar</option><option>SGD - Singapore Dollar</option></select></label><label>Code<input value={form.code} onChange={(event) => update("code", event.target.value)} placeholder="Auto generated" /></label><label>Name<input required value={form.name} onChange={(event) => update("name", event.target.value)} placeholder="Current Account" /></label><label className="wide">Description<textarea value={form.description} onChange={(event) => update("description", event.target.value)} placeholder="Description of the account" /></label></div></section><div className="detail-save"><button type="button" onClick={onCancel}>Cancel</button><button type="submit" className="primary" disabled={saving}>{saving ? "Saving…" : "Save"}</button></div></form></div>;

  return <div className="quotation-detail-page"><div className="bukku-breadcrumb">⌂ Home <span>/</span> ▥ Bank <span>/</span> {title} <span>/</span> <strong>New {title}</strong></div><div className="quotation-detail-heading"><h1>New {title}</h1><button type="button" onClick={onCancel}>Back</button></div><form className="quotation-detail-card" onSubmit={onSubmit}><nav aria-label={`${title} form sections`}>{tabs.map((tab) => <button type="button" key={tab} className={activeTab === tab ? "active" : ""} onClick={() => jump(tab)}>{tabLabel[tab]}</button>)}</nav><section className="detail-section" id="bank-new-general"><div className="detail-section-title"><h2>General Info</h2><small>General information such as number, date and transaction details.</small></div><div className="detail-fields"><label>No.<input readOnly value={isTransfer ? "FT-[5DIGIT]" : isMoneyIn ? "OR-[5DIGIT]" : "PV-[5DIGIT]"} /></label><label>Reference No.<input value={form.reference} onChange={(event) => update("reference", event.target.value)} /></label><label>Date<input required type="date" value={form.date} onChange={(event) => update("date", event.target.value)} /></label><label>Currency<select defaultValue="MYR"><option>MYR - Malaysian Ringgit</option><option>USD - US Dollar</option><option>SGD - Singapore Dollar</option></select></label><label>Rate<input value="1" readOnly /></label><label>Description<input value={form.description} onChange={(event) => update("description", event.target.value)} /></label><label>Internal Note<input value={form.note} onChange={(event) => update("note", event.target.value)} /></label><label>Tags<input value={form.tags} onChange={(event) => update("tags", event.target.value)} placeholder="Select Tags" /></label></div></section>{isTransfer ? <section className="detail-section" id="bank-new-transfer"><div className="detail-section-title"><h2>Transfer Info</h2><small>Move funds between two bank or cash accounts.</small></div><div className="detail-fields"><label>Transfer From<input required value={form.from} onChange={(event) => update("from", event.target.value)} placeholder="1000-01 Bank Account" /></label><label>Transfer To<input required value={form.to} onChange={(event) => update("to", event.target.value)} placeholder="1000-00 Petty Cash" /></label><label>Amount<input required type="number" min="0.01" step="0.01" value={form.amount} onChange={(event) => update("amount", event.target.value)} /></label></div></section> : <section className="detail-section" id={`bank-new-${isMoneyIn ? "received" : "made"}`}><div className="detail-section-title"><h2>{isMoneyIn ? "Money Received" : "Payment Made"}</h2><small>{isMoneyIn ? "Payments received for the transaction." : "Payments made for the transaction."}</small></div><div className="detail-fields"><label>{partyLabel}<input value={form.contact} onChange={(event) => update("contact", event.target.value)} placeholder={`Select ${partyLabel.toLowerCase()}`} /></label><label>{accountLabel}<input required value={isMoneyIn ? form.to : form.from} onChange={(event) => update(isMoneyIn ? "to" : "from", event.target.value)} placeholder="1000-01 Bank Account" /></label><label>Amount<input required type="number" min="0.01" step="0.01" value={form.amount} onChange={(event) => update("amount", event.target.value)} /></label><label>Reference No.<input value={form.reference} onChange={(event) => update("reference", event.target.value)} /></label><label>Fee Amount<input type="number" min="0" step="0.01" value={form.feeAmount} onChange={(event) => update("feeAmount", event.target.value)} /></label><label>Fee Account<input value={form.feeAccount} onChange={(event) => update("feeAccount", event.target.value)} /></label><div className="quotation-total wide"><div><span>TOTAL</span><strong>RM {Number(form.amount || 0).toFixed(2)}</strong></div></div></div><button type="button" onClick={() => update("amount", form.amount || "0.00")}>＋ Payment</button></section>}<section className="detail-section" id="bank-new-additional"><div className="detail-section-title"><h2>Additional Info</h2><small>Additional information and remarks for this transaction.</small></div><div className="detail-fields"><label className="wide">Remarks<textarea value={form.description} onChange={(event) => update("description", event.target.value)} placeholder="Please enter remarks." /></label></div></section>{isMoneyIn && <section className="detail-section" id="bank-new-custom"><div className="detail-section-title"><h2>Custom Fields</h2><small>User-defined fields added to the transaction.</small></div><div className="detail-fields"><label>Next Mileage<input placeholder="Text" /></label><label>Vehicle No.<input placeholder="Text" /></label></div></section>}<section className="detail-section" id="bank-new-attachments"><div className="detail-section-title"><h2>Attachments</h2><small>Supporting documents attached to the transaction.</small></div><div className="upload-box">＋<br />Click or drop files to upload<br /><small>or select from File Manager.</small></div></section><section className="detail-section" id="bank-new-controls"><div className="detail-section-title"><h2>Controls</h2><small>Controls and statuses for the transaction.</small></div><div className="detail-fields"><label>Status<select defaultValue="Draft"><option>Draft</option><option>Pending Approval</option><option>Ready</option></select></label></div></section><div className="detail-save"><button type="button" onClick={onCancel}>Cancel</button><button type="submit" className="primary" disabled={saving}>{saving ? "Saving…" : "Save"}</button></div></form></div>;
}
