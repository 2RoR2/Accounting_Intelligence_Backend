import type { User } from "@/types";
import { api, get } from "@/lib/api/client";
import type {
  BankMoneyTransaction,
  BankMoneyTransactionDetail,
  BankReconciliationCandidate,
  CashSummary,
  MoneyDirection,
} from "@/types/money";

type BankTransactionResponse = Omit<BankMoneyTransaction, "amount" | "amountDelta" | "currency" | "createdAt"> & {
  amount: string | null;
  amountDelta: string | null;
  currency: string | null;
  createdAt: string | null;
  importId: string;
  sourceRow: number;
  candidateId?: string | null;
  expectedRevision?: number | null;
  candidates?: BankReconciliationCandidate[];
};

function mapTransaction(row: BankTransactionResponse): BankMoneyTransactionDetail {
  return {
    ...row,
    amount: row.amount,
    amountDelta: row.amountDelta,
    currency: row.currency ?? "MYR",
    createdAt: row.createdAt,
    candidates: row.candidates ?? [],
    candidateId: row.candidateId ?? null,
    expectedRevision: row.expectedRevision ?? null,
  };
}

export async function getMoneyTransactions(
  _user: User,
  direction: MoneyDirection,
): Promise<BankMoneyTransaction[]> {
  const rows = await get<BankTransactionResponse[]>(
    `/api/bank/transactions?direction=${encodeURIComponent(direction)}`,
  );
  return rows.map(mapTransaction);
}

export async function getMoneyTransactionDetail(
  _user: User,
  id: string,
): Promise<BankMoneyTransactionDetail | undefined> {
  const row = await get<BankTransactionResponse>(`/api/bank/transactions/${encodeURIComponent(id)}`);
  return mapTransaction(row);
}

export async function decideMatch(
  _user: User,
  candidateId: string,
  decision: "confirm" | "reject",
  expectedRevision: number,
  reason?: string,
): Promise<void> {
  await api<{ id: string; candidateId: string; decision: string; settlementId: string | null }>(`/api/bank/reconciliation/candidates/${encodeURIComponent(candidateId)}/decision`, {
    method: "POST",
    body: JSON.stringify({
      candidateId,
      decision: decision === "confirm" ? "confirmed" : "rejected",
      expectedRevision,
      ...(reason?.trim() ? { reason: reason.trim() } : {}),
    }),
  });
}

export type ManualMatchRecord = { recordId: string; recordCode: string; documentId: string; category: string; party: string | null; reference: string | null; invoiceNumber: string | null; currency: string | null; totalAmount: string | null; allocatedAmount: string; remainingAmount: string };
export const searchMatchRecords = (id: string, query: string) => get<ManualMatchRecord[]>(`/api/bank/transactions/${encodeURIComponent(id)}/match-search?q=${encodeURIComponent(query)}`);
export const createManualMatchCandidate = (bankTransactionId: string, recordId: string) => api(`/api/bank/reconciliation/candidates`, { method: "POST", body: JSON.stringify({ bankTransactionId, recordId, proposedBy: "accountant", reasonCodes: ["MANUAL_SELECTION"] }) });

export interface BankImportPreview {
  importId: string;
  rowCount: number;
  duplicateRowsSkipped: number;
  needsReviewCount: number;
  canConfirm: boolean;
  status: "previewed";
  sampleRows: Array<{
    sourceRow: number;
    postedDate: string | null;
    description: string;
    reference: string | null;
    debit: string | null;
    credit: string | null;
    direction: MoneyDirection | null;
    currency: string | null;
    status: string;
    reviewReasons: string[];
  }>;
}

export async function previewBankImport(
  _user: User,
  file: File,
  accountRef: string,
  mapping: Record<string, string>,
  dateFormat: "iso" | "dmy" | "mdy",
  defaultCurrency = "MYR",
): Promise<BankImportPreview> {
  const form = new FormData();
  form.set("file", file);
  form.set("source_bank_account_ref", accountRef);
  form.set("mapping", JSON.stringify(mapping));
  form.set("date_format", dateFormat);
  form.set("default_currency", defaultCurrency);
  return api<BankImportPreview>("/api/bank/imports", { method: "POST", body: form });
}

export async function inspectBankStatement(file: File): Promise<{ format: string; headers: string[] }> {
  const form = new FormData();
  form.set("file", file);
  form.set("inspect_only", "true");
  return api<{ format: string; headers: string[] }>("/api/bank/imports", { method: "POST", body: form });
}

