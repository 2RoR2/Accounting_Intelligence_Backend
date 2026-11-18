import React from "react";
import { decimalDisplay as money } from "@/lib/api/document-contract";
import type { TransactionAnomaly } from "@/types/validation";

export function TransactionAnomalyVisual({ result }: { result: TransactionAnomaly }) {
  const hasBaseline = result.upper_bound !== null && result.baseline_median !== null;
  const amount = Number(result.amount);
  const bound = Number(result.upper_bound);
  const scale = Math.max(amount, bound);
  const chartReady = hasBaseline && Number.isFinite(amount) && Number.isFinite(bound) && scale > 0;
  const amountWidth = chartReady ? Math.min(100, amount / scale * 100) : 0;
  const thresholdPosition = chartReady ? Math.min(100, bound / scale * 100) : 0;
  const state = result.status === "anomaly" ? "Anomaly flagged" : result.status === "normal" ? "Within baseline" : "Baseline unavailable";
  const badgeTone = result.status === "anomaly" ? "red" : result.status === "normal" ? "green" : "amber";

  return (
    <section className="transaction-anomaly-visual" aria-label="Transaction anomaly analysis">
      <div className="transaction-anomaly-heading">
        <div><strong>Transaction anomaly analysis</strong><span>Compared with {result.baseline_count} prior passed same-currency invoices</span></div>
        <span className={`badge ${badgeTone}`}><span className="badge-dot" />{state}</span>
      </div>
      <div className="transaction-anomaly-values">
        <div><span>Current amount</span><strong>{money(result.amount, result.currency)}</strong></div>
        <div><span>Reference median</span><strong>{hasBaseline ? money(result.baseline_median, result.currency) : "Not available"}</strong></div>
        <div><span>Review threshold</span><strong>{hasBaseline ? money(result.upper_bound, result.currency) : "Not available"}</strong></div>
      </div>
      {chartReady && <div className="transaction-anomaly-chart" role="img" aria-label={`Current amount ${money(result.amount, result.currency)}; upper review threshold ${money(result.upper_bound, result.currency)}`}>
        <div className="transaction-anomaly-track"><span className={result.is_anomaly ? "flagged" : "normal"} style={{ width: `${amountWidth}%` }} /><i style={{ left: `${thresholdPosition}%` }} /></div>
        <div className="transaction-anomaly-legend"><span>Current amount</span><span>Upper review threshold</span></div>
      </div>}
      <p>{result.reason}{result.is_anomaly ? " This flags the transaction for human review; it does not determine fraud." : ""}</p>
    </section>
  );
}
