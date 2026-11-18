"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { ArrowUp, CircleHelp } from "lucide-react";
import Image from "next/image";
import accordAiLogo from "@/images/AccordAI_logo.png";
import accountingWireframe from "@/images/public_page_img/accordai-accounting-wireframe.png";
import { WorkflowStory } from "@/features/auth/workflow-story";
import { AccordAiHero } from "@/features/auth/accordai-hero";

const MODULES = [
  { id: "platform", name: "Platform" },
  { id: "documents", name: "Documents" },
  { id: "exceptions", name: "Exceptions" },
  { id: "records", name: "Records" },
  { id: "reports", name: "Reports" },
] as const;

const NAV = ["Platform", "Documents", "Exceptions", "Records", "Reports"] as const;

const STEPS = [
  ["Upload", "The document comes in as an invoice, a bill, or a receipt. This is the only manual step before extraction starts."],
  ["Extract", "The system pulls the accounting fields: who billed you, the date, each line, and the total."],
  ["Validate", "Those fields are checked against each other. A total that does not add up, or a missing value, fails here."],
  ["Review", "Failed documents become exceptions. They go to the right person, and every correction stays on the document."],
  ["Standardise", "A document that passes is written as one record, with the same fields every time."],
  ["Export", "Approved records can be exported with their internal ledger and reconciliation references. Payment execution and external ERP posting remain out of scope."],
] as const;

