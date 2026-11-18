"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Check, ChevronDown } from "lucide-react";
import { Panel } from "@/components/common/ui";
import type { KPI } from "@/types";
import { getTrends } from "@/lib/api/processing.service";
import { useResource } from "@/features/common/hooks/use-resource";
import { ErrorState, LoadingState } from "@/components/common/ui";
export function PerformanceCards({ metrics }: { metrics: KPI[] }) {
  return (
    <div className="kpi-grid">
      {metrics.map((m) => (
        <div className="kpi-card" key={m.label}>
          <div className="kpi-label">{m.label}</div>
          <div className="kpi-value">
            {m.value}
            <span>{m.change}</span>
          </div>
          {m.target && (
            <div className="kpi-target">
              <span>Target {m.target}</span>
              <strong>
                {m.met ? <><Check size={12} /> Target met</> : "Target not met"}
              </strong>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
export function TrendChart({
  kind = "processed",
}: {
  kind?: "processed" | "accuracy" | "exceptions";
}) {
  const router = useRouter();
  const [period, setPeriod] = useState("week");
  const { data, error, loading, refresh } = useResource(
    () => getTrends(period),
    period,
  );
  const title =
    kind === "processed"
      ? "Document processing"
      : kind === "accuracy"
        ? "Job success rate"
        : "Exception trend";
  if (loading)
    return (
      <Panel title={title}>
        <LoadingState variant="chart" />
      </Panel>
    );
  if (error || !data)
    return (
      <Panel title={title}>
        <ErrorState message={error} retry={refresh} />
      </Panel>
    );
  if (!data.length)
    return (
      <Panel title={title} subtitle="Processing trend">
        <div className="chart-empty-state">
          <strong>No trend data yet</strong>
          <span>Upload documents to start building this view.</span>
        </div>
      </Panel>
    );
  const vals = data.map((point) => point[kind]);
  const peak = Math.max(0, ...vals);
  const maximum =
    kind === "accuracy"
      ? 100
      : kind === "processed"
        ? Math.max(50, Math.ceil(peak / 50) * 50)
        : Math.max(5, Math.ceil(peak / 5) * 5);
  const minimum = 0;
  const unit = kind === "accuracy" ? "%" : "";
  const plotLeft = 78;
  const plotRight = 548;
  const plotTop = 30;
  const plotBottom = 170;
  const xFor = (index: number) =>
    vals.length === 1
      ? (plotLeft + plotRight) / 2
      : plotLeft + index * ((plotRight - plotLeft) / (vals.length - 1));
  const yFor = (value: number) =>
    plotBottom -
    ((value - minimum) / (maximum - minimum)) * (plotBottom - plotTop);
  const points = vals.map((v, i) => `${xFor(i)},${yFor(v)}`).join(" ");
  return (
    <Panel
      title={title}
      subtitle="Processing trend"
      action={
        <div className="chart-period">
          <select
            aria-label={`${title} period`}
            value={period}
            onChange={(e) => setPeriod(e.target.value)}
          >
            <option value="day">Per day</option>
            <option value="week">Per week</option>
            <option value="month">Per month</option>
            <option value="year">Per year</option>
          </select>
          <ChevronDown size={12} />
        </div>
      }
    >
      <span className="chart-style-badge">LUPI BASICS · HAIRLINE LINE</span>
      <div className="chart-key">
        <span className="chart-dot" />{" "}
        {kind === "processed"
          ? "Documents processed"
          : kind === "accuracy"
            ? "Job success (%)"
            : "Exceptions"}
      </div>
      <div className="trend-chart">
        <svg
          viewBox="0 0 590 215"
          role="img"
          aria-label={`${title} ${period} trend`}
        >
          <defs>
            <linearGradient id={`fill-${kind}`} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor="var(--color-0)" stopOpacity="0.16" />
              <stop offset="100%" stopColor="var(--color-0)" stopOpacity="0" />
            </linearGradient>
          </defs>
          {[30, 76.67, 123.33, 170].map((y, i) => (
            <g key={y}>
              <line
                x1={plotLeft}
                x2={plotRight}
                y1={y}
                y2={y}
                stroke="var(--chart-grid)"
                strokeDasharray="4 4"
              />
              <text x="8" y={y + 4} fill="var(--chart-muted)" fontSize="10">
                {Math.round(maximum - i * ((maximum - minimum) / 3))}
                {unit}
              </text>
            </g>
          ))}
          <polygon
            points={`${plotLeft},${plotBottom + 6} ${points} ${plotRight},${plotBottom + 6}`}
            fill={`url(#fill-${kind})`}
          />
          <polyline
            points={points}
            fill="none"
            stroke="var(--color-0)"
            strokeWidth="2.5"
            strokeLinejoin="round"
          />
          {vals.map((v, i) => (
            <circle
              key={i}
              cx={xFor(i)}
              cy={yFor(v)}
              r="3.5"
              fill="white"
              stroke="var(--color-0)"
              strokeWidth="2"
              tabIndex={0}
              role="button"
              aria-label={`Open ${data[i].day}: ${v}${unit ? unit : kind === "processed" ? " documents" : ""}`}
              onClick={() => router.push(kind === "exceptions" ? "/workspace/exceptions" : kind === "accuracy" ? "/workspace/records" : "/workspace/documents")}
              onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") router.push(kind === "exceptions" ? "/workspace/exceptions" : kind === "accuracy" ? "/workspace/records" : "/workspace/documents"); }}
            >
              <title>{`${data[i].day}: ${v}${unit ? unit : kind === "processed" ? " documents" : ""}`}</title>
            </circle>
          ))}
          {data.map(({ day: label }, i) => (
            <text
              key={label}
              x={xFor(i)}
              y="204"
              textAnchor="middle"
              fill="var(--chart-muted)"
              fontSize="10"
            >
              {label}
            </text>
          ))}
        </svg>
      </div>
      <div className="chart-source">LIVE WORKSPACE DATA · {period.toUpperCase()}</div>
    </Panel>
  );
}
