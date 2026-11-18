import type { BankMoneyTransaction, MoneyDirection } from "@/types/money";

const WIDTH = 1400;
const HEIGHT = 240;
const PAD = { top: 20, right: 28, bottom: 42, left: 82 };

function amount(value: string | null): number | null {
  if (value === null || value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function axisTick(value: number): string {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function money(value: number, currency: string): string {
  return new Intl.NumberFormat("en-MY", {
    style: "currency",
    currency,
    currencyDisplay: "code",
    maximumFractionDigits: 2,
  }).format(value);
}

function shortDate(value: string | null): string {
  if (!value) return "Unknown date";
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("en-MY", { day: "2-digit", month: "short" }).format(date);
}

function CurrencyChart({
  rows,
  currency,
  directionLabel,
}: {
  rows: BankMoneyTransaction[];
  currency: string;
  directionLabel: string;
}) {
  const items = rows
    .map((row) => ({ row, value: amount(row.amount), threshold: row.anomaly?.upperBound == null ? null : amount(row.anomaly.upperBound) }))
    .filter((item): item is { row: BankMoneyTransaction; value: number; threshold: number | null } => item.value !== null)
    .sort((a, b) => {
      const dateA = a.row.postedDate ?? "";
      const dateB = b.row.postedDate ?? "";
      return dateA.localeCompare(dateB) || a.row.id.localeCompare(b.row.id);
    })
    .slice(-30);

  const flagged = items.filter(({ row }) => row.anomaly?.isAnomaly).length;
  const insufficient = items.filter(({ row }) => row.anomaly?.status === "insufficient_baseline").length;
  const maximum = Math.max(1, ...items.flatMap(({ value, threshold }) => [value, threshold ?? 0]));
  const yMax = maximum * 1.14;
  const innerWidth = WIDTH - PAD.left - PAD.right;
  const innerHeight = HEIGHT - PAD.top - PAD.bottom;
  const x = (index: number) => PAD.left + (items.length <= 1 ? innerWidth / 2 : (index / (items.length - 1)) * innerWidth);
  const y = (value: number) => PAD.top + innerHeight - (value / yMax) * innerHeight;
  const yTicks = [0, yMax / 2, yMax];
  const labelIndices = Array.from(new Set([0, Math.floor((items.length - 1) / 2), items.length - 1])).filter((index) => index >= 0);

  return (
    <section className="bank-anomaly-overview-currency" aria-label={`${currency} ${directionLabel} anomaly chart`}>
      <div className="bank-anomaly-overview-subhead">
        <strong>{currency}</strong>
        <span>{items.length} transactions · {flagged} flagged · {insufficient} with insufficient history</span>
      </div>
      {items.length ? (
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`${directionLabel} transaction amounts and review thresholds over time for ${currency}; vertical axis uses compact amount labels`} style={{ width: "100%", height: "auto", display: "block" }}>
          {yTicks.map((tick, index) => (
            <g key={index}>
              <line x1={PAD.left} x2={WIDTH - PAD.right} y1={y(tick)} y2={y(tick)} stroke="#dce6f2" strokeWidth="1" />
              <text x={PAD.left - 10} y={y(tick) + 4} textAnchor="end" fill="#657993" fontSize="12">{axisTick(tick)}</text>
            </g>
          ))}
          {items.map(({ row, value, threshold }, index) => {
            const px = x(index);
            const color = row.anomaly?.isAnomaly ? "#c63d4d" : row.anomaly?.status === "insufficient_baseline" ? "#91a2b8" : "#2672c9";
            const description = `${row.description} · ${shortDate(row.postedDate)} · ${money(value, currency)} · ${row.anomaly?.upperBound == null ? "No threshold available" : `Threshold ${money(threshold ?? 0, currency)}`} · ${row.anomaly?.label ?? "No anomaly result"}`;
            return (
              <g key={row.id}>
                <title>{description}</title>
                <line x1={px} x2={px} y1={y(0)} y2={y(value)} stroke={color} strokeWidth="5" strokeLinecap="round" opacity="0.76" />
                <circle cx={px} cy={y(value)} r={row.anomaly?.isAnomaly ? 7 : 5} fill={color} stroke="white" strokeWidth="2" />
                {threshold !== null && <path d={`M ${px} ${y(threshold) - 5} L ${px + 5} ${y(threshold)} L ${px} ${y(threshold) + 5} L ${px - 5} ${y(threshold)} Z`} fill="#d28a13" stroke="white" strokeWidth="1" />}
              </g>
            );
          })}
          {labelIndices.map((index) => (
            <text key={index} x={x(index)} y={HEIGHT - 14} textAnchor={index === 0 ? "start" : index === items.length - 1 ? "end" : "middle"} fill="#657993" fontSize="12">{shortDate(items[index].row.postedDate)}</text>
          ))}
        </svg>
      ) : (
        <p className="bank-anomaly-overview-empty">No transaction amounts are available to plot.</p>
      )}
      <div className="bank-anomaly-overview-legend" aria-label="Chart legend">
        <span><i className="bank-anomaly-legend-dot bank-anomaly-legend-normal" />Amount within usual range</span>
        <span><i className="bank-anomaly-legend-dot bank-anomaly-legend-flagged" />Unusually high amount</span>
        <span><i className="bank-anomaly-legend-diamond" />Review threshold</span>
        <span><i className="bank-anomaly-legend-dot bank-anomaly-legend-insufficient" />Insufficient history</span>
      </div>
    </section>
  );
}

export function BankAnomalyOverview({
  rows,
  direction,
}: {
  rows: BankMoneyTransaction[];
  direction: MoneyDirection;
}) {
  const directionLabel = direction === "money_in" ? "Money In" : "Money Out";
  const currencies = Array.from(new Set(rows.map((row) => row.currency ?? "MYR"))).sort();
  const flagged = rows.filter((row) => row.anomaly?.isAnomaly).length;

  return (
    <section className="bank-anomaly-overview" aria-labelledby={`bank-anomaly-overview-${direction}`}>
      <div className="bank-anomaly-overview-heading">
        <div>
          <p className="bank-anomaly-overview-kicker">TRANSACTION INSIGHTS</p>
          <h2 id={`bank-anomaly-overview-${direction}`}>{directionLabel} amount overview</h2>
          <p>Compare transaction amounts with the review thresholds saved for each bank row.</p>
        </div>
        <div className="bank-anomaly-overview-count"><strong>{flagged}</strong><span>flagged for review</span></div>
      </div>
      <p className="bank-anomaly-overview-scope">Amount anomaly detection only. It flags unusually high amounts; it does not check duplicates or unusual frequency, and a flag does not prove fraud.</p>
      {rows.length ? currencies.map((currency) => (
        <CurrencyChart key={currency} rows={rows.filter((row) => (row.currency ?? "MYR") === currency)} currency={currency} directionLabel={directionLabel} />
      )) : <p className="bank-anomaly-overview-empty">Import a bank statement to see transaction amounts and anomaly thresholds here.</p>}
    </section>
  );
}
