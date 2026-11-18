"use client";
import { useState } from "react";
import { Activity, Download, ScanLine, Eye, RefreshCw, Files, FileCheck2, CircleAlert, ClipboardCheck, AlertCircle } from "lucide-react";
import type { User } from "@/types";
import { useResource } from "@/features/common/hooks/use-resource";
import {
  getPerformanceMetrics,
  getProcessingJob,
  getProcessingJobs,
  getProcessingWorkers,
} from "@/lib/api/processing.service";
import { getDocuments } from "@/lib/api/document.service";
import { getTenants } from "@/lib/api/tenant.service";
import { getAuditLogs } from "@/lib/api/audit.service";
import { downloadCsv } from "@/lib/api/record.service";
import {
  Badge,
  DataTable,
  ErrorState,
  Filter,
  LoadingState,
  PageHeader,
  Panel,
  SearchInput,
  StatCard,
  formatDate,
} from "@/components/common/ui";
import { PerformanceCards, TrendChart } from "@/components/common/dashboard/charts";
import { MalaysiaDayNight } from "@/components/common/dashboard/malaysia-day-night";
export function ProcessingPage({
  user,
  tenantId,
}: {
  user: User;
  tenantId?: string;
}) {
  const { data, error, loading, refresh } = useResource(
    async () => ({
      jobs: await getProcessingJobs(user),
      tenants: await getTenants(user),
      docs: await getDocuments(user),
    }),
    user.id,
  );
  const [tenant, setTenant] = useState(tenantId ?? "");
  const [status, setStatus] = useState("");
  const [date, setDate] = useState("");
  const [selectedJob, setSelectedJob] = useState<string | null>(null);
  const [jobDetail, setJobDetail] = useState<Awaited<ReturnType<typeof getProcessingJob>> | null>(null);
  const [workers, setWorkers] = useState<Awaited<ReturnType<typeof getProcessingWorkers>>>([]);
  async function inspectJob(id: string) {
    setSelectedJob(id);
    setJobDetail(await getProcessingJob(id));
  }
  async function refreshWorkers() {
    setWorkers(await getProcessingWorkers());
  }
  if (loading) return <LoadingState />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;
  const jobs = data.jobs.filter(
    (j) =>
      (!tenant || j.tenantId === tenant) &&
      (!status || j.status === status) &&
      (!date || j.startedAt.startsWith(date)),
  );
  return (
    <>
      <PageHeader
        eyebrow="PLATFORM MONITORING"
        title="AI processing"
        description="Monitor job health and processing metadata across the platform."
        actions={<><button type="button" className="btn small" onClick={refreshWorkers}><RefreshCw size={15} /> Worker status</button><Badge>MONITORING ONLY</Badge></>}
      />
      <div className="service-status">
        <span>
          <ScanLine size={18} />
          <strong>AI service</strong>
          <Badge>SIMULATED</Badge>
        </span>
        <span>
          <Activity size={18} />
          <strong>Queue status</strong>
          <Badge>HEALTHY</Badge>
        </span>
      </div>
      <div className="stats-grid">
        {["QUEUED", "PROCESSING", "COMPLETED", "FAILED"].map((s) => (
          <StatCard
            key={s}
            label={s.charAt(0) + s.slice(1).toLowerCase()}
            value={jobs.filter((j) => j.status === s).length}
          />
        ))}
      </div>
      <Panel title="Processing jobs">
        <div className="filter-bar">
          {!tenantId && (
            <select
              aria-label="Tenant"
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
            >
              <option value="">All tenants</option>
              {data.tenants.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          )}
          <Filter
            label="Statuses"
            value={status}
            onChange={setStatus}
            options={Array.from(new Set(data.jobs.map((j) => j.status)))}
          />
          <input
            aria-label="Processing date"
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
          />
        </div>
        <DataTable
          rows={jobs}
          rowKey={(j) => j.id}
          columns={[
            { label: "Job ID", render: (j) => <strong>{j.id}</strong> },
            {
              label: "Tenant",
              render: (j) =>
                data.tenants.find((t) => t.id === j.tenantId)?.name,
            },
            {
              label: "Document",
              render: (j) => data.docs.find((d) => d.id === j.documentId)?.name,
            },
            { label: "Status", render: (j) => <Badge>{j.status}</Badge> },
            {
              label: "Time",
              render: (j) => (j.status === "FAILED" ? "—" : `${j.seconds} sec`),
            },
            { label: "Started", render: (j) => formatDate(j.startedAt) },
            { label: "Completed", render: (j) => formatDate(j.completedAt) },
            { label: "Details", render: (j) => <button type="button" className="btn small" onClick={() => inspectJob(j.id)}><Eye size={14} /> View</button> },
          ]}
        />
      </Panel>
      {workers.length > 0 && <Panel title="Background workers"><DataTable rows={workers} rowKey={(worker) => worker.name} columns={[{ label: "Worker", render: (worker) => worker.name }, { label: "Queue", render: (worker) => worker.queue }, { label: "Status", render: (worker) => <Badge>{worker.status}</Badge> }, { label: "Redis", render: (worker) => worker.redis }]} /></Panel>}
      {selectedJob && jobDetail && <Panel title={`Job ${selectedJob}`}><div className="detail-grid"><span>Status<strong>{jobDetail.status}</strong></span><span>Attempts<strong>{jobDetail.attempts} / {jobDetail.maxAttempts}</strong></span><span>Last error<strong>{jobDetail.lastError || "None"}</strong></span><span>Created<strong>{formatDate(jobDetail.createdAt)}</strong></span></div><button type="button" className="btn" onClick={() => { setSelectedJob(null); setJobDetail(null); }}>Close</button></Panel>}
    </>
  );
}
export function PerformancePage({ user }: { user: User }) {
  const { data, error, loading, refresh } = useResource(
    async () => ({
      metrics: await getPerformanceMetrics(user),
      tenants: await getTenants(user),
      docs: await getDocuments(user),
    }),
    user.id,
  );
  if (loading) return <LoadingState />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;
  const company = user.role !== "SUPER_ADMIN";
  const standardised = data.docs.filter((d) => ["COMPLETED", "VALIDATED", "STANDARDISED"].includes(d.status)).length;
  const exceptions = data.docs.filter((d) => d.status === "EXCEPTION").length;
  const inReview = data.docs.filter((d) => d.status === "IN_REVIEW").length;
  const failed = data.docs.filter((d) => d.status === "FAILED").length;
  const jobs = data.metrics.find((m) => m.label.toLowerCase().includes("processed"))?.value ?? "—";
  return (
    <div className="dashboard-page company-performance-page">
      <div className="workspace-dashboard-header dashboard-reference-header">
        <MalaysiaDayNight />
        <div>
          <span className="workspace-dashboard-kicker">{company ? "COMPANY PERFORMANCE" : "PLATFORM PERFORMANCE"}</span>
          <h1>{company ? "How the company is doing" : "Platform performance"}</h1>
        </div>
        <div className="workspace-dashboard-actions">
          <button className="btn primary" onClick={() => downloadCsv("performance-report.csv", [
            ["Metric", "Value"],
            ["Documents", String(data.docs.length)],
            ["Standardised", String(standardised)],
            ["In review", String(inReview)],
            ["Exceptions", String(exceptions)],
            ["Processing jobs", String(jobs)],
            ["Failed documents", String(failed)],
          ])}><Download size={16} /> Export</button>
        </div>
      </div>

      <section className="performance-block">
        <div className="performance-heading">
          <span className="workspace-dashboard-kicker">COMPANY TOTALS</span>
          <h2>Documents in this company</h2>
        </div>
        <div className="performance-metric-row">
          <div><strong>{data.docs.length}</strong><span>Documents</span></div>
          <div><strong>{standardised}</strong><span>Standardised</span></div>
          <div><strong>{inReview}</strong><span>In review</span></div>
          <div><strong>{exceptions}</strong><span>Exceptions</span></div>
        </div>
      </section>

      <section className="performance-block">
        <div className="performance-heading">
          <span className="workspace-dashboard-kicker">PROCESSING</span>
          <h2>Jobs and quality</h2>
        </div>
        <div className="performance-metric-row">
          <div><strong>{jobs}</strong><span>Processing jobs</span></div>
          <div><strong>{failed}</strong><span>Failed documents</span></div>
          <div><strong>{data.metrics.find((m) => m.label.toLowerCase().includes("success"))?.value ?? "—"}</strong><span>Processing success rate</span></div>
          <div><strong>{data.metrics.find((m) => m.label.toLowerCase().includes("time"))?.value ?? "—"}</strong><span>Avg. processing time</span></div>
          <div><strong>{data.metrics.find((m) => m.label.toLowerCase().includes("exception rate"))?.value ?? "—"}</strong><span>Open exception rate</span></div>
        </div>
      </section>

      <section className="performance-block">
        <div className="performance-heading">
          <span className="workspace-dashboard-kicker">TRENDS</span>
          <h2>How the company is moving</h2>
        </div>
        <div className="performance-trends">
          <TrendChart />
          <TrendChart kind="accuracy" />
          <TrendChart kind="exceptions" />
        </div>
      </section>
    </div>
  );
}
export function AuditPage({
  user,
  tenantId,
}: {
  user: User;
  tenantId?: string;
}) {
  const { data, error, loading, refresh } = useResource(
    () => getAuditLogs(user),
    user.id,
  );
  const [search, setSearch] = useState("");
  const [action, setAction] = useState("");
  if (loading) return <LoadingState />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;
  return (
    <>
      <PageHeader
        eyebrow="GOVERNANCE"
        title="Audit logs"
        description="Trace decisions, corrections, and administrative changes."
      />
      <Panel>
        <div className="filter-bar">
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search user, action, or resource…"
          />
          <Filter
            label="Actions"
            value={action}
            onChange={setAction}
            options={Array.from(new Set(data.map((a) => a.action)))}
          />
        </div>
        <DataTable
          rows={data.filter(
            (a) =>
              (!tenantId || a.tenantId === tenantId) &&
              (!action || a.action === action) &&
              `${a.user} ${a.action} ${a.resource}`
                .toLowerCase()
                .includes(search.toLowerCase()),
          )}
          rowKey={(a) => a.id}
          columns={[
            {
              label: "Timestamp",
              render: (a) => (
                <div>
                  {formatDate(a.timestamp)}
                  <small>
                    {new Date(a.timestamp).toLocaleTimeString("en-MY", {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </small>
                </div>
              ),
            },
            {
              label: "User",
              render: (a) => <strong>{a.user}</strong>,
            },
            {
              label: "Role",
              render: (a) => (
                <span className="audit-role">
                  {a.role.replaceAll("_", " ")}
                </span>
              ),
            },
            {
              label: "Action",
              render: (a) => (
                <span className="audit-action">
                  {a.action.replaceAll("_", " ")}
                </span>
              ),
            },
            { label: "Resource", render: (a) => a.resource },
            {
              label: "Details",
              render: (a) =>
                user.role === "SUPER_ADMIN" &&
                a.action === "EXTRACTION_CORRECTED"
                  ? "Extraction field corrected; details available through authorised support."
                  : a.details,
            },
          ]}
        />
      </Panel>
    </>
  );
}
