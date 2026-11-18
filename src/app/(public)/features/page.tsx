import Link from "next/link";
import Image from "next/image";
import { CircleHelp } from "lucide-react";
import accordAiLogo from "@/images/AccordAI_logo.png";

const features = [
  ["01", "Field extraction", "Read supplier, date, line items, and totals from invoices, bills, and receipts."],
  ["02", "Validation and exceptions", "Check every amount and route mismatches to a person before they become records."],
  ["03", "Standardised records", "Turn approved documents into one consistent accounting record shape."],
  ["04", "Performance visibility", "Track accuracy, processing time, volume, and exception rates in one workspace."],
];

export default function FeaturesPage() {
  return <div className="home-wash public-info-page"><header className="hp-header"><Link href="/" className="hp-brand"><Image src={accordAiLogo} alt="AccordAI" width={150} height={46} priority /></Link><nav><span className="hp-nav-center"><Link href="/">Home</Link><Link href="/features">Features</Link><Link href="/about-us">About us</Link></span><span className="hp-nav-actions"><Link href="/help" className="hp-help-link" aria-label="Help and support" title="Help and support"><CircleHelp size={18} strokeWidth={2} /><span>Help</span></Link><Link href="/request-access" className="hp-register">Register company</Link><Link href="/login" className="hp-login">Log in</Link></span></nav></header><main className="public-info-main"><p className="hp-eyebrow">AccordAI platform</p><h1>Everything between <span>document and record.</span></h1><p className="hp-lead">A clear, auditable workflow for finance teams handling invoices, bills, and receipts.</p><div className="hp-cards two">{features.map(([number, title, copy]) => <article className="plain-card" key={title}><span>{number}</span><h2>{title}</h2><p>{copy}</p></article>)}</div><Link href="/" className="hp-btn light">Back to home</Link></main></div>;
}
