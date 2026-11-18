"use client";
import { useState, type FormEvent } from "react";
import {
  BookOpen,
  CheckCircle2,
  ClipboardCheck,
  FileCheck2,
  Headphones,
  LifeBuoy,
  PhoneCall,
  ScanLine,
  ShieldCheck,
  Upload,
} from "lucide-react";
import { Badge, Field, ErrorState } from "@/components/common/ui";

const TUTORIAL_STEPS = [
  {
    icon: Upload,
    title: "Upload source documents",
    detail:
      "Add invoices or supporting files from Upload. Supported formats appear in the workspace checklist.",
  },
  {
    icon: ScanLine,
    title: "Review AI extraction",
    detail:
      "Open Documents to inspect extracted fields, confidence signals, and any items flagged for attention.",
  },
  {
    icon: ShieldCheck,
    title: "Resolve exceptions",
    detail:
      "Use Exceptions to correct mismatches, assign ownership, and keep every change audit-ready.",
  },
  {
    icon: FileCheck2,
    title: "Confirm standardised records",
    detail:
      "Validated entries land in Standardised Records, ready for reporting and downstream accounting use.",
  },
  {
    icon: ClipboardCheck,
    title: "Govern access and approvals",
    detail:
      "Company admins manage users and rules. Platform admins review company registration requests.",
  },
] as const;

type HelpTab = "tutorial" | "helpline";

export function HelpCenter({
  compact = false,
  defaultTab = "tutorial",
  onSubmitted,
}: {
  compact?: boolean;
  defaultTab?: HelpTab;
  onSubmitted?: () => void;
}) {
  const [tab, setTab] = useState<HelpTab>(defaultTab);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);

  async function submitHelpline(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    const name = String(form.get("name") ?? "").trim();
    const email = String(form.get("email") ?? "").trim();
    const topic = String(form.get("topic") ?? "").trim();
    const message = String(form.get("message") ?? "").trim();
    if (!name || !email || !topic || !message) {
      setError("Please complete all required fields before submitting.");
      setBusy(false);
      return;
    }
    try {
      await new Promise((resolve) => setTimeout(resolve, 450));
      setSuccess(true);
      onSubmitted?.();
      event.currentTarget.reset();
    } catch {
      setError("Unable to submit your helpline request. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`help-center ${compact ? "compact" : ""}`}>
      {!compact && (
        <div className="help-intro">
          <span className="help-intro-icon">
            <LifeBuoy size={22} />
          </span>
          <div>
            <h2>How can we help?</h2>
            <p>
              Follow the guided tutorial for Accounting Intelligence, or request
              a helpline callback from the AccordAI support team.
            </p>
          </div>
        </div>
      )}

      <div className="help-tabs" role="tablist" aria-label="Help options">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "tutorial"}
          className={tab === "tutorial" ? "active" : ""}
          onClick={() => setTab("tutorial")}
        >
          <BookOpen size={15} /> Tutorial
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "helpline"}
          className={tab === "helpline" ? "active" : ""}
          onClick={() => setTab("helpline")}
        >
          <Headphones size={15} /> Request helpline
        </button>
      </div>

      {tab === "tutorial" ? (
        <div className="help-tutorial" role="tabpanel">
          <div className="help-tutorial-banner">
            <Badge>GUIDED WALKTHROUGH</Badge>
            <p>
              Complete these steps to move from first upload to a trusted
              standardised accounting record.
            </p>
          </div>
          <ol className="help-steps">
            {TUTORIAL_STEPS.map((step, index) => {
              const Icon = step.icon;
              return (
                <li key={step.title}>
                  <span className="help-step-index">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <span className="help-step-icon">
                    <Icon size={18} />
                  </span>
                  <div>
                    <strong>{step.title}</strong>
                    <p>{step.detail}</p>
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      ) : success ? (
        <div className="success-state help-success" role="tabpanel">
          <CheckCircle2 size={36} />
          <h2>Helpline request received</h2>
          <p>
            A AccordAI support specialist will review your request. Your submission
            has been recorded and our team will follow up using your preferred
            contact method.
          </p>
          <button className="btn" type="button" onClick={() => setSuccess(false)}>
            Submit another request
          </button>
        </div>
      ) : (
        <form className="help-helpline" role="tabpanel" onSubmit={submitHelpline}>
          <div className="help-helpline-banner">
            <PhoneCall size={18} />
            <div>
              <strong>Company helpline</strong>
              <span>
                Share your workspace issue and preferred contact details. Typical
                response window: next business day.
              </span>
            </div>
          </div>
          {error && <ErrorState message={error} />}
          <div className="form-grid">
            <Field label="Full name *">
              <input name="name" required placeholder="Your name" />
            </Field>
            <Field label="Work email *">
              <input
                name="email"
                type="email"
                required
                placeholder="you@company.com"
              />
            </Field>
            <Field label="Topic *">
              <select name="topic" required defaultValue="">
                <option value="" disabled>
                  Select a topic
                </option>
                <option value="access">Access & login</option>
                <option value="enterprise">Company registration</option>
                <option value="documents">Document processing</option>
                <option value="exceptions">Exceptions & validation</option>
                <option value="billing">Accounts & licences</option>
                <option value="other">Other</option>
              </select>
            </Field>
            <Field label="Urgency">
              <select name="urgency" defaultValue="normal">
                <option value="low">Low</option>
                <option value="normal">Normal</option>
                <option value="high">High — blocking work</option>
              </select>
            </Field>
          </div>
          <Field label="How can we help? *">
            <textarea
              name="message"
              rows={compact ? 3 : 4}
              required
              placeholder="Describe the issue, tenant, and any document or user IDs if relevant."
            />
          </Field>
          <button className="btn primary full" disabled={busy} type="submit">
            {busy ? "Submitting…" : "Request helpline support"}
            <Headphones size={15} />
          </button>
        </form>
      )}
    </div>
  );
}
