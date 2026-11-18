export type DocumentStatus = "UPLOADED" | "QUEUED" | "PROCESSING" | "EXTRACTED" | "VALIDATING" | "EXCEPTION" | "IN_REVIEW" | "CORRECTED" | "VALIDATED" | "STANDARDISED" | "COMPLETED" | "FAILED";
export type DocumentKind = "INVOICE" | "BILL" | "RECEIPT" | "CREDIT_NOTE" | "DEBIT_NOTE";
export interface Document {
  id: string;
  tenantId: string;
  ownerId: string;
  assignedTo: string | null;
  name: string;
  type: string;
  kind?: string;
  code?: string;
  category?: string;
  jobId?: string;
  queueJobId?: string | null;
  status: DocumentStatus;
  createdAt: string;
  contentRevision?: number;
  dueDate?: string | null;
  seconds: number;
  scenario: string;
  /** Prefer string to avoid float precision loss; backend list may return number, records return string. */
  totalAmount?: string | number | null;
  supplier?: string | null;
  customer?: string | null;
}
export interface ProcessingJob { id: string; documentId: string; tenantId: string; status: DocumentStatus; seconds: number; startedAt: string; completedAt: string; }
