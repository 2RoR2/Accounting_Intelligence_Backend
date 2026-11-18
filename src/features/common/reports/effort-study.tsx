"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Badge, DataTable, PageHeader, Panel } from "@/components/common/ui";
import { get, post } from "@/lib/api/client";

interface StudyTask { sampleFile: string; kind: string; mode: "manual" | "ai_assisted"; set: string; file: string; fields: Array<{ key: string; type: string }>; prefill: Record<string, string | null> | null }
interface StudyPlan { participant: string; aiRun: string | null; tasks: StudyTask[]; lineItems: string }
interface SessionRow { participant: string; mode: string; order: number; sampleFile: string; kind: string; activeSeconds: number; idleSeconds: number; pausedSeconds: number;
  fieldsTotal: number; fieldsTyped: number; aiAccepted: number; aiCorrected: number; correct: number; quality: number | null; recordedAt?: string }
interface Paired { participant: string; manualSeconds: number; assistedSeconds: number; manualFirst: boolean; manualQuality: number; assistedQuality: number; reduction: number | null }
interface Results { sessions: number; participants: number; paired: Paired[]; reduction: number | null; valid: boolean; status: string; rule: string; rows: SessionRow[] }

const IDLE_AFTER_SECONDS = 30;
const label = (key: string) => key.replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
const modeLabel = (mode: string) => mode === "manual" ? "Manual entry" : "AI-assisted";

/** Active, idle and paused seconds, counted from real interaction; nothing is estimated. */
function useActiveTimer(running: boolean, paused: boolean) {
  const [time, setTime] = useState({ active: 0, idle: 0, paused: 0 });
  const lastInput = useRef(Date.now());
  useEffect(() => {
    const touch = () => { lastInput.current = Date.now(); };
    const events = ["keydown", "input", "pointerdown", "wheel", "scroll"];
    events.forEach(name => window.addEventListener(name, touch, { passive: true, capture: true }));
    return () => events.forEach(name => window.removeEventListener(name, touch, { capture: true }));
  }, []);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setTime(t => {
      if (paused) return { ...t, paused: t.paused + 1 };
      const idle = document.visibilityState !== "visible" || (Date.now() - lastInput.current) / 1000 > IDLE_AFTER_SECONDS;
      return idle ? { ...t, idle: t.idle + 1 } : { ...t, active: t.active + 1 };
    }), 1000);
    return () => window.clearInterval(timer);
  }, [running, paused]);
  const reset = useCallback(() => { lastInput.current = Date.now(); setTime({ active: 0, idle: 0, paused: 0 }); }, []);
  return { time, reset };
}

function StudyResults({ results, onExport }: { results?: Results; onExport: () => void }) {
  if (!results) return null;
  return <Panel title="Measured results" subtitle={results.rule}>
    <div className="panel-body">
      <p role="status"><Badge>{results.status === "NOT MEASURED" ? "Not yet measured" : results.status === "MET" ? "Met" : "Not met"}</Badge>{" "}
        {results.valid && results.reduction != null ? `Measured reduction ${results.reduction}% across ${results.paired.length} paired participants.`
          : `${results.sessions} task${results.sessions === 1 ? "" : "s"} recorded from ${results.participants} participant${results.participants === 1 ? "" : "s"}; ${results.paired.length} completed both modes. No reduction is reported until the rule above is satisfied.`}</p>
      <button type="button" className="btn small" onClick={onExport} disabled={!results.sessions}>Download CSV</button>
    </div>
    {results.paired.length > 0 && <DataTable rows={results.paired} rowKey={r => r.participant} columns={[
      { label: "Participant", render: r => r.participant }, { label: "Order", render: r => r.manualFirst ? "Manual first" : "AI-assisted first" },
      { label: "Manual active (s)", render: r => r.manualSeconds }, { label: "AI-assisted active (s)", render: r => r.assistedSeconds },
      { label: "Reduction", render: r => r.reduction == null ? "—" : `${r.reduction}%` },
      { label: "Accuracy manual / AI", render: r => `${Math.round(r.manualQuality * 100)}% / ${Math.round(r.assistedQuality * 100)}%` },
    ]} />}
    {results.rows.length > 0 && <DataTable rows={results.rows} rowKey={r => `${r.participant}-${r.sampleFile}-${r.recordedAt}`} columns={[
      { label: "Participant", render: r => r.participant }, { label: "Mode", render: r => modeLabel(r.mode) }, { label: "Document", render: r => `${r.sampleFile} (${r.kind})` },
      { label: "Active / idle / paused (s)", render: r => `${r.activeSeconds} / ${r.idleSeconds} / ${r.pausedSeconds}` },
      { label: "Typed", render: r => `${r.fieldsTyped} of ${r.fieldsTotal}` }, { label: "AI accepted / corrected", render: r => r.mode === "manual" ? "—" : `${r.aiAccepted} / ${r.aiCorrected}` },
      { label: "Correct fields", render: r => `${r.correct} of ${r.fieldsTotal}` },
    ]} />}
  </Panel>;
}