function ModuleScreen({ id }: { id: (typeof MODULES)[number]["id"] }) {
  const title = {
    platform: ["AccordAI", "Companies and account limits."],
    documents: ["Intake", "Invoices, bills, and receipts."],
    exceptions: ["Review", "Items that need a person."],
    records: ["Ledger", "Records in one standard shape."],
    reports: ["Performance", "How processing is going."],
  }[id];
  const stats = {
    platform: [["Companies", "2"], ["Seats", "12"], ["Pending", "1"]],
    documents: [["Uploaded", "18"], ["Extracting", "3"], ["Ready", "15"]],
    exceptions: [["Open", "7"], ["Today", "2"], ["Rate", "8%"]],
    records: [["Ready", "31"], ["Exported", "20"], ["Currency", "MYR"]],
    reports: [["Accuracy", "96%"], ["Time", "12s"], ["Volume", "42"]],
  }[id];
  const lines = {
    platform: ["ABC Sdn Bhd approved", "XYZ Sdn Bhd pending", "Seat limit set"],
    documents: ["INV-2026-00821 extracted", "Receipt batch uploaded", "Supplier field checked"],
    exceptions: ["Total does not match", "Supplier missing", "Sent to the owner"],
    records: ["Office supplies standardised", "Ready to export", "Change kept on the record"],
    reports: ["Pilot accuracy holding", "Under 20 seconds", "Concurrent uploads ok"],
  }[id];
  return (
    <div className="hp-dash">
      <div className="hp-dash-top">
        <span className="hp-lights" aria-hidden="true"><i /><i /><i /></span>
        <span>Accounting Intelligence workspace</span>
        <span className="hp-ready">Ready</span>
      </div>
      <div className="hp-dash-body">
        <aside>
          <p className="hp-label">Workspace</p>
          {NAV.map((name) => (
            <p key={name} className={name.toLowerCase() === id ? "on" : ""}>{name}</p>
          ))}
        </aside>
        <div className="hp-dash-main">
          <div className="hp-dash-head">
            <div>
              <p className="hp-kicker">{title[0]}</p>
              <p className="hp-dash-title">{title[1]}</p>
            </div>
            <span className="hp-avatar">AT</span>
          </div>
          <div className="hp-stats">
            {stats.map(([label, value]) => (
              <p key={label}><span>{label}</span><strong>{value}</strong><em>Updated just now</em></p>
            ))}
          </div>
          <div className="hp-lower">
            <div>
              <p className="hp-panel-title">{id === "reports" || id === "platform" ? "Processing volume" : "In this module"} <span>Now</span></p>
              <div className="hp-bars" aria-hidden="true"><i /><i /><i /><i /><i /><i /><i /></div>
            </div>
            <div>
              <p className="hp-panel-title">Latest <span>Live</span></p>
              <ul>
                {lines.map((line, index) => (
                  <li key={line}><i className={index === 0 ? "a" : index === 1 ? "b" : "c"} />{line}</li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export function LandingPage() {
  const [moduleIndex, setModuleIndex] = useState(2);
  const [switchKey, setSwitchKey] = useState(0);
  const [processIndex, setProcessIndex] = useState(0);
  const [showBackToTop, setShowBackToTop] = useState(false);
  const dragX = useRef(0);
  const processRef = useRef<HTMLElement | null>(null);

  function showModule(next: number) {
    setModuleIndex((next + MODULES.length) % MODULES.length);
    setSwitchKey((key) => key + 1);
  }

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const timer = window.setInterval(() => {
      setModuleIndex((current) => (current + 1) % MODULES.length);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [switchKey]);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const timer = window.setInterval(() => setProcessIndex((current) => (current + 1) % STEPS.length), 4200);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const onScroll = () => setShowBackToTop(window.scrollY > Math.max(window.innerHeight * 0.7, 420));
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    const section = processRef.current;
    if (!section || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let frame = 0;
    const updateParallax = () => {
      frame = 0;
      const bounds = section.getBoundingClientRect();
      const progress = (window.innerHeight / 2 - (bounds.top + bounds.height / 2)) / Math.max(bounds.height, 1);
      section.style.setProperty("--process-parallax", `${Math.max(-22, Math.min(22, progress * 44))}px`);
    };
    const onScroll = () => { if (!frame) frame = window.requestAnimationFrame(updateParallax); };
    updateParallax();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => { window.removeEventListener("scroll", onScroll); window.removeEventListener("resize", onScroll); if (frame) window.cancelAnimationFrame(frame); };
  }, []);

  return (
    <div className="home-wash" id="home">
      <header className="hp-header">
        <Link href="/" className="hp-brand">
          <Image src={accordAiLogo} alt="AccordAI" width={150} height={46} priority />
        </Link>
        <nav>
          <span className="hp-nav-center">
            <Link href="#home">Home</Link>
            <Link href="/features">Features</Link>
            <Link href="/about-us">About us</Link>
          </span>
          <span className="hp-nav-actions">
            <Link href="/help" className="hp-help-link" aria-label="Help and support" title="Help and support"><CircleHelp size={18} strokeWidth={2} /><span>Help</span></Link>
            <Link href="/request-access" className="hp-register">Register company</Link>
            <Link href="/login" className="hp-login">Log in</Link>
          </span>
        </nav>
      </header>
      <main>
        <AccordAiHero />
<section className="hp-block" aria-labelledby="modules-title">
          <p className="hp-eyebrow">Modules</p>
          <h2 id="modules-title">A general look across the workspace.</h2>
          <p className="hp-copy">The open card stays in the centre and moves every 4 seconds.</p>
          <div
            className="stack"
            onPointerDown={(event) => { dragX.current = event.clientX; }}
            onPointerUp={(event) => {
              const delta = event.clientX - dragX.current;
              if (delta > 48) showModule(moduleIndex - 1);
              if (delta < -48) showModule(moduleIndex + 1);
            }}
          >
            {MODULES.map((item, index) => {
              let slot = index - moduleIndex;
              if (slot > MODULES.length / 2) slot -= MODULES.length;
              if (slot < -MODULES.length / 2) slot += MODULES.length;
              const place = slot === -1 || slot === 0 || slot === 1 ? String(slot) : "hide";
              return (
                <article key={item.id} data-slot={place} className="stack-card">
                  <ModuleScreen id={item.id} />
                </article>
              );
            })}
          </div>
          <div className="stack-nav">
            <button type="button" aria-label="Previous module" onClick={() => showModule(moduleIndex - 1)}>←</button>
            <div role="tablist" aria-label="Module pages">
              {MODULES.map((item, index) => (
                <button key={item.id} type="button" className={index === moduleIndex ? "on" : ""} aria-label={item.name} aria-pressed={index === moduleIndex} onClick={() => showModule(index)} />
              ))}
            </div>
            <button type="button" className="next" aria-label="Next module" onClick={() => showModule(moduleIndex + 1)}>→</button>
          </div>
        </section>

        <WorkflowStory />
        <section id="process" ref={processRef} className="hp-block bend-process-section">
          <p className="hp-eyebrow">The six steps</p>
          <h2>What each step is for.</h2>
          <p className="hp-copy">A document only becomes a standardised record after these six steps. The later ones exist so a bad extraction is never treated as finished.</p>
          <div className="step-palette" aria-label="Workflow step selector">{STEPS.map(([name, detail], index) => <button key={name} type="button" className={index === processIndex ? "active" : ""} aria-label={`Show ${name} step`} onClick={() => setProcessIndex(index)}><span className="step-palette-number">Step 0{index + 1}</span><strong>{name}</strong><small>{detail}</small></button>)}</div>
        </section>

        <section className="hp-block">
          <div className="brand-gradient" style={{ backgroundImage: `linear-gradient(90deg, rgba(236,245,255,.96) 0%, rgba(236,245,255,.82) 42%, rgba(236,245,255,.38) 100%), url(${accountingWireframe.src})`, backgroundPosition: "center", backgroundSize: "cover" }}>
            <p className="hp-eyebrow light">Ready to try the workspace?</p>
            <h2>Clean extraction. Checked records. A standard you can export.</h2>
            <p className="cta-supporting-copy">Bring your documents into a workflow your team can review with confidence.</p>
            <div className="hp-actions cta-actions">
              <Link href="/request-access" className="hp-btn light">Register company</Link>
              <Link href="/login" className="hp-btn ghost">Sign in</Link>
            </div>
            <div className="cta-dashboard" aria-label="AccordAI accounting dashboard preview">
              <div className="cta-revenue-card"><span>Processed this month</span><strong className="cta-skeleton-value" aria-label="Loading processed amount" /><small>documents checked</small></div>
              <div className="cta-expense-card"><strong>Documents</strong><div className="cta-donut"><span>64%</span></div><div className="cta-legend"><i />Invoices <i />Bills <i />Receipts</div></div>
              <div className="cta-trend-card"><strong>Review trend</strong><svg viewBox="0 0 320 120" role="img" aria-label="Upward review trend"><path d="M8 100 C45 96 50 72 88 78 S120 70 148 48 S190 72 220 54 S265 48 312 12" /><circle cx="220" cy="54" r="7" /></svg></div>
            </div>
          </div>
        </section>
      </main>      <footer className="hp-footer" id="about">
        <div className="footer-brand-column">
          <Link href="/" className="footer-brand"><Image src={accordAiLogo} alt="AccordAI" width={150} height={46} /></Link>
          <p>Intelligent document processing for finance teams that need clarity, control, and dependable records.</p>
          <a className="footer-email" href="mailto:ai.adminportal@gmail.com">Contact us: ai.adminportal@gmail.com</a>
        </div>
        <div className="footer-link-column">
          <p className="hp-eyebrow">Explore</p>
          <Link href="/features">Features</Link>
          <a href="#process">Workflow</a>
          <Link href="/about-us">About us</Link>
        </div>
        <div className="footer-link-column">
          <p className="hp-eyebrow">Get started</p>
          <Link href="/request-access">Register company</Link>
          <Link href="/login">Log in</Link>
          <Link href="/help">Help & support</Link>
        </div>
        <div className="footer-scope-column">
          <p className="hp-eyebrow">Built for confident decisions</p>
          <p>Extract, validate, review, and standardise invoices, bills, and receipts before they move downstream.</p>
        </div>
        <small><span>© 2026 AccordAI</span><span>Accounting Intelligence platform</span><span>Privacy · Terms</span></small>
      </footer>
      <button type="button" className={`back-to-top${showBackToTop ? " visible" : ""}`} onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })} aria-label="Back to top"><ArrowUp size={19} strokeWidth={2.4} /></button>
    </div>
  );
}
