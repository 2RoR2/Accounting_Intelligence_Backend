"use client";
import {
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  ArrowDownToLine,
  Check,
  ChevronLeft,
  ChevronRight,
  Search,
  X,
} from "lucide-react";
export function Badge({ children }: { children: ReactNode }) {
  const value = String(children);
  const tone =
    /ACTIVE|PASS|COMPLETED|APPROVED|VALIDATED|RESOLVED|HIGH CONFIDENCE/.test(
      value,
    )
      ? "green"
      : /FAIL|REJECT|SUSPEND|DISABLED|HIGH$/.test(value)
        ? "red"
        : /PENDING|INVITED|EXCEPTION|LOW|WARNING|REVIEW|CORRECTED/.test(value)
          ? "amber"
          : "neutral";
  return (
    <span className={`badge ${tone}`}>
      <span className="badge-dot" />
      {value.replaceAll("_", " ")}
    </span>
  );
}
export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      <div className="actions">{actions}</div>
    </div>
  );
}
export function Panel({
  title,
  subtitle,
  action,
  children,
  className = "",
}: {
  title?: ReactNode;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {title && (
        <div className="panel-heading">
          <div>
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}
export function StatCard({
  label,
  value,
  detail,
  icon,
  tone,
  variant,
}: {
  label: string;
  value: ReactNode;
  detail?: string;
  icon?: ReactNode;
  tone?: "invoice" | "bill" | "receipt" | "note" | "processing" | "review" | "exception" | "failed";
  variant?: "solid";
}) {
  return (
    <div className={`stat-card${tone ? ` stat-card-${tone}` : ""}${variant ? ` stat-card-${variant}` : ""}`}>
      <div className="stat-top">
        <span>{label}</span>
        {icon && <span className="stat-icon">{icon}</span>}
      </div>
      <div className="stat-value">{value}</div>
      <div className="stat-foot">
        <span>{detail ?? "in this workspace"}</span>
      </div>
    </div>
  );
}
export function EmptyState({
  message = "No results match your filters.",
  action,
}: {
  message?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <Search size={28} />
      <h3>Nothing here yet</h3>
      <p>{message}</p>
      {action}
    </div>
  );
}
const skeletonRevealStyles = `
.t-skel{position:relative}.t-skel-skeleton,.t-skel-content{position:absolute;inset:0}.t-skel-skeleton{z-index:1;opacity:1;filter:blur(0);transition:opacity 400ms ease-in-out,filter 400ms ease-in-out}.t-skel-content{z-index:2;opacity:0;filter:blur(2px);transition:opacity 400ms ease-in-out,filter 400ms ease-in-out}.t-skel.is-revealed .t-skel-skeleton{opacity:0;filter:blur(2px)}.t-skel.is-revealed .t-skel-content{opacity:1;filter:blur(0)}.t-skel.is-resetting>*{transition:none!important}.t-skel-skeleton.is-pulsing>*{animation:t-skel-pulse 1000ms ease-in-out 1}@keyframes t-skel-pulse{0%,100%{opacity:1}50%{opacity:.5}}@media(prefers-reduced-motion:reduce){.t-skel-skeleton,.t-skel-content{transition:none!important}.t-skel-skeleton.is-pulsing>*{animation:none!important}}`;

export function SkeletonReveal({ skeleton, children, loading = true, replayKey }: { skeleton: ReactNode; children: ReactNode; loading?: boolean; replayKey?: string | number }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [revealed, setRevealed] = useState(!loading);
  useEffect(() => {
    if (!document.getElementById("accordai-skeleton-reveal")) {
      const style = document.createElement("style"); style.id = "accordai-skeleton-reveal"; style.textContent = skeletonRevealStyles; document.head.appendChild(style);
    }
  }, []);
  useEffect(() => {
    if (!loading) { setRevealed(true); return; }
    setRevealed(false);
    const timer = window.setTimeout(() => setRevealed(true), 1000);
    return () => window.clearTimeout(timer);
  }, [loading, replayKey]);
  return <div ref={wrapRef} className={`t-skel${revealed ? " is-revealed" : ""}`} style={{ minHeight: "inherit" }}><div className="t-skel-skeleton is-pulsing" aria-hidden="true">{skeleton}</div><div className="t-skel-content">{children}</div></div>;
}

export function SpinningCounter({ value, prefix = "RM " }: { value: number; prefix?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const root = ref.current;
    const formatted = Math.max(0, value).toLocaleString("en-MY", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    root.textContent = "";
    const strips: Array<{ strip: HTMLSpanElement; digit: number }> = [];
    [...formatted].forEach((character) => {
      if (!/\d/.test(character)) { const separator = document.createElement("span"); separator.className = "t-reel-sep"; separator.textContent = character; root.appendChild(separator); return; }
      const column = document.createElement("span"); column.className = "t-reel-col";
      const strip = document.createElement("span"); strip.className = "t-reel-strip";
      for (let index = 0; index < 41; index += 1) { const cell = document.createElement("span"); cell.className = "t-reel-digit"; cell.textContent = String(index % 10); strip.appendChild(cell); }
      column.appendChild(strip); root.appendChild(column); strips.push({ strip, digit: Number(character) });
    });
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    strips.forEach(({ strip, digit }, index) => { strip.style.transition = reduce ? "none" : `transform 1400ms cubic-bezier(0.16,1,0.3,1) ${index * 90}ms`; strip.style.transform = `translateY(-${(reduce ? digit : 30 + digit) * 30}px)`; });
  }, [value]);
  return <span className="amount-counter" aria-label={`${prefix}${value.toFixed(2)}`}><span className="amount-counter-prefix">{prefix}</span><span ref={ref} className="t-reel" /></span>;
}

export function LoadingState({ variant = "default" }: { variant?: "default" | "dashboard" | "table" | "form" | "chart" | "review" }) {
  void variant;
  const variantContent = <div className="loading-table-skeleton" aria-hidden="true">{[1, 2, 3, 4, 5, 6].map((row) => <div className="loading-skeleton-table-row" key={row}><i /><i /><i /><i /></div>)}</div>;
  return (
    <div className="loading-state" role="status" aria-live="polite" aria-busy="true">
      {variantContent}
      {variant !== "dashboard" && <><strong>Accord AI is preparing your workspace</strong><span>Loading your accounting workflow…</span></>}
    </div>
  );
}
export function ProcessingStages({ current = "queue" }: { current?: "queue" | "extract" | "validate" | "complete" }) {
  const stages = [
    ["queue", "Queued", "Waiting for the processing worker"],
    ["extract", "AI extraction", "Reading fields with the document model"],
    ["validate", "Validation", "Checking totals and required fields"],
    ["complete", "Ready", "Your document is ready to review"],
  ] as const;
  const active = stages.findIndex(([key]) => key === current);
  return <div className="processing-stages" role="status" aria-live="polite" aria-busy={current !== "complete"}>
    {stages.map(([key, title, detail], index) => <div className={`processing-stage${index <= active ? " is-active" : ""}${index === active ? " is-current" : ""}`} key={key}>
      <span className="processing-stage-dot">{index < active ? "✓" : index + 1}</span>
      <span><strong>{title}</strong><small>{index === active ? detail : ""}</small></span>
    </div>)}
  </div>;
}
export function ErrorState({
  message,
  retry,
  dismissible = false,
  onClose,
}: {
  message: string;
  retry?: () => void;
  dismissible?: boolean;
  onClose?: () => void;
}) {
  if (dismissible) return <div className="error-popup-backdrop" role="presentation">
    <div role="alertdialog" aria-modal="true" aria-labelledby="error-popup-title" className="error-box error-popup">
      <button type="button" className="error-popup-close" aria-label="Close error" onClick={onClose}>×</button>
      <strong id="error-popup-title">We couldn’t complete that request.</strong>
      <p>{message}</p>
      {retry && <button className="btn" onClick={retry}>Try again</button>}
    </div>
  </div>;
  return (
    <div role="alert" className="error-box">
      <strong>We couldn’t complete that request.</strong>
      <p>{message}</p>
      {retry && (
        <button className="btn" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  );
}
export function SearchInput({
  value,
  onChange,
  placeholder = "Search…",
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <div className="search">
      <Search size={17} />
      <input
        aria-label={placeholder}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}
export function Filter({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  return (
    <select
      aria-label={label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">All {label.toLowerCase()}</option>
      {options.map((o) => (
        <option key={o} value={o}>
          {o.replaceAll("_", " ")}
        </option>
      ))}
    </select>
  );
}
export interface Column<T> {
  label: string;
  render: (row: T) => ReactNode;
}
export function DataTable<T>({
  columns,
  rows,
  rowKey,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
}) {
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(rows.length / 8));
  const current = Math.min(page, pages - 1);
  return (
    <>
      <div
        className="table-scroll"
        role="region"
        aria-label="Data table"
        tabIndex={0}
      >
        <table>
          <thead>
            <tr>
              {columns.map((c) => (
                <th
                  scope="col"
                  className={
                    /^Actions?$/.test(c.label) ? "table-action-cell" : undefined
                  }
                  key={c.label}
                >
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(current * 8, current * 8 + 8).map((row) => (
              <tr key={rowKey(row)}>
                {columns.map((c) => (
                  <td
                    className={
                      /^Actions?$/.test(c.label)
                        ? "table-action-cell"
                        : undefined
                    }
                    key={c.label}
                  >
                    {c.render(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {!rows.length && <EmptyState />}
      </div>
      <div className="pagination">
        <span>
          {rows.length
            ? `${current * 8 + 1}–${Math.min(current * 8 + 8, rows.length)}`
            : "0"}{" "}
          of {rows.length} results
        </span>
        <div>
          <button
            aria-label="Previous page"
            disabled={current === 0}
            onClick={() => setPage(current - 1)}
          >
            <ChevronLeft size={16} />
          </button>
          <span>
            Page {current + 1} of {pages}
          </span>
          <button
            aria-label="Next page"
            disabled={current === pages - 1}
            onClick={() => setPage(current + 1)}
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </div>
    </>
  );
}
export function Modal({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      onCancel={onClose}
      onClick={(e) => {
        if (e.target === ref.current) onClose();
      }}
      aria-label={title}
    >
      <div className="modal-heading">
        <h2>{title}</h2>
        <button
          className="icon-btn"
          aria-label="Close dialog"
          onClick={onClose}
        >
          <X size={20} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  const fieldId = useId();
  const required = label.trimEnd().endsWith("*");
  const labelText = required ? label.trimEnd().slice(0, -1).trimEnd() : label;
  return (
    <div className="field">
      <label htmlFor={fieldId}>
        {labelText}
        {required && (
          <span className="field-required" aria-hidden="true">
            {" "}
            *
          </span>
        )}
      </label>
      {isValidElement<{ id?: string; "aria-describedby"?: string }>(children)
        ? cloneElement(children, {
            id: fieldId,
            "aria-describedby": hint ? `${fieldId}-hint` : undefined,
          })
        : children}
      {hint && <small id={`${fieldId}-hint`}>{hint}</small>}
    </div>
  );
}
export function AccountUsage({ used, limit }: { used: number; limit: number }) {
  return (
    <div className="account-usage">
      <div>
        <strong>
          {used} / {limit}
        </strong>
        <span> accounts used / reserved</span>
      </div>
      <div className="progress-track">
        <div style={{ width: `${Math.min(100, (used / limit) * 100)}%` }} />
      </div>
      <small>
        {Math.max(0, limit - used)} available · Pending invitations reserve a
        seat
      </small>
    </div>
  );
}
export function Toast({ message }: { message: string }) {
  return message ? (
    <div className="toast" role="status">
      <Check size={18} />
      {message}
    </div>
  ) : null;
}
export const ExportIcon = () => <ArrowDownToLine size={16} />;
export const formatDate = (date?: string | null) => {
  if (!date || date === "—" || date === "â€”") return "—";
  const parsed = new Date(date);
  if (Number.isNaN(parsed.getTime())) return "—";
  return new Intl.DateTimeFormat("en-MY", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(parsed);
};
export const money = (value: string | number) =>
  new Intl.NumberFormat("en-MY", { style: "currency", currency: "MYR" }).format(
    Number(value),
  );
