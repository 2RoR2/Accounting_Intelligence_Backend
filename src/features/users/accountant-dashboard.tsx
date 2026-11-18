"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type ReactNode, type DragEvent } from "react";
import { ArrowRight, Files, FileCheck2, Plus } from "lucide-react";
import type { Document, User } from "@/types";
import type { BankMoneyTransaction } from "@/types/money";
import { Badge, formatDate } from "@/components/common/ui";
import { useResource } from "@/features/common/hooks/use-resource";
import { getTrends } from "@/lib/api/processing.service";
import { DocumentStackWidget } from "@/components/common/documents/document-stack-widget";
import { MalaysiaDayNight } from "@/components/common/dashboard/malaysia-day-night";
import { getMoneyTransactions } from "@/lib/api/money.service";
import { getProfitAndLossDashboard } from "@/lib/api/ledger.service";

type DashboardPeriod = "dashboard" | "today" | "week" | "month" | "quarter" | "year" | "7d" | "14d" | "30d" | "12m" | "lastMonth" | "lastQuarter" | "lastYear" | "custom";
type CustomPeriodUnit = "day" | "month";
type DashboardBasis = "accrual" | "cash";
type ChartKey = "income" | "profitLoss" | "cashflow" | "forecast" | "incomeBreakdown" | "expenseBreakdown";

const chartPeriodGroups: Array<[string, Array<[DashboardPeriod, string]>]> = [["Calendar periods", [["today", "Today"], ["week", "This Week"], ["month", "This Month"], ["quarter", "This Quarter"], ["year", "This Year"]]], ["Rolling periods", [["7d", "Last 7 Days"], ["14d", "Last 14 Days"], ["30d", "Last 30 Days"], ["12m", "Last 12 Months"]]], ["Previous periods", [["lastMonth", "Last Month"], ["lastQuarter", "Last Quarter"], ["lastYear", "Last Year"]]], ["Custom", [["custom", "Custom Date Range…"]]]];
const chartPeriodOptions = chartPeriodGroups.flatMap(([, options]) => options);

function ChartPeriodSelect({ value, customLabel, onChange }: { value: DashboardPeriod; customLabel: string; onChange: (value: DashboardPeriod) => void }) {
  return <label className="dashboard-chart-period-select" aria-label="Chart period">
    <span className="sr-only">Chart period</span>
    <select className="dashboard-chart-period-trigger" value={value} onChange={(event) => onChange(event.target.value as DashboardPeriod)}>
      <option value="dashboard">Use Dashboard Period</option>{chartPeriodGroups.map(([group, options]) => <optgroup label={group} key={group}>{options.map(([option, optionLabel]) => <option value={option} key={`period-${option}`}>{option === "custom" ? customLabel : optionLabel}</option>)}</optgroup>)}
    </select>
  </label>;
}

const localDateValue = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;

function dashboardRange(period: DashboardPeriod, customFrom: string, customTo: string, customValue = 30, customUnit: CustomPeriodUnit = "day") {
  const to = new Date();
  to.setHours(0, 0, 0, 0);
  const from = new Date(to);
  let granularity: "day" | "week" | "month" = "day";
  if (period === "today") { /* today */ }
  else if (period === "7d") from.setDate(from.getDate() - 6);
  else if (period === "week") { from.setDate(from.getDate() - ((from.getDay() + 6) % 7)); }
  else if (period === "14d") from.setDate(from.getDate() - 13);
  else if (period === "30d") from.setDate(from.getDate() - 29);
  else if (period === "month") { from.setDate(1); }
  else if (period === "quarter") { from.setMonth(Math.floor(from.getMonth() / 3) * 3, 1); granularity = "month"; }
  else if (period === "12m") { from.setDate(1); from.setMonth(from.getMonth() - 11); granularity = "month"; }
  else if (period === "year") { from.setMonth(0, 1); granularity = "month"; }
  else if (period === "lastMonth") { from.setDate(1); from.setMonth(from.getMonth() - 1); to.setDate(0); granularity = "month"; }
  else if (period === "lastQuarter") { const quarter = Math.floor(from.getMonth() / 3); from.setMonth((quarter - 1) * 3, 1); to.setMonth(quarter * 3, 0); granularity = "month"; }
  else if (period === "lastYear") { from.setFullYear(from.getFullYear() - 1, 0, 1); to.setFullYear(to.getFullYear() - 1, 11, 31); granularity = "month"; }
  else {
    const start = new Date(`${customFrom}T00:00:00`);
    const end = new Date(`${customTo}T00:00:00`);
    if (customValue > 0) {
      if (customUnit === "month") from.setMonth(from.getMonth() - customValue + 1, 1);
      else from.setDate(from.getDate() - customValue + 1);
    } else if (!Number.isNaN(start.getTime())) from.setTime(start.getTime());
    if (!Number.isNaN(end.getTime())) to.setTime(end.getTime());
    const days = Math.ceil((to.getTime() - from.getTime()) / 86400000);
    granularity = days <= 14 ? "day" : days <= 84 ? "week" : "month";
  }
  return { fromDate: localDateValue(from), toDate: localDateValue(to), granularity };
}

function dashboardAmount(value: string, currency: string) {
  const match = value.match(/^(-?)(\d+)(?:\.(\d+))?$/);
  if (!match) return `${currency} —`;
  const integer = match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const fraction = (match[3] ?? "").padEnd(2, "0").slice(0, 2);
  return `${currency} ${match[1]}${integer}.${fraction}`;
}

function dashboardPeriodLabel(value: string, granularity: "day" | "week" | "month") {
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return value;
  const label = new Intl.DateTimeFormat("en-MY", granularity === "month" ? { month: "short", year: "2-digit" } : { day: "numeric", month: "short" }).format(date);
  return granularity === "week" ? `Week of ${label}` : label;
}

const numericAmount = (value: string | number | null | undefined) => Number(value || 0);
const sumAmounts = (rows: { amount: string | number }[]) => rows.reduce((sum, row) => sum + numericAmount(row.amount), 0);
const moneyValue = (value: number, currency = "RM") => dashboardAmount(value.toFixed(2), currency);

function useProfitAndLoss(scope: string, fromDate: string, toDate: string, basis: DashboardBasis, granularity: "day" | "week" | "month") {
  const key = `${scope}:${fromDate}:${toDate}:${basis}:${granularity}`;
  const [state, setState] = useState<{ key: string; data?: Awaited<ReturnType<typeof getProfitAndLossDashboard>>; error: string; loading: boolean }>({
    key: "", error: "", loading: true,
  });
  useEffect(() => {
    let active = true;
    let request = 0;
    const refresh = async () => {
      const currentRequest = ++request;
      setState((previous) => ({ key, data: previous.key === key ? previous.data : undefined, error: "", loading: true }));
      try {
        const data = await getProfitAndLossDashboard(fromDate, toDate, basis, granularity);
        if (active && currentRequest === request) setState({ key, data, error: "", loading: false });
      } catch (error) {
        if (active && currentRequest === request) setState({
          key, error: error instanceof Error ? error.message : "Unable to load profit and loss.",
          loading: false,
        });
      }
    };
    void refresh();
    window.addEventListener("ai:changed", refresh);
    window.addEventListener("storage", refresh);
    return () => {
      active = false;
      request++;
      window.removeEventListener("ai:changed", refresh);
      window.removeEventListener("storage", refresh);
    };
  }, [key, fromDate, toDate, basis, granularity]);
  const current = state.key === key;
  return {
    data: current ? state.data : undefined,
    error: current ? state.error : "",
    loading: !current || state.loading,
  };
}

