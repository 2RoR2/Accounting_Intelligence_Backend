"use client";

type SalesPoint = { label: string; value: number };

export function TotalSalesChart({ data, currency = "RM" }: { data: SalesPoint[]; currency?: string }) {
  const max = Math.max(1, ...data.map((point) => point.value));
  const total = data.reduce((sum, point) => sum + point.value, 0);
  const width = 520;
  const height = 150;
  const points = data.map((point, index) => {
    const x = data.length === 1 ? width / 2 : 14 + (index * (width - 28)) / (data.length - 1);
    const y = height - 18 - (point.value / max) * (height - 38);
    return `${x},${y}`;
  }).join(" ");

  return <div className="total-sales-chart" aria-label={`Sales total ${currency} ${total.toFixed(2)}`}>
    <div className="total-sales-chart-head"><div><span>Total sales</span><strong>{currency} {total.toFixed(2)}</strong></div><small>Approved documents</small></div>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Sales trend">
      {[0, 1, 2, 3].map((line) => <line key={line} x1="0" x2={width} y1={20 + line * 36} y2={20 + line * 36} />)}
      <polyline points={points} />
      {data.map((point, index) => { const [x, y] = points.split(" ")[index].split(","); return <g key={`${point.label}-${index}`}><circle cx={x} cy={y} r="4" /><title>{`${point.label}: ${currency} ${point.value.toFixed(2)}`}</title></g>; })}
    </svg>
    <div className="total-sales-chart-labels">{data.map((point) => <span key={point.label}>{point.label}</span>)}</div>
  </div>;
}
