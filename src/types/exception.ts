export type ExceptionStatus = "OPEN" | "ASSIGNED" | "IN_REVIEW" | "RESOLVED";
export interface Exception { id: string; documentId: string; tenantId: string; type: string; severity: "LOW" | "MEDIUM" | "HIGH"; assignedTo: string; createdAt: string; status: ExceptionStatus; message: string; note?: string; }