const approvedStatuses = ["COMPLETED", "STANDARDISED"];

const overdueCount = (docs: Document[], type: "INVOICE" | "BILL") => {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return docs.filter((doc) => {
    if (doc.type !== type || !doc.dueDate || approvedStatuses.includes(doc.status)) return false;
    const dueDate = new Date(doc.dueDate);
    dueDate.setHours(0, 0, 0, 0);
    return dueDate < today;
  }).length;
};

const documentAction = (status: string) => {
  if (status === "FAILED") return "View error";
  if (["EXTRACTED", "IN_REVIEW", "EXCEPTION", "CORRECTED"].includes(status)) return "Review";
  if (["UPLOADED", "QUEUED", "PROCESSING"].includes(status)) return "View progress";
  return "View record";
};

function DashboardWidget({ id, title, subtitle, onDragStart, onDrop, onKeyboardMove, children }: { id: string; title: string; subtitle?: string; onDragStart: (id: string) => void; onDrop: (id: string) => void; onKeyboardMove: (id: string, direction: -1 | 1) => void; children: ReactNode }) {
  return <section className={`dashboard-widget dashboard-widget-${id}`} draggable onDragStart={() => onDragStart(id)} onDragOver={(event) => event.preventDefault()} onDrop={() => onDrop(id)}><div className="dashboard-widget-heading"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div><button className="widget-drag-handle" type="button" draggable aria-label={`Reorder ${title}`} title="Drag this grip to reorder. Use Alt + Arrow keys to move" onDragStart={() => onDragStart(id)} onKeyDown={(event) => { if (event.altKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) { event.preventDefault(); onKeyboardMove(id, event.key === "ArrowUp" ? -1 : 1); } }}><span className="drag-dots" aria-hidden="true"><i /><i /><i /><i /><i /><i /></span></button></div>{children}</section>;
}

