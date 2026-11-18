import { api, get } from "@/lib/api/client";

export type ERPCatalogEntry = {
  id: string;
  kind: string;
  code?: string | null;
  name: string;
  description?: string | null;
  data: Record<string, unknown>;
  isActive: boolean;
};

export type ERPDocument = {
  id: string;
  type: string;
  number: string;
  contactName?: string | null;
  documentDate?: string | null;
  dueDate?: string | null;
  currency?: string | null;
  amount: string | number;
  status: "draft" | "pending_approval" | "ready" | "void";
  fields?: Record<string, unknown>;
};

type ERPDocumentPage = { items: ERPDocument[]; total: number; page: number; pageSize: number };

export const listERPCatalog = (kind: string, includeInactive = false) =>
  get<ERPCatalogEntry[]>(`/api/erp/catalog/${encodeURIComponent(kind)}?includeInactive=${includeInactive}`);

export const createERPCatalog = (kind: string, payload: Record<string, unknown>) =>
  api<ERPCatalogEntry>(`/api/erp/catalog/${encodeURIComponent(kind)}`, { method: "POST", body: JSON.stringify(payload) });

export const updateERPCatalog = (kind: string, id: string, payload: Record<string, unknown>) =>
  api<ERPCatalogEntry>(`/api/erp/catalog/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(payload) });

export const archiveERPCatalog = (kind: string, id: string) =>
  api<void>(`/api/erp/catalog/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`, { method: "DELETE" });

export const listERPDocuments = (type: string, params: { page?: number; pageSize?: number } = {}) => {
  const query = new URLSearchParams({ page: String(params.page ?? 1), pageSize: String(params.pageSize ?? 100) });
  return get<ERPDocumentPage>(`/api/erp/documents/${encodeURIComponent(type)}?${query}`);
};

export const createERPDocument = (type: string, payload: Record<string, unknown>) =>
  api<ERPDocument>(`/api/erp/documents/${encodeURIComponent(type)}`, { method: "POST", body: JSON.stringify(payload) });

export const transitionERPDocument = (type: string, id: string, status: ERPDocument["status"], reason?: string) =>
  api<ERPDocument>(`/api/erp/documents/${encodeURIComponent(type)}/${encodeURIComponent(id)}/transition`, { method: "POST", body: JSON.stringify({ status, reason }) });
