"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { get } from "@/lib/api/client";
import { usePathname } from "next/navigation";
import { Badge, DataTable, LoadingState, PageHeader, Panel } from "@/components/common/ui";

interface Score { numerator: number; denominator: number; value: number | null }
interface TypeRow { type: string; documents: number; exact: Score; normalized: Score; tolerant: Score; correct: number; incorrect: number; missing: number }
interface FieldExample { document: string; expected: string; extracted: string | null }
interface FieldRow { errors?: string[]; examples?: FieldExample[]; type: string; field: string; n: number; exact: Score; normalized: Score; tolerant: Score; correct: number; incorrect: number; missing: number }
interface Criterion { name: string; target: string; result: string; status: string; note: string }
interface Evaluation {
  liveRun?: { statement: string } | null;
  generated: string; dataset: string[]; criteria: Criterion[]; classification: Score & { errors: string[] };
  byType: TypeRow[]; baseline: TypeRow[]; lineItems: Array<{ scope: string; exact: Score; tolerant: Score; note: string }>;
  fields: FieldRow[]; notEvaluated: Array<{ field: string; reason: string }>;
  qwen: Array<{ type: string; documents: number; requested: number; verified: number; unverified: number; rejected: number; labelledCorrect: number; labelledDisagree: number; unlabelled: number }>;
  latency: Array<{ measure: string; n: number; median: number; mean: number; p95: number; max: number; note: string }>;
  exceptions: { documents: number; total: number; reasons: Array<{ reason: string; count: number }> };
  corrections: Array<{ type: string; documents: number; completed: number; entries: number; suppliers: number; typedFields: number }>;
  qwenDisagreements?: Array<{ document: string; field: string; parser: string | null; model: string | null; sourceCheck: string | null; shownToReviewer: boolean; labelSaysModel: string }>;
}

const pct = (score?: Score | null) => score?.value == null ? "Not evaluated" : `${(score.value * 100).toFixed(1)}% (${score.numerator}/${score.denominator})`;

