"use client";
import { useCallback, useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import type { AiAssistResultField, AiAssistState, SourceEvidence, User } from "@/types";
import { aiAssistCanApply, aiAssistOutcomeLabel, modelIdentity } from "@/lib/api/document-contract";
import { decideAiAssist, getAiAssist, requestAiAssist, updateExtractedField } from "@/lib/api/document.service";

/**
 * Second pass by the local model. Everything here is a suggestion: a value is
 * only stored when the reviewer accepts it, through the normal correction route.
 */
export function AiAssistPanel({ user, documentId, contentRevision, readonly, labelFor, onShowSource, onSaved }: {
  user: User; documentId: string; contentRevision?: number; readonly: boolean;
  labelFor: (storedKey: string) => string;
  onShowSource: (label: string, evidence: SourceEvidence[]) => void;
  onSaved: () => Promise<void> | void;
}) {
  const [state, setState] = useState<AiAssistState | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [choice, setChoice] = useState("");
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState("");

  const load = useCallback(async () => {
    try { setState(await getAiAssist(user, documentId)); } catch { setState(null); }
  }, [user, documentId]);
  useEffect(() => { void load(); }, [load]);

  const latest = state?.latest;
  const decided = (field: string) => (state?.decisions ?? []).filter(item => item.attempt_id === latest?.attempt_id && item.field === field).at(-1)?.decision;
  const valueOf = (row: AiAssistResultField) => edits[row.field] ?? row.candidate ?? "";

  async function examine() {
    setRunning(true); setError(""); setNotice(""); setEdits({});
    try {
      await requestAiAssist(user, documentId, choice ? [choice] : undefined);
      await load();
    } catch (e) { setError((e as Error).message); } finally { setRunning(false); }
  }
  async function decide(row: AiAssistResultField, accept: boolean) {
    setSaving(row.field); setError(""); setNotice("");
    try {
      if (accept) {
        await updateExtractedField(user, documentId, row.stored_key, valueOf(row).trim(), contentRevision!);
        setNotice(`${labelFor(row.stored_key)} saved as your correction. Press Save & revalidate to re-run validation.`);
      }
      await decideAiAssist(user, documentId, { field: row.field, decision: accept ? "accepted" : "rejected", attemptId: latest?.attempt_id });
      await load();
      if (accept) await onSaved();
    } catch (e) { setError((e as Error).message); } finally { setSaving(""); }
  }

  if (!state) return null;
  return <section className="ai-assist" aria-label="AI Assist">
    <div className="ai-assist-head">
      <strong><Sparkles size={15} aria-hidden="true" /> AI Assist (Qwen)</strong>
      {!readonly && state.available && <div className="ai-assist-controls">
        <select aria-label="Field to examine" value={choice} disabled={running} onChange={e => setChoice(e.target.value)}>
          <option value="">Unresolved fields</option>
          {state.supportedFields.map(item => <option key={item.field} value={item.field}>Second opinion: {labelFor(item.storedKey)}</option>)}
        </select>
        <button type="button" className="btn small" disabled={running || !!saving} onClick={examine}>{running ? "Examining…" : "Examine with AI"}</button>
      </div>}
    </div>
    {!state.available && <p>AI Assist is off on this installation. Continue reviewing manually.</p>}
    {running && <p role="status">The local model is reading this page. Nothing leaves this machine.</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {latest && !running && <>
      {latest.message && <p role="status">{latest.message}</p>}
      {latest.model && <small>{modelIdentity(latest)}. Suggestions only; no value changes until you accept it.</small>}
      <ul className="ai-assist-results">{latest.fields.map(row => {
        const outcome = decided(row.field);
        return <li key={row.field} data-outcome={row.outcome}>
          <div><strong>{labelFor(row.stored_key)}</strong> <span className="ai-assist-outcome">{aiAssistOutcomeLabel(row.outcome)}</span></div>
          {row.candidate != null && <p>Model read: <b>{row.candidate}</b>{row.current_value ?? row.parser_value ? ` (current value: ${row.current_value ?? row.parser_value})` : ""}</p>}
          <p>{row.explanation}</p>
          <small>Asked because: {row.request_reason}</small>
          {!!row.evidence?.length && <button type="button" className="text-link" onClick={() => onShowSource(labelFor(row.stored_key), row.evidence!)}>View source</button>}
          {outcome && <p role="status">{outcome === "accepted" ? "Accepted by reviewer." : "Rejected by reviewer."}</p>}
          {!readonly && !outcome && aiAssistCanApply(row) && <div className="ai-assist-actions">
            <label>Value to save<input value={valueOf(row)} onChange={e => setEdits(current => ({ ...current, [row.field]: e.target.value }))} /></label>
            <button type="button" className="btn small" disabled={saving === row.field || !valueOf(row).trim()} onClick={() => decide(row, true)}>Accept and save</button>
            <button type="button" className="btn small" disabled={saving === row.field} onClick={() => decide(row, false)}>Reject</button>
          </div>}
        </li>;
      })}</ul>
    </>}
  </section>;
}
