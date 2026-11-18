"use client";
import Link from "next/link";
import { AppShell } from "@/components/common/layout/app-shell";
import { AccessDenied } from "@/components/common/errors/access-denied";
import { ErrorState, LoadingState } from "@/components/common/ui";
import { useResource } from "@/features/common/hooks/use-resource";
import { AUTH_IDENTITY_CONTRACT, getCurrentUser } from "@/lib/api/auth.service";
import { isAllowedWorkspaceRoute, isPublicRoute } from "@/lib/constants/routes";
import { roleHome } from "@/lib/permissions";
import { PublicPage } from "@/features/auth/public-pages";
import { UserRoutes } from "@/features/users/route-content";
import { ForcePasswordChange } from "@/features/users/force-password";
import { LocalAdminRoutes } from "@/features/localadmin/route-content";
import { SuperAdminRoutes } from "@/features/superadmin/route-content";
import { AlertTriangle, LogIn } from "lucide-react";
export function Application({ path }: { path: string[] }) {
  const publicRoute = isPublicRoute(path);
  const {
    data: user,
    error,
    loading,
    refresh,
  } = useResource(
    publicRoute ? () => Promise.resolve(null) : getCurrentUser,
    `${AUTH_IDENTITY_CONTRACT}:${path.join("/")}`,
  );
  const [area] = path;
  if (publicRoute) return <PublicPage page={area} />;
  if (loading) return <LoadingState />;
  if (error) return <main className="access-gate"><ErrorState message={error} retry={refresh} /></main>;
  if (!user)
    return (
      <main className="access-gate">
        <div className="access-gate-illustration" aria-hidden="true">
          <div className="access-gate-window"><span /><span /><span /><i /><i /><i /></div>
          <div className="access-gate-alert"><AlertTriangle size={44} /></div>
        </div>
        <section className="access-gate-message" role="alert">
          <h1>We couldn’t complete that request.</h1>
          <p>{error || "Sign in with your account to enter your workspace."}</p>
        </section>
        <Link className="btn primary access-gate-login" href="/login" onMouseEnter={(event) => { event.currentTarget.style.background = "#eaf5ff"; event.currentTarget.style.color = "#2a3a4a"; }} onMouseLeave={(event) => { event.currentTarget.style.background = "#2b3e52"; event.currentTarget.style.color = "#f6f1e8"; }}>
          <LogIn size={20} /> Continue to login
        </Link>
      </main>
    );
  if (!isAllowedWorkspaceRoute(path, user.role))
    return <AccessDenied homeHref={roleHome(user.role)} />;
  if (user.role === "ACCOUNTANT" && user.mustChangePassword !== false)
    return <ForcePasswordChange user={user} />;
  return (
    <AppShell user={user}>
      {user.role === "SUPER_ADMIN" ? (
        <SuperAdminRoutes path={path} user={user} />
      ) : user.role === "LOCAL_ADMIN" ? (
        <LocalAdminRoutes path={path} user={user} />
      ) : (
        <UserRoutes path={path} user={user} />
      )}
    </AppShell>
  );
}
