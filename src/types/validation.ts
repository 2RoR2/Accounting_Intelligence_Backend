export interface TransactionAnomaly {
  transaction_id: string; amount: string; currency: string; is_anomaly: boolean;
  status: "anomaly" | "normal" | "insufficient_baseline"; anomaly_score: string | null;
  baseline_median: string | null; upper_bound: string | null; baseline_count: number;
  reason: string;
}
export interface ValidationResult { cause?: string | null; causeLabel?: string | null; rule: string; result: "PASS" | "WARNING" | "FAIL" | "NOT_APPLICABLE"; message: string; field?: string; severity?: string; checkStatus?: string; code?: string; required?: boolean; preventsApproval?: boolean; duplicate_of?: string; transaction_anomaly?: TransactionAnomaly;
  vendor_candidate?: { vendor_id?: string | null; vendor_name?: string | null; similarity?: number | null; score_method?: string | null }; }
export interface ValidationRules { supplier: boolean; invoice: boolean; date: boolean; total: boolean; duplicate: boolean; threshold: number; invoiceByType?: boolean; billReferenceByType?: boolean; receiptIdentifierByType?: boolean; }
