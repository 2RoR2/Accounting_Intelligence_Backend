"use client";
import Link from "next/link";
import { useEffect, useRef } from "react";
import {
  Building2,
  Users,
  Files,
  ClipboardCheck,
  ArrowRight,
  Plus,
  Clock3,
  CircleAlert,
  ScanLine,
  Upload,
  FileCheck2,
  AlertCircle,
  ArrowDownToLine,
} from "lucide-react";
import type { User } from "@/types";
import { useResource } from "@/features/common/hooks/use-resource";
import { getTenants, getCompanyRequests } from "@/lib/api/tenant.service";
import { getTenantUsers } from "@/lib/api/user.service";
import { getDocuments, getExceptions } from "@/lib/api/document.service";
import { getAuditLogs } from "@/lib/api/audit.service";
import { getPerformanceMetrics } from "@/lib/api/processing.service";
import {
  AccountUsage,
  Badge,
  DataTable,
  ErrorState,
  LoadingState,
  PageHeader,
  Panel,
  StatCard,
  formatDate,
} from "@/components/common/ui";
import { PerformanceCards, TrendChart } from "@/components/common/dashboard/charts";
import { AccountantDashboard } from "@/features/users/accountant-dashboard";

export function Dashboard({ user }: { user: User }) {
  const admin = user.role === "SUPER_ADMIN";
  const local = user.role === "LOCAL_ADMIN";
  const accountant = !admin && !local;
  const base = admin ? "/admin" : local ? "/company" : "/workspace";

  // The last exception list the API actually returned. A slow or failed request must not
  // be shown as "0 open exceptions" while the sidebar still counts them.
  const lastExceptions = useRef<Awaited<ReturnType<typeof getExceptions>> | null>(null);
  const { data, error, loading, refresh } = useResource(
    async () => {
      const optional = async <T,>(request: Promise<T>, fallback: T) => {
        try {
          return await Promise.race([
            request,
            new Promise<T>((resolve) => window.setTimeout(() => resolve(fallback), 4000)),
          ]);
        } catch {
          return fallback;
        }
      };
      const [docs, tenants, users, requests, exceptions, metrics, audit] = await Promise.all([
        optional(getDocuments(user), []),
        admin ? optional(getTenants(user), []) : Promise.resolve([]),
        admin || local ? optional(getTenantUsers(user), []) : Promise.resolve([]),
        admin ? optional(getCompanyRequests(user), []) : Promise.resolve([]),
        optional<Awaited<ReturnType<typeof getExceptions>> | null>(getExceptions(user), null),
        optional(getPerformanceMetrics(user), []),
        optional(getAuditLogs(user), []),
      ]);
      if (exceptions) lastExceptions.current = exceptions;
      return { tenants, users, requests, docs, exceptions: lastExceptions.current, metrics, audit };
    },
    user.id,
  );

  useEffect(() => {
    if (admin) return;
    // Seven requests of about a second each: refreshing every 3 s queued them behind each other
    // and pushed the exception count past its timeout. Skip refreshes while the tab is hidden.
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, 15000);
    return () => window.clearInterval(timer);
  }, [admin, refresh]);

  if (loading) return <LoadingState variant="dashboard" />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;

  const open = data.exceptions ? data.exceptions.filter((e) => e.status !== "RESOLVED").length : null;
  const firstName = user.name?.split(" ")[0] || "there";

  if (local || accountant) {
    return (
      <AccountantDashboard
        user={user}
        docs={data.docs}
        exceptions={open}
        userName={firstName}
        base={local ? "/company" : "/workspace"}
      />
    );
  }

  const completed = data.docs.filter((d) =>
    ["COMPLETED", "VALIDATED", "STANDARDISED"].includes(d.status),
  ).length;
  const activeUsers = data.users.filter((u) => u.status === "ACTIVE" || u.status === "INVITED");
  const queued = data.docs.filter((d) => ["QUEUED", "UPLOADED"].includes(d.status)).length;
  const running = data.docs.filter((d) => d.status === "PROCESSING").length;
  const failed = data.docs.filter((d) => d.status === "FAILED").length;
  const extracted = data.docs.length - queued - running - failed;
  const denominator = data.docs.length || 1;
  const completeEnd = (extracted / denominator) * 100;
  const queueEnd = completeEnd + (queued / denominator) * 100;
  const runningEnd = queueEnd + (running / denominator) * 100;
  const ringStyle = {
    background: data.docs.length
      ? `conic-gradient(#8fb7e6 0% ${completeEnd}%, #c6dbf3 ${completeEnd}% ${queueEnd}%, #dcecff ${queueEnd}% ${runningEnd}%, #b5cfee ${runningEnd}% 100%)`
      : "#eaf5ff",
  };

  return (
    <div className="dashboard-page">
      <PageHeader
        eyebrow="YOUR PLATFORM, AT A GLANCE"
        title="Platform overview"
        description="Monitor tenants, access requests, and AI processing health across the platform."
      />
      <div className="date-line">
        <span className="pulse-dot" />
        {new Intl.DateTimeFormat("en-MY", { weekday: "long", day: "numeric", month: "long", year: "numeric" }).format(new Date())}{" "}
      </div>
      {data.requests.some((r) => r.status === "PENDING") && (
        <Link className="attention-banner" href="/admin/approvals">
          <span className="attention-icon"><ClipboardCheck size={20} /></span>
          <div>
            <strong>{data.requests.filter((r) => r.status === "PENDING").length} companies are waiting for approval</strong>
            <p>Review access requests and help new teams get started.</p>
          </div>
          <span className="attention-action">Review requests <ArrowRight size={16} /></span>
        </Link>
      )}
      <div className="stats-grid">
        <StatCard label="Active tenants" value={data.tenants.filter((t) => t.status === "ACTIVE").length} icon={<Building2 size={17} />} />
        <StatCard label="Pending requests" value={data.requests.filter((r) => r.status === "PENDING").length} icon={<ClipboardCheck size={17} />} detail="awaiting your review" />
        <StatCard label="Total users" value={data.users.length} icon={<Users size={17} />} />
        <StatCard label="Documents processed" value={data.docs.length} icon={<Files size={17} />} />
      </div>
      <div className="section-heading">
        <h2>Performance at a glance <span className="subtle-tag">Reference benchmarks</span></h2>
        <Link href="/admin/performance">View performance <ArrowRight size={14} /></Link>
      </div>
      <PerformanceCards metrics={data.metrics} />
      <div className="dashboard-middle">
        <TrendChart />
        <Panel title="AI processing status" subtitle="Extraction jobs · independent of record validation" action={<span className="badge green"><span className="badge-dot" />Processing queue</span>}>
          <div className="processing-total">
            <div className="processing-ring" style={ringStyle}><ScanLine size={25} /><strong>{data.docs.length}</strong><small>Total documents</small></div>
          </div>
          <div className="processing-legend">
            {[["Queued", queued, "amber"], ["Processing", running, "blue"], ["Extracted", extracted, "green"], ["Failed", failed, "red"]].map(([label, count, color]) => (
              <div key={String(label)}><span className={`legend-dot ${color}`} /><span>{label}</span><strong>{count}</strong></div>
            ))}
          </div>
        </Panel>
      </div>
      <Panel title="Tenant activity" subtitle="Account usage and document activity across your companies" action={<Link className="text-link" href="/admin/tenants">View all tenants <ArrowRight size={14} /></Link>}>
        <DataTable
          rows={data.tenants}
          rowKey={(t) => t.id}
          columns={[
            { label: "Company", render: (t) => <Link className="company-cell" href={`/admin/tenants/${t.id}`}><span className="company-avatar">{t.name.slice(0, 2)}</span><span><strong>{t.name}</strong></span></Link> },
            { label: "Users / limit", render: (t) => <span>{activeUsers.filter((u) => u.tenantId === t.id).length} <span className="muted">/ {t.accountLimit}</span></span> },
            { label: "Documents", render: (t) => data.docs.filter((d) => d.tenantId === t.id).length },
            { label: "Exceptions", render: (t) => <span className="exception-count">{(data.exceptions ?? []).filter((e) => e.tenantId === t.id && e.status !== "RESOLVED").length}</span> },
            { label: "Status", render: (t) => <Badge>{t.status}</Badge> },
            { label: "", render: (t) => <Link className="icon-btn" aria-label={`View ${t.name}`} href={`/admin/tenants/${t.id}`}><ArrowRight size={16} /></Link> },
          ]}
        />
      </Panel>
    </div>
  );
}
