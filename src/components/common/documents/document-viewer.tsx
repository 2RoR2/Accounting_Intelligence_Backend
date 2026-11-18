"use client";
import React, { useEffect, useRef, useState } from "react";
import { Maximize2, RotateCcw, ZoomIn, ZoomOut } from "lucide-react";
import type { Extraction, SourceEvidence } from "@/types";
import { imageEvidenceBox } from "@/lib/document-viewer";

export function SourceEvidenceList({ evidence, documentId }: { evidence: SourceEvidence[]; documentId: string }) {
  const source = "/api/documents/" + encodeURIComponent(documentId) + "/file";
  return <>{evidence.map((item, index) => <div className="source-evidence-item" key={index}>
    <a href={source + (item.page ? "#page=" + item.page : "")} target="_blank" rel="noreferrer">Source page {item.page ?? "not provided"}</a>
    <blockquote>{item.text ?? "Source text not provided"}</blockquote>
    {item.method === "HUMAN" && item.source_note && <p>Reviewer source note: {item.source_note}</p>}
    <details><summary>Source reference and coordinates</summary>
      <p>{item.source_ref ?? "Reference not provided"}</p>
      <p>{item.bbox ? "Box: " + [item.bbox.l, item.bbox.t, item.bbox.r, item.bbox.b].join(", ") + " " + (item.coordinate_units ?? "Units not provided") + " | Origin: " + (item.bbox.coord_origin ?? "Not provided") : "Source coordinates not available."}</p>
      {item.page_size && <p>Source page size: {item.page_size.width} × {item.page_size.height}</p>}
      {item.text_span && <p>Text span [{item.text_span.join(", ")}) in source block.</p>}
    </details>
    {item.bbox_scope === "source_block" && <p>Box covers the source block, not a tight field boundary.</p>}
  </div>)}</>;
}

