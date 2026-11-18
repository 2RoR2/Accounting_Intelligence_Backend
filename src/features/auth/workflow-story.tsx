"use client";

import { useEffect, useState } from "react";
import { FileText, ScanLine, Sparkles, UserRound } from "lucide-react";

const STEPS = [
  { label: "Scan", title: "A paper invoice bends into view.", copy: "Drop the document into the workspace and let the intake flow begin.", icon: FileText },
  { label: "Extract", title: "A scan reveals every field.", copy: "Supplier, date, line items, and total are surfaced without retyping.", icon: ScanLine },
  { label: "Review", title: "A person stays in control.", copy: "Exceptions are highlighted clearly so questionable data never slips through.", icon: UserRound },
  { label: "Record", title: "The dashboard updates.", copy: "Approved information becomes one standard, export-ready accounting record.", icon: Sparkles },
] as const;

interface Measured { generated: string; classification?: { numerator: number; denominator: number }; byType?: Array<{ type: string; exact: { numerator: number; denominator: number; value: number | null } }>;
  latency?: Array<{ measure: string; median: number; n: number }>; exceptions?: { documents: number; total: number } }

/** Facts from the published public-benchmark run only; with no file, the story shows no numbers at all. */
function measuredFacts(data?: Measured): Array<string[]> {
  if (!data) return [[], [], [], []];
  const invoice = data.byType?.find(row => row.type === "Invoice")?.exact;
  const time = data.latency?.find(row => row.measure.startsWith("End-to-end"));
  return [
    data.classification ? [`Type identified for ${data.classification.numerator} of ${data.classification.denominator} public test documents`] : [],
    [...(invoice?.value != null ? [`Invoice fields exact: ${(invoice.value * 100).toFixed(1)}% (${invoice.numerator}/${invoice.denominator})`] : []),
     ...(time ? [`Median ${time.median.toFixed(1)} s per document (n=${time.n})`] : [])],
    data.exceptions ? [`${data.exceptions.documents} of ${data.exceptions.total} routed to a person with a stated reason`] : [],
    ["Created only after a person approves; AI suggestions are never applied automatically"],
  ];
}

export function WorkflowStory() {
  const [measured, setMeasured] = useState<Measured>();
  useEffect(() => {
    fetch("/evaluation/summary.json").then(r => r.ok ? r.json() : undefined).then(setMeasured).catch(() => undefined);
  }, []);
  const facts = measuredFacts(measured);
  return (
    <section className="workflow-story" aria-labelledby="workflow-story-title">
      <div className="workflow-story-intro">
        <p className="hp-eyebrow">The flow in motion</p>
        <h2 id="workflow-story-title">From paper to <span>trusted record.</span></h2>
        <p className="hp-copy">Every handoff is visible. Scroll through the journey a finance team takes from the first scan to a confident approval.</p>
        {measured && <p className="hp-copy"><small>Figures are measured on 35 public benchmark documents ({measured.generated}), not on your documents.</small></p>}
      </div>
      <div className="workflow-rail">
        <div className="workflow-line" aria-hidden="true" />
        {STEPS.map((step, index) => {
          const Icon = step.icon;
          return (
            <article className="workflow-step" key={step.label}>
              <div className="workflow-marker"><Icon size={19} /><span>0{index + 1}</span></div>
              <div className="workflow-card">
                <p className="workflow-kicker">{step.label}</p>
                <h3>{step.title}</h3>
                <p>{step.copy}</p>
                {facts[index].length > 0 && <div className="workflow-fields" aria-label={`Measured: ${step.label}`}>{facts[index].map(fact => <span key={fact}>{fact}</span>)}</div>}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
