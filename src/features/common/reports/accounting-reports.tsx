"use client";
import { useMemo } from "react";
import { useResource } from "@/features/common/hooks/use-resource";
import { getDocuments, getExceptions } from "@/lib/api/document.service";
import { getStandardisedRecords, downloadCsv } from "@/lib/api/record.service";
import { getPerformanceMetrics } from "@/lib/api/processing.service";
import { currencyTotals, decimalDisplay, escapeMarkup, isPlainNumber } from "@/lib/api/document-contract";
import type { User } from "@/types";
import { EvaluationPage } from "@/features/common/reports/evaluation-page";
import {
  ErrorState,
  LoadingState,
  PageHeader,
  Panel,
  StatCard,
} from "@/components/common/ui";
import {
  CircleAlert,
  FileCheck2,
  Files,
  Timer,
  Download,
  FileSpreadsheet,
  FileText,
} from "lucide-react";

type SummaryRow = [string, string];

function asNumber(value: unknown): number {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : 0;
}

function downloadExcel(filename: string, rows: SummaryRow[]) {
  const cells = rows
    .map(
      (row) =>
        `<Row>${row
          .map(
            (value) =>
              `<Cell><Data ss:Type="${isPlainNumber(value) ? "Number" : "String"}">${escapeMarkup(value)}</Data></Cell>`,
          )
          .join("")}</Row>`,
    )
    .join("");
  const xml = `<?xml version="1.0"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Worksheet ss:Name="Processing Summary">
  <Table>${cells}</Table>
 </Worksheet>
</Workbook>`;
  const url = URL.createObjectURL(new Blob([xml], { type: "application/vnd.ms-excel" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function downloadPdf(title: string, rows: SummaryRow[]) {
  const body = rows
    .map(([label, value]) => `<tr><td>${escapeMarkup(label)}</td><td>${escapeMarkup(value)}</td></tr>`)
    .join("");
  const html = `<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>${escapeMarkup(title)}</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #2a3a4a; padding: 28px; }
    h1 { margin: 0 0 6px; font-size: 22px; }
    p { margin: 0 0 18px; color: #5e7384; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #e6eef5; }
    th { background: #2a3a4a; color: #f6f1e8; }
    tr:nth-child(even) td { background: #f6f1e8; }
  </style>
</head>
<body>
  <h1>${escapeMarkup(title)}</h1>
  <p>Accounting Intelligence · Processing Summary · ${new Date().toLocaleString("en-MY")}</p>
  <table>
    <thead><tr><th>Item</th><th>Value</th></tr></thead>
    <tbody>${body}</tbody>
  </table>
  <script>window.onload = () => { window.print(); }</script>
</body>
</html>`;
  const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
  window.open(url, "_blank", "noopener,noreferrer");
  setTimeout(() => URL.revokeObjectURL(url), 15000);
}

export function AccountingReportsPage({ user }: { user: User }) {
  const { data, error, loading, refresh } = useResource(
    async () => {
      const [docs, exceptions, records, metrics] = await Promise.all([
        getDocuments(user),
        getExceptions(user),
        getStandardisedRecords(user),
        getPerformanceMetrics(user).catch(() => []),
      ]);
      return { docs, exceptions, records, metrics };
    },
    user.id,
  );

  const summary = useMemo(() => {
    if (!data) return null;
    const total = data.docs.length;
    const completed = data.docs.filter((d) =>
      ["COMPLETED", "STANDARDISED"].includes(d.status),
    ).length;
    const approved = data.docs.filter((d) => d.status === "VALIDATED").length;
    const openExceptions = data.exceptions.filter((e) => e.status !== "RESOLVED");
    const open = openExceptions.length;
    const failed = data.docs.filter((d) => d.status === "FAILED").length;
    // One document can raise several findings, so the rate counts documents, not findings.
    const documentsWithExceptions = new Set(openExceptions.map((e) => e.documentId)).size;
    const exceptionRate = total ? Math.round((documentsWithExceptions / total) * 100) : 0;
    const completionRate = total ? Math.round((completed / total) * 100) : 0;
    // Documents with no recorded time report 0; they are unmeasured, not instant.
    const timed = data.docs
      .map((doc) => asNumber((doc as { seconds?: number }).seconds))
      .filter((seconds) => seconds > 0);
    const avgSeconds = timed.length
      ? Math.round(timed.reduce((sum, seconds) => sum + seconds, 0) / timed.length)
      : 0;

    const ar = data.records.filter((r) => r.category === "ACCOUNTS_RECEIVABLE");
    const ap = data.records.filter((r) => r.category === "ACCOUNTS_PAYABLE");
    const totals = (rows: typeof data.records) =>
      currencyTotals(rows.map((r) => ({ currency: r.extraction.fields.currency?.value, total: r.extraction.fields.total?.value })));
    const groups = [["Accounts Receivable", totals(ar)], ["Accounts Payable", totals(ap)]] as const;
    // Currencies are never added together, on screen or in an export.
    const totalRows: SummaryRow[] = groups.flatMap(([label, rows]): SummaryRow[] =>
      rows.length
        ? rows.map((row): SummaryRow => [
            `${label} (${row.records} ${row.records === 1 ? "record" : "records"})`,
            decimalDisplay(row.total, row.currency ?? "No currency"),
          ])
        : [[label, "No records"]],
    );

    const byStatus: SummaryRow[] = [
      ["Uploaded / queued", String(data.docs.filter((d) => ["UPLOADED", "QUEUED"].includes(d.status)).length)],
      ["Processing", String(data.docs.filter((d) => d.status === "PROCESSING").length)],
      [
        "Extracted / in review",
        String(
          data.docs.filter((d) =>
            ["EXTRACTED", "IN_REVIEW", "CORRECTED", "EXCEPTION"].includes(d.status),
          ).length,
        ),
      ],
      ["Approved, not yet standardised", String(approved)],
      ["Standardised", String(completed)],
      ["Failed", String(failed)],
    ];

    // Workspace backlog figures only. SAIC success criteria are judged by the measured benchmark below, never by these.
    const criteria: SummaryRow[] = [
      ["Documents with open exceptions (workspace backlog, not SC4)", `${exceptionRate}%`],
      ["Average processing time (workspace uploads)", avgSeconds ? `${avgSeconds}s` : "Not available"],
    ];

    const rows: SummaryRow[] = [
      ["Documents", String(total)],
      ["Standardised", `${completed} (${completionRate}%)`],
      ["Open exceptions", String(open)],
      ["Documents with open exceptions", `${documentsWithExceptions} of ${total} (${exceptionRate}%)`],
      ["Failed", String(failed)],
      ...byStatus,
      ...totalRows,
      ["Standardised records", String(data.records.length)],
      ...criteria,
      ...data.metrics.map((m): SummaryRow => [m.label, m.target ? `${m.value} (target ${m.target})` : m.value]),
    ];

    return {
      total,
      completed,
      open,
      failed,
      documentsWithExceptions,
      exceptionRate,
      completionRate,
      avgSeconds,
      totalRows,
      recordCount: data.records.length,
      byStatus,
      criteria,
      rows,
    };
  }, [data]);

  if (loading) return <LoadingState />;
  if (error || !data || !summary) return <ErrorState message={error} retry={refresh} />;

  const exportName = "processing-summary";
  const exportCsv = () => downloadCsv(`${exportName}.csv`, [["Item", "Value"], ...summary.rows]);
  const exportExcel = () => downloadExcel(`${exportName}.xls`, [["Item", "Value"], ...summary.rows]);
  const exportPdf = () => downloadPdf("Processing Summary", summary.rows);

  return (
    <div className="dashboard-page reports-page">
      <PageHeader
        title="Processing Summary"
        description="Overall totals, status breakdown, and performance against the success criteria. Use Documents, Exceptions, or Standardised Records for detail."
        actions={
          <>
            <button className="btn" onClick={exportCsv}>
              <Download size={15} /> CSV
            </button>
            <button className="btn" onClick={exportExcel}>
              <FileSpreadsheet size={15} /> Excel
            </button>
            <button className="btn" onClick={exportPdf}>
              <FileText size={15} /> PDF
            </button>
          </>
        }
      />

      <div className="stats-grid pipeline-health-grid">
        <StatCard variant="solid" tone="invoice" label="Documents" value={summary.total} icon={<Files size={17} />} detail="All uploads" />
        <StatCard
          variant="solid"
          tone="processing"
          label="Standardised"
          value={summary.completed}
          icon={<FileCheck2 size={17} />}
          detail={`${summary.completionRate}% completed`}
        />
        <StatCard
          variant="solid"
          tone="exception"
          label="Open exceptions"
          value={summary.open}
          icon={<CircleAlert size={17} />}
          detail={`${summary.documentsWithExceptions} of ${summary.total} documents (${summary.exceptionRate}%)`}
        />
        <StatCard variant="solid" tone="failed" label="Failed" value={summary.failed} icon={<Timer size={17} />} detail="Need retry" />
      </div>

      <div className="dashboard-middle">
        <Panel title="Status breakdown" subtitle="Counts only — open Documents for the list">
          <div className="statement">
            {summary.byStatus.map(([label, count]) => (
              <div className="statement-row" key={label}>
                <span>{label}</span>
                <strong>{count}</strong>
              </div>
            ))}
          </div>
        </Panel>

        <Panel title="Standardised totals" subtitle="Completed records only, by currency. Currencies are never added together.">
          <div className="statement">
            {summary.totalRows.map(([label, value]) => (
              <div className="statement-row" key={label + value}>
                <span>{label}</span>
                <strong>{value}</strong>
              </div>
            ))}
            <div className="statement-row strong">
              <span>Records</span>
              <strong>{summary.recordCount}</strong>
            </div>
          </div>
          <div className="panel-body">
            <p className="empty-state">Export the full list from Standardised Records.</p>
          </div>
        </Panel>
      </div>

      <Panel title="Workspace backlog" subtitle="Live figures from this workspace. They are not the SAIC success-criteria measurements below.">
        <div className="statement">
          {summary.criteria.map(([label, value]) => (
            <div className="statement-row" key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
            </div>
          ))}
        </div>
      </Panel>

      <EvaluationPage embedded />
    </div>
  );
}
