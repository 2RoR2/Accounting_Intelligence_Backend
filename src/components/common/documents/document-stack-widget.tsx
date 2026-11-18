import Link from "next/link";
import type { CSSProperties } from "react";

type StackItem = { label: "Invoices" | "Bills" | "Receipts"; count: number; href: string; tone: "blue" | "green" | "red" };

export function DocumentStackWidget({ items }: { items: StackItem[] }) {
  return (
    <div className="document-stack-widget" aria-label="Document type summary">
      <div className="document-stack-scene">
        {items.map((item, index) => (
          <Link
            className={`document-stack document-stack-${item.tone}`}
            href={item.href}
            key={item.label}
            style={{ "--stack-index": index } as CSSProperties}
            aria-label={`${item.label}: ${item.count} documents. Open filtered list.`}
          >
            <span className="document-stack-papers" aria-hidden="true"><i /><i /><i /></span>
            <span className="document-stack-label">{item.label}</span>
            <strong>{item.count}</strong>
            <span className="document-stack-action">Open list <span aria-hidden="true">→</span></span>
          </Link>
        ))}
      </div>
      <p className="document-stack-hint">Select a stack to filter documents.</p>
    </div>
  );
}
