"use client";
import { useEffect } from "react";
import Link from "next/link";
import type { User } from "@/types";
import { useResource } from "@/features/common/hooks/use-resource";
import { getDocuments, getExceptions } from "@/lib/api/document.service";
import { Badge, DataTable, ErrorState, LoadingState, PageHeader, Panel } from "@/components/common/ui";

const PASSED = new Set(["VALIDATED", "STANDARDISED", "COMPLETED"]);
const NEEDS_REVIEW = new Set(["EXCEPTION", "IN_REVIEW", "FAILED"]);

export function ValidationOverviewPage({ user }: { user: User }) {
  const { data, error, loading, refresh } = useResource(async () => {
    const [docs, exceptions] = await Promise.all([
      getDocuments(user),
      getExceptions(user),
    ]);
    return { docs, exceptions };
  }, user.id);

  useEffect(() => {
    const id = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(id);
  }, [refresh]);

  if (loading && !data) return <LoadingState variant="table" />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;

  const total = data.docs.length || 1;
  const validated = data.docs.filter((d) => PASSED.has(d.status)).length;
  const open = data.exceptions.filter((e) => e.status !== "RESOLVED").length;
  const exceptionRate = Math.round((open / total) * 100);
  const validatedRate = Math.round((validated / total) * 100);
  const base = "/company";

  const rows = data.docs.map((doc) => {
    const openForDoc = data.exceptions.filter((e) => e.documentId === doc.id && e.status !== "RESOLVED").length;
    const passed = PASSED.has(doc.status) && openForDoc === 0;
    const rate = passed ? 100 : openForDoc > 0 ? Math.max(0, 100 - openForDoc * 25) : NEEDS_REVIEW.has(doc.status) ? 0 : 50;
    return { ...doc, openForDoc, rate, passed };
  });

  return (
    <div className="validation-overview-page">
      <PageHeader
        eyebrow="VALIDATION"
        title="Validation overview"
        description="Company-wide validation performance. Updates live every 10 seconds. Exception rate should stay below 10%."
      />
      <div className="dashboard-widget-metrics validation-metrics">
        <div className="widget-metric-grid">
          <Link href={`${base}/documents`}>
            <strong>{data.docs.length}</strong>
            <span>Documents checked</span>
          </Link>
          <Link href={`${base}/documents?status=VALIDATED`}>
            <strong>{validated}</strong>
            <span>Validated · {validatedRate}%</span>
          </Link>
          <Link href={`${base}/exceptions`}>
            <strong>{open}</strong>
            <span>Open exceptions · {exceptionRate}%</span>
          </Link>
          <Link href={`${base}/exceptions`}>
            <strong>{"< 10%"}</strong>
            <span>Exception rate goal</span>
          </Link>
        </div>
      </div>
      <Panel title="Documents by validation status">
        <DataTable
          rows={rows}
          rowKey={(d) => d.id}
          columns={[
            { label: "Document", render: (d) => d.name },
            { label: "Type", render: (d) => d.type },
            { label: "Status", render: (d) => <Badge>{d.status}</Badge> },
            { label: "Open exceptions", render: (d) => d.openForDoc },
            {
              label: "Validation",
              render: (d) => (
                <span style={{ fontWeight: 650, color: d.passed ? "#1d6abd" : d.openForDoc > 0 ? "#c2410c" : "#5e7384" }}>
                  {d.passed ? "Passed" : d.openForDoc > 0 ? "Needs review" : d.status}
                </span>
              ),
            },
            { label: "Rate", render: (d) => `${d.rate}%` },
          ]}
        />
      </Panel>
    </div>
  );
}
