"use client";
import { useEffect, useRef, useState } from "react";
import invoiceBillsReceipts from "@/images/public_page_img/invoice_bills_receipts.png";
import calculatorImage from "@/images/public_page_img/calculator.png";

const FRONT = calculatorImage.src;
const BACK = invoiceBillsReceipts.src;

export function AccordAiHero() {
  const hero = useRef<HTMLElement>(null), figure = useRef<HTMLDivElement>(null), canvas = useRef<HTMLCanvasElement>(null), front = useRef<HTMLImageElement>(null), cursor = useRef<HTMLDivElement>(null);
  const [coarse, setCoarse] = useState(false);
  useEffect(() => {
    const root = hero.current, fig = figure.current, main = canvas.current, image = front.current;
    if (!root || !fig || !main || !image) return;
    const ctx = main.getContext("2d"); if (!ctx) return;
    const mask = document.createElement("canvas"), buf = document.createElement("canvas"), mctx = mask.getContext("2d"), bctx = buf.getContext("2d"); if (!mctx || !bctx) return;
    const back = new Image(); back.crossOrigin = "anonymous"; back.src = BACK;
    let w = 0, h = 0, dpr = 1, frontReady = image.complete && !!image.naturalWidth, backReady = false, last: { x: number; y: number } | null = null, lastInput = 0, wander = 0;
    type Stamp = { x: number; y: number; t: number; lobes: { dx: number; dy: number; r: number }[] }; let stamps: Stamp[] = [];
    const mq = matchMedia("(hover: none), (pointer: coarse)"), sync = () => setCoarse(mq.matches); sync(); mq.addEventListener?.("change", sync);
    const resize = () => { const r = fig.getBoundingClientRect(); dpr = Math.min(devicePixelRatio || 1, 2); w = r.width; h = r.height; [main, mask, buf].forEach((c) => { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }); };
    const containImage = (c: CanvasRenderingContext2D, img: HTMLImageElement) => { const s = Math.min(w / img.naturalWidth, h / img.naturalHeight), dw = img.naturalWidth * s, dh = img.naturalHeight * s; c.drawImage(img, (w - dw) / 2, (h - dh) / 2, dw, dh); };
    const stamp = (x: number, y: number, t: number) => { const base = Math.max(w, 260) * (mq.matches ? .15 : .12); const lobes = Array.from({ length: 3 + Math.floor(Math.random() * 3) }, () => { const a = Math.random() * Math.PI * 2, offset = Math.random() * base * .45; return { dx: Math.cos(a) * offset, dy: Math.sin(a) * offset, r: base * (.45 + Math.random() * .55) }; }); stamps.push({ x, y, t, lobes }); };
    const paint = (cx: number, cy: number, now: number) => { const r = fig.getBoundingClientRect(), x = cx - r.left, y = cy - r.top; if (last) { const distance = Math.hypot(x - last.x, y - last.y), count = Math.min(40, Math.ceil(distance / (Math.max(w, 260) * .04))); for (let i = 1; i <= count; i++) stamp(last.x + (x - last.x) * i / count, last.y + (y - last.y) * i / count, now); } else stamp(x, y, now); last = { x, y }; lastInput = now; };
    const move = (e: PointerEvent) => { const now = performance.now(); paint(e.clientX, e.clientY, now); if (cursor.current) { cursor.current.style.transform = `translate(${e.clientX}px,${e.clientY}px)`; cursor.current.classList.add("on"); } };
    const leave = () => { last = null; cursor.current?.classList.remove("on"); };
    root.addEventListener("pointermove", move); root.addEventListener("pointerdown", move); root.addEventListener("pointerleave", leave); addEventListener("resize", resize); back.addEventListener("load", () => { backReady = true; }); if (!frontReady) image.addEventListener("load", () => { frontReady = true; main.classList.add("ready"); }, { once: true }); resize(); if (frontReady) main.classList.add("ready");
    let frame = 0; const render = (now: number) => { if (mq.matches && now - lastInput > 2500) { wander += .016; const r = fig.getBoundingClientRect(), x = w * (.5 + Math.sin(wander * 1.3) * .28), y = h * (.32 + Math.sin(wander * .9 + 1) * .22); if (!last || Math.hypot(x - last.x, y - last.y) > w * .02) paint(r.left + x, r.top + y, now); } stamps = stamps.filter((s) => now - s.t < 1100); mctx.setTransform(dpr, 0, 0, dpr, 0, 0); mctx.clearRect(0, 0, w, h); mctx.fillStyle = "#000"; stamps.forEach((s) => { const k = 1 - (now - s.t) / 1100, scale = k < .75 ? k / .75 : 1; s.lobes.forEach((l) => { mctx.beginPath(); mctx.arc(s.x + l.dx * scale, s.y + l.dy * scale, l.r * Math.pow(scale, .8), 0, Math.PI * 2); mctx.fill(); }); }); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.globalCompositeOperation = "source-over"; ctx.clearRect(0, 0, w, h); if (frontReady) containImage(ctx, image); if (stamps.length) { ctx.globalCompositeOperation = "destination-out"; ctx.drawImage(mask, 0, 0, w, h); if (backReady) { bctx.setTransform(dpr, 0, 0, dpr, 0, 0); bctx.clearRect(0, 0, w, h); bctx.globalCompositeOperation = "source-over"; bctx.drawImage(mask, 0, 0, w, h); bctx.globalCompositeOperation = "source-in"; containImage(bctx, back); ctx.globalCompositeOperation = "source-over"; ctx.drawImage(buf, 0, 0, w, h); } } frame = requestAnimationFrame(render); }; frame = requestAnimationFrame(render);
    return () => { cancelAnimationFrame(frame); root.removeEventListener("pointermove", move); root.removeEventListener("pointerdown", move); root.removeEventListener("pointerleave", leave); removeEventListener("resize", resize); mq.removeEventListener?.("change", sync); };
  }, []);
  return <section ref={hero} className="accordai-hero-hero" aria-label="Hover to reveal AccordAI accounting tools"><div className="accordai-hero-marquees" aria-hidden="true"><div className="accordai-hero-row ghost">. <span>Accord AI</span>. <span>Accord AI</span></div><div className="accordai-hero-row solid">. <span>Accounting Intelligence</span>. <span>Accounting Intelligence</span></div></div><div ref={figure} className="accordai-hero-figure"><img ref={front} alt="AccordAI calculator" src={FRONT} /><canvas ref={canvas} aria-hidden="true" /></div><p className="accordai-hero-hint"><i />{coarse ? "Drag to reveal" : "Move your cursor to reveal"}</p><span className="accordai-hero-tag">AccordAI · Accounting Intelligence · Invoices · Bills · Receipts</span><div ref={cursor} className="accordai-hero-cursor" /></section>;
}
