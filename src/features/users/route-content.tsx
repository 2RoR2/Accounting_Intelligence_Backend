import { EffortStudyPage } from "@/features/common/reports/effort-study";
import { Dashboard } from "@/features/common/dashboard/dashboard";
import { DocumentReview } from "@/features/common/documents/document-review";
import { DocumentList, UploadPage } from "@/features/common/documents/documents";
import { ExceptionsPage } from "@/features/common/exceptions/exceptions";
import { RecordsPage } from "@/features/common/records/records";
import { ProfilePage } from "@/features/common/settings/profile-page";
import { ContactsPage } from "@/features/common/contacts/contacts";
import { ContactDetailPage } from "@/features/common/contacts/contact-detail";
import { AccountingReportsPage } from "@/features/common/reports/accounting-reports";
import { SettingsPage } from "@/features/common/settings/settings";
import { WorkspaceSectionPage } from "@/features/common/workspace/workspace-section-page";
import { MoneyFlowPage } from "@/features/common/money/money-flow";
import { LedgerPage } from "@/features/common/ledger/ledger-page";
import { ReconciliationPage } from "@/features/common/money/reconciliation-page";
import { UserManagement } from "@/features/common/users/user-management";
import { HelpCenter } from "@/features/common/help/help-center";
import { ValidationOverviewPage } from "@/features/common/validation/validation-overview";
import { BankPage } from "@/features/common/bank/bank-page";
import { AccountingPage } from "@/features/common/accounting/accounting-pages";
import { CompanyProfilePage } from "@/features/common/settings/company-profile-page";
import { CompanySettingsPage } from "@/features/common/settings/company-settings-page";
import type { User } from "@/types";
import { useEffect } from "react";
import { useRouter } from "next/navigation";

function RedirectTo({ href }: { href: string }) {
  const router = useRouter();
  useEffect(() => { router.replace(href); }, [href, router]);
  return <p className="empty-state" role="status">Redirecting to Performance Evaluation… <a href={href}>Open it now</a>.</p>;
}

export function UserRoutes({ path, user }: { path: string[]; user: User }) {
  const [, page, id] = path;
  // Money pages must win even if the sidebar still appends /view-money-in
  if (page === "money-in" || page === "money-out") {
    return <MoneyFlowPage user={user} direction={page === "money-in" ? "money_in" : "money_out"} />;
  }
  if (page === "ledger") return <LedgerPage initialSection="journals" />;
  if (page === "accounting") return <LedgerPage />;
  if (page === "validation-overview") return <ValidationOverviewPage user={user} />;
  if (page === "invoices") return <DocumentList user={user} initialTab="sales-invoices" />;
  if (page === "credit-notes") return <DocumentList user={user} initialTab="credit-notes" />;
  if (page === "debit-notes") return <DocumentList user={user} initialTab="debit-notes" />;
  if (page === "bills") return <DocumentList user={user} initialTab="purchase-bills" />;
  if (page === "receipts") return <DocumentList user={user} initialTab="receipts" />;
  if (page === "purchase-credit-notes") return <DocumentList user={user} initialTab="credit-notes" />;
  if (page === "purchase-debit-notes") return <DocumentList user={user} initialTab="debit-notes" />;
  if (page === "bank-money-in") return <BankPage user={user} mode="money-in" />;
  if (page === "bank-money-out") return <BankPage user={user} mode="money-out" />;
  if (page === "bank-transfers") return <BankPage user={user} mode="transfers" />;
  if (page === "bank-accounts") return <BankPage user={user} mode="accounts" />;
  if (page === "bank-feeds") return <BankPage user={user} mode="feeds" />;
  if (page === "accounting-deemed-payments") return <AccountingPage user={user} mode="deemed" />;
  if (page === "accounting-chart") return <AccountingPage user={user} mode="accounts" />;
  if (page === "accounting-journal-entries") return <AccountingPage user={user} mode="journals" />;
  if (page === "accounting-trial-balance") return <LedgerPage initialSection="trial" />;
  if (page === "accounting-contras") return <AccountingPage user={user} mode="contras" />;
  if (page === "company-profile") return <CompanyProfilePage user={user} />;
  if (page === "company-settings") return <CompanySettingsPage user={user} />;
  if (page === "contact-detail") return <ContactDetailPage user={user} />;
  if (page === "reconciliation") return <ReconciliationPage user={user} />;
  if (page === "users") return <UserManagement user={user} />;
  if (page === "settings") return <SettingsPage user={user} />;
  if (page === "help") return <HelpCenter />;
  if (id === "view-settings") return <SettingsPage user={user} />;
  if (id === "view-customers" || id === "view-suppliers" || id === "view-contact-matching") return <ContactsPage user={user} />;
  if (id?.startsWith("view-")) {
    return <WorkspaceSectionPage user={user} section={id.slice(5)} />;
  }
  switch (page) {
    case "dashboard": return <Dashboard user={user} />;
    case "documents": return id ? <DocumentReview user={user} documentId={id} /> : <DocumentList user={user} />;
    case "upload": return <UploadPage user={user} />;
    case "exceptions": return <ExceptionsPage user={user} />;
    case "contacts": return <ContactsPage user={user} />;
    case "records": return <RecordsPage user={user} initialRecordId={id} />;
    case "reports": return <AccountingReportsPage user={user} />;
    // Keep the legacy URL working while sending it to the actual report section.
    case "evaluation": {
      if (id === "effort-study") return <EffortStudyPage />;
      const area = path[0] || "workspace";
      return <RedirectTo href={`/${area}/reports#saic-evaluation`} />;
    }
    case "profile": return <ProfilePage user={user} />;
    default: return <WorkspaceSectionPage user={user} section={page ?? "workspace"} />;
  }
}
