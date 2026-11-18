import type { Extraction } from "./extraction";
export type RecordCategory = "ACCOUNTS_RECEIVABLE" | "ACCOUNTS_PAYABLE";
export interface StandardisedRecord { id: string; code?: string; documentId: string; tenantId: string; createdAt: string; category: RecordCategory; validation: "PASSED"; status: "COMPLETED"; extraction: Extraction; supplier?: string | null; customer?: string | null; invoiceNumber?: string | null; referenceNumber?: string | null; invoiceDate?: string | null; dueDate?: string | null; totalAmount?: string | null; currency?: string | null; }
