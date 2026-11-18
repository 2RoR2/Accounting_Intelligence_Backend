/** Candidate comparison inputs observed in Phase 3C; no settlement decision. */
export type BankAccountingRecord = { record_id: string; total_amount: string; currency: string; reference_number?: string | null };
export type BankTransaction = { transaction_id: string; amount: string; currency: string; reference?: string | null };
function rows(value: unknown, label: string): Record<string, unknown>[] {
  if (!Array.isArray(value)) throw new Error(label + " must be a JSON array.");
  return value.map((row, index) => {
    if (!row || typeof row !== "object" || Array.isArray(row)) throw new Error(label + " row " + (index + 1) + " must be an object.");
    return row as Record<string, unknown>;
  });
}
function text(row: Record<string, unknown>, key: string, label: string): string {
  if (typeof row[key] !== "string") throw new Error(label + " " + key + " must be a string.");
  return row[key];
}
function amount(row: Record<string, unknown>, key: string, label: string): string {
  const value = row[key];
  if (typeof value !== "string" || !/^-?[0-9]+(?:\.[0-9]+)?$/.test(value)) {
    throw new Error(label + " " + key + ' must be a decimal string, for example "100.00" or "-25.50".');
  }
  return value;
}
function reference(row: Record<string, unknown>, key: string, label: string): string | null | undefined {
  const value = row[key];
  if (value !== undefined && value !== null && typeof value !== "string") throw new Error(label + " " + key + " must be a string, null, or omitted.");
  return value;
}
export function validateBankMatchingInputs(accounting: unknown, transactions: unknown): { accounting_records: BankAccountingRecord[]; bank_transactions: BankTransaction[] } {
  return {
    accounting_records: rows(accounting, "Accounting records").map((row, i) => {
      const label = "Accounting records row " + (i + 1);
      return { record_id: text(row, "record_id", label), total_amount: amount(row, "total_amount", label), currency: text(row, "currency", label), reference_number: reference(row, "reference_number", label) };
    }),
    bank_transactions: rows(transactions, "Bank transactions").map((row, i) => {
      const label = "Bank transactions row " + (i + 1);
      return { transaction_id: text(row, "transaction_id", label), amount: amount(row, "amount", label), currency: text(row, "currency", label), reference: reference(row, "reference", label) };
    }),
  };
}
