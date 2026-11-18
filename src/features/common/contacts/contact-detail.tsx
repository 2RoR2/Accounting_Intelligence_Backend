"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type { User } from "@/types";

const sections: Array<[string, string[]]> = [
  ["Basic Information", ["Entity Type", "Legal Name", "Other Name", "Contact Code", "Registration No. Type", "Registration No.", "Old Registration No.", "TIN", "SST Registration No."]],
  ["Type & Grouping", ["Types", "Receivable Account", "Credit Limit", "Payable Account", "Groups", "Price Level"]],
  ["Contact Information", ["Contact No.", "Email Addresses"]],
  ["Contact Addresses", ["Address Name", "Street Address", "City", "Postcode", "Country", "State"]],
  ["Default Settings", ["Currency", "Payment Term", "Income Account", "Expense Account", "Location", "Tags", "MyInvois Control"]],
  ["Custom Fields", ["Account number", "Bank name's", "Date", "Tax Exemption", "TIN"]],
  ["Communication", ["Monthly Statement", "Invoice Reminder"]],
  ["Other Information", ["Bukku Connect Key", "Remarks"]],
];

export function ContactDetailPage({ user }: { user: User }) {
  const router = useRouter();
  const [active, setActive] = useState(true);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const base = user.role === "LOCAL_ADMIN" ? "/company" : "/workspace";
  const notify = (message: string) => {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 3200);
  };

  const save = (event: React.FormEvent) => {
    event.preventDefault();
    notify("Contact changes saved as the current version.");
  };

  return (
    <div className="contact-detail-page">
      <div className="bukku-breadcrumb">⌂ Home <span>/</span> ▧ Contacts <span>/</span> <strong>View Contact</strong></div>
      <div className="contact-detail-heading"><h1>View Contact <span>✓</span></h1><div><button type="button" className="danger" onClick={() => { setActive((value) => !value); notify(active ? "Contact deactivated." : "Contact reactivated."); }}>{active ? "Deactivate" : "Reactivate"}</button><button type="button" className="primary" onClick={() => setHistoryOpen(true)}>History</button></div></div>
      <form onSubmit={save}>
        {sections.map(([name, fields]) => <section key={name}><div className="contact-section-title"><strong>{name} ✓</strong><small>Manage contact information and settings.</small></div><div className="contact-fields">{fields.map((field) => <label key={field}>{field}{field === "Remarks" || field === "Street Address" ? <textarea defaultValue={field === "Remarks" ? "" : "338, Jalan Tun Perak"} /> : field === "Types" ? <div className="contact-checks"><label><input type="checkbox" defaultChecked /> Customer</label><label><input type="checkbox" defaultChecked /> Supplier</label><label><input type="checkbox" /> Employee</label></div> : field.includes("Reminder") || field.includes("Statement") ? <input type="checkbox" defaultChecked /> : <input defaultValue={field === "Legal Name" ? "1Playground Group 123" : field === "Contact Code" ? "C-10001" : field === "Registration No." ? "198801006969" : ""} />}</label>)}</div></section>)}
        <div className="contact-save"><button type="button" onClick={() => router.push(`${base}/contacts`)}>Cancel</button><button type="submit" className="primary">Save</button></div>
      </form>
      {historyOpen && <div className="modal-backdrop" onMouseDown={() => setHistoryOpen(false)}><div className="quotation-modal" role="dialog" onMouseDown={(event) => event.stopPropagation()}><div className="modal-title"><h2>Contact history</h2><button type="button" onClick={() => setHistoryOpen(false)}>×</button></div><div className="quotation-detail"><span>Contact<strong>1Playground Group 123</strong></span><span>Latest action<strong>Record opened</strong></span><span>State<strong>{active ? "Active" : "Inactive"}</strong></span></div><div className="modal-actions"><button type="button" className="btn" onClick={() => setHistoryOpen(false)}>Close</button></div></div></div>}
      {notice && <div className="workflow-toast" role="status">{notice}</div>}
    </div>
  );
}
