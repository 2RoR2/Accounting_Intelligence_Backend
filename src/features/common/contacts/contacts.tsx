"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Search } from "lucide-react";
import { getCustomers, getSuppliers } from "@/lib/api/workflow.service";
import { getDocuments } from "@/lib/api/document.service";
import type { Document, User } from "@/types";
import { Badge, ErrorState, Panel } from "@/components/common/ui";

type Contact = {
  id: string;
  name: string;
  registrationNumber?: string;
  address?: string;
  type: "Customer" | "Supplier";
};

export function ContactsPage({ user }: { user: User }) {
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Contact | null>(null);
  const base = user.role === "LOCAL_ADMIN" ? "/company" : "/workspace";

  useEffect(() => {
    let active = true;
    Promise.all([getCustomers(), getSuppliers(), getDocuments(user)])
      .then(([customers, suppliers, docs]) => {
        if (!active) return;
        setContacts([
          ...customers.map((c) => ({ ...c, type: "Customer" as const })),
          ...suppliers.map((s) => ({ ...s, type: "Supplier" as const })),
        ]);
        setDocuments(docs);
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : "Unable to load contacts.");
      });
    return () => {
      active = false;
    };
  }, [user]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return contacts;
    return contacts.filter(
      (c) =>
        c.name.toLowerCase().includes(q) ||
        (c.registrationNumber || "").toLowerCase().includes(q),
    );
  }, [contacts, query]);

  const related = useMemo(() => {
    if (!selected) return [];
    const name = selected.name.trim().toLowerCase();
    return documents.filter((doc) => {
      const party = selected.type === "Supplier" ? doc.supplier : doc.customer;
      return (party || "").trim().toLowerCase() === name;
    });
  }, [selected, documents]);

  return (
    <div className="contacts-page">
      <header className="page-header">
        <div>
          <h1>Contacts</h1>
          <p>Find a customer or supplier, then open their documents for checking.</p>
        </div>
      </header>

      {error ? (
        <ErrorState message={error} />
      ) : (
        <div className="contacts-layout">
          <Panel title="Customers & suppliers" subtitle={`${filtered.length} shown`}>
            <div className="search contacts-search">
              <Search size={16} />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search name or registration"
              />
            </div>
            {filtered.length === 0 ? (
              <div className="panel-body">
                <p className="empty-state">No contacts match your search.</p>
              </div>
            ) : (
              <ul className="contacts-list">
                {filtered.map((contact) => (
                  <li key={`${contact.type}-${contact.id}`}>
                    <button
                      type="button"
                      className={selected?.id === contact.id && selected.type === contact.type ? "active" : ""}
                      onClick={() => setSelected(contact)}
                    >
                      <strong>{contact.name}</strong>
                      <span>{contact.type}</span>
                      <small>{contact.registrationNumber || "No registration"}</small>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel
            title={selected ? <>{selected.name} <Link className="text-link" href={`${base}/contact-detail`}>View Contact</Link></> : "Related documents"}
            subtitle={selected ? `${related.length} document${related.length === 1 ? "" : "s"}` : "Select a contact"}
          >
            {!selected ? (
              <div className="panel-body">
                <p className="empty-state">Choose a customer or supplier to see their documents.</p>
              </div>
            ) : related.length === 0 ? (
              <div className="panel-body">
                <p className="empty-state">No documents linked to this contact yet.</p>
              </div>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Document</th>
                      <th>Type</th>
                      <th>Status</th>
                      <th>Date</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {related.map((doc) => (
                      <tr key={doc.id}>
                        <td>
                          <strong>{doc.name}</strong>
                          <small>{doc.code || doc.id}</small>
                        </td>
                        <td>{doc.type}</td>
                        <td>
                          <Badge>{doc.status}</Badge>
                        </td>
                        <td>{new Date(doc.createdAt).toLocaleDateString("en-MY")}</td>
                        <td>
                          <Link className="text-link" href={`${base}/documents/${doc.id}`}>
                            Open
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      )}
    </div>
  );
}
