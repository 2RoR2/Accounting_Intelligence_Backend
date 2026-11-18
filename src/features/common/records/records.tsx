"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ArrowRight, Download, ShieldCheck } from "lucide-react";
import type { StandardisedRecord, User } from "@/types";
import { useResource } from "@/features/common/hooks/use-resource";
import {
  getRecordMetadata,
  getStandardisedRecord,
  getStandardisedRecords,
  getOutstandingRecordIds,
  openSupportView,
} from "@/lib/api/record.service";
import { decimalDisplay as money, fieldLabel, MONEY_KEYS, type SupportViewRecord } from "@/lib/api/document-contract";
import { getTenants } from "@/lib/api/tenant.service";
import { getDocuments, getExceptions } from "@/lib/api/document.service";
import { createExport, downloadExport, getExports } from "@/lib/api/workflow.service";
import {
  Badge,
  DataTable,
  ErrorState,
  Field,
  Filter,
  LoadingState,
  Modal,
  PageHeader,
  Panel,
  SearchInput,
  formatDate,
} from "@/components/common/ui";

export function RecordDetail({
  record,
  support = false,
}: {
  record: StandardisedRecord | SupportViewRecord;
  support?: boolean;
}) {
  return (
    <>
      <div className={support ? "support-banner" : "record-banner"}>
        <ShieldCheck size={19} />
        <span>
          {support
            ? "READ-ONLY SUPPORT VIEW"
            : "STANDARDISED ACCOUNTING RECORD"}
        </span>
        {"category" in record && <Badge>
          {record.category === "ACCOUNTS_RECEIVABLE"
            ? "ACCOUNTS RECEIVABLE"
            : "ACCOUNTS PAYABLE"}
        </Badge>}
        {"validation" in record && <Badge>{record.validation}</Badge>}
      </div>
      <Panel
        title={record.id}
        subtitle={"createdAt" in record ? `Processed ${formatDate(record.createdAt)}` : "Standardised fields only"}
      >
        <div className="record-fields">
          {Object.entries(record.extraction.fields).map(([key, f]) => (
            <div key={key}>
              <small>{fieldLabel("", key, f.label)}</small>
              <strong>
                {MONEY_KEYS.has(key) ? money(f.value) : (f.value || "—")}
              </strong>
            </div>
          ))}
        </div>
        <DataTable
          rows={record.extraction.lineItems}
          rowKey={(r) => r.id ?? r.description ?? "line"}
          columns={[
            { label: "Description", render: (r) => r.description },
            { label: "Quantity", render: (r) => r.quantity },
            { label: "Unit price", render: (r) => money(r.unitPrice) },
            { label: "Amount", render: (r) => money(r.amount) },
          ]}
        />
      </Panel>
    </>
  );
}

