"use client";
import Link from "next/link";
import { useState, type FormEvent } from "react";
import { Pencil, Plus } from "lucide-react";
import type { User, UserStatus } from "@/types";
import { useResource } from "@/features/common/hooks/use-resource";
import { getTenants, requestAccountLimit } from "@/lib/api/tenant.service";
import {
  changeUserStatus,
  getTenantUsers,
  inviteAccountant,
  resendInvitation,
  updateAccountantCredentials,
} from "@/lib/api/user.service";
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
import { WorkflowControls } from "@/features/common/workflow/workflow-controls";
export function UserManagement({
  user,
  tenantId,
}: {
  user: User;
  tenantId?: string;
}) {
  const admin = user.role === "SUPER_ADMIN";
  const canManage = user.role === "LOCAL_ADMIN";
  const { data, error, loading, refresh } = useResource(
    async () => ({
      users: await getTenantUsers(user, tenantId),
      tenants: user.role === "ACCOUNTANT" ? [] : await getTenants(user),
    }),
    `${user.id}:${tenantId}`,
  );
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [modal, setModal] = useState<"invite" | "limit">();
  const [confirm, setConfirm] = useState<{ user: User; status: UserStatus }>();
  const [view, setView] = useState<User>();
  const [edit, setEdit] = useState<User>();
  const [failure, setFailure] = useState("");
  const [toast, setToast] = useState("");
  const [busy, setBusy] = useState(false);
  if (loading) return <LoadingState />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;
  const tenant = data.tenants.find((t) => t.id === (tenantId ?? user.tenantId));
  const used = data.users.filter((u) =>
    ["ACTIVE", "INVITED"].includes(u.status),
  ).length;
  const full = !!tenant && used >= tenant.accountLimit;
  async function run(op: () => Promise<unknown>, message: string) {
    setBusy(true);
    try {
      await op();
      setToast(message);
      setFailure("");
      setModal(undefined);
      setConfirm(undefined);
      setEdit(undefined);
      await refresh();
    } catch (e) {
      setFailure((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    void run(
      () =>
        modal === "invite"
          ? inviteAccountant(
              user,
              String(f.get("name")),
              String(f.get("email")),
            )
          : requestAccountLimit(
              user,
              Number(f.get("limit")),
              String(f.get("reason")),
            ),
      modal === "invite"
        ? `Invitation sent to ${f.get("email")}.`
        : "Account limit request submitted to AccordAI.",
    );
  }
  return (
    <div className="professional-admin-page">
      <PageHeader
        title="User management"
        description={
          admin
            ? "Administrative account information and invitation status."
            : "Manage your company’s accountants and reserved seats."
        }
        actions={
            canManage && (
            <>
              <button
                className="btn"
                onClick={() => {
                  setModal("limit");
                  setFailure("");
                }}
              >
                Request more accounts
              </button>
              <button
                className="btn primary"
                disabled={full}
                onClick={() => {
                  setModal("invite");
                  setFailure("");
                }}
              >
                <Plus size={16} /> Invite accountant
              </button>
            </>
          )
        }
      />
      {tenant && (
        <Panel>
          <div className="panel-body">
            <AccountUsage used={used} limit={tenant.accountLimit} />
            {full && (
              <p className="warning-text">
                ACCOUNT LIMIT REACHED · Your organisation has reached its
                approved account limit of {tenant.accountLimit}.
              </p>
            )}
          </div>
        </Panel>
      )}
      {failure && !modal && !confirm && <ErrorState message={failure} />}
      <Panel>
        <div className="filter-bar">
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search name or company email…"
          />
          <Filter
            label="Statuses"
            value={status}
            onChange={setStatus}
            options={["ACTIVE", "INVITED", "DISABLED", "INVITATION_EXPIRED"]}
          />
        </div>
        <DataTable
          rows={data.users.filter(
            (u) =>
              (!status || u.status === status) &&
              `${u.name} ${u.email}`
                .toLowerCase()
                .includes(search.toLowerCase()),
          )}
          rowKey={(u) => u.id}
          columns={[
            {
              label: "Name",
              render: (u) => (
                <div className="company-cell">
                  <span className="avatar">
                    {u.name
                      .split(" ")
                      .map((n) => n[0])
                      .slice(0, 2)
                      .join("")}
                  </span>
                  <span>
                    <strong>{u.name}</strong>
                  </span>
                </div>
              ),
            },
            { label: "Company email", render: (u) => u.email },
            {
              label: "Role",
              render: (u) => (
                <span className="role-label">
                  {u.role.replaceAll("_", " ")}
                </span>
              ),
            },
            { label: "Status", render: (u) => <Badge>{u.status}</Badge> },
            {
              label: "Invited by",
              render: (u) => u.invitedBy,
            },
            {
              label: "Invitation date",
              render: (u) => formatDate(u.invitedAt),
            },
            { label: "Last login", render: (u) => formatDate(u.lastLogin) },
            {
              label: "Actions",
              render: (u) => (
                <div className="row-actions">
                  <button className="text-link" onClick={() => setView(u)}>
                    View
                  </button>
                  {canManage && u.role === "ACCOUNTANT" && (
                    <>
                      <button className="text-link" onClick={() => { setEdit(u); setFailure(""); }}>
                        <Pencil size={13} /> Edit
                      </button>
                      {u.status === "ACTIVE" ? (
                        <button
                          className="text-link danger-text"
                          onClick={() => {
                            setConfirm({ user: u, status: "DISABLED" });
                            setFailure("");
                          }}
                        >
                          Disable
                        </button>
                      ) : u.status === "INVITED" ? (
                        <>
                          <button
                            className="text-link"
                            onClick={() =>
                              run(
                                () => resendInvitation(user, u.id),
                                "Invitation resent.",
                              )
                            }
                          >
                            Resend
                          </button>
                          <button
                            className="text-link danger-text"
                            onClick={() => {
                              setConfirm({
                                user: u,
                                status: "INVITATION_EXPIRED",
                              });
                              setFailure("");
                            }}
                          >
                            Cancel
                          </button>
                        </>
                      ) : (
                        <button
                          className="text-link"
                          onClick={() =>
                            run(
                              () =>
                                changeUserStatus(
                                  user,
                                  u.id,
                                  u.status === "DISABLED"
                                    ? "ACTIVE"
                                    : "INVITED",
                                ),
                              "Account updated.",
                            )
                          }
                        >
                          {u.status === "DISABLED" ? "Reactivate" : "Reinvite"}
                        </button>
                      )}
                    </>
                  )}
                </div>
              ),
            },
          ]}
        />
      </Panel>
      <WorkflowControls user={user} />
      {modal && (
        <Modal
          title={
            modal === "invite" ? "Invite accountant" : "Request more accounts"
          }
          onClose={() => setModal(undefined)}
        >
          <form className="panel-body" onSubmit={submit}>
            {failure && <ErrorState message={failure} />}
            {modal === "invite" ? (
              <>
                <Field label="Full name *">
                  <input name="name" required />
                </Field>
                <Field label="Company email *">
                  <input name="email" type="email" required />
                </Field>
                <div className="info-box">
                  <Badge>ACCOUNTANT</Badge>
                  <span>
                    Company email is the login ID. No password is created by the
                    administrator.
                  </span>
                </div>
                <AccountUsage used={used} limit={tenant?.accountLimit ?? 1} />
              </>
            ) : (
              <>
                <p>Current account limit: {tenant?.accountLimit}</p>
                <Field label="Requested account limit *">
                  <input
                    name="limit"
                    required
                    type="number"
                    min={(tenant?.accountLimit ?? 0) + 1}
                    defaultValue={(tenant?.accountLimit ?? 0) + 5}
                  />
                </Field>
                <Field label="Reason *">
                  <textarea name="reason" required />
                </Field>
              </>
            )}
            <div className="modal-actions">
              <button
                className="btn"
                type="button"
                onClick={() => setModal(undefined)}
              >
                Cancel
              </button>
              <button className="btn primary" disabled={busy}>
                {modal === "invite" ? "Send invitation" : "Submit request"}
              </button>
            </div>
          </form>
        </Modal>
      )}
      {confirm && (
        <Modal
          title={`${confirm.status === "DISABLED" ? "Disable account" : "Cancel invitation"}?`}
          onClose={() => setConfirm(undefined)}
        >
          <div className="panel-body">
            <p>
              {confirm.user.email} will lose access or their reserved
              invitation. This can be reversed from user management.
            </p>
            {failure && <ErrorState message={failure} />}
            <div className="modal-actions">
              <button className="btn" onClick={() => setConfirm(undefined)}>
                Keep account
              </button>
              <button
                className="btn danger"
                disabled={busy}
                onClick={() =>
                  run(
                    () =>
                      changeUserStatus(user, confirm.user.id, confirm.status),
                    "Account updated.",
                  )
                }
              >
                Confirm
              </button>
            </div>
          </div>
        </Modal>
      )}
      {edit && (
        <Modal title={`Edit ${edit.name}`} onClose={() => setEdit(undefined)}>
          <form className="panel-body" onSubmit={(event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            void run(
              () => updateAccountantCredentials(user, edit.id, String(form.get("name")), String(form.get("password"))),
              "Accountant profile and credentials updated.",
            );
          }}>
            {failure && <ErrorState message={failure} />}
            <Field label="Accountant name *">
              <input name="name" defaultValue={edit.name} required minLength={2} />
            </Field>
            <Field label="Company email">
              <input value={edit.email} disabled />
            </Field>
            <Field label="New password" hint="Leave blank to keep the existing password.">
              <input name="password" type="password" autoComplete="new-password" minLength={8} />
            </Field>
            <div className="modal-actions">
              <button className="btn" type="button" onClick={() => setEdit(undefined)}>Cancel</button>
              <button className="btn primary" disabled={busy}>Save accountant</button>
            </div>
          </form>
        </Modal>
      )}
      {view && (
        <Modal title={view.name} onClose={() => setView(undefined)}>
          <div className="panel-body detail-list">
            <div>
              <span>Company email</span>
              <strong>{view.email}</strong>
            </div>
            <div>
              <span>Role</span>
              <Badge>{view.role}</Badge>
            </div>
            <div>
              <span>Status</span>
              <Badge>{view.status}</Badge>
            </div>
            <div>
              <span>Invited by</span>
              <strong>{view.invitedBy}</strong>
            </div>
            <div>
              <span>Invited</span>
              <strong>{formatDate(view.invitedAt)}</strong>
            </div>
            {view.status === "INVITED" && (
              <Link href={`/activate?user=${view.id}`} className="btn primary">
                Open activation
              </Link>
            )}
          </div>
        </Modal>
      )}
      <Toast message={toast} />
    </div>
  );
}
