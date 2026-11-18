import { api } from "./client";

export type ConsolidatedSubmission = {
  id: string;
  documentType: string;
  status: "queued" | "submitted" | "failed";
  documentCount: number;
  totalAmount: string;
};

export const submitConsolidatedDocument = (payload: { documentType: string; dateFrom: string; dateTo: string; documentIds: string[]; idempotencyKey: string }) =>
  api<ConsolidatedSubmission>("/api/myinvois/consolidated", { method: "POST", body: JSON.stringify(payload) });