export function EffortStudyPage() {
  // Workspace evaluation lives in Summary; the company area keeps its own evaluation page.
  const evaluationHref = (usePathname() ?? "/workspace").startsWith("/company") ? "/company/evaluation" : "/workspace/reports";
  const [participant, setParticipant] = useState("");
  const [plan, setPlan] = useState<StudyPlan>();
  const [index, setIndex] = useState(0);
  const [values, setValues] = useState<Record<string, string>>({});
  const [edited, setEdited] = useState<string[]>([]);
  const [paused, setPaused] = useState(false);
  const [startedAt, setStartedAt] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<Results>();
  const task = plan?.tasks[index];
  const { time, reset } = useActiveTimer(!!task, paused);

  const loadResults = useCallback(() => { get<Results>("/api/effort-study/results").then(setResults).catch(() => undefined); }, []);
  useEffect(() => { loadResults(); }, [loadResults]);
  useEffect(() => {
    if (!task) return;
    const initial: Record<string, string> = {};
    for (const field of task.fields) initial[field.key] = task.mode === "ai_assisted" ? (task.prefill?.[field.key] ?? "") : "";
    setValues(initial); setEdited([]); setPaused(false); setStartedAt(new Date().toISOString()); reset();
  }, [task, reset]);

  async function start() {
    setMessage(""); setBusy(true);
    try { setPlan(await get<StudyPlan>(`/api/effort-study/tasks?participant=${encodeURIComponent(participant.trim())}`)); setIndex(0); }
    catch (e) { setMessage((e as Error).message); }
    finally { setBusy(false); }
  }

  async function submit() {
    if (!plan || !task) return;
    setBusy(true); setMessage("");
    try {
      const saved = await post<SessionRow>("/api/effort-study/sessions", { participant: plan.participant, sampleFile: task.sampleFile, mode: task.mode,
        activeSeconds: time.active, idleSeconds: time.idle, pausedSeconds: time.paused, values, editedKeys: edited, startedAt, completedAt: new Date().toISOString() });
      setMessage(`Saved: ${saved.correct} of ${saved.fieldsTotal} fields match the published labels; ${saved.activeSeconds} s active.`);
      loadResults();
      if (index + 1 < plan.tasks.length) setIndex(index + 1); else setPlan(undefined);
    } catch (e) { setMessage((e as Error).message); }
    finally { setBusy(false); }
  }

  async function exportCsv() {
    const response = await fetch("/api/effort-study/results.csv", { credentials: "include", headers: { "X-Requested-With": "AccountingIntelligence" } });
    if (!response.ok) { setMessage("The CSV could not be downloaded."); return; }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a"); link.href = url; link.download = "accordai-effort-study.csv"; link.click(); URL.revokeObjectURL(url);
  }

  const change = (key: string, value: string) => { setValues(v => ({ ...v, [key]: value })); setEdited(list => list.includes(key) ? list : [...list, key]); };

  return <>
    <PageHeader title="Manual effort study (SC2)" description="Timed human tasks on public benchmark documents: the same work typed manually or checked with AccordAI's extraction." />
    <p><Link href={evaluationHref} className="text-link">Back to the summary</Link></p>
    {!plan && <Panel title="Start a session" subtitle="Each participant types one set of documents manually and checks the other set with AccordAI's extraction. Odd participant numbers start with manual entry, even numbers with AI assistance, so learning effects balance out.">
      <div className="panel-body effort-start">
        <label htmlFor="effort-participant">Participant code</label>
        <input id="effort-participant" value={participant} placeholder="P01" onChange={e => setParticipant(e.target.value)} maxLength={40} />
        <button type="button" className="btn primary" disabled={busy || !participant.trim()} onClick={start}>Start</button>
        <small>Use a code, not a name. Timing counts only while you interact: more than {IDLE_AFTER_SECONDS} s without input, or a hidden tab, counts as idle. Line items are not part of this study version.</small>
      </div>
    </Panel>}
    {message && <p role="status" className="effort-message">{message}</p>}
    {plan && task && <section className="effort-task" aria-label="Study task">
      <header className="effort-task-head">
        <div><strong>{modeLabel(task.mode)}</strong> · Task {index + 1} of {plan.tasks.length} · {task.kind} {task.sampleFile}</div>
        <div className="effort-timer" aria-live="polite">Active {time.active} s · Idle {time.idle} s · Paused {time.paused} s{" "}
          <button type="button" className="btn small" onClick={() => setPaused(p => !p)}>{paused ? "Resume" : "Pause"}</button></div>
      </header>
      <div className="effort-task-body">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <div className="effort-source"><img src={task.file} alt={`Original document ${task.sampleFile}`} /></div>
        <form className="effort-fields" onSubmit={e => { e.preventDefault(); void submit(); }}>
          <p>{task.mode === "manual" ? "Type each value exactly as printed. No AI values are shown in this mode."
            : `AccordAI's extraction (run ${plan.aiRun}) is filled in. Check each value against the document and correct or complete it.`}</p>
          {task.fields.map(field => <label key={field.key} className="effort-field">
            <span>{label(field.key)}{field.type === "date" ? " (YYYY-MM-DD)" : field.type === "decimal" ? " (amount, e.g. 12.50)" : ""}</span>
            {/address/.test(field.key)
              ? <textarea rows={2} value={values[field.key] ?? ""} disabled={paused} onChange={e => change(field.key, e.target.value)} />
              : <input value={values[field.key] ?? ""} disabled={paused} onChange={e => change(field.key, e.target.value)} />}
          </label>)}
          <button type="submit" className="btn primary" disabled={busy || paused || task.fields.some(f => !(values[f.key] ?? "").trim())}>Submit task</button>
        </form>
      </div>
    </section>}
    <StudyResults results={results} onExport={exportCsv} />
  </>;
}
