"use client";
import Link from "next/link";
import { ArrowRight, Building2, CircleHelp } from "lucide-react";
import { Brand } from "@/components/common/layout/app-shell";

export function PublicHeader({
  active,
}: {
  active?: "help" | "register" | "login";
}) {
  return (
    <header className="public-header">
      <Link href="/" aria-label="Accounting Intelligence home">
        <Brand />
      </Link>
      <nav aria-label="Public navigation">
        <span className="status-chip">
          <span />
          Accounting Intelligence
        </span>
        <Link
          href="/help"
          className={`header-link ${active === "help" ? "active" : ""}`}
        >
          <CircleHelp size={15} /> Help
        </Link>
        <Link
          href="/request-access"
          className={`btn header-register ${active === "register" ? "active" : ""}`}
        >
          <Building2 size={15} /> Register company
        </Link>
        <Link
          href="/login"
          className={`btn primary ${active === "login" ? "active" : ""}`}
        >
          Log in <ArrowRight size={15} />
        </Link>
      </nav>
    </header>
  );
}
