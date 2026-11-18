"use client";

import type { Document } from "@/types";

const INK = "#2a3a4a";
const ACCENT = "#7aa8c8";
const ICE = "#d7f1ff";
const MIST = "#a8d3f0";
const SLATE = "#5e7384";

function Spark({ values }: { values: number[] }) {
  const hi = Math.max(...values, 1);
  const lo = Math.min(...values, 0);
  const span = hi - lo || 1;
  const width = 640;
  const height = 220;
  const coords = values.map((value, index) => {
    const x = values.length === 1 ? width / 2 : (index / (values.length - 1)) * width;
    const y = height - 16 - ((value - lo) / span) * (height - 36);
    return [x, y] as const;
  });
  const line = coords.map(([x, y]) => `${x},${y}`).join(" ");
  return (
    <svg className="acct-spark" viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <polygon points={`${line} ${width},${height} 0,${height}`} fill={ICE} />
      <polyline points={line} stroke={ACCENT} />
    </svg>
  );
}

function Bars({ items }: { items: { label: string; value: number }[] }) {
  const max = Math.max(...items.map((item) => item.value), 1);
  return (
    <div className="acct-bars" aria-hidden="true">
      {items.map((item) => (
        <div key={item.label}>
          <i style={{ height: `${Math.max(8, (item.value / max) * 100)}%` }} />
          <small>{item.label}</small>
        </div>
      ))}
    </div>
  );
}

