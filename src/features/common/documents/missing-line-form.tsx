"use client";
import { useState } from "react";
import type { MissingLineInput } from "@/lib/api/document.service";

export function MissingLineForm({ revision, disabled, onSave }: { revision: number; disabled: boolean; onSave: (input: MissingLineInput) => Promise<void> }) {
  const [values, setValues] = useState({ description: "", quantity: "", unitPrice: "", amount: "", sourcePage: "1", sourceNote: "" });
  const [error, setError] = useState("");
  return <details className="shoebox-lines"><summary>Add a missing printed item</summary>
    <p>Read the original document and enter one printed row. Leave quantity or unit price empty if it is unprinted or includes a line discount. Explain any discount in the source note. Every addition is audited and revalidated.</p>
    <form onSubmit={async event => {
      event.preventDefault(); setError("");
      try {
        await onSave({ ...values, quantity: values.quantity.trim() || null, unitPrice: values.unitPrice.trim() || null, sourcePage: Number(values.sourcePage), expectedContentRevision: revision });
        setValues({ description: "", quantity: "", unitPrice: "", amount: "", sourcePage: "1", sourceNote: "" });
      } catch (e) { setError((e as Error).message); }
    }}>
      <div className="shoebox-fields">{([
        ["description", "Printed description"], ["quantity", "Printed quantity (optional)"], ["unitPrice", "Printed unit price (optional)"],
        ["amount", "Printed line amount"], ["sourcePage", "Source page"], ["sourceNote", "Source note"],
      ] as const).map(([key, label]) => <label key={key}>{label}<input aria-label={label} value={values[key]} disabled={disabled}
        required={!["quantity", "unitPrice"].includes(key)} minLength={key === "sourceNote" ? 10 : undefined}
        type={key === "sourcePage" ? "number" : "text"} min={key === "sourcePage" ? 1 : undefined}
        onChange={event => setValues(previous => ({ ...previous, [key]: event.target.value }))} /></label>)}</div>
      {error && <p role="alert">{error}</p>}
      <button className="btn" type="submit" disabled={disabled}>Save item &amp; revalidate</button>
    </form>
  </details>;
}