export async function confirmBankImport(_user: User, importId: string) {
  return api<{ importId: string; status: string; rowCount: number; candidatesProposed: number }>(
    `/api/bank-imports/${encodeURIComponent(importId)}/confirm`,
    { method: "POST" },
  );
}

export async function discardBankImportPreview(_user: User, importId: string) {
  return api<{ importId: string; status: string }>(
    `/api/bank-imports/${encodeURIComponent(importId)}/discard`,
    { method: "POST" },
  );
}

export async function ignoreBankTransaction(_user: User, id: string): Promise<boolean> {
  const result = await api<{ id: string; status: string }>(
    `/api/bank/transactions/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  );
  return result.status === "ignored";
}

export type CodingAccount = { id: string; code: string; name: string; accountType?: string };
export type CostCentre = { id: string; code: string; name: string };

export const getCodingAccounts = () => get<CodingAccount[]>("/api/coa/accounts");
export const getCodingCostCentres = () => get<CostCentre[]>("/api/cost-centres");

export async function codeBankTransaction(
  _user: User,
  id: string,
  payload: { accountId: string; costCentreId?: string; decision?: "accepted" | "overridden"; reason?: string },
) {
  return api<{ id: string; bankTransactionId: string; accountId: string; costCentreId?: string | null; decision: string; reason?: string | null }>(
    `/api/bank-transactions/${encodeURIComponent(id)}/coding`,
    { method: "POST", body: JSON.stringify({ decision: payload.decision ?? "accepted", ...payload }) },
  );
}

export async function clearBankTransactionCoding(_user: User, id: string) {
  return api<{ id: string; decision: string }>(
    `/api/bank-transactions/${encodeURIComponent(id)}/coding`,
    { method: "DELETE" },
  );
}

export async function getCashSummary(_user: User): Promise<CashSummary> {
  void _user;
  const rows = await get<BankTransactionResponse[]>("/api/bank/transactions");
  const currencies = new Map<string | null, CashSummary["currencies"][number]>();
  for (const row of rows) {
    const currency = row.currency ?? "MYR";
    const item = currencies.get(currency) ?? { currency, moneyIn: "0.00", moneyOut: "0.00", verifiedIn: "0.00", verifiedOut: "0.00" };
    if (row.direction === "money_in") item.moneyIn = addMoney(item.moneyIn, row.amount ?? "0.00");
    if (row.direction === "money_out") item.moneyOut = addMoney(item.moneyOut, row.amount ?? "0.00");
    if (row.status === "matched" && row.direction === "money_in") item.verifiedIn = addMoney(item.verifiedIn, row.amount ?? "0.00");
    if (row.status === "matched" && row.direction === "money_out") item.verifiedOut = addMoney(item.verifiedOut, row.amount ?? "0.00");
    currencies.set(currency, item);
  }
  return { currencies: [...currencies.values()] };
}

export type BankReconciliationPeriod = { id: string; bankAccountRef: string; startsOn: string; endsOn: string; openingBalance: string; closingBalance: string; bookBalance: string; statementAdjustment: string; bookAdjustment: string; adjustmentEvidence?: string | null; moneyIn: string; moneyOut: string; expectedClosingBalance: string; statementDifference: string; adjustedStatementBalance: string; adjustedBookBalance: string; unexplainedDifference: string; unaccountedLines: number; status: "open" | "completed"; signedOffBy?: string | null; signedOffAt?: string | null };
export const getReconciliationPeriods = () => get<BankReconciliationPeriod[]>("/api/bank/reconciliation/periods");
export const createReconciliationPeriod = (payload: Record<string, unknown>) => api<BankReconciliationPeriod>("/api/bank/reconciliation/periods", { method: "POST", body: JSON.stringify(payload) });
export const completeReconciliationPeriod = (id: string) => api<BankReconciliationPeriod>(`/api/bank/reconciliation/periods/${encodeURIComponent(id)}/complete`, { method: "POST" });
export const reopenReconciliationPeriod = (id: string, reason: string) => api(`/api/bank/reconciliation/periods/${encodeURIComponent(id)}/reopen`, { method: "POST", body: JSON.stringify({ reason }) });

function addMoney(left: string, right: string): string {
  const cents = (value: string) => {
    const [whole, fraction = ""] = value.split(".");
    return BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0").slice(0, 2));
  };
  const total = cents(left) + cents(right);
  return `${total / 100n}.${String(total % 100n).padStart(2, "0")}`;
}
