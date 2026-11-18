"use client";
import { useEffect, useState } from "react";
import type { User } from "@/types";
import { Badge, DataTable, Panel } from "@/components/common/ui";
import { deleteDocumentTag, deleteSavedFilter, downloadDocumentVersion, getDocumentTags, getDocumentVersions, getSavedFilters, getWorkload } from "@/lib/api/workflow.service";

export function WorkflowControls({ user, documentId }: { user: User; documentId?: string }) {
  const [filters, setFilters] = useState<Awaited<ReturnType<typeof getSavedFilters>>>([]);
  const [tags, setTags] = useState<Awaited<ReturnType<typeof getDocumentTags>>>([]);
  const [workload, setWorkload] = useState<Awaited<ReturnType<typeof getWorkload>>>([]);
  const [versions, setVersions] = useState<Awaited<ReturnType<typeof getDocumentVersions>>>([]);
  useEffect(() => { void Promise.all([getSavedFilters(), getDocumentTags(), getWorkload(), documentId ? getDocumentVersions(documentId) : Promise.resolve([])]).then(([f, t, w, v]) => { setFilters(f); setTags(t); setWorkload(w); setVersions(v); }).catch(() => undefined); }, [user.id, documentId]);
  return <div className="workflow-control-grid">
    <Panel title="Saved filters"><div className="panel-body">{filters.map((filter) => <div className="selected-file" key={filter.id}><strong>{filter.name}</strong><span>{filter.resource}</span><button type="button" className="text-link danger-text" onClick={() => deleteSavedFilter(filter.id).then(() => setFilters((items) => items.filter((item) => item.id !== filter.id)))}>Delete</button></div>)}{!filters.length && <p className="empty-state">No saved filters.</p>}</div></Panel>
    <Panel title="Document tags"><div className="panel-body">{tags.map((tag) => <div className="selected-file" key={tag.id}><strong>{tag.name}</strong><Badge>{tag.color}</Badge><button type="button" className="text-link danger-text" onClick={() => deleteDocumentTag(tag.id).then(() => setTags((items) => items.filter((item) => item.id !== tag.id)))}>Delete</button></div>)}{!tags.length && <p className="empty-state">No document tags.</p>}</div></Panel>
    <Panel title="Workload assignments"><DataTable rows={workload} rowKey={(row) => row.id} columns={[{ label: "Document", render: (row) => row.documentCode }, { label: "Assignee", render: (row) => row.assigneeName }, { label: "Priority", render: (row) => <Badge>{row.priority}</Badge> }, { label: "Due", render: (row) => row.dueAt }]} /></Panel>
    {documentId && <Panel title="Document versions"><DataTable rows={versions} rowKey={(row) => row.id} columns={[{ label: "Version", render: (row) => `v${row.versionNumber}` }, { label: "Created", render: (row) => row.createdAt }, { label: "File", render: (row) => <a className="btn small" href={downloadDocumentVersion(documentId, row.id)} target="_blank" rel="noreferrer">Download</a> }]} /></Panel>}
  </div>;
}
