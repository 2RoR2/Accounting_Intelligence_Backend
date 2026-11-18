"use client";
import Link from "next/link";
import { useState } from "react";
import { ArrowRight, Building2, Plus } from "lucide-react";
import type { CompanyRequest, Tenant, User } from "@/types";
import { useResource } from "@/features/common/hooks/use-resource";
import {
  approveCompanyRequest,
  getAccountLimitRequests,
  getCompanyRequests,
  getTenants,
  rejectCompanyRequest,
  reviewAccountLimit,
  setTenantStatus,
  updateAccountLimit,
} from "@/lib/api/tenant.service";
import { getTenantUsers } from "@/lib/api/user.service";
import { getDocuments } from "@/lib/api/document.service";
import { getAuditLogs } from "@/lib/api/audit.service";
import { approveMfaRecovery, getMfaRecoveryRequests, type MfaRecoveryRequest } from "@/lib/api/auth.service";
import {
  AccountUsage,
  Badge,
  DataTable,
  ErrorState,
  Field,
  Filter,
  LoadingState,
  Modal,
  PageHeader,
  Panel,
  SearchInput,
  Toast,
  formatDate,
} from "@/components/common/ui";
import { UserManagement } from "@/features/common/users/user-management";
import { ProcessingPage, AuditPage } from "@/features/common/monitoring/monitoring";
import { RecordsPage } from "@/features/common/records/records";
export function Approvals({ user }: { user: User }) {
  const { data, error, loading, refresh } = useResource(
    async () => ({
      requests: await getCompanyRequests(user),
      limits: await getAccountLimitRequests(user),
      tenants: await getTenants(user),
      mfaRecovery: await getMfaRecoveryRequests(),
    }),
    user.id,
  );
  const [review, setReview] = useState<CompanyRequest>();
  const [mode, setMode] = useState("review");
  const [limit, setLimit] = useState(10);
  const [reason, setReason] = useState("");
  const [failure, setFailure] = useState("");
  const [toast, setToast] = useState("");
  const [status, setStatus] = useState("PENDING");
  const [search, setSearch] = useState("");
  const [limitReview, setLimitReview] = useState<{
    id: string;
    approve: boolean;
  }>();
  const [busy, setBusy] = useState(false);
  async function act(operation: () => Promise<unknown>, message: string) {
    setBusy(true);
    try {
      await operation();
      setReview(undefined);
      setLimitReview(undefined);
      setToast(message);
      setFailure("");
      await refresh();
    } catch (e) {
      setFailure((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  if (loading) return <LoadingState />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;
  return (
    <>
      <PageHeader
        eyebrow="PLATFORM ADMINISTRATION"
        title="Pending approvals"
        description="Review company access and account limit requests."
      />
      <Panel>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">SECURITY ADMINISTRATION</p>
            <h2>MFA recovery requests</h2>
            <p>Approve a lost-authenticator request to clear the old TOTP key and require fresh enrollment at the next login.</p>
          </div>
        </div>
        {data.mfaRecovery.length === 0 ? <p className="muted">No MFA recovery requests.</p> : (
          <DataTable
            columns={[
              { label: "User", render: (item: MfaRecoveryRequest) => `${item.target_name} (${item.target_email})` },
              { label: "Reason", render: (item: MfaRecoveryRequest) => item.reason },
              { label: "Status", render: (item: MfaRecoveryRequest) => <Badge>{item.status}</Badge> },
              { label: "Action", render: (item: MfaRecoveryRequest) => item.status === "pending" ? <button className="btn primary" disabled={busy} onClick={() => act(() => approveMfaRecovery(item.id), "MFA recovery approved. The user will enroll a new authenticator at next login.")}>Approve recovery</button> : <span className="muted">Reviewed</span> },
            ]}
            rows={data.mfaRecovery}
            rowKey={(item) => item.id}
          />
        )}
      </Panel>
      <Panel>
        <div className="filter-bar">
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search company or contact…"
          />
          <Filter
            label="Statuses"
            value={status}
            onChange={setStatus}
            options={["PENDING", "APPROVED", "REJECTED"]}
          />
        </div>
        <DataTable
          rows={data.requests.filter(
            (r) =>
              (!status || r.status === status) &&
              `${r.company} ${r.registration} ${r.contact} ${r.email}`
                .toLowerCase()
                .includes(search.toLowerCase()),
          )}
          rowKey={(r) => r.id}
          columns={[
            {
              label: "Company",
              render: (r) => <strong>{r.company}</strong>,
            },
            { label: "Registration number", render: (r) => r.registration },
            {
              label: "Contact person",
              render: (r) => r.contact,
            },
            { label: "Company email", render: (r) => r.email },
            { label: "Accounts", render: (r) => r.accounts },
            { label: "Submitted", render: (r) => formatDate(r.submittedAt) },
            { label: "Status", render: (r) => <Badge>{r.status}</Badge> },
            {
              label: "Action",
              render: (r) => (
                <button
                  className="btn small"
                  onClick={() => {
                    setReview(r);
                    setLimit(r.accounts);
                    setReason("");
                    setFailure("");
                    setMode("review");
                  }}
                >
                  Review <ArrowRight size={14} />
                </button>
              ),
            },
          ]}
        />
      </Panel>
      <Panel
        title="Account limit requests"
        subtitle="Seat increases requested by company administrators"
      >
        <DataTable
          rows={data.limits}
          rowKey={(r) => r.id}
          columns={[
            {
              label: "Company",
              render: (r) =>
                data.tenants.find((t) => t.id === r.tenantId)?.name,
            },
            {
              label: "Current → requested",
              render: (r) => `${r.current} → ${r.requested}`,
            },
            { label: "Reason", render: (r) => r.reason },
            { label: "Status", render: (r) => <Badge>{r.status}</Badge> },
            {
              label: "Actions",
              render: (r) =>
                r.status === "PENDING" ? (
                  <div className="actions">
                    <button
                      className="btn small"
                      onClick={() => {
                        setLimitReview({ id: r.id, approve: false });
                        setReason("");
                        setFailure("");
                      }}
                    >
                      Reject
                    </button>
                    <button
                      className="btn primary small"
                      onClick={() => {
                        setLimitReview({ id: r.id, approve: true });
                        setReason("Approved additional capacity.");
                        setFailure("");
                      }}
                    >
                      Approve
                    </button>
                  </div>
                ) : (
                  "Reviewed"
                ),
            },
          ]}
        />
      </Panel>
      {review && (
        <Modal
          title={
            mode === "review"
              ? "Review company request"
              : mode === "approve"
                ? "Approve company"
                : "Reject company"
          }
          onClose={() => setReview(undefined)}
        >
          <div className="panel-body">
            <Badge>{review.status}</Badge>
            <h3 className="dialog-title">{review.company}</h3>
            <div className="detail-list">
              {[
                ["Registration", review.registration],
                ["Manager", review.contact],
                ["Company email", review.email],
                ["Phone", review.phone || "—"],
                ["Requested accounts", String(review.accounts)],
                ["Submitted", formatDate(review.submittedAt)],
                [
                  "Employee emails",
                  review.employees.join(", ") || "None provided",
                ],
                ["Notes", review.notes || "None provided"],
              ].map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
            {review.rejectionReason && (
              <p>Rejection reason: {review.rejectionReason}</p>
            )}
            {failure && <ErrorState message={failure} />}
            {mode === "approve" && (
              <Field label="Approved account limit *">
                <input
                  type="number"
                  min="1"
                  value={limit}
                  onChange={(e) => setLimit(Number(e.target.value))}
                />
              </Field>
            )}
            {mode === "reject" && (
              <Field label="Rejection reason *">
                <textarea
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </Field>
            )}
            {review.status === "PENDING" && (
              <div className="modal-actions">
                {mode === "review" ? (
                  <>
                    <button
                      className="btn danger"
                      onClick={() => setMode("reject")}
                    >
                      Reject
                    </button>
                    <button
                      className="btn primary"
                      onClick={() => setMode("approve")}
                    >
                      Approve company
                    </button>
                  </>
                ) : (
                  <>
                    <button className="btn" onClick={() => setMode("review")}>
                      Back
                    </button>
                    <button
                      disabled={busy}
                      className={`btn ${mode === "approve" ? "primary" : "danger"}`}
                      onClick={() =>
                        act(
                          () =>
                            mode === "approve"
                              ? approveCompanyRequest(user, review.id, limit)
                              : rejectCompanyRequest(user, review.id, reason),
                          mode === "approve"
                            ? `${review.company} approved. Local Administrator activation invitation created for ${review.email}.`
                            : "Company request rejected.",
                        )
                      }
                    >
                      Confirm {mode === "approve" ? "approval" : "rejection"}
                    </button>
                  </>
                )}
              </div>
            )}
          </div>
        </Modal>
      )}
      {limitReview && (
        <Modal
          title={`${limitReview.approve ? "Approve" : "Reject"} account limit request`}
          onClose={() => setLimitReview(undefined)}
        >
          <div className="panel-body">
            {failure && <ErrorState message={failure} />}
            <Field label="Reason *">
              <textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </Field>
            <button
              className="btn primary"
              disabled={busy}
              onClick={() =>
                act(
                  () =>
                    reviewAccountLimit(
                      user,
                      limitReview.id,
                      limitReview.approve,
                      reason,
                    ),
                  "Account limit request reviewed.",
                )
              }
            >
              Confirm decision
            </button>
          </div>
        </Modal>
      )}
      <Toast message={toast} />
    </>
  );
}
export function Tenants({ user, tenantId }: { user: User; tenantId?: string }) {
  const { data, error, loading, refresh } = useResource(
    async () => ({
      tenants: await getTenants(user),
      users: await getTenantUsers(user),
      docs: await getDocuments(user),
      audit: await getAuditLogs(user),
    }),
    user.id,
  );
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [usageFilter, setUsageFilter] = useState("");
  const [tab, setTab] = useState("Overview");
  const [modal, setModal] = useState<"limit" | "suspend">();
  const [limit, setLimit] = useState(10);
  const [reason, setReason] = useState("");
  const [failure, setFailure] = useState("");
  const [toast, setToast] = useState("");
  if (loading) return <LoadingState />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;
  const used = (id: string) =>
    data.users.filter(
      (u) => u.tenantId === id && ["ACTIVE", "INVITED"].includes(u.status),
    ).length;
  const tenant = data.tenants.find((t) => t.id === tenantId);
  async function save(t: Tenant) {
    try {
      if (modal === "limit")
        await updateAccountLimit(user, t.id, limit, reason);
      else await setTenantStatus(user, t.id);
      setModal(undefined);
      setToast("Company updated successfully.");
      await refresh();
    } catch (e) {
      setFailure((e as Error).message);
    }
  }
  if (tenantId && !tenant) return <ErrorState message="Company not found." />;
  if (tenant)
    return (
      <>
        <PageHeader
          eyebrow={`TENANT MANAGEMENT / ${tenant.id}`}
          title={tenant.name}
          description={`Registered ${formatDate(tenant.createdAt)} · ${tenant.registration}`}
          actions={
            <>
              <Badge>{tenant.status}</Badge>
              <button
                className="btn"
                onClick={() => {
                  setModal("limit");
                  setLimit(tenant.accountLimit);
                  setReason("");
                  setFailure("");
                }}
              >
                Change account limit
              </button>
              <button
                className="btn danger"
                onClick={() => {
                  setModal("suspend");
                  setFailure("");
                }}
              >
                {tenant.status === "ACTIVE" ? "Suspend" : "Reactivate"} tenant
              </button>
            </>
          }
        />
        <div className="tabs">
          {["Overview", "Users", "Processing", "Records", "Audit Logs"].map(
            (t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={tab === t ? "active" : ""}
              >
                {t}
              </button>
            ),
          )}
        </div>
        {tab === "Overview" && (
          <div className="two-col">
            <Panel title="Company information">
              <div className="panel-body detail-list">
                {[
                  ["Company", tenant.name],
                  ["Registration", tenant.registration],
                  ["Tenant ID", tenant.id],
                  [
                    "Local administrator",
                    data.users.find(
                      (u) =>
                        u.tenantId === tenant.id && u.role === "LOCAL_ADMIN",
                    )?.name ?? "Pending invitation",
                  ],
                  ["Company email", tenant.adminEmail],
                  ["Created", formatDate(tenant.createdAt)],
                ].map(([k, v]) => (
                  <div key={k}>
                    <span>{k}</span>
                    <strong>{v}</strong>
                  </div>
                ))}
              </div>
            </Panel>
            <Panel title="Account usage">
              <div className="panel-body">
                <AccountUsage
                  used={used(tenant.id)}
                  limit={tenant.accountLimit}
                />
                <div className="detail-list">
                  <div>
                    <span>Active accounts</span>
                    <strong>
                      {
                        data.users.filter(
                          (u) =>
                            u.tenantId === tenant.id && u.status === "ACTIVE",
                        ).length
                      }
                    </strong>
                  </div>
                  <div>
                    <span>Pending invitations</span>
                    <strong>
                      {
                        data.users.filter(
                          (u) =>
                            u.tenantId === tenant.id && u.status === "INVITED",
                        ).length
                      }
                    </strong>
                  </div>
                  <div>
                    <span>Documents</span>
                    <strong>
                      {data.docs.filter((d) => d.tenantId === tenant.id).length}
                    </strong>
                  </div>
                </div>
              </div>
            </Panel>
          </div>
        )}
        {tab === "Users" && <UserManagement user={user} tenantId={tenant.id} />}
        {tab === "Processing" && (
          <ProcessingPage user={user} tenantId={tenant.id} />
        )}
        {tab === "Records" && <RecordsPage user={user} tenantId={tenant.id} />}
        {tab === "Audit Logs" && <AuditPage user={user} tenantId={tenant.id} />}
        {modal && (
          <Modal
            title={
              modal === "limit"
                ? "Change account limit"
                : `${tenant.status === "ACTIVE" ? "Suspend" : "Reactivate"} ${tenant.name}?`
            }
            onClose={() => setModal(undefined)}
          >
            <div className="panel-body">
              {failure && <ErrorState message={failure} />}
              {modal === "limit" ? (
                <>
                  <p>
                    Current limit: {tenant.accountLimit} · {used(tenant.id)}{" "}
                    used / reserved
                  </p>
                  <Field label="New limit *">
                    <input
                      type="number"
                      min={used(tenant.id)}
                      value={limit}
                      onChange={(e) => setLimit(Number(e.target.value))}
                    />
                  </Field>
                  <Field label="Reason *">
                    <textarea
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                    />
                  </Field>
                </>
              ) : (
                <p>
                  {tenant.status === "ACTIVE"
                    ? "Company users will be unable to access their workspace until reactivated."
                    : "Company users will be able to sign in again."}
                </p>
              )}
              <div className="modal-actions">
                <button className="btn" onClick={() => setModal(undefined)}>
                  Cancel
                </button>
                <button className="btn primary" onClick={() => save(tenant)}>
                  Confirm update
                </button>
              </div>
            </div>
          </Modal>
        )}
        <Toast message={toast} />
      </>
    );
  return (
    <>
      <PageHeader
        eyebrow="PLATFORM ADMINISTRATION"
        title="Tenant management"
        description="Manage companies, account capacity, and platform access."
        actions={
          <Link href="/admin/approvals" className="btn primary">
            <Plus size={16} /> Review requests
          </Link>
        }
      />
      <Panel>
        <div className="filter-bar">
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search company…"
          />
          <Filter
            label="Statuses"
            value={status}
            onChange={setStatus}
            options={["ACTIVE", "SUSPENDED"]}
          />
          <Filter
            label="Account usage"
            value={usageFilter}
            onChange={setUsageFilter}
            options={["AVAILABLE", "FULL"]}
          />
        </div>
        <DataTable
          rows={data.tenants.filter(
            (t) =>
              t.name.toLowerCase().includes(search.toLowerCase()) &&
              (!status || t.status === status) &&
              (!usageFilter ||
                (usageFilter === "FULL"
                  ? used(t.id) >= t.accountLimit
                  : used(t.id) < t.accountLimit)),
          )}
          rowKey={(t) => t.id}
          columns={[
            {
              label: "Company",
              render: (t) => (
                <Link href={`/admin/tenants/${t.id}`} className="company-cell">
                  <span className="company-avatar">
                    <Building2 size={18} />
                  </span>
                  <span>
                    <strong>{t.name}</strong>
                  </span>
                </Link>
              ),
            },
            { label: "Tenant ID", render: (t) => t.id },
            {
              label: "Local admin",
              render: (t) =>
                data.users.find(
                  (u) => u.tenantId === t.id && u.role === "LOCAL_ADMIN",
                )?.name,
            },
            { label: "Admin email", render: (t) => t.adminEmail },
            {
              label: "Users / limit",
              render: (t) => `${used(t.id)} / ${t.accountLimit}`,
            },
            {
              label: "Documents",
              render: (t) =>
                data.docs.filter((d) => d.tenantId === t.id).length,
            },
            { label: "Status", render: (t) => <Badge>{t.status}</Badge> },
            { label: "Created", render: (t) => formatDate(t.createdAt) },
            {
              label: "Action",
              render: (t) => (
                <Link href={`/admin/tenants/${t.id}`} className="text-link">
                  View <ArrowRight size={14} />
                </Link>
              ),
            },
          ]}
        />
      </Panel>
    </>
  );
}