function Donut({
  items,
}: {
  items: { label: string; value: number; color: string }[];
}) {
  const count = items.reduce((sum, item) => sum + item.value, 0);
  const total = count || 1;
  let cursor = 0;
  const stops = items
    .map((item) => {
      const start = cursor;
      cursor += (item.value / total) * 100;
      return `${item.color} ${start}% ${cursor}%`;
    })
    .join(", ");
  return (
    <div className="acct-mix">
      <div className="acct-donut" style={{ background: `conic-gradient(${stops})` }}>
        <span>{count}</span>
      </div>
      <ul className="acct-legend">
        {items.map((item) => (
          <li key={item.label}>
            <i style={{ background: item.color }} />
            <span>{item.label}</span>
            <strong>{item.value}</strong>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Gauge({ value, limit }: { value: number; limit: number }) {
  const ratio = Math.max(0, Math.min(1, limit ? value / limit : 0));
  const radius = 70;
  const cx = 90;
  const cy = 86;
  const angle = Math.PI * ratio;
  const x = cx - radius * Math.cos(angle);
  const y = cy - radius * Math.sin(angle);
  const arc =
    ratio === 0
      ? ""
      : `M ${cx - radius} ${cy} A ${radius} ${radius} 0 ${ratio > 0.5 ? 1 : 0} 1 ${x} ${y}`;
  return (
    <svg className="acct-gauge" viewBox="0 0 180 118" aria-hidden="true">
      <path
        d={`M ${cx - radius} ${cy} A ${radius} ${radius} 0 1 1 ${cx + radius} ${cy}`}
        fill="none"
        stroke={ICE}
        strokeWidth="14"
        strokeLinecap="round"
      />
      {arc && (
        <path d={arc} fill="none" stroke={ACCENT} strokeWidth="14" strokeLinecap="round" />
      )}
      <text x={cx} y={cy - 6} textAnchor="middle">
        {value}s
      </text>
    </svg>
  );
}

function Rank({
  items,
}: {
  items: { label: string; value: number; color: string }[];
}) {
  const total = items.reduce((sum, item) => sum + item.value, 0) || 1;
  return (
    <ul className="acct-rank">
      {items.map((item) => (
        <li key={item.label}>
          <span>{item.label}</span>
          <b>{item.value}</b>
          <i>
            <em
              style={{
                width: `${Math.max(item.value ? 6 : 0, (item.value / total) * 100)}%`,
                background: item.color,
              }}
            />
          </i>
          <small>{Math.round((item.value / total) * 100)}%</small>
        </li>
      ))}
    </ul>
  );
}

const OPEN = new Set(["UPLOADED", "QUEUED", "PROCESSING"]);

function dayKey(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toISOString().slice(0, 10);
}

function percent(part: number, total: number) {
  if (!total) return 0;
  return Math.round((part / total) * 10) / 10;
}

export function AccountantBoard({
  docs,
  openExceptions,
  completed,
  processing,
}: {
  docs: Document[];
  openExceptions: number;
  completed: number;
  processing: number;
}) {
  const queued = docs.filter((doc) => ["QUEUED", "UPLOADED"].includes(doc.status)).length;
  const running = docs.filter((doc) => doc.status === "PROCESSING").length;
  const failed = docs.filter((doc) => doc.status === "FAILED").length;
  const ready = Math.max(0, docs.length - queued - running - failed);
  const finished = docs.filter((doc) => !OPEN.has(doc.status));
  const clean = finished.filter((doc) => doc.status !== "FAILED" && doc.status !== "EXCEPTION");
  const accuracy = percent(clean.length, finished.length);
  const exceptionRate = percent(openExceptions, docs.length);
  const reduction = percent(completed, docs.length);
  const averageSeconds = finished.length
    ? Math.round((finished.reduce((sum, doc) => sum + doc.seconds, 0) / finished.length) * 10) / 10
    : 0;
  const grouped = new Map<string, number>();
  for (const doc of docs) {
    const key = dayKey(doc.createdAt);
    if (!key) continue;
    grouped.set(key, (grouped.get(key) ?? 0) + 1);
  }
  const days = [...grouped.keys()].sort().slice(-7);
  const series = (days.length ? days : ["today"]).map((key) => ({
    label: key === "today" ? "Now" : key.slice(8),
    value: grouped.get(key) ?? 0,
  }));
  const mix = [
    { label: "Queue", value: queued, color: MIST },
    { label: "Running", value: running, color: ACCENT },
    { label: "Ready", value: ready, color: INK },
    { label: "Failed", value: failed, color: SLATE },
  ];

  return (
    <section className="acct-board" aria-label="Processing performance">
      <div className="acct-kpis">
        <article className="acct-kpi">
          <h3>Extraction accuracy</h3>
          <strong>{finished.length ? `${accuracy}%` : "—"}</strong>
          <p>Target ≥ 95%</p>
        </article>
        <article className="acct-kpi">
          <h3>Avg. processing time</h3>
          <strong>{finished.length ? `${averageSeconds}s` : "—"}</strong>
          <p>Target ≤ 20 sec</p>
        </article>
        <article className="acct-kpi">
          <h3>Exception rate</h3>
          <strong>{docs.length ? `${exceptionRate}%` : "—"}</strong>
          <p>{"Target < 10%"}</p>
        </article>
        <article className="acct-kpi">
          <h3>Manual entry reduction</h3>
          <strong>{docs.length ? `${reduction}%` : "—"}</strong>
          <p>Target ≥ 70%</p>
        </article>
      </div>

      <article className="acct-tile acct-trend">
        <header>
          <h3>Documents over time</h3>
          <p>{docs.length} in your workspace · updates on its own</p>
        </header>
        <Spark values={series.map((point) => point.value)} />
        <div className="acct-axis">
          {series.map((point) => (
            <span key={point.label}>{point.label}</span>
          ))}
        </div>
      </article>

      <article className="acct-tile acct-status">
        <header>
          <h3>Documents by status</h3>
          <p>
            {processing} running · {openExceptions} need review · {completed} completed
          </p>
        </header>
        <Rank items={mix} />
      </article>

      <article className="acct-tile acct-third">
        <header>
          <h3>By day</h3>
          <p>Latest days with documents</p>
        </header>
        <Bars items={series} />
      </article>

      <article className="acct-tile acct-third">
        <header>
          <h3>Status mix</h3>
          <p>Share of your documents</p>
        </header>
        <Donut items={mix} />
      </article>

      <article className="acct-tile acct-third">
        <header>
          <h3>Processing time</h3>
          <p>Average against the 20 second target</p>
        </header>
        <Gauge value={averageSeconds} limit={20} />
        <p className="acct-gauge-note">
          {averageSeconds}s of 20s
        </p>
      </article>
    </section>
  );
}