export function AccountantDashboard({ user, docs, exceptions, userName = "there", base = "/workspace" }: { user: User; docs: Document[]; exceptions: number | null; userName?: string; base?: string }) {
  const malaysiaHour = Number(new Intl.DateTimeFormat("en-MY", { timeZone: "Asia/Kuala_Lumpur", hour: "numeric", hour12: false }).format(new Date()));
  const greeting = malaysiaHour < 5 ? "Good night" : malaysiaHour < 12 ? "Good morning" : malaysiaHour < 17 ? "Good afternoon" : malaysiaHour < 21 ? "Good evening" : "Good night";
  const { data: trends } = useResource(() => getTrends("week"), `${user.id}:accountant-dashboard-week`);
  const { data: bankTransactions } = useResource(
    () => Promise.all([getMoneyTransactions(user, "money_in"), getMoneyTransactions(user, "money_out")]).then(([moneyIn, moneyOut]) => [...moneyIn, ...moneyOut]),
    `${user.id}:accountant-dashboard-bank-anomalies`,
  );
  const [basis, setBasis] = useState<DashboardBasis>("accrual");
  const [sharedBankAccount, setSharedBankAccount] = useState("all");
  const [sharedCostCentre, setSharedCostCentre] = useState("all");
  const [period, setPeriod] = useState<DashboardPeriod>("12m");
  const [customFrom] = useState(() => localDateValue(new Date(new Date().getFullYear(), new Date().getMonth(), 1)));
  const [customTo] = useState(() => localDateValue(new Date()));
  const [customPeriodValue, setCustomPeriodValue] = useState(30);
  const [customPeriodUnit, setCustomPeriodUnit] = useState<CustomPeriodUnit>("day");
  const [customPeriodOpen, setCustomPeriodOpen] = useState(false);
  const [chartPeriods, setChartPeriods] = useState<Record<ChartKey, DashboardPeriod>>({ income: "30d", profitLoss: "year", cashflow: "14d", forecast: "12m", incomeBreakdown: "30d", expenseBreakdown: "12m" });
  const [chartCustoms, setChartCustoms] = useState<Record<ChartKey, { value: number; unit: CustomPeriodUnit }>>({ income: { value: 30, unit: "day" }, profitLoss: { value: 12, unit: "month" }, cashflow: { value: 14, unit: "day" }, forecast: { value: 12, unit: "month" }, incomeBreakdown: { value: 30, unit: "day" }, expenseBreakdown: { value: 12, unit: "month" } });
  const [activeCustomChart, setActiveCustomChart] = useState<ChartKey | null>(null);
  const reportRange = dashboardRange(period, customFrom, customTo, customPeriodValue, customPeriodUnit);
  const { data: pnl, error: pnlError, loading: pnlLoading } = useProfitAndLoss(user.id, reportRange.fromDate, reportRange.toDate, basis, reportRange.granularity);
  const chartRange = (key: ChartKey) => { const custom = chartCustoms[key]; const selected = chartPeriods[key] === "dashboard" ? period : chartPeriods[key]; return dashboardRange(selected, customFrom, customTo, custom.value, custom.unit); };
  const incomeRange = chartRange("income");
  const profitLossRange = chartRange("profitLoss");
  const cashflowRange = chartRange("cashflow");
  const incomeBreakdownRange = chartRange("incomeBreakdown");
  const expenseBreakdownRange = chartRange("expenseBreakdown");
  const { data: incomePnl } = useProfitAndLoss(user.id, incomeRange.fromDate, incomeRange.toDate, basis, incomeRange.granularity);
  const { data: profitLossPnl } = useProfitAndLoss(user.id, profitLossRange.fromDate, profitLossRange.toDate, basis, profitLossRange.granularity);
  const { data: incomeBreakdownPnl } = useProfitAndLoss(user.id, incomeBreakdownRange.fromDate, incomeBreakdownRange.toDate, basis, incomeBreakdownRange.granularity);
  const { data: expenseBreakdownPnl } = useProfitAndLoss(user.id, expenseBreakdownRange.fromDate, expenseBreakdownRange.toDate, basis, expenseBreakdownRange.granularity);
  const [widgetOrder, setWidgetOrder] = useState(["accounting", "attention", "metrics", "bank", "activity", "review-queue", "documents", "stacks", "health", "review-activity", "finance", "transaction-insights", "actions"]);
  const [draggedWidget, setDraggedWidget] = useState<string | null>(null);
  const [editMode, setEditMode] = useState(false);
  const draggedReference = useRef<HTMLElement | null>(null);
  const referenceHandle = null;
  const dashboardLayoutKey = `accordai-dashboard-layout-${user.id}-${base}`;

  const reorderWidget = (target: string) => {
    if (!draggedWidget || draggedWidget === target) return;
    const next = [...widgetOrder]; const from = next.indexOf(draggedWidget); const to = next.indexOf(target);
    if (from < 0 || to < 0) return;
    next.splice(from, 1); next.splice(to, 0, draggedWidget); setWidgetOrder(next); setDraggedWidget(null);
  };
  const moveWidget = (id: string, direction: -1 | 1) => {
    const index = widgetOrder.indexOf(id); const target = index + direction;
    if (index < 0 || target < 0 || target >= widgetOrder.length) return;
    const next = [...widgetOrder]; [next[index], next[target]] = [next[target], next[index]]; setWidgetOrder(next);
  };

  const changeChartPeriod = (key: ChartKey, next: DashboardPeriod) => {
    if (next === "custom") {
      const custom = chartCustoms[key];
      setCustomPeriodValue(custom.value);
      setCustomPeriodUnit(custom.unit);
      setActiveCustomChart(key);
      setCustomPeriodOpen(true);
      return;
    }
    setChartPeriods((previous) => ({ ...previous, [key]: next }));
  };
  const applyCustomPeriod = () => {
    if (activeCustomChart) {
      setChartCustoms((previous) => ({ ...previous, [activeCustomChart]: { value: customPeriodValue, unit: customPeriodUnit } }));
      setChartPeriods((previous) => ({ ...previous, [activeCustomChart]: "custom" }));
      setActiveCustomChart(null);
    } else setPeriod("custom");
    setCustomPeriodOpen(false);
  };
  const referenceDrag = (id: string) => ({
    "data-reference-id": id,
    ...(editMode ? { draggable: true } : {}),
    onDragStart: (event: DragEvent<HTMLElement>) => {
      if (!editMode) return;
      draggedReference.current = event.currentTarget;
      if (id === "reference-panel") {
        const heading = event.currentTarget.querySelector("h2")?.textContent?.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
        if (heading) event.currentTarget.dataset.referenceId = `panel-${heading}`;
      }
      event.dataTransfer.effectAllowed = "move";
      event.currentTarget.classList.add("is-dragging");
    },
    onDragOver: (event: DragEvent<HTMLElement>) => {
      if (!editMode) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      event.currentTarget.classList.add("drag-over");
    },
    onDragLeave: (event: DragEvent<HTMLElement>) => event.currentTarget.classList.remove("drag-over"),
    onDrop: (event: DragEvent<HTMLElement>) => {
      if (!editMode) return;
      event.preventDefault();
      const source = draggedReference.current;
      const target = event.currentTarget;
      target.classList.remove("drag-over");
      if (!source || source === target || source.parentElement !== target.parentElement) return;
      const parent = target.parentElement;
      const targetRect = target.getBoundingClientRect();
      const insertAfter = event.clientY >= targetRect.top + targetRect.height / 2;
      if (insertAfter) parent?.insertBefore(source, target.nextSibling);
      else parent?.insertBefore(source, target);
      source.classList.remove("is-dragging");
      if (parent) window.localStorage.setItem(dashboardLayoutKey, JSON.stringify([...parent.children].map((item) => (item as HTMLElement).dataset.referenceId).filter(Boolean)));
      draggedReference.current = null;
    },
  });

  useEffect(() => {
    const saved = window.localStorage.getItem(dashboardLayoutKey);
    if (!saved) return;
    try {
      const order = JSON.parse(saved) as unknown;
      if (!Array.isArray(order)) return;
      const root = document.querySelector<HTMLElement>(".dashboard-page");
      if (!root) return;
      const nodes = new Map([...root.children].map((node) => [(node as HTMLElement).dataset.referenceId, node]));
      for (const id of order) {
        const node = nodes.get(id);
        if (node) root.appendChild(node);
      }
    } catch { /* ignore malformed saved layouts */ }
  }, [dashboardLayoutKey]);

  const processing = docs.filter((doc) => ["UPLOADED", "QUEUED", "PROCESSING"].includes(doc.status)).length;
  const review = docs.filter((doc) => ["EXTRACTED", "IN_REVIEW", "EXCEPTION", "CORRECTED"].includes(doc.status)).length;
  const failed = docs.filter((doc) => doc.status === "FAILED").length;
  const standardised = docs.filter((doc) => approvedStatuses.includes(doc.status)).length;
  const queued = docs.filter((doc) => ["UPLOADED", "QUEUED"].includes(doc.status)).length;
  const active = docs.filter((doc) => doc.status === "PROCESSING").length;
  const oldestWaiting = docs.filter((doc) => ["UPLOADED", "QUEUED", "PROCESSING"].includes(doc.status)).sort((a, b) => new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime())[0];
  const maxProcessed = Math.max(1, ...(trends ?? []).map((point) => point.processed));
  const averageProcessed = trends?.length ? Math.round(trends.reduce((sum, point) => sum + point.processed, 0) / trends.length) : 0;
  const reviewDocs = docs.filter((doc) => ["EXTRACTED", "IN_REVIEW", "EXCEPTION", "CORRECTED"].includes(doc.status)).slice(0, 4);
  const overdueInvoices = overdueCount(docs, "INVOICE");
  const overdueBills = overdueCount(docs, "BILL");
  const approvedInvoices = docs.filter((doc) => doc.type === "INVOICE" && approvedStatuses.includes(doc.status)).length;
  const approvedBills = docs.filter((doc) => doc.type === "BILL" && approvedStatuses.includes(doc.status)).length;
  const flaggedTransactions = (bankTransactions ?? []).filter((transaction) => transaction.anomaly?.isAnomaly).sort((a, b) => new Date(b.postedDate).getTime() - new Date(a.postedDate).getTime()).slice(0, 6);
  const largestFlaggedAmount = Math.max(1, ...flaggedTransactions.map((transaction) => Number(transaction.amount) || 0));
  const widgetProps = { onDragStart: setDraggedWidget, onDrop: reorderWidget, onKeyboardMove: moveWidget };
  const pnlCurrency = pnl?.totals[0]?.currency ?? "RM";
  const pnlTotal = pnl?.totals[0];
  const incomeTotal = incomePnl?.totals[0];
  const profitLossTotal = profitLossPnl?.totals[0];
  const aging = pnl?.aging ?? [];
  const outstandingInvoices = sumAmounts(aging.filter((item) => item.category === "accounts_receivable"));
  const outstandingBills = sumAmounts(aging.filter((item) => item.category !== "accounts_receivable"));
  const overdueInvoiceAmount = sumAmounts(aging.filter((item) => item.category === "accounts_receivable" && ["1_30_days", "31_60_days", "61_90_days", "over_90_days"].includes(item.bucket)));
  const overdueBillAmount = sumAmounts(aging.filter((item) => item.category !== "accounts_receivable" && ["1_30_days", "31_60_days", "61_90_days", "over_90_days"].includes(item.bucket)));
  const recentSales = docs.filter((doc) => ["EXTRACTED", "IN_REVIEW", "EXCEPTION", "CORRECTED"].includes(doc.status)).slice(0, 5);
  const incomeAccounts = (incomeBreakdownPnl?.accounts ?? []).filter((account) => account.accountType === "income").slice(0, 6);
  const expenseAccounts = (expenseBreakdownPnl?.accounts ?? []).filter((account) => account.accountType === "expense").slice(0, 6);
  const maxAccountAmount = Math.max(1, ...[...incomeAccounts, ...expenseAccounts].map((account) => numericAmount(account.amount)));
  const filterBankRows = (range: { fromDate: string; toDate: string }) => (bankTransactions ?? []).filter((row) => row.postedDate >= range.fromDate && row.postedDate <= range.toDate).slice().sort((a, b) => new Date(a.postedDate).getTime() - new Date(b.postedDate).getTime());
  const cashflowRows = filterBankRows(cashflowRange);
  const maxCashflowAmount = Math.max(1, ...cashflowRows.map((row) => numericAmount(row.amount)));
  const bankAccounts = [...new Set((bankTransactions ?? []).map((row) => row.bankAccountRef).filter(Boolean))].slice(0, 5);
  const outstandingPanels = [
    { id: "outstanding-invoices", title: "Outstanding invoices", category: "accounts_receivable" as const, href: `${base}/documents?type=INVOICE` },
    { id: "outstanding-bills", title: "Outstanding bills", category: "accounts_payable" as const, href: `${base}/documents?type=BILL` },
  ];
  const monthlyRows = (pnl?.periods ?? []).filter(() => pnl?.granularity === "month").slice(-12);
  const monthlyIncome = monthlyRows.reduce((sum, row) => sum + numericAmount(row.income), 0);
  const monthlyExpenses = monthlyRows.reduce((sum, row) => sum + numericAmount(row.expenses), 0);
  const monthlyNet = monthlyRows.reduce((sum, row) => sum + numericAmount(row.netProfit), 0);

  const reportRangeIsValid = reportRange.fromDate <= reportRange.toDate;
  const reportIsCurrent = reportRangeIsValid && pnl?.fromDate === reportRange.fromDate && pnl.toDate === reportRange.toDate && pnl.basis === basis && pnl.granularity === reportRange.granularity;
  const accountingWidget = <DashboardWidget id="accounting" title="Profit and loss" subtitle={`${basis === "accrual" ? "Accrual" : "Cash"} basis · ${reportRange.fromDate} to ${reportRange.toDate}`} {...widgetProps}>
    <div className="dashboard-accounting-controls">
      <label>Period
        <select value={period} onChange={(event) => { const next = event.target.value as DashboardPeriod; setPeriod(next); if (next === "custom") setCustomPeriodOpen(true); }}>
          <option value="7d">7-Day</option><option value="week">This Week</option><option value="14d">14-Day</option>
          <option value="30d">30-Day</option><option value="month">This Month</option><option value="12m">12-Month</option>
          <option value="year">This Year</option><option value="custom">Custom</option>
        </select>
      </label>
      <div className="dashboard-basis-toggle" role="group" aria-label="Profit and loss basis">
        <button type="button" aria-pressed={basis === "accrual"} className={basis === "accrual" ? "selected" : ""} onClick={() => setBasis("accrual")}>Accrual</button>
        <button type="button" aria-pressed={basis === "cash"} className={basis === "cash" ? "selected" : ""} onClick={() => setBasis("cash")}>Cash</button>
      </div>
      {period === "custom" && <button type="button" className="dashboard-custom-edit" onClick={() => setCustomPeriodOpen(true)}>{customPeriodValue} {customPeriodUnit === "day" ? "days" : "months"} <span aria-hidden="true">✎</span></button>}
      {customPeriodOpen && <div className="dashboard-custom-popover" role="dialog" aria-label="Custom reporting period"><button type="button" className="dashboard-custom-close" onClick={() => { setActiveCustomChart(null); setCustomPeriodOpen(false); }} aria-label="Close">×</button><strong>Custom Dates</strong><label><span>Period</span><input type="number" min="1" max="120" value={customPeriodValue} onChange={(event) => setCustomPeriodValue(Math.max(1, Number(event.target.value) || 1))} /><select className="dashboard-unit-button" value={customPeriodUnit} onChange={(event) => setCustomPeriodUnit(event.target.value as CustomPeriodUnit)}><option value="day">Day</option><option value="month">Month</option></select></label><div><button type="button" className="btn" onClick={() => { setActiveCustomChart(null); setCustomPeriodOpen(false); }}>Cancel</button><button type="button" className="btn primary" onClick={applyCustomPeriod}>Apply</button></div></div>}
    </div>
    {!reportRangeIsValid && <div className="dashboard-report-message dashboard-report-error" role="alert">The end date must be on or after the start date.</div>}
    {pnlError && !pnl && <div className="dashboard-report-message dashboard-report-error" role="alert">Profit and loss could not be loaded: {pnlError}</div>}
    {pnlLoading && !pnl && <div className="widget-empty" aria-live="polite">Loading posted accounting activity…</div>}
    {reportIsCurrent && pnl && <>
      {pnl[basis === "accrual" ? "accrualNote" : "cashBasisNote"] && <p className="dashboard-report-note">{pnl[basis === "accrual" ? "accrualNote" : "cashBasisNote"]}</p>}
      {pnl.totals.length ? <div className="dashboard-pnl-totals">{pnl.totals.map((total) => <div className="dashboard-pnl-total" key={total.currency}>
        <strong>{dashboardAmount(total.netProfit, total.currency)}</strong><span>Net profit</span>
        <small>Income {dashboardAmount(total.income, total.currency)} · Expenses {dashboardAmount(total.expenses, total.currency)}</small>
      </div>)}</div> : <div className="widget-empty">No posted profit-and-loss entries in this period. Unposted documents are not included.</div>}
      {pnl.periods.length > 0 && <div className="dashboard-pnl-periods">
        <h3>{pnl.granularity === "day" ? "Daily summary" : pnl.granularity === "week" ? "Weekly summary" : "Monthly summary"}</h3>
        <div className="dashboard-pnl-period-head"><span>Period</span><span>Income</span><span>Expenses</span><span>Net profit</span></div>
        {pnl.periods.map((row, index) => <div className="dashboard-pnl-period-row" key={`${row.currency}-${row.period}-${index}`}>
          <span>{dashboardPeriodLabel(row.period, pnl.granularity)} · {row.currency}</span>
          <strong>{dashboardAmount(row.income, row.currency)}</strong>
          <strong>{dashboardAmount(row.expenses, row.currency)}</strong>
          <strong>{dashboardAmount(row.netProfit, row.currency)}</strong>
        </div>)}
      </div>}
      {pnl.accounts.length > 0 && <div className="dashboard-pnl-accounts">
        <h3>Income and expense accounts</h3>
        {pnl.accounts.map((account) => <div className="dashboard-pnl-account" key={`${account.currency}-${account.accountType}-${account.accountCode}`}>
          <span><strong>{account.accountName}</strong><small>{account.accountCode} · {account.accountType}</small></span>
          <b>{dashboardAmount(account.amount, account.currency)}</b>
        </div>)}
      </div>}
      {pnl.aging.length > 0 && <div className="dashboard-aging">
        <h3>Open invoices and bills</h3>
        {pnl.aging.map((item) => <div className="dashboard-aging-row" key={`${item.category}-${item.currency}-${item.bucket}`}>
          <span><strong>{item.category === "accounts_receivable" ? "Customer invoice" : "Supplier bill"}</strong><small>{item.bucket.replaceAll("_", " ")} · {item.itemCount} item{item.itemCount === 1 ? "" : "s"}</small></span>
          <b>{dashboardAmount(item.amount, item.currency)}</b>
        </div>)}
        <small className="dashboard-report-note">Open balances are approved records less active settlement allocations; unmatched payments may still be outstanding.</small>
      </div>}
      <Link className="widget-inline-link" href={`${base}/ledger`}>Open ledger <ArrowRight size={15} /></Link>
    </>}
  </DashboardWidget>;

  const widgets: Record<string, ReactNode> = {
    accounting: accountingWidget,
    metrics: <DashboardWidget id="metrics" title="Document summary" subtitle="Select a metric to filter your workflow" {...widgetProps}><div className="widget-metric-grid"><Link href={`${base}/documents`}><strong>{docs.length}</strong><span>All documents</span><em className="metric-spark spark-blue" /></Link><Link href={`${base}/documents?status=PROCESSING`}><strong>{processing}</strong><span>Processing</span><em className="metric-spark spark-cyan" /></Link><Link href={`${base}/documents?status=IN_REVIEW`}><strong>{review}</strong><span>Needs review</span><em className="metric-spark spark-amber" /></Link><Link href={`${base}/records`}><strong>{standardised}</strong><span>Standardised</span><em className="metric-spark spark-green" /></Link></div><div className="widget-overdue-row"><Link href={`${base}/documents?type=INVOICE&status=OVERDUE`}><strong>{overdueInvoices}</strong><span>Overdue invoices</span><ArrowRight size={14} /></Link><Link href={`${base}/documents?type=BILL&status=OVERDUE`}><strong>{overdueBills}</strong><span>Overdue bills</span><ArrowRight size={14} /></Link></div></DashboardWidget>,
    attention: <DashboardWidget id="attention" title="Attention needed" subtitle="Failures and review work are tracked separately" {...widgetProps}><div className="widget-attention-list"><Link className="widget-attention-row" href={`${base}/documents?status=FAILED`}><span><strong>{failed} failed job{failed === 1 ? "" : "s"}</strong><small>Review the error or retry the source document.</small></span><ArrowRight size={18} /></Link><Link className="widget-attention-row" href={`${base}/documents`}><span><strong>{review} document{review === 1 ? "" : "s"} to review</strong><small>Awaiting correction or approval.</small></span><ArrowRight size={18} /></Link><Link className="widget-attention-row" href={`${base}/exceptions`}><span><strong>{exceptions === null ? "Checking open exceptions…" : `${exceptions} open exception${exceptions === 1 ? "" : "s"}`}</strong><small>Validation findings waiting for a decision; one document can have several.</small></span><ArrowRight size={18} /></Link></div></DashboardWidget>,
    activity: <DashboardWidget id="activity" title="Processing activity" subtitle="Processing attempts per day, including retries" {...widgetProps}>{trends?.length ? <div className="processing-visual"><div className="processing-visual-top"><strong>{trends.reduce((sum, point) => sum + point.processed, 0)}</strong><span>Total this week</span><span className="processing-legend"><i /> Processed <i className="failed" /> Failed (included)</span></div><div className="processing-visual-date">Current week · daily volume</div><div className="processing-chart-shell"><div className="processing-y-axis"><span>{maxProcessed}</span><span>{Math.round(maxProcessed * .66)}</span><span>{Math.round(maxProcessed * .33)}</span><span>0</span></div><div className="processing-visual-chart">{[maxProcessed, Math.round(maxProcessed * .66), Math.round(maxProcessed * .33), 0].map((tick, index) => <i className="processing-gridline" key={`gridline-${index}-${tick}`} style={{ bottom: `${(tick / maxProcessed) * 100}%` }} />)}<div className="processing-average" style={{ bottom: `${Math.max(8, (averageProcessed / maxProcessed) * 100)}%` }}><span>Avg. {averageProcessed}</span></div>{trends.map((point) => <div className="processing-bar-wrap" key={point.day}><button className="processing-bar" style={{ height: `${Math.max(12, (point.processed / maxProcessed) * 100)}%` }} title={`${point.day}: ${point.processed} processed, ${point.failed ?? 0} failed`} aria-label={`${point.day}: ${point.processed} processing attempts and ${point.failed ?? 0} failed`} /><strong>{point.processed}</strong><span>{point.day}</span></div>)}</div></div><div className="processing-readings"><span>Latest total <strong>{trends[trends.length - 1]?.processed ?? 0}</strong></span><span>Weekly total <strong>{trends.reduce((sum, point) => sum + point.processed, 0)}</strong></span></div></div> : <div className="widget-empty">Trend data will appear after documents are processed.</div>}<div className="widget-health"><strong>Processing health</strong><span>{queued} queued · {active} active{oldestWaiting ? ` · oldest ${formatDate(oldestWaiting.createdAt)}` : ""}</span></div></DashboardWidget>,
    documents: <DashboardWidget id="documents" title="Recent documents" subtitle="The latest files moving through your workflow" {...widgetProps}><div className="widget-document-mini">{docs.slice(0, 4).map((doc) => <Link key={doc.id} href={`/workspace/documents/${doc.id}`}><span><Files size={14} /><strong>{doc.name}</strong></span><Badge>{doc.status}</Badge></Link>)}{!docs.length && <div className="widget-empty">Upload an invoice, bill, or receipt to get started.</div>}</div></DashboardWidget>,
    stacks: <DashboardWidget id="stacks" title="Document types" subtitle="Your workspace at a glance" {...widgetProps}><DocumentStackWidget items={[{ label: "Invoices", count: docs.filter((doc) => doc.type === "INVOICE").length, href: "/workspace/documents?type=INVOICE", tone: "blue" }, { label: "Bills", count: docs.filter((doc) => doc.type === "BILL").length, href: "/workspace/documents?type=BILL", tone: "green" }, { label: "Receipts", count: docs.filter((doc) => doc.type === "RECEIPT").length, href: "/workspace/documents?type=RECEIPT", tone: "red" }]} /></DashboardWidget>,
    "review-queue": <DashboardWidget id="review-queue" title="Your review queue" subtitle="Documents that need your attention" {...widgetProps}><div className="widget-queue">{reviewDocs.map((doc) => <Link key={doc.id} href={`/workspace/documents/${doc.id}`}><span><FileCheck2 size={15} /><strong>{doc.name}</strong></span><ArrowRight size={15} /></Link>)}{!reviewDocs.length && <div className="widget-empty"><FileCheck2 size={20} /> No review tasks waiting.</div>}</div></DashboardWidget>,
    finance: <DashboardWidget id="finance" title="Approved document activity" subtitle="Counts only; financial results use posted entries above" {...widgetProps}><div className="widget-finance-grid"><Link href={`${base}/money-in`}><strong>{approvedInvoices}</strong><span>Approved invoices</span></Link><Link href={`${base}/money-out`}><strong>{approvedBills}</strong><span>Approved bills</span></Link><Link href={`${base}/documents?type=RECEIPT`}><strong>{docs.filter((doc) => doc.type === "RECEIPT" && approvedStatuses.includes(doc.status)).length}</strong><span>Approved receipts</span></Link><div><strong>{overdueInvoices + overdueBills}</strong><span>Documents with past due dates</span></div></div></DashboardWidget>,
    health: <DashboardWidget id="health" title="Processing health" subtitle="Current document processing status" {...widgetProps}><div className="widget-health-diagram"><div><strong>{queued}</strong><span>Queued</span></div><div><strong>{active}</strong><span>Active</span></div><div><strong>{review}</strong><span>Review</span></div><div><strong>{standardised}</strong><span>Standardised</span></div></div><div className="widget-health-line"><i /><i /><i /><i /></div><p className="widget-health-note">{oldestWaiting ? `Oldest active item: ${formatDate(oldestWaiting.createdAt)}` : "All processing stages are clear."}</p></DashboardWidget>,
    bank: <DashboardWidget id="bank" title="Bank reconciliation" subtitle="Match imported bank transactions to approved records" {...widgetProps}><div className="widget-bank-summary"><Link href={`${base}/money-in`}><strong>{approvedInvoices}</strong><span>Approved Money In</span><small>Incoming records</small></Link><Link href={`${base}/money-out`}><strong>{approvedBills}</strong><span>Approved Money Out</span><small>Outgoing records</small></Link><Link href={`${base}/money-in`}><strong>{bankTransactions ? bankTransactions.filter(row => row.status === "proposed").length : "Checking"}</strong><span>Suggested matches</span><small>Awaiting review</small></Link><Link href={`${base}/money-in`}><strong>{bankTransactions ? bankTransactions.filter(row => row.status === "needs_review").length : "Checking"}</strong><span>Needs review</span><small>Human confirmation</small></Link></div><Link className="widget-inline-link" href={`${base}/money-in`}>Review reconciliation <ArrowRight size={15} /></Link></DashboardWidget>,
    "transaction-insights": <DashboardWidget id="transaction-insights" title="Transaction insights" subtitle="Amount anomalies in imported bank rows" {...widgetProps}><div className="transaction-insights-note">Amount anomaly detection only. Duplicates and unusual frequency are not checked.</div>{flaggedTransactions.length ? <div className="transaction-insights-list">{flaggedTransactions.map((transaction: BankMoneyTransaction) => { const directionPath = transaction.direction === "money_in" ? "money-in" : "money-out"; const width = Math.max(10, ((Number(transaction.amount) || 0) / largestFlaggedAmount) * 100); return <Link className="transaction-insight-row" key={transaction.id} href={`${base}/${directionPath}?transaction=${encodeURIComponent(transaction.id)}`}><span className="transaction-insight-main"><strong>{transaction.description || "Bank transaction"}</strong><small>{transaction.postedDate} · {transaction.direction === "money_in" ? "Money In" : "Money Out"}</small><i><em style={{ width: `${width}%` }} /></i></span><span className="transaction-insight-amount">{transaction.currency ?? "MYR"} {Number(transaction.amount || 0).toLocaleString("en-MY", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}<ArrowRight size={15} /></span></Link>})}</div> : <div className="widget-empty">No amount anomalies have been flagged.</div>}</DashboardWidget>,
    "review-activity": <DashboardWidget id="review-activity" title="Recent activity" subtitle="The latest events in your accounting workflow" {...widgetProps}><div className="widget-history-list"><div className="widget-history-item"><i className="history-blue" /><div><strong>{docs.length ? `Uploaded ${docs.length} document${docs.length === 1 ? "" : "s"}` : "No documents uploaded yet"}</strong><span>{docs[0] ? formatDate(docs[0].createdAt) : "Upload a document to begin"}</span></div></div><div className="widget-history-item"><i className="history-green" /><div><strong>{standardised ? `${standardised} record${standardised === 1 ? "" : "s"} approved` : "Approval activity"}</strong><span>{standardised ? "Ready for accounting use" : "No approvals yet"}</span></div></div><div className="widget-history-item"><i className="history-red" /><div><strong>{failed ? `${failed} failed job${failed === 1 ? "" : "s"}` : "Processing is healthy"}</strong><span>{failed ? "Review the error details" : `${active} active document${active === 1 ? "" : "s"}`}</span></div></div></div></DashboardWidget>,
    actions: <DashboardWidget id="actions" title="Next actions" subtitle="Shortcuts for the accounting workflow" {...widgetProps}><div className="widget-actions"><Link href={`${base}/exceptions`}>Open exception queue <ArrowRight size={16} /></Link><Link href={`${base}/records`}>View standardised records <ArrowRight size={16} /></Link><Link href={`${base}/upload`}>Upload a new document <ArrowRight size={16} /></Link></div></DashboardWidget>,
  };

  return <div className={`dashboard-page ${editMode ? "dashboard-edit-mode" : ""}`}>
    <div className="workspace-dashboard-header dashboard-reference-header"><MalaysiaDayNight /><div><span className="workspace-dashboard-kicker">ACCOUNTING WORKSPACE</span><h1>{greeting}, {userName}</h1><p className="dashboard-header-note">A live view of what needs review, approval, reconciliation, and posting.</p></div><div className="workspace-dashboard-actions"><button type="button" className={`btn ${editMode ? "primary" : ""}`} onClick={() => setEditMode((value) => !value)}>{editMode ? "Done editing" : "Edit dashboard"}</button>{editMode && <button type="button" className="btn" onClick={() => { window.localStorage.removeItem(dashboardLayoutKey); window.location.reload(); }}>Restore default</button>}<Link className="btn" href={`${base}/documents`}>Search documents</Link><Link className="btn primary" href={`${base}/upload`}><Plus size={15} /> Upload documents</Link></div></div>
    {customPeriodOpen && <div className="dashboard-custom-popover dashboard-custom-popover-root" role="dialog" aria-modal="true" aria-label="Custom reporting period"><button type="button" className="dashboard-custom-close" onClick={() => { setActiveCustomChart(null); setCustomPeriodOpen(false); }} aria-label="Close">×</button><strong>Custom Dates</strong><label><span>Period</span><input type="number" min="1" max="120" value={customPeriodValue} onChange={(event) => setCustomPeriodValue(Math.max(1, Number(event.target.value) || 1))} /><select className="dashboard-unit-button" value={customPeriodUnit} onChange={(event) => setCustomPeriodUnit(event.target.value as CustomPeriodUnit)}><option value="day">Day</option><option value="month">Month</option></select></label><div><button type="button" className="btn" onClick={() => { setActiveCustomChart(null); setCustomPeriodOpen(false); }}>Cancel</button><button type="button" className="btn primary" onClick={applyCustomPeriod}>Apply</button></div></div>}
    <section className="dashboard-daily-work" aria-label="Dashboard filters and today's work">
      <div className="dashboard-shared-filters"><label>Period<select value={period} onChange={(event) => setPeriod(event.target.value as DashboardPeriod)}>{chartPeriodOptions.filter(([value]) => value !== "custom").map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label><label>Bank account<select value={sharedBankAccount} onChange={(event) => setSharedBankAccount(event.target.value)}><option value="all">All bank accounts</option>{bankAccounts.map((account) => <option value={String(account)} key={String(account)}>{account}</option>)}</select></label><label>Cost centre<select value={sharedCostCentre} onChange={(event) => setSharedCostCentre(event.target.value)}><option value="all">All cost centres</option></select></label></div>
      <div className="dashboard-daily-work-grid"><Link href={`${base}/documents?status=IN_REVIEW`}><strong>{review}</strong><span>Documents awaiting review</span><ArrowRight size={14} /></Link><Link href={`${base}/exceptions`}><strong>{exceptions === null ? "—" : exceptions}</strong><span>Open validation findings</span><ArrowRight size={14} /></Link><Link href={`${base}/approvals`}><strong>{docs.filter((doc) => doc.status === "IN_REVIEW").length}</strong><span>Pending approvals</span><ArrowRight size={14} /></Link><Link href={`${base}/reconciliation`}><strong>{bankTransactions ? bankTransactions.filter((row) => row.status === "needs_review").length : "—"}</strong><span>Bank items to reconcile</span><ArrowRight size={14} /></Link></div>
    </section>
    <section className="dashboard-reference-overview" aria-label="Financial overview" {...referenceDrag("financial-overview")}>
      <div className="dashboard-reference-kpis" {...referenceDrag("reference-kpis")}>
        <Link href={`${base}/records?balance=outstanding&category=accounts_receivable`} className="dashboard-reference-kpi kpi-invoice"><span>INVOICES</span><strong>{moneyValue(outstandingInvoices, pnlCurrency)}</strong><small>Outstanding</small><b>＋</b></Link>
        <Link href={`${base}/records?balance=outstanding&category=accounts_receivable&overdue=true`} className="dashboard-reference-kpi kpi-invoice"><span>INVOICES</span><strong>{moneyValue(overdueInvoiceAmount, pnlCurrency)}</strong><small>Overdue</small><b>＋</b></Link>
        <Link href={`${base}/records?balance=outstanding&category=accounts_payable`} className="dashboard-reference-kpi kpi-bill"><span>BILLS</span><strong>{moneyValue(outstandingBills, pnlCurrency)}</strong><small>Outstanding</small><b>−</b></Link>
        <Link href={`${base}/records?balance=outstanding&category=accounts_payable&overdue=true`} className="dashboard-reference-kpi kpi-bill"><span>BILLS</span><strong>{moneyValue(overdueBillAmount, pnlCurrency)}</strong><small>Overdue</small><b>−</b></Link>
      </div>
      <div className="dashboard-reference-two-column">
         {outstandingPanels.map((panel) => {
           const rows = aging.filter((item) => item.category === panel.category); const total = Math.max(1, sumAmounts(rows));
           return <section key={panel.id} className="dashboard-reference-panel" {...referenceDrag(panel.id)}><div className="dashboard-reference-panel-heading"><h2>{panel.title}</h2><Link href={panel.href}>View details <ArrowRight size={13} /></Link></div><div className="dashboard-aging-bar">{rows.map((item, index) => { const rowKey = `${panel.id}-bar-${item.currency ?? "unknown"}-${item.bucket ?? "unknown"}-${index}`; return <Link href={`${panel.href}&age=${item.bucket}`} key={rowKey} className={`aging-segment aging-${index % 5}`} style={{ width: `${Math.max(1, (numericAmount(item.amount) / total) * 100)}%` }} title={`${item.bucket.replaceAll("_", " ")}: ${moneyValue(numericAmount(item.amount), item.currency)}`} />; })}</div><div className="dashboard-aging-legend">{rows.map((item, index) => { const rowKey = `${panel.id}-legend-${item.currency ?? "unknown"}-${item.bucket ?? "unknown"}-${index}`; return <span key={rowKey}><i className={`aging-dot aging-${index % 5}`} />{item.bucket.replaceAll("_", " ")}</span>; })}{!rows.length && <span>No approved outstanding balances</span>}</div></section>;
        })}
      </div>
      <div className="dashboard-reference-two-column">
        <section className="dashboard-reference-panel dashboard-chart-panel" {...referenceDrag("reference-panel")}><div className="dashboard-reference-panel-heading"><div><h2>Income</h2><strong>{incomeTotal ? moneyValue(numericAmount(incomeTotal.income), pnlCurrency) : "—"}</strong><small> {chartPeriods.income === "custom" ? `${chartCustoms.income.value}-${chartCustoms.income.unit}` : chartPeriods.income}</small></div><div className="dashboard-panel-actions">{referenceHandle}<ChartPeriodSelect value={chartPeriods.income} customLabel={`${chartCustoms.income.value} ${chartCustoms.income.unit === "day" ? "Days" : "Months"}`} onChange={(value) => changeChartPeriod("income", value)} /></div></div><div className="dashboard-mini-chart">{(incomePnl?.periods ?? []).slice(-16).map((row, index) => <span key={`${row.period}-${index}`} style={{ height: `${Math.max(4, (numericAmount(row.income) / Math.max(1, ...((incomePnl?.periods ?? []).map((item) => numericAmount(item.income))))) * 100)}%` }} title={`${row.period}: ${moneyValue(numericAmount(row.income), row.currency)}`} />)}</div></section>
        <section className="dashboard-reference-panel dashboard-chart-panel" {...referenceDrag("reference-panel")}><div className="dashboard-reference-panel-heading"><div><h2>Profit &amp; Loss</h2><strong>{profitLossTotal ? moneyValue(numericAmount(profitLossTotal.netProfit), pnlCurrency) : "—"}</strong><small> {chartPeriods.profitLoss === "custom" ? `${chartCustoms.profitLoss.value}-${chartCustoms.profitLoss.unit}` : chartPeriods.profitLoss}</small></div><div className="dashboard-panel-actions">{referenceHandle}<ChartPeriodSelect value={chartPeriods.profitLoss} customLabel={`${chartCustoms.profitLoss.value} ${chartCustoms.profitLoss.unit === "day" ? "Days" : "Months"}`} onChange={(value) => changeChartPeriod("profitLoss", value)} /><Link href={`${base}/ledger`}>Open ledger <ArrowRight size={13} /></Link></div></div><div className="dashboard-mini-chart pnl-chart">{(profitLossPnl?.periods ?? []).slice(-12).map((row, index) => <span key={`${row.period}-pnl-${index}`} className={numericAmount(row.netProfit) < 0 ? "negative" : "positive"} style={{ height: `${Math.max(4, (Math.abs(numericAmount(row.netProfit)) / Math.max(1, ...((profitLossPnl?.periods ?? []).map((item) => Math.abs(numericAmount(item.netProfit)))))) * 100)}%` }} title={`${row.period}: ${moneyValue(numericAmount(row.netProfit), row.currency)}`} />)}</div></section>
      </div>
      <div className="dashboard-reference-two-column">
        <section className="dashboard-reference-panel dashboard-chart-panel" {...referenceDrag("reference-panel")}><div className="dashboard-reference-panel-heading"><h2>Cashflow trend</h2><div className="dashboard-panel-actions">{referenceHandle}<ChartPeriodSelect value={chartPeriods.cashflow} customLabel={`${chartCustoms.cashflow.value} ${chartCustoms.cashflow.unit === "day" ? "Days" : "Months"}`} onChange={(value) => changeChartPeriod("cashflow", value)} /><Link href={`${base}/money-in`}>Review bank <ArrowRight size={13} /></Link></div></div><div className="dashboard-cashflow-chart">{cashflowRows.map((row, index) => <span key={`${row.id}-${index}`} className={row.direction === "money_out" ? "out" : "in"} style={{ height: `${Math.max(5, (numericAmount(row.amount) / maxCashflowAmount) * 100)}%` }} title={`${row.postedDate}: ${row.currency} ${row.amount}`} />)}{!cashflowRows.length && <div className="widget-empty">No imported bank activity yet.</div>}</div><small className="dashboard-chart-note">Actual imported bank transactions only.</small></section>
        <section className="dashboard-reference-panel dashboard-list-panel"><div className="dashboard-reference-panel-heading"><h2>Documents needing review</h2><Link href={`${base}/documents?type=INVOICE&status=IN_REVIEW`}>View all <ArrowRight size={13} /></Link></div>{recentSales.map((doc, index) => <Link className="dashboard-reference-list-row" href={`${base}/documents/${doc.id}`} key={`${doc.id}-${index}`}><span>{formatDate(doc.createdAt)}</span><strong>{doc.name}</strong><Badge>{doc.status}</Badge></Link>)}{!recentSales.length && <div className="widget-empty">No documents currently need review.</div>}</section>
      </div>
      <div className="dashboard-reference-two-column">
        <section className="dashboard-reference-panel dashboard-chart-panel dashboard-forecast-panel" aria-label="Cashflow forecast"><div className="dashboard-reference-panel-heading"><h2>Cashflow forecast</h2></div><div className="widget-empty">Forecast is not yet enabled for production reporting.</div></section>
        <section className="dashboard-reference-panel dashboard-list-panel"><div className="dashboard-reference-panel-heading"><h2>Bank accounts</h2><Link href={`${base}/money-in`}>Manage <ArrowRight size={13} /></Link></div>{bankAccounts.map((account, index) => <Link className="dashboard-reference-list-row" href={`${base}/money-in?account=${encodeURIComponent(String(account))}`} key={`${account}-${index}`}><strong>{account}</strong><span>Imported activity</span><ArrowRight size={13} /> </Link>)}{!bankAccounts.length && <div className="widget-empty">No bank accounts with imported activity.</div>}</section>
      </div>
      <div className="dashboard-reference-two-column">
        <section className="dashboard-reference-panel dashboard-breakdown" {...referenceDrag("reference-panel")}><div className="dashboard-reference-panel-heading"><div><h2>Income breakdown</h2><strong>{incomeTotal ? moneyValue(numericAmount(incomeTotal.income), pnlCurrency) : "—"}</strong></div><div className="dashboard-panel-actions">{referenceHandle}<ChartPeriodSelect value={chartPeriods.incomeBreakdown} customLabel={`${chartCustoms.incomeBreakdown.value} ${chartCustoms.incomeBreakdown.unit === "day" ? "Days" : "Months"}`} onChange={(value) => changeChartPeriod("incomeBreakdown", value)} /><Link href={`${base}/ledger`}>Details <ArrowRight size={13} /></Link></div></div>{incomeAccounts.map((account, index) => <div className="dashboard-breakdown-row" key={`${account.accountCode}-${index}`}><span><i className={`breakdown-dot dot-${index % 5}`} />{account.accountName}</span><strong>{moneyValue(numericAmount(account.amount), account.currency)}</strong><em><b style={{ width: `${Math.max(3, (numericAmount(account.amount) / maxAccountAmount) * 100)}%` }} /></em></div>)}{!incomeAccounts.length && <div className="widget-empty">No income accounts in this period.</div>}</section>
        <section className="dashboard-reference-panel dashboard-breakdown" {...referenceDrag("reference-panel")}><div className="dashboard-reference-panel-heading"><div><h2>Expense breakdown</h2><strong>{expenseBreakdownPnl?.totals[0] ? moneyValue(numericAmount(expenseBreakdownPnl.totals[0].expenses), pnlCurrency) : "—"}</strong></div><div className="dashboard-panel-actions">{referenceHandle}<ChartPeriodSelect value={chartPeriods.expenseBreakdown} customLabel={`${chartCustoms.expenseBreakdown.value} ${chartCustoms.expenseBreakdown.unit === "day" ? "Days" : "Months"}`} onChange={(value) => changeChartPeriod("expenseBreakdown", value)} /><Link href={`${base}/ledger`}>Details <ArrowRight size={13} /></Link></div></div>{expenseAccounts.map((account, index) => <div className="dashboard-breakdown-row" key={`${account.accountCode}-${index}`}><span><i className={`breakdown-dot dot-${index % 5}`} />{account.accountName}</span><strong>{moneyValue(numericAmount(account.amount), account.currency)}</strong><em><b style={{ width: `${Math.max(3, (numericAmount(account.amount) / maxAccountAmount) * 100)}%` }} /></em></div>)}{!expenseAccounts.length && <div className="widget-empty">No expense accounts in this period.</div>}</section>
      </div>
    </section>
    <section className="dashboard-accounting-summary" aria-label="Monthly accounting summary" {...referenceDrag("monthly-accounting-summary")}>
      <section className="dashboard-reference-panel monthly-summary-panel"><div className="dashboard-reference-panel-heading"><div><h2>Monthly summary</h2><p className="dashboard-panel-help">The month-by-month view an accountant uses for review and close.</p></div><Link href={`${base}/ledger`}>Open ledger <ArrowRight size={13} /></Link></div><div className="monthly-summary-grid"><div><span>Income</span><strong>{moneyValue(monthlyIncome, pnlCurrency)}</strong></div><div><span>Expenses</span><strong>{moneyValue(monthlyExpenses, pnlCurrency)}</strong></div><div><span>Net result</span><strong className={monthlyNet < 0 ? "negative-value" : "positive-value"}>{moneyValue(monthlyNet, pnlCurrency)}</strong></div><div><span>Months shown</span><strong>{monthlyRows.length || "—"}</strong></div></div></section>
      <section className="dashboard-reference-panel monthly-ledger-panel"><div className="dashboard-reference-panel-heading"><div><h2>Monthly ledger</h2><p className="dashboard-panel-help">Income, expenses, and net result by month.</p></div><Link href={`${base}/ledger`}>View all <ArrowRight size={13} /></Link></div><div className="monthly-ledger-table"><div className="monthly-ledger-head"><span>Month</span><span>Income</span><span>Expenses</span><span>Net</span></div>{monthlyRows.map((row, index) => <Link href={`${base}/ledger?period=${encodeURIComponent(row.period)}`} className="monthly-ledger-row" key={`${row.period}-ledger-${index}`}><span>{dashboardPeriodLabel(row.period, "month")}</span><strong>{moneyValue(numericAmount(row.income), row.currency)}</strong><strong>{moneyValue(numericAmount(row.expenses), row.currency)}</strong><strong className={numericAmount(row.netProfit) < 0 ? "negative-value" : "positive-value"}>{moneyValue(numericAmount(row.netProfit), row.currency)}</strong></Link>)}{!monthlyRows.length && <div className="widget-empty">Monthly ledger data appears after posted journals are available.</div>}</div></section>
    </section>
    <section className="dashboard-priority-strip" aria-label="Accountant priorities" {...referenceDrag("accountant-priorities")}>
      <Link className="dashboard-priority-card priority-blue" href={`${base}/documents?type=INVOICE&status=OVERDUE`}><span>Receivables</span><strong>{overdueInvoices}</strong><small>overdue invoices</small><ArrowRight size={15} /></Link>
      <Link className="dashboard-priority-card priority-purple" href={`${base}/documents?type=BILL&status=OVERDUE`}><span>Payables</span><strong>{overdueBills}</strong><small>overdue bills</small><ArrowRight size={15} /></Link>
      <Link className="dashboard-priority-card priority-amber" href={`${base}/documents?status=IN_REVIEW`}><span>Review queue</span><strong>{review}</strong><small>documents awaiting review</small><ArrowRight size={15} /></Link>
      <Link className="dashboard-priority-card priority-red" href={`${base}/exceptions`}><span>Exceptions</span><strong>{exceptions === null ? "—" : exceptions}</strong><small>open validation findings</small><ArrowRight size={15} /></Link>
      <Link className="dashboard-priority-card priority-green" href={`${base}/money-in`}><span>Bank review</span><strong>{bankTransactions ? bankTransactions.filter((row) => row.status === "needs_review").length : "—"}</strong><small>transactions to reconcile</small><ArrowRight size={15} /></Link>
    </section>
    <section className="workspace-dashboard-layout" {...referenceDrag("dashboard-section")}><section className="workspace-documents-card"><div className="workspace-documents-toolbar"><div><span className="workspace-dashboard-kicker">RECENT DOCUMENTS</span><h2>Recent document activity</h2><p>{docs.length} documents in this workspace</p></div><Link className="btn" href={`${base}/documents`}>View all <ArrowRight size={14} /></Link></div><div className="workspace-document-table"><div className="workspace-document-row workspace-document-head"><span>Document</span><span>Type</span><span>Uploaded</span><span>Status</span><span>Action</span></div>{docs.slice(0, 6).map((doc) => <Link className="workspace-document-row" key={doc.id} href={`/workspace/documents/${doc.id}`}><strong><Files size={15} />{doc.name}</strong><span>{doc.type.replaceAll("_", " ")}</span><span>{formatDate(doc.createdAt)}</span><Badge>{doc.status}</Badge><span className="document-row-action">{documentAction(doc.status)} <ArrowRight size={13} /></span></Link>)}{!docs.length && <div className="workspace-document-empty">No documents yet. Upload an invoice, bill, or receipt to begin.</div>}</div></section></section>
  </div>;
}
