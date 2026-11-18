export type MoneyDirection = "money_in" | "money_out";

/** Status of a bank statement row (matches backend bank_transactions.status). */
export type BankTransactionStatus =
  | "imported"
  | "needs_review"
  | "proposed"
  | "matched"
  | "ignored";

export interface BankReconciliationCandidate {
  id: string;
  recordId: string;
  recordCode: string;
  documentId: string | null;
  documentCode: string | null;
  revision: number;
  category: string;
  reference: string | null;
  party: string | null;
  currency: string | null;
  amount: string | null;
  amountDelta: string | null;
  dateDeltaDays: number | null;
  reasons: string[];
  evidence: Record<string, unknown>;
  status: "proposed" | "confirmed" | "rejected" | "reversed" | "superseded";
}

/** Simplified match state for the Money In / Money Out UI. */
export type MatchStatus = "unmatched" | "proposed" | "matched" | "ignored";

export type BankTransactionAnomalyStatus = "anomaly" | "normal" | "insufficient_baseline";

export interface BankTransactionAnomaly {
  status: BankTransactionAnomalyStatus;
  isAnomaly: boolean;
  label: string;
  reason: string;
  amount: string;
  currency: string;
  baselineMedian: string | null;
  upperBound: string | null;
  baselineCount: number;
  anomalyScore: string | null;
}

/**
 * Bank money movement shown on Money In / Money Out.
 * Amount is always positive; direction decides in vs out.
 * Related record fields are filled once the bank row is matched to a
 * standardised record.
 */
export interface BankMoneyTransaction {
  id: string;
  tenantId: string;
  direction: MoneyDirection;
  postedDate: string;
  valueDate?: string | null;
  description: string;
  reference?: string | null;
  amount: string | null;
  currency: string | null;
  status: BankTransactionStatus;
  matchStatus: MatchStatus;
  bankAccountRef?: string | null;
  relatedRecordId?: string | null;
  relatedRecordCode?: string | null;
  relatedDocumentId?: string | null;
  relatedDocumentCode?: string | null;
  amountDelta?: string | null;
  createdAt: string | null;
  candidateId?: string | null;
  expectedRevision?: number | null;
  candidates?: BankReconciliationCandidate[];
  accountId?: string | null;
  costCentreId?: string | null;
  codingDecision?: string | null;
  codingReason?: string | null;
  anomaly?: BankTransactionAnomaly | null;
}

export interface BankMoneyTransactionDetail extends BankMoneyTransaction {
  importId?: string | null;
  sourceRow?: number | null;
  candidates?: BankReconciliationCandidate[];
}

export interface CashSummary {
  currencies: Array<{
    currency: string | null;
    moneyIn: string;
    moneyOut: string;
    verifiedIn: string;
    verifiedOut: string;
  }>;
}
