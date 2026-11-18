import type { BankTransactionAnomaly, MoneyDirection } from "@/types/money";

function formatMoney(value: string | null, currency: string): string {
  if (value === null || !Number.isFinite(Number(value))) return "Not available";
  return new Intl.NumberFormat("en-MY", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(value));
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ padding: "10px 12px", border: "1px solid #dfe8f3", borderRadius: 8, background: "white" }}>
      <div style={{ color: "#657993", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.04em" }}>{label}</div>
      <div style={{ marginTop: 4, fontSize: 15, fontWeight: 700 }}>{value}</div>
    </div>
  );
}

export function BankAnomalyChart({
  anomaly,
  direction,
}: {
  anomaly: BankTransactionAnomaly;
  direction: MoneyDirection;
}) {
  const median = anomaly.baselineMedian === null ? null : Number(anomaly.baselineMedian);
  const threshold = anomaly.upperBound === null ? null : Number(anomaly.upperBound);
  const score = anomaly.anomalyScore === null ? null : Number(anomaly.anomalyScore);
  const hasBaseline =
    median !== null &&
    threshold !== null &&
    Number.isFinite(median) &&
    Number.isFinite(threshold) &&
    anomaly.baselineCount >= 5;
  const canPlotMultiples =
    hasBaseline &&
    threshold! > 0 &&
    score !== null &&
    Number.isFinite(score);
  const maxMultiple = canPlotMultiples ? Math.max(1.2, score! * 1.08) : 1;
  const medianPosition = canPlotMultiples ? Math.min(100, (median! / threshold!) / maxMultiple * 100) : 0;
  const currentPosition = canPlotMultiples ? Math.min(100, score! / maxMultiple * 100) : 0;
  const thresholdPosition = canPlotMultiples ? 100 / maxMultiple : 0;
  const directionLabel = direction === "money_in" ? "Money In" : "Money Out";
  const statusText =
    anomaly.status === "anomaly"
      ? "Above review threshold"
      : anomaly.status === "normal"
        ? "Within usual range"
        : "Not enough history";
  const accent = anomaly.isAnomaly ? "#c63d4d" : "#2672c9";

  return (
    <section
      className="bank-anomaly-chart"
      aria-label={directionLabel + " amount anomaly chart"}
      style={{ border: "1px solid #d7e3f2", borderRadius: 12, padding: 16, background: "#f8fbff", color: "#20324b" }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "start", flexWrap: "wrap" }}>
        <div>
          <h3 style={{ margin: 0, fontSize: 16 }}>Transaction amount vs review threshold</h3>
          <p style={{ margin: "5px 0 0", color: "#5c708c", fontSize: 13 }}>
            Amount anomaly detection for {directionLabel.toLowerCase()} transactions
          </p>
        </div>
        <span style={{ color: accent, fontWeight: 700, fontSize: 13 }}>{statusText}</span>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(135px, 1fr))", gap: 8, marginTop: 14 }}>
        <Metric label="Typical prior amount" value={formatMoney(anomaly.baselineMedian, anomaly.currency)} />
        <Metric label="Review threshold" value={formatMoney(anomaly.upperBound, anomaly.currency)} />
        <Metric label="This transaction" value={formatMoney(anomaly.amount, anomaly.currency)} />
      </div>

      {canPlotMultiples ? (
        <div style={{ marginTop: 18 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8, color: "#5c708c", fontSize: 12 }}>
            <span>Amount as multiples of the threshold</span>
            <span>Compared with {anomaly.baselineCount} prior {directionLabel} transactions</span>
          </div>
          <svg
            viewBox="0 0 640 118"
            role="img"
            aria-label={
              "Typical amount is " + (median! / threshold!).toFixed(2) +
              " times the threshold; current amount is " + score!.toFixed(2) + " times."
            }
            style={{ display: "block", width: "100%", height: "auto", marginTop: 8, overflow: "visible" }}
          >
            <line x1="32" x2="608" y1="72" y2="72" stroke="#cad7e8" strokeWidth="8" strokeLinecap="round" />
            <line x1="32" x2={32 + 576 * currentPosition / 100} y1="72" y2="72" stroke={accent} strokeWidth="8" strokeLinecap="round" opacity="0.72" />
            <line x1={32 + 576 * thresholdPosition / 100} x2={32 + 576 * thresholdPosition / 100} y1="35" y2="87" stroke="#d28a13" strokeWidth="3" strokeDasharray="5 4" />
            <circle cx={32 + 576 * medianPosition / 100} cy="72" r="7" fill="#2672c9" stroke="white" strokeWidth="3" />
            <circle cx={32 + 576 * currentPosition / 100} cy="72" r="8" fill={accent} stroke="white" strokeWidth="3" />
            <text x="32" y="108" fill="#657993" fontSize="12">0×</text>
            <text x={32 + 576 * thresholdPosition / 100} y="24" textAnchor="middle" fill="#9a650c" fontSize="12" fontWeight="700">Threshold · 1×</text>
            <text x="608" y="108" textAnchor="end" fill="#657993" fontSize="12">{maxMultiple.toFixed(maxMultiple < 3 ? 1 : 0)}×</text>
          </svg>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "8px 18px", color: "#5c708c", fontSize: 12, marginTop: 2 }}>
            <span><i style={{ display: "inline-block", width: 9, height: 9, borderRadius: "50%", background: "#2672c9", marginRight: 6 }} />Typical prior amount</span>
            <span><i style={{ display: "inline-block", width: 9, height: 9, borderRadius: "50%", background: accent, marginRight: 6 }} />Current transaction</span>
            <span><i style={{ display: "inline-block", width: 2, height: 12, background: "#d28a13", margin: "0 9px 0 3px", verticalAlign: "-2px" }} />Review threshold</span>
          </div>
          <p style={{ margin: "8px 0 0", color: "#5c708c", fontSize: 13 }}>
            Current amount: {score!.toFixed(2)}× the threshold. Values above 1× are flagged for review.
          </p>
        </div>
      ) : hasBaseline ? (
        <p style={{ margin: "14px 0 0", padding: 12, borderRadius: 8, background: "#fff7e8", color: "#75561b", fontSize: 13 }}>
          The baseline threshold is zero, so a multiples scale cannot be plotted. Compare the amounts above with the threshold shown.
        </p>
      ) : (
        <p style={{ margin: "14px 0 0", padding: 12, borderRadius: 8, background: "#fff7e8", color: "#75561b", fontSize: 13 }}>
          At least five earlier transactions with the same account, direction, and currency are needed to set a threshold.
        </p>
      )}

      <p style={{ margin: "12px 0 0", color: "#5c708c", fontSize: 13 }}>
        {anomaly.reason} {anomaly.isAnomaly ? "This requests human review; it does not prove fraud." : ""}
      </p>
    </section>
  );
}
