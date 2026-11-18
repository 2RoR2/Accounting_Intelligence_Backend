import { Dashboard } from "@/features/common/dashboard/dashboard";
import { AuditPage, PerformancePage, ProcessingPage } from "@/features/common/monitoring/monitoring";
import { RecordsPage } from "@/features/common/records/records";
import { SettingsPage } from "@/features/common/settings/settings";
import { ProfilePage } from "@/features/common/settings/profile-page";
import { UserManagement } from "@/features/common/users/user-management";
import { Approvals, Tenants } from "@/features/superadmin/tenants/management";
import type { User } from "@/types";

export function SuperAdminRoutes({ path, user }: { path: string[]; user: User }) {
  const [, page, id] = path;
  switch (page) {
    case "dashboard": return <Dashboard user={user} />;
    case "approvals": return <Approvals user={user} />;
    case "tenants": return <Tenants user={user} tenantId={id} />;
    case "users": return <UserManagement user={user} />;
    case "processing": return <ProcessingPage user={user} />;
    case "performance": return <PerformancePage user={user} />;
    case "reports": return <PerformancePage user={user} />;
    case "records": return <RecordsPage user={user} tenantId={id} />;
    case "audit": return <AuditPage user={user} />;
    case "profile": return <ProfilePage user={user} />;
    default: return <SettingsPage user={user} />;
  }
}
