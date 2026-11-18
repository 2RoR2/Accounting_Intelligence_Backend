import { api } from "./client";

export type LedgerAccount = { id: string; code: string; name: string; accountType: string; isActive?: boolean; systemAccount?: string | null; description?: string | null };
export type ChartAccount = { id: string; code: string; name: string; accountClass: string; parentId?: string | null; isActive: boolean };
export type AccountingPeriod = { id: string; periodCode: string; startsOn: string; endsOn: string; status: string };
export type JournalLineInput = { accountId: string; debit?: string; credit?: string; description?: string; sourceField?: string | null };
export type Journal = { id: string; periodId: string; sourceType: string; sourceId?: string | null; status: string; date: string; description: string; totalDebit: string; totalCredit: string; idempotencyKey: string };
export type Settlement = { id: string; settlementRef: string; date?: string; direction: "money_in" | "money_out"; amount: string; allocated: string; unmatchedAmount: string; currency: string; status: string };
export type Allocation = { id: string; settlementId: string; settlementRef: string; recordId: string; amount: string; currency: string; direction: string; status: string; unmatchedSettlementAmount: string; createdAt: string; reversedAt?: string | null; reversalReason?: string | null };
export type TrialBalanceRow = { accountId: string; code: string; name: string; accountType: string; totalDebit: string; totalCredit: string; netBalance: string };
export type SupplierPaymentJournal = { id: string; status: string; idempotent: boolean; recordId: string | null; settlementId: string; amount: string; currency: string };
export type ProfitAndLossDashboard = {
  basis: "accrual" | "cash";
  fromDate: string;
  toDate: string;
  granularity: "day" | "week" | "month";
  periods: Array<{ period: string; currency: string; income: string; expenses: string; netProfit: string }>;
  totals: Array<{ currency: string; income: string; expenses: string; netProfit: string }>;
  accounts: Array<{ currency: string; accountType: "income" | "expense"; accountCode: string; accountName: string; amount: string }>;
  aging: Array<{ category: string; currency: string; bucket: string; amount: string; itemCount: number }>;
  cashBasisNote: string | null;
  accrualNote: string | null;
};

export const getLedgerAccounts = () => api<LedgerAccount[]>("/api/ledger/accounts");
export const getChartAccounts = (includeInactive = false) => api<ChartAccount[]>(`/api/chart-of-accounts?includeInactive=${includeInactive ? "true" : "false"}`);
export const createChartAccount = (payload: { code: string; name: string; accountClass: string; parentId?: string | null }) =>
  api<ChartAccount>("/api/chart-of-accounts", { method: "POST", body: JSON.stringify(payload) });
export const updateChartAccount = (accountId: string, payload: { name?: string; accountClass?: string; parentId?: string | null }) =>
  api<ChartAccount>(`/api/chart-of-accounts/${encodeURIComponent(accountId)}`, { method: "PATCH", body: JSON.stringify(payload) });
export const deactivateChartAccount = (accountId: string) =>
  api<void>(`/api/chart-of-accounts/${encodeURIComponent(accountId)}`, { method: "DELETE" });
export const getAccountingPeriods = () => api<AccountingPeriod[]>("/api/ledger/periods");
export const getLedgerJournals = () => api<Journal[]>("/api/ledger/journals");
export const createJournal = (payload: { periodId: string; idempotencyKey: string; sourceType: string; sourceId?: string; journalDate: string; description: string; lines: JournalLineInput[] }) =>
  api<Journal>("/api/ledger/journals", { method: "POST", body: JSON.stringify(payload) });
export const getSettlements = () => api<Settlement[]>("/api/ledger/settlements");
export const getAllocations = () => api<Allocation[]>("/api/ledger/allocations");
export const getTrialBalance = () => api<TrialBalanceRow[]>("/api/ledger/trial-balance");
export const getProfitAndLossDashboard = (fromDate: string, toDate: string, basis: "accrual" | "cash", granularity: "day" | "week" | "month") =>
  api<ProfitAndLossDashboard>(`/api/ledger/profit-and-loss?fromDate=${encodeURIComponent(fromDate)}&toDate=${encodeURIComponent(toDate)}&basis=${basis}&granularity=${granularity}`);

export const createSettlementFromBank = (transactionId: string) =>
  api(`/api/ledger/settlements/from-bank/${encodeURIComponent(transactionId)}`, { method: "POST" });

export const allocateSettlement = (settlementId: string, recordId: string, amount: string) =>
  api("/api/ledger/allocations", { method: "POST", body: JSON.stringify({ settlementId, recordId, amount }) });

export const reverseAllocation = (allocationId: string) =>
  api(`/api/ledger/allocations/${encodeURIComponent(allocationId)}/reverse`, { method: "POST" });

export const reverseJournal = (journalId: string) =>
  api(`/api/ledger/journals/${encodeURIComponent(journalId)}/reverse`, { method: "POST" });

export const postSupplierBill = (recordId: string, payload: { periodId: string; expenseAccountId: string; payableAccountId: string; journalDate: string; description?: string }) =>
  api(`/api/ledger/journals/from-supplier-bill/${encodeURIComponent(recordId)}`, { method: "POST", body: JSON.stringify(payload) });

export const postSupplierPayment = (transactionId: string, payload: { periodId?: string; payableAccountId?: string; bankAccountId?: string; journalDate?: string }) =>
  api<SupplierPaymentJournal>(`/api/ledger/journals/from-supplier-payment/${encodeURIComponent(transactionId)}`, { method: "POST", body: JSON.stringify(payload) });

export const postCustomerInvoice = (recordId: string, payload: { periodId: string; receivableAccountId: string; revenueAccountId: string; journalDate: string; description?: string }) =>
  api(`/api/ledger/journals/from-customer-invoice/${encodeURIComponent(recordId)}`, { method: "POST", body: JSON.stringify(payload) });

export const postDocumentAdjustment = (recordId: string, payload: { periodId: string; debitAccountId: string; creditAccountId: string; amount: string; journalDate: string; adjustmentKind: "credit_note" | "debit_note"; description?: string }) =>
  api(`/api/ledger/journals/from-adjustment/${encodeURIComponent(recordId)}`, { method: "POST", body: JSON.stringify(payload) });

export const createSettlement = (payload: { settlementRef: string; settlementDate: string; direction: "money_in" | "money_out"; amount: string; currency: string; sourceType: "bank_import" | "manual_record" | "document_evidence"; sourceId?: string }) =>
  api("/api/ledger/settlements", { method: "POST", body: JSON.stringify(payload) });