function ImageViewer({ source, name, evidence }: { source: string; name: string; evidence: SourceEvidence[] }) {
  const viewport = useRef<HTMLDivElement>(null);
  const [bounds, setBounds] = useState({ width: 0, height: 0 });
  const [natural, setNatural] = useState({ width: 0, height: 0 });
  const [zoom, setZoom] = useState(1);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setBounds({ width: element.clientWidth - 32, height: element.clientHeight - 32 }));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const fit = natural.width && bounds.width ? Math.min(bounds.width / natural.width, bounds.height / natural.height, 1) : 0;
  const scale = fit * zoom;
  const boxes = evidence.flatMap(item => {
    const style = imageEvidenceBox(item, natural.width, natural.height);
    return style ? [{ style, item }] : [];
  });
  function reset() { setZoom(1); viewport.current?.scrollTo({ left: 0, top: 0 }); }
  return <>
    <div className="image-viewer-stage">
    <div className="image-viewer-controls" role="group" aria-label="Image zoom controls">
      <button type="button" className="viewer-icon-button" onClick={() => setZoom(value => Math.max(0.25, value / 1.25))} disabled={!fit || failed || zoom <= 0.25} aria-label="Zoom out" title="Zoom out"><ZoomOut size={18} aria-hidden="true" /></button>
      <output aria-live="polite">{fit ? `${Math.round(scale * 100)}%${zoom === 1 ? " · Fit" : ""}` : "Loading image…"}</output>
      <button type="button" className="viewer-icon-button" onClick={() => setZoom(value => Math.min(16, value * 1.25))} disabled={!fit || failed || zoom >= 16} aria-label="Zoom in" title="Zoom in"><ZoomIn size={18} aria-hidden="true" /></button>
      <button type="button" className="viewer-icon-button" onClick={reset} disabled={!fit || failed} aria-label="Fit to page" title="Fit to page"><Maximize2 size={18} aria-hidden="true" /></button>
      <button type="button" className="viewer-icon-button" onClick={reset} disabled={!fit || failed} aria-label="Reset view" title="Reset view"><RotateCcw size={17} aria-hidden="true" /></button>
    </div>
    {!!evidence.length && <p className="image-evidence-note">{boxes.length ? "Source region outlined below. Zoom in to inspect the text." : "Source highlight not available: coordinates cannot be accurately mapped to this image."}</p>}
    <div className="image-viewer-viewport" ref={viewport} tabIndex={0} role="region" aria-label="Original image; scroll to inspect when zoomed">
      {failed ? <div className="image-viewer-error" role="alert"><p>Original image could not be loaded. Your corrections are retained.</p><button type="button" className="btn" onClick={() => { setFailed(false); setAttempt(value => value + 1); }}>Retry image</button><a href={source} target="_blank" rel="noreferrer">Open original source</a></div> :
        <div className="image-viewer-page" style={{ width: natural.width * scale, height: natural.height * scale, visibility: fit ? "visible" : "hidden" }}>
          {/* Native image preserves the authenticated source and its original resolution for zooming. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img key={attempt} src={source} alt={`Original document: ${name}`} onLoad={event => setNatural({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })} onError={() => setFailed(true)} draggable={false} />
          {boxes.map(({ style, item }, index) => <span key={index} className="image-evidence-box" style={style} role="img" aria-label={`Source ${item.bbox_scope === "source_block" ? "block" : "region"} on page ${item.page}: ${item.text ?? "Text not provided"}`} />)}
        </div>}
    </div>
    </div>
  </>;
}

export function DocumentViewer({ name, extraction, highlightedField, selectedSource }: { name: string; extraction: Extraction; highlightedField?: string; selectedSource?: { label: string; evidence: SourceEvidence[] } }) {
  const selected = selectedSource ?? (highlightedField ? { label: extraction.fields[highlightedField]?.label ?? highlightedField, evidence: extraction.fields[highlightedField]?.evidence ?? [] } : undefined);
  const evidence = selected?.evidence ?? [];
  const page = evidence[0]?.page;
  const source = "/api/documents/" + encodeURIComponent(extraction.documentId) + "/file";
  const url = source + (page ? "#page=" + page : "#view=FitH");
  const image = /\.(png|jpe?g)$/i.test(name);
  const structured = /\.json$/i.test(name);
  return <div className="doc-viewer">
    <div className="doc-viewer-toolbar"><strong>Original document: {name}</strong><a href={image ? source : url} target="_blank" rel="noreferrer">Open original source</a></div>
    <section className="selected-source-evidence" aria-label="Source evidence" aria-live="polite">
      <strong>{selected ? `Selected field evidence: ${selected.label}` : "Select a field to inspect its source evidence"}</strong>
      {selected && !evidence.length && <p>Source evidence not available.</p>}
      <SourceEvidenceList evidence={evidence} documentId={extraction.documentId} />
    </section>
    {structured ? <StructuredSource payload={extraction.raw?.payload} source={source} /> : image ? <ImageViewer key={extraction.documentId} source={source} name={name} evidence={evidence} /> : <iframe title="Original document" className="document-pdf-frame" src={url} />}
  </div>;
}

function StructuredSource({ payload, source }: { payload: unknown; source: string }) {
  const [data, setData] = useState<unknown>(payload);
  const [error, setError] = useState("");
  useEffect(() => {
    if (payload) { setData(payload); return; }
    const controller = new AbortController();
    fetch(source, { credentials: "include", signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error("The original structured document could not be loaded.");
      setData(await response.json());
    }).catch(e => { if (e.name !== "AbortError") setError(e.message); });
    return () => controller.abort();
  }, [payload, source]);
  function value(path: string): string {
    let node: unknown = data;
    for (const part of path.split(".")) {
      if (Array.isArray(node)) node = node[0];
      if (!node || typeof node !== "object") return "Not provided";
      node = (node as Record<string, unknown>)[part];
    }
    if (Array.isArray(node)) node = node[0];
    if (node && typeof node === "object") node = (node as Record<string, unknown>)._;
    return typeof node === "string" || typeof node === "number" ? String(node) : "Not provided";
  }
  const fields = [
    ["Document number", "Invoice.ID"], ["Issue date", "Invoice.IssueDate"],
    ["Document type code", "Invoice.InvoiceTypeCode"], ["Currency", "Invoice.DocumentCurrencyCode"],
    ["Supplier", "Invoice.AccountingSupplierParty.Party.PartyLegalEntity.RegistrationName"],
    ["Customer", "Invoice.AccountingCustomerParty.Party.PartyLegalEntity.RegistrationName"],
    ["Related invoice", "Invoice.BillingReference.InvoiceDocumentReference.ID"],
    ["Payable amount", "Invoice.LegalMonetaryTotal.PayableAmount"],
  ];
  return <section className="structured-source" tabIndex={0} aria-label="Original structured document">
    <h3>Structured e-invoice source</h3>
    <p>Values below come from the original JSON document. Review source placeholders before using them.</p>
    {error && <p role="alert">{error}</p>}
    {!data && !error && <p role="status">Loading original document…</p>}
    {!!data && <><dl>{fields.map(([label, path]) => <div key={path}><dt>{label}</dt><dd>{value(path)}</dd></div>)}</dl>
      <details><summary>View complete original JSON</summary><pre>{JSON.stringify(data, null, 2)}</pre></details></>}
    <a href={source} target="_blank" rel="noreferrer">Open original JSON</a>
  </section>;
}