/** embedded: shown inside Summary, which supplies its own page header. */
export function EvaluationPage({ embedded = false }: { embedded?: boolean } = {}) {
  const area = (usePathname() ?? "/workspace").split("/")[1] || "workspace";
  const studyHref = `/${area}/evaluation/effort-study`;
  const [data, setData] = useState<Evaluation>();
  const [error, setError] = useState("");
  const [study, setStudy] = useState<{ sessions: number; participants: number; paired: unknown[]; reduction: number | null; valid: boolean; status: string }>();
  useEffect(() => { get<typeof study>("/api/effort-study/results").then(setStudy).catch(() => undefined); }, []);
  useEffect(() => {
    fetch("/evaluation/summary.json").then(r => { if (!r.ok) throw new Error("Evaluation results have not been generated."); return r.json(); })
      .then(setData).catch(e => setError((e as Error).message));
  }, []);
  if (error) return <p role="alert">{error}</p>;
  if (!data) return <LoadingState />;
  const typeColumns = [
    { label: "Type", render: (r: TypeRow) => r.type },
    { label: "Documents", render: (r: TypeRow) => r.documents },
    { label: "Exact", render: (r: TypeRow) => pct(r.exact) },
    { label: "Normalized", render: (r: TypeRow) => pct(r.normalized) },
    { label: "Formatting-tolerant", render: (r: TypeRow) => pct(r.tolerant) },
    { label: "Correct / incorrect / missing", render: (r: TypeRow) => `${r.correct} / ${r.incorrect} / ${r.missing}` },
  ];
  return <>
    {embedded ? <h2 className="summary-section-title" id="saic-evaluation">SAIC success criteria (measured benchmark)</h2>
      : <PageHeader title="Extraction evaluation" description={`Measured against published labels. Generated ${data.generated}.`} />}
    <section className="saic-scorecard" aria-label="SAIC success criteria scorecard">
      {data.criteria.map(raw => {
        // SC2 comes from recorded human sessions, read live; it changes only when the study rule is satisfied.
        const item = raw.name.startsWith("SC2") && study ? { ...raw, status: study.status,
          result: study.valid && study.reduction != null ? `Measured reduction ${study.reduction}% (${study.paired.length} paired participants)`
            : `${study.sessions} tasks from ${study.participants} participants recorded; ${study.paired.length} completed both modes` } : raw;
        const tone = item.status === "MET" ? "met" : item.status === "NOT MET" ? "not-met" : "not-measured";
        return <article key={item.name} className={`saic-card is-${tone}`}>
          <span className="saic-card-status">{item.status === "MET" ? "Met" : item.status === "NOT MET" ? "Not met" : "Not yet measured"}</span>
          <h3>{item.name}</h3>
          <p className="saic-card-target">Target: {item.target}</p>
          <p className="saic-card-result">{item.result}</p>
        </article>;
      })}
    </section>
    <p className="effort-link"><Link href={studyHref} className="btn small">Open the manual effort study (SC2)</Link></p>
    <Panel title="What this page measures">
      <div className="panel-body">
        <p role="status"><strong>This is a fixed 35-document benchmark, not the accuracy of the documents you upload.</strong> A new upload has no labels to compare with, so its accuracy is not evaluated. Each field&apos;s evidence and reliability state on the review page describe that document; they are not probabilities and do not come from this benchmark.</p>
        {data.liveRun && <p>{data.liveRun.statement}</p>}
      </div>
    </Panel>
    <Panel title="About this evaluation">
      <div className="panel-body"><ul>{data.dataset.map(line => <li key={line}>{line}</li>)}</ul></div>
    </Panel>
    <Panel title="AccordAI success criteria">
      <DataTable rows={data.criteria} rowKey={r => r.name} columns={[
        { label: "Criterion", render: r => r.name }, { label: "Target", render: r => r.target }, { label: "Result", render: r => r.result },
        { label: "Status", render: r => <Badge>{r.status}</Badge> }, { label: "Conditions", render: r => r.note },
      ]} />
    </Panel>
    <Panel title="Document fields by type" subtitle="Scored on the parser's output. Accountant corrections are never counted here.">
      <DataTable rows={data.byType} rowKey={r => r.type} columns={typeColumns} />
    </Panel>
    <Panel title="Earlier baseline" subtitle="The same labels and evaluator, on the code before this work.">
      <DataTable rows={data.baseline} rowKey={r => r.type} columns={typeColumns} />
    </Panel>
    <Panel title="Classification and line items">
      <div className="panel-body"><p>Classification: {pct(data.classification)}. {data.classification.errors.join(" ")}</p></div>
      {data.lineItems.length > 0 && <DataTable rows={data.lineItems} rowKey={r => r.scope} columns={[
        { label: "Scope", render: r => r.scope }, { label: "Exact", render: r => pct(r.exact) }, { label: "Formatting-tolerant", render: r => pct(r.tolerant) }, { label: "Note", render: r => r.note },
      ]} />}
    </Panel>
    <Panel title="Qwen-assisted results" subtitle="Suggestions need a person to accept them, so they are reported apart from parser accuracy. Scored only where a label exists.">
      <DataTable rows={data.qwen} rowKey={r => r.type} columns={[
        { label: "Type", render: r => r.type }, { label: "Documents called", render: r => r.documents }, { label: "Fields asked", render: r => r.requested },
        { label: "Source-verified", render: r => r.verified }, { label: "Unverified", render: r => r.unverified }, { label: "Rejected", render: r => r.rejected },
        { label: "Labels agree", render: r => r.labelledCorrect }, { label: "Labels disagree", render: r => r.labelledDisagree }, { label: "No label", render: r => r.unlabelled },
      ]} />
    </Panel>
    {!!data.qwenDisagreements?.length && <Panel title="Recorded Qwen disagreements" subtitle="From a recorded run on public benchmark files, not a live call. A disagreement keeps the parser value and needs a person's decision.">
      <DataTable rows={data.qwenDisagreements} rowKey={r => r.document + r.field} columns={[
        { label: "Document", render: r => r.document }, { label: "Field", render: r => r.field },
        { label: "Parser read", render: r => r.parser ?? "nothing" }, { label: "Qwen read", render: r => r.model ?? "nothing" },
        { label: "Source check", render: r => r.shownToReviewer ? (r.sourceCheck ?? "not recorded") : `${r.sourceCheck}: hidden from the reviewer` },
        { label: "Published label", render: r => r.labelSaysModel === "agree" ? "Agrees with Qwen" : r.labelSaysModel === "disagree" ? "Disagrees with Qwen" : "No label" },
      ]} />
    </Panel>}
    <Panel title="Processing time (seconds)">
      <DataTable rows={data.latency} rowKey={r => r.measure} columns={[
        { label: "Measure", render: r => r.measure }, { label: "n", render: r => r.n }, { label: "Median", render: r => r.median }, { label: "Mean", render: r => r.mean },
        { label: "p95", render: r => r.p95 }, { label: "Max", render: r => r.max }, { label: "Conditions", render: r => r.note },
      ]} />
    </Panel>
    <Panel title="Exceptions" subtitle={`${data.exceptions.documents} of ${data.exceptions.total} documents had a failed rule at first validation.`}>
      <DataTable rows={data.exceptions.reasons} rowKey={r => r.reason} columns={[{ label: "Reason", render: r => r.reason }, { label: "Findings", render: r => r.count }]} />
    </Panel>
    {data.corrections.length > 0 && <Panel title="Manual actions" subtitle="Counts of actions, not time. This is not a measured reduction in effort.">
      <DataTable rows={data.corrections} rowKey={r => r.type} columns={[
        { label: "Type", render: r => r.type }, { label: "Documents", render: r => r.documents }, { label: "Standardised", render: r => r.completed },
        { label: "Field entries", render: r => r.entries }, { label: "Suppliers added", render: r => r.suppliers }, { label: "Labelled fields, for scale", render: r => r.typedFields },
      ]} />
    </Panel>}
    <Panel title="Accuracy by field" subtitle="Only fields with labels are scored.">
      <DataTable rows={data.fields} rowKey={r => r.type + r.field} columns={[
        { label: "Type", render: r => r.type }, { label: "Field", render: r => r.field }, { label: "n", render: r => r.n }, { label: "Exact", render: r => pct(r.exact) },
        { label: "Normalized", render: r => pct(r.normalized) }, { label: "Formatting-tolerant", render: r => pct(r.tolerant) },
        { label: "Correct / incorrect / missing", render: r => `${r.correct} / ${r.incorrect} / ${r.missing}` },
        { label: "Documents with errors", render: r => r.errors?.length ? r.errors.join(", ") : "None" },
        { label: "Examples from the source", render: r => r.examples?.length
          ? r.examples.map(e => <div key={e.document}>{e.document}: label &quot;{e.expected}&quot;, extracted {e.extracted == null ? "nothing" : `"${e.extracted}"`}</div>)
          : "None" },
      ]} />
    </Panel>
    <Panel title="Not evaluated">
      <DataTable rows={data.notEvaluated} rowKey={r => r.field} columns={[{ label: "Item", render: r => r.field }, { label: "Reason", render: r => r.reason }]} />
    </Panel>
  </>;
}