export function RecordsPage({
  user,
  tenantId,
  initialRecordId,
}: {
  user: User;
  tenantId?: string;
  initialRecordId?: string;
}) {
  const admin = user.role === "SUPER_ADMIN";
  const searchParams = useSearchParams();
  const balance = searchParams.get("balance");
  const balanceCategory = searchParams.get("category") as "accounts_receivable" | "accounts_payable" | null;
  const balanceOverdue = searchParams.get("overdue") === "true";
  const { data, error, loading, refresh } = useResource(
    async () => ({
      tenants: await getTenants(user),
      docs: await getDocuments(user),
      exceptions: await getExceptions(user),
      records: admin ? [] : await getStandardisedRecords(user),
      outstandingIds: !admin && balance === "outstanding" && balanceCategory ? await getOutstandingRecordIds(user, balanceCategory, balanceOverdue) : null,
      metadata: admin ? await getRecordMetadata(user, tenantId) : [],
    }),
    `${user.id}:${tenantId}:${balance}:${balanceCategory}:${balanceOverdue}`,
  );
  const [selectedTenant, setSelectedTenant] = useState(tenantId ?? "");
  const [search, setSearch] = useState("");
  const [supplier, setSupplier] = useState("");
  const [category, setCategory] = useState("");
  const [date, setDate] = useState("");
  const [validation, setValidation] = useState("");
  const [status, setStatus] = useState("");
  const [detail, setDetail] = useState<StandardisedRecord | SupportViewRecord>();
  const [support, setSupport] = useState("");
  const [reason, setReason] = useState("");
  const [failure, setFailure] = useState("");
  const [exports, setExports] = useState<Array<{ id: string; resource: string; format: string; status: string; rowCount: number; createdAt: string }>>([]);
  const [exportsLoaded, setExportsLoaded] = useState(false);
  const [exportError, setExportError] = useState("");
  const [metadataId, setMetadataId] = useState("");
  const [deepLinkDismissed, setDeepLinkDismissed] = useState(false);
  useEffect(() => {
    if (!initialRecordId || admin) return;
    let cancelled = false;
    void getStandardisedRecord(user, initialRecordId)
      .then((matched) => {
        if (!cancelled && matched) setDetail(matched);
      })
      .catch(() => {
        if (!cancelled) {
          const matched = data?.records.find((record) => record.id === initialRecordId || record.code === initialRecordId);
          if (matched) setDetail(matched);
        }
      });
    return () => { cancelled = true; };
  }, [admin, data, initialRecordId, user]);
  if (loading) return <LoadingState variant="table" />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;
  const linkedRecord = initialRecordId
    ? data.records.find((record) => record.id === initialRecordId || record.code === initialRecordId)
    : undefined;
  const visibleDetail = detail ?? (!deepLinkDismissed ? linkedRecord : undefined);
  const records = data.records.filter(
    (r) =>
      (!data.outstandingIds || data.outstandingIds.includes(r.id)) &&
      `${r.code ?? ""} ${r.id} ${(r.extraction.fields.invoice?.value ?? "")} ${(r.extraction.fields.supplier?.value ?? "")}`
        .toLowerCase()
        .includes(search.toLowerCase()) &&
      (!supplier || (r.extraction.fields.supplier?.value ?? "") === supplier) &&
      (!category || r.category === category) &&
      (!date || (r.extraction.fields.date?.value ?? "") === date) &&
      (!validation || r.validation === validation) &&
      (!status || r.status === status),
  );
  const metadata = data.metadata.find((r) => r.id === metadataId);
  const createBackendExport = async () => {
    setExportError("");
    try {
      const created = await createExport("records", {}, "csv") as { id: string };
      window.open(downloadExport(created.id), "_blank", "noopener,noreferrer");
      const history = await getExports();
      setExports(history); setExportsLoaded(true);
    } catch (e) { setExportError(e instanceof Error ? e.message : "Unable to create the export."); }
  };
  const loadExportHistory = async () => {
    setExportError("");
    try { setExports(await getExports()); setExportsLoaded(true); }
    catch (e) { setExportError(e instanceof Error ? e.message : "Unable to load export history."); }
  };
  return (
    <>
      <PageHeader
        eyebrow={admin ? "PLATFORM MONITORING" : undefined}
        title="Standardised Records"
        description={
          admin
            ? "Tenant-level monitoring. Detailed accounting data is available only through Support View."
            : "Approved documents converted into standardised accounting records. View records, inspect supporting documents and export data."
        }
        actions={
          !admin && (
            <div className="page-header-actions" style={{ display: "flex", gap: 12 }}>
              <button className="btn primary" type="button" disabled={!records.length} onClick={() => void createBackendExport()}><Download size={16} /> Export</button>
              <button className="btn" type="button" onClick={() => void loadExportHistory()}>Export history</button>
            </div>
          )
        }
      />
      {exportError && <p className="error-text" role="alert">{exportError}</p>}
      {exportsLoaded && <Panel title="Export history" subtitle="Download a previous export or check when it was created.">
        {exports.length ? <DataTable rows={exports} rowKey={(row) => row.id} columns={[{ label: "Resource", render: (row) => row.resource }, { label: "Format", render: (row) => row.format.toUpperCase() }, { label: "Rows", render: (row) => row.rowCount }, { label: "Status", render: (row) => <Badge>{row.status}</Badge> }, { label: "Created", render: (row) => formatDate(row.createdAt) }, { label: "Action", render: (row) => <a className="btn small" href={downloadExport(row.id)} target="_blank" rel="noreferrer">Download</a> }]} /> : <div className="empty-state"><strong>No exports yet</strong><p>Create an export to see it here.</p></div>}
      </Panel>}
      {admin && !selectedTenant ? (
        <Panel title="Records by tenant">
          <DataTable
            rows={data.tenants}
            rowKey={(t) => t.id}
            columns={[
              { label: "Tenant", render: (t) => <strong>{t.name}</strong> },
              {
                label: "Total records",
                render: (t) =>
                  data.metadata.filter((r) => r.tenantId === t.id).length,
              },
              {
                label: "Validated",
                render: (t) =>
                  data.metadata.filter((r) => r.tenantId === t.id).length,
              },
              {
                label: "Exceptions",
                render: (t) =>
                  data.exceptions.filter(
                    (e) => e.tenantId === t.id && e.status !== "RESOLVED",
                  ).length,
              },
              {
                label: "Last processed",
                render: (t) => {
                  const r = data.metadata.find((r) => r.tenantId === t.id);
                  return r ? formatDate(r.createdAt) : "—";
                },
              },
              {
                label: "Action",
                render: (t) => (
                  <button
                    className="text-link"
                    onClick={() => setSelectedTenant(t.id)}
                  >
                    View records <ArrowRight size={14} />
                  </button>
                ),
              },
            ]}
          />
        </Panel>
      ) : admin ? (
        <Panel
          title={`${data.tenants.find((t) => t.id === selectedTenant)?.name ?? ""} · Record metadata`}
          action={
            !tenantId && (
              <button
                className="text-link"
                onClick={() => setSelectedTenant("")}
              >
                All tenants
              </button>
            )
          }
        >
          <DataTable
            rows={data.metadata.filter((r) => r.tenantId === selectedTenant)}
            rowKey={(r) => r.id}
            columns={[
              { label: "Record ID", render: (r) => <strong>{r.id}</strong> },
              { label: "Document", render: (r) => r.document?.name },
              { label: "Document type", render: (r) => r.document?.type },
              {
                label: "Category",
                render: (r) =>
                  r.category === "ACCOUNTS_RECEIVABLE"
                    ? "Accounts Receivable"
                    : "Accounts Payable",
              },
              {
                label: "Validation",
                render: (r) => <Badge>{r.validation}</Badge>,
              },
              {
                label: "Processing",
                render: (r) => `${r.document?.seconds} sec`,
              },
              { label: "Processed at", render: (r) => formatDate(r.createdAt) },
              {
                label: "Actions",
                render: (r) => (
                  <div className="row-actions">
                    <button
                      className="text-link"
                      onClick={() => setMetadataId(r.id)}
                    >
                      View metadata
                    </button>
                    <button
                      className="text-link"
                      onClick={() => {
                        setSupport(r.id);
                        setReason("");
                        setFailure("");
                      }}
                    >
                      Support View
                    </button>
                  </div>
                ),
              },
            ]}
          />
        </Panel>
      ) : (
        <Panel>
          <div className="filter-bar">
            <SearchInput
              value={search}
              onChange={setSearch}
              placeholder="Search records…"
            />
            <Filter
              label="Categories"
              value={category}
              onChange={setCategory}
              options={["ACCOUNTS_RECEIVABLE", "ACCOUNTS_PAYABLE"]}
            />
            <Filter
              label="Suppliers"
              value={supplier}
              onChange={setSupplier}
              options={Array.from(
                new Set(
                  data.records.map((r) => (r.extraction.fields.supplier?.value ?? "")),
                ),
              )}
            />
            <input
              aria-label="Invoice date"
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
            <Filter
              label="Validation"
              value={validation}
              onChange={setValidation}
              options={["PASSED"]}
            />
            <Filter
              label="Statuses"
              value={status}
              onChange={setStatus}
              options={["COMPLETED"]}
            />
          </div>
          <DataTable
            rows={records}
            rowKey={(r) => r.id}
            columns={[
              {
                label: "Record / invoice",
                render: (r) => (
                  <div>
                    <strong title={r.id}>{r.code ?? r.id}</strong>
                    <small>{(r.extraction.fields.invoice?.value ?? "")}</small>
                  </div>
                ),
              },
              {
                label: "Category",
                render: (r) => (
                  <Badge>
                    {r.category === "ACCOUNTS_RECEIVABLE" ? "AR" : "AP"}
                  </Badge>
                ),
              },
              {
                label: "Supplier",
                render: (r) => (r.extraction.fields.supplier?.value ?? ""),
              },
              {
                label: "Invoice date",
                render: (r) => (r.extraction.fields.date?.value ?? ""),
              },
              {
                label: "Total",
                render: (r) => (
                  <strong>{money((r.extraction.fields.total?.value ?? ""))}</strong>
                ),
              },
              {
                label: "Currency",
                render: (r) => (r.extraction.fields.currency?.value ?? ""),
              },
              {
                label: "Validation",
                render: (r) => <Badge>{r.validation}</Badge>,
              },
              { label: "Status", render: (r) => <Badge>{r.status}</Badge> },
              {
                label: "Action",
                render: (r) => (
                  <button className="text-link" onClick={() => setDetail(r)}>
                    View <ArrowRight size={14} />
                  </button>
                ),
              },
            ]}
          />
        </Panel>
      )}
      {metadata && (
        <Modal title="Record metadata" onClose={() => setMetadataId("")}>
          <div className="panel-body detail-list">
            <div>
              <span>Record ID</span>
              <strong>{metadata.id}</strong>
            </div>
            <div>
              <span>Document</span>
              <strong>{metadata.document?.name}</strong>
            </div>
            <div>
              <span>Type</span>
              <strong>{metadata.document?.type}</strong>
            </div>
            <div>
              <span>Category</span>
              <strong>
                {metadata.category === "ACCOUNTS_RECEIVABLE"
                  ? "Accounts Receivable"
                  : "Accounts Payable"}
              </strong>
            </div>
            <div>
              <span>Validation</span>
              <Badge>{metadata.validation}</Badge>
            </div>
            <div>
              <span>Processed</span>
              <strong>{formatDate(metadata.createdAt)}</strong>
            </div>
          </div>
        </Modal>
      )}
      {support && (
        <Modal title="Authorised support access" onClose={() => setSupport("")}>
          <form
            className="panel-body"
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                setDetail(await openSupportView(user, support, reason));
                setSupport("");
              } catch (e) {
                setFailure((e as Error).message);
              }
            }}
          >
            <div className="info-box">
              <ShieldCheck size={23} />
              <p>
                Accessing detailed tenant accounting information should only be
                used for authorised support or troubleshooting.
              </p>
            </div>
            <p>
              This read-only access and your reason will appear in the audit
              log.
            </p>
            {failure && <ErrorState message={failure} />}
            <Field label="Support reason *">
              <textarea
                required
                minLength={5}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </Field>
            <div className="modal-actions">
              <button
                type="button"
                className="btn"
                onClick={() => setSupport("")}
              >
                Cancel
              </button>
              <button className="btn primary">Continue</button>
            </div>
          </form>
        </Modal>
      )}
      {visibleDetail && (
        <Modal
          title={admin ? "Read-only support record" : "Standardised record"}
          onClose={() => { setDetail(undefined); setDeepLinkDismissed(true); }}
        >
          <div className="panel-body">
            <RecordDetail record={visibleDetail} support={admin} />
            {!admin && (
              <Link
                className="btn"
                href={`/${user.role === "LOCAL_ADMIN" ? "company" : "workspace"}/documents/${visibleDetail.documentId}`}
              >
                View original document
              </Link>
            )}
          </div>
        </Modal>
      )}
    </>
  );
}
