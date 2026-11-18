"use client";

import { useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { MoreHorizontal, Pencil, Plus, Trash2, X } from "lucide-react";
import type { User } from "@/types";
import { useResource } from "@/features/common/hooks/use-resource";
import {
  archiveERPCatalog,
  createERPCatalog,
  listERPCatalog,
  updateERPCatalog,
  type ERPCatalogEntry,
} from "@/lib/api/erp.service";
import { ErrorState, LoadingState, PageHeader, Panel, Toast } from "@/components/common/ui";

type CustomRate = { id: string; startDate: string; endDate: string; rate: string };
type CurrencyData = { symbol?: string; rate?: string | number | null; isBase?: boolean; customRates?: CustomRate[] };

type SupportedCurrency = { code: string; symbol: string; name: string };

const SUPPORTED_CURRENCIES: SupportedCurrency[] = [
  { code: "EUR", symbol: "€", name: "Euro" },
  { code: "GBP", symbol: "£", name: "British Pound Sterling" },
  { code: "AED", symbol: "د.إ", name: "United Arab Emirates Dirham" },
  { code: "AFN", symbol: "؋", name: "Afghan Afghani" },
  { code: "ALL", symbol: "Lek", name: "Albanian Lek" },
  { code: "AUD", symbol: "A$", name: "Australian Dollar" },
  { code: "CAD", symbol: "C$", name: "Canadian Dollar" },
  { code: "CNY", symbol: "CN¥", name: "Chinese Yuan" },
  { code: "HKD", symbol: "HK$", name: "Hong Kong Dollar" },
  { code: "INR", symbol: "₹", name: "Indian Rupee" },
  { code: "JPY", symbol: "¥", name: "Japanese Yen" },
  { code: "MYR", symbol: "RM", name: "Malaysian Ringgit" },
  { code: "NZD", symbol: "NZ$", name: "New Zealand Dollar" },
  { code: "SGD", symbol: "S$", name: "Singapore Dollar" },
  { code: "THB", symbol: "฿", name: "Thai Baht" },
  { code: "USD", symbol: "US$", name: "US Dollar" },
];

const asCurrencyData = (entry: ERPCatalogEntry): CurrencyData => {
  const data = entry.data as CurrencyData;
  return { ...data, customRates: Array.isArray(data.customRates) ? data.customRates : [] };
};

const dateLabel = (value: string) => {
  if (!value) return "—";
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString("en-GB", { day: "2-digit", month: "2-digit", year: "numeric", weekday: "short" });
};

const rateLabel = (value: string | number | null | undefined) => value == null || value === "" ? "—" : Number(value).toFixed(6);

const effectiveRate = (data: CurrencyData) => {
  const today = new Date().toISOString().slice(0, 10);
  const current = [...(data.customRates ?? [])]
    .filter((rate) => rate.startDate <= today && today <= rate.endDate)
    .sort((a, b) => b.startDate.localeCompare(a.startDate))[0];
  return current?.rate ?? data.rate;
};

const routeBase = () => {
  if (typeof window === "undefined") return "/workspace";
  const parts = window.location.pathname.split("/").filter(Boolean);
  return parts[0] === "company" ? "/company" : "/workspace";
};

function Drawer({ title, children, onClose }: { title: string; children: React.ReactNode; onClose: () => void }) {
  return <div className="currency-drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
    <aside className="currency-drawer" role="dialog" aria-modal="true" aria-label={title}>
      <header><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X size={18} /></button><h2>{title}</h2></header>
      <div className="currency-drawer-body">{children}</div>
    </aside>
  </div>;
}

export function CurrencySettingsPage({ user }: { user: User }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const customRatesCode = searchParams.get("currency");
  const view = searchParams.get("view") === "custom-rates" || Boolean(customRatesCode) ? "custom-rates" : "currencies";
  const { data, error, loading, refresh } = useResource(() => listERPCatalog("currency", true), `${user.id}:currency-catalog`);
  const [includeInactive, setIncludeInactive] = useState(false);
  const [currencyDrawer, setCurrencyDrawer] = useState(false);
  const [rateDrawer, setRateDrawer] = useState<{ currency: ERPCatalogEntry; rate?: CustomRate }>();
  const [currencySearch, setCurrencySearch] = useState("");
  const [selectedCode, setSelectedCode] = useState(customRatesCode ?? "");
  const [message, setMessage] = useState("");
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);

  const currencies = useMemo(() => (data ?? []).map((entry) => ({ ...entry, parsedData: asCurrencyData(entry) })), [data]);
  const activeCurrencies = currencies.filter((entry) => includeInactive || entry.isActive);
  const selectedCurrency = currencies.find((entry) => entry.code === (selectedCode || customRatesCode)) ?? currencies[0];

  if (loading) return <LoadingState variant="table" />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;

  async function saveCurrency(currency: SupportedCurrency) {
    setBusy(true); setFailure("");
    try {
      const existing = currencies.find((entry) => entry.code === currency.code);
      if (existing) {
        await updateERPCatalog("currency", existing.id, { name: currency.name, code: currency.code, active: true, data: { ...asCurrencyData(existing), symbol: currency.symbol } });
      } else {
        await createERPCatalog("currency", { name: currency.name, code: currency.code, active: true, data: { symbol: currency.symbol, rate: null, customRates: [] } });
      }
      setCurrencyDrawer(false); setCurrencySearch(""); setMessage(`${currency.code} activated.`); await refresh();
    } catch (e) { setFailure(e instanceof Error ? e.message : "Unable to activate currency."); }
    finally { setBusy(false); }
  }

  async function archiveCurrency(entry: ERPCatalogEntry) {
    if (!window.confirm(`Deactivate ${entry.code} (${entry.name})? Existing documents will remain unchanged.`)) return;
    setBusy(true); setFailure("");
    try { await archiveERPCatalog("currency", entry.id); setMessage(`${entry.code} deactivated.`); await refresh(); }
    catch (e) { setFailure(e instanceof Error ? e.message : "Unable to deactivate currency."); }
    finally { setBusy(false); }
  }

  async function saveRate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!rateDrawer) return;
    const form = new FormData(event.currentTarget);
    const startDate = String(form.get("startDate") || "");
    const endDate = String(form.get("endDate") || "");
    const rate = String(form.get("rate") || "").trim();
    if (!startDate || !endDate || startDate > endDate) { setFailure("The effective date range is invalid."); return; }
    if (!rate || !Number.isFinite(Number(rate)) || Number(rate) <= 0) { setFailure("Enter a custom rate greater than zero."); return; }
    const current = asCurrencyData(rateDrawer.currency);
    const nextRate: CustomRate = { id: rateDrawer.rate?.id ?? (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `rate-${Date.now()}-${Math.random().toString(36).slice(2)}`), startDate, endDate, rate };
    const overlaps = (current.customRates ?? []).some((item) => item.id !== nextRate.id && item.startDate <= endDate && startDate <= item.endDate);
    if (overlaps) { setFailure("This effective period overlaps an existing custom rate."); return; }
    setBusy(true); setFailure("");
    try {
      await updateERPCatalog("currency", rateDrawer.currency.id, { name: rateDrawer.currency.name, code: rateDrawer.currency.code ?? undefined, active: rateDrawer.currency.isActive, data: { ...current, customRates: [...(current.customRates ?? []).filter((item) => item.id !== nextRate.id), nextRate].sort((a, b) => b.startDate.localeCompare(a.startDate)) } });
      setRateDrawer(undefined); setMessage("Custom rate saved."); await refresh();
    } catch (e) { setFailure(e instanceof Error ? e.message : "Unable to save custom rate."); }
    finally { setBusy(false); }
  }

  async function deleteRate(entry: ERPCatalogEntry, rate: CustomRate) {
    if (!window.confirm(`Delete the ${rate.rate} rate for ${rate.startDate} to ${rate.endDate}?`)) return;
    const current = asCurrencyData(entry);
    setBusy(true); setFailure("");
    try { await updateERPCatalog("currency", entry.id, { name: entry.name, code: entry.code ?? undefined, active: entry.isActive, data: { ...current, customRates: (current.customRates ?? []).filter((item) => item.id !== rate.id) } }); setMessage("Custom rate deleted."); await refresh(); }
    catch (e) { setFailure(e instanceof Error ? e.message : "Unable to delete custom rate."); }
    finally { setBusy(false); }
  }

  if (view === "custom-rates") {
    const entry = selectedCurrency;
    if (!entry) return <><PageHeader title="Custom Rates" description="No currencies are active yet." actions={<button type="button" className="btn primary" onClick={() => setCurrencyDrawer(true)}><Plus size={16} /> New currency</button>} /><Panel title="Custom Rates"><div className="empty-state">Activate a currency first to define effective exchange rates.</div></Panel></>;
    const rates = [...(entry.parsedData.customRates ?? [])].sort((a, b) => b.startDate.localeCompare(a.startDate));
    return <div className="professional-admin-page currency-settings-page">
      <PageHeader title="Custom Rates" description="User-defined override rates used by the system." actions={<button type="button" className="btn primary" onClick={() => setRateDrawer({ currency: entry })}><Plus size={16} /> New</button>} />
      {failure && <div className="notice danger">{failure}</div>}
      <Panel title="Current Effective Rate">
        <div className="currency-current-rate"><label>Currency<select value={entry.code ?? ""} onChange={(event) => { setSelectedCode(event.target.value); router.push(`${routeBase()}/settings?view=custom-rates&currency=${encodeURIComponent(event.target.value)}`); }}>{currencies.map((item) => <option key={item.id} value={item.code ?? ""}>{item.code} — {item.name}</option>)}</select></label><strong>{effectiveRate(entry.parsedData) == null ? "—" : `RM ${effectiveRate(entry.parsedData)}`}<small>1 {entry.code} = current configured rate</small></strong></div>
      </Panel>
      <Panel title={`${entry.code} custom rates`}>
        <div className="currency-table-wrap"><table className="currency-table"><thead><tr><th>Start Date</th><th>End Date</th><th>Rate</th><th>Actions</th></tr></thead><tbody>{rates.length ? rates.map((rate) => <tr key={rate.id}><td>{dateLabel(rate.startDate)}</td><td>{dateLabel(rate.endDate)}</td><td>RM <strong>{rateLabel(rate.rate)}</strong></td><td><button type="button" className="btn small" disabled={busy} onClick={() => setRateDrawer({ currency: entry, rate })}><Pencil size={14} /> Edit</button><button type="button" className="icon-button" aria-label={`Delete rate ${rate.rate}`} disabled={busy} onClick={() => void deleteRate(entry, rate)}><Trash2 size={14} /></button></td></tr>) : <tr><td colSpan={4}><div className="empty-state">No custom rates configured for {entry.code}.</div></td></tr>}</tbody></table></div>
      </Panel>
      {rateDrawer && <Drawer title={`${rateDrawer.rate ? "Edit" : "New"} Custom Rate`} onClose={() => { setRateDrawer(undefined); setFailure(""); }}><form className="settings-drawer-form" onSubmit={(event) => void saveRate(event)}><label>Currency<input readOnly value={`${rateDrawer.currency.code} — ${rateDrawer.currency.name}`} /></label><label>Effective Period<select name="period" defaultValue="custom"><option value="custom">Custom</option><option value="month">This month</option><option value="year">This year</option></select></label><div className="settings-date-range"><label>Start date<input name="startDate" type="date" required defaultValue={rateDrawer.rate?.startDate ?? new Date().toISOString().slice(0, 10)} /></label><label>End date<input name="endDate" type="date" required defaultValue={rateDrawer.rate?.endDate ?? new Date().toISOString().slice(0, 10)} /></label></div><label>Custom Rate<input name="rate" type="number" min="0.000001" step="0.000001" required defaultValue={rateDrawer.rate?.rate ?? ""} placeholder="e.g. 0.614166" /></label><p className="form-hint">Rate means 1 {rateDrawer.currency.code} equals this amount in the workspace base currency.</p><div className="drawer-actions"><button type="button" className="btn" onClick={() => setRateDrawer(undefined)}>Cancel</button><button type="submit" className="btn primary" disabled={busy}>{busy ? "Saving…" : "Save"}</button></div></form></Drawer>}
      <Toast message={message} />
    </div>;
  }

  const filteredSupported = SUPPORTED_CURRENCIES.filter((currency) => `${currency.code} ${currency.name}`.toLowerCase().includes(currencySearch.toLowerCase()));
  return <div className="professional-admin-page currency-settings-page">
    <PageHeader title="Currencies" description="Currencies that can be used in transactions." actions={<button type="button" className="btn primary" onClick={() => setCurrencyDrawer(true)}><Plus size={16} /> New</button>} />
    {failure && <div className="notice danger">{failure}</div>}
    <Panel title="Currencies"><label className="currency-include-inactive"><input type="checkbox" checked={includeInactive} onChange={(event) => setIncludeInactive(event.target.checked)} /> Include inactive</label><div className="currency-table-wrap"><table className="currency-table"><thead><tr><th>Code</th><th>Symbol</th><th>Name</th><th>Rate</th><th>Action</th></tr></thead><tbody>{activeCurrencies.length ? activeCurrencies.map((entry) => <tr key={entry.id}><td><strong>{entry.code}</strong></td><td>{entry.parsedData.symbol ?? "—"}</td><td>{entry.name}{entry.parsedData.isBase && <small className="currency-base-badge">Base</small>}</td><td>{rateLabel(effectiveRate(entry.parsedData))}</td><td><button type="button" className="btn small" onClick={() => router.push(`${routeBase()}/settings?view=custom-rates&currency=${encodeURIComponent(entry.code ?? "")}`)}>Custom Rates</button>{entry.parsedData.isBase ? <span className="currency-muted">Active</span> : <button type="button" className="icon-button" aria-label={`Deactivate ${entry.code}`} disabled={busy} onClick={() => void archiveCurrency(entry)}><MoreHorizontal size={16} /></button>}</td></tr>) : <tr><td colSpan={5}><div className="empty-state">No currencies configured.</div></td></tr>}</tbody></table></div></Panel>
    {currencyDrawer && <Drawer title="Activate Currency" onClose={() => { setCurrencyDrawer(false); setCurrencySearch(""); }}><div className="settings-drawer-form"><label>Currency<input autoFocus value={currencySearch} onChange={(event) => setCurrencySearch(event.target.value)} placeholder="Search currency" /></label><div className="currency-picker-list">{filteredSupported.map((currency) => <button type="button" key={currency.code} disabled={busy} onClick={() => void saveCurrency(currency)}><span><strong>{currency.code}</strong> — {currency.name}</span><small>{currency.symbol}</small></button>)}{!filteredSupported.length && <div className="empty-state">No matching currencies.</div>}</div></div></Drawer>}
    <Toast message={message} />
  </div>;
}
