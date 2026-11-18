"use client";
import { useState } from "react";
import { useSearchParams } from "next/navigation";
import type { User, ValidationRules } from "@/types";
import { useResource } from "@/features/common/hooks/use-resource";
import {
  getValidationRules,
  updateValidationRules,
} from "@/lib/api/document.service";
import { getSettings, saveSettings } from "@/lib/api/settings.service";
import { resetPassword } from "@/lib/api/auth.service";
import { CurrencySettingsPage } from "@/features/common/settings/currency-pages";
import {
  ErrorState,
  Field,
  LoadingState,
  PageHeader,
  Panel,
  Toast,
} from "@/components/common/ui";
export function RulesPage({ user }: { user: User }) {
  const { data, error, loading, refresh } = useResource(
    () => getValidationRules(user),
    user.id,
  );
  const [edited, setEdited] = useState<ValidationRules>();
  const [message, setMessage] = useState("");
  const [failure, setFailure] = useState("");
  if (loading) return <LoadingState />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;
  const rules = edited ?? data;
  return (
    <div className="professional-admin-page">
      <PageHeader
        eyebrow="COMPANY CONFIGURATION"
        title="Validation rules"
        description="Company-specific checks applied when documents are revalidated."
      />
      <Panel title="Invoice validation">
        <form
          className="panel-body"
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await updateValidationRules(user, rules);
              setMessage(
                "Company validation rules saved. Revalidate documents to apply.",
              );
              setFailure("");
            } catch (e) {
              setFailure((e as Error).message);
            }
          }}
        >
          {failure && <ErrorState message={failure} />}
          {(
            [
              ["supplier", "Supplier Required"],
              ["invoice", "Invoice Number Required"],
              ["date", "Invoice Date Required"],
              ["total", "Total Calculation"],
              ["duplicate", "Duplicate Invoice Detection"],
            ] as const
          ).map(([key, label]) => (
            <label className="rule-row" key={key}>
              <span>
                <strong>{label}</strong>
                <small>
                  {rules[key] ? "Enabled" : "Disabled"} for this company
                </small>
              </span>
              <input
                className="switch"
                role="switch"
                type="checkbox"
                checked={rules[key]}
                onChange={(e) =>
                  setEdited({ ...rules, [key]: e.target.checked })
                }
              />
            </label>
          ))}
          <Field label="Low confidence threshold (%)">
            <input
              type="number"
              required
              min="0"
              max="100"
              value={rules.threshold}
              onChange={(e) =>
                setEdited({ ...rules, threshold: Number(e.target.value) })
              }
            />
          </Field>
          <button className="btn primary">Save company rules</button>
        </form>
      </Panel>
      <Toast message={message} />
    </div>
  );
}
export function SettingsPage({
  user,
  profile = false,
}: {
  user: User;
  profile?: boolean;
}) {
  const searchParams = useSearchParams();
  const controlView = searchParams.get("view");
  const { data, error, loading, refresh } = useResource(
    () => getSettings(user),
    user.id,
  );
  const [message, setMessage] = useState("");
  const [failure, setFailure] = useState("");
  if (!profile && (controlView === "currencies" || controlView === "custom-rates")) {
    return <CurrencySettingsPage user={user} />;
  }
  if (loading) return <LoadingState />;
  if (error || !data) return <ErrorState message={error} retry={refresh} />;
  return (
    <>
      <PageHeader
        title={
          profile
            ? "My profile"
            : user.role === "SUPER_ADMIN"
              ? "System settings"
              : "Company settings"
        }
        description="Manage workspace behaviour, notifications, and document intake preferences."
      />
      <div className="two-col">
        <Panel title="Account overview" subtitle="Your signed-in workspace identity">
          <div className="panel-body detail-list">
            {[
              ["Name", user.name],
              ["Company email", user.email],
              ["Role", user.role.replaceAll("_", " ")],
              ["Status", user.status],
            ].map(([k, v]) => (
              <div key={k}>
                <span>{k}</span>
                <strong>{v}</strong>
              </div>
            ))}
          </div>
        </Panel>
        {!profile && (
          <Panel title="Workspace preferences" subtitle="These settings apply to your company workspace">
            <form
              className="panel-body settings-preferences-form"
              onSubmit={async (e) => {
                e.preventDefault();
                const form = new FormData(e.currentTarget);
                try {
                  await saveSettings(user, {
                    timezone: String(form.get("timezone")),
                    notifications: String(form.get("notifications")),
                    sessionTimeoutMinutes: Number(form.get("sessionTimeoutMinutes")),
                    defaultDocumentView: String(form.get("defaultDocumentView")) as "all" | "exceptions" | "review",
                    emailAssignmentNotifications: form.get("emailAssignmentNotifications") === "on",
                    processingStatusNotifications: form.get("processingStatusNotifications") === "on",
                    uploadMaxSizeMb: data.uploadMaxSizeMb,
                    allowCameraUploads: form.get("allowCameraUploads") === "on",
                  });
                  setMessage("Workspace preferences saved.");
                } catch (e) {
                  setFailure((e as Error).message);
                }
              }}
            >
              {failure && <ErrorState message={failure} />}
              <Field label="Preferred timezone">
                <select name="timezone" defaultValue={data.timezone}>
                  <option>Asia/Kuala_Lumpur</option>
                  <option>Asia/Singapore</option>
                  <option>UTC</option>
                </select>
              </Field>
              <Field label="Notification preference (no email is sent)">
                <select name="notifications" defaultValue={data.notifications}>
                  <option value="enabled">Enabled</option>
                  <option value="disabled">Disabled</option>
                </select>
              </Field>
              <Field label="Default document view" hint="Choose the first list shown when you open Documents.">
                <select name="defaultDocumentView" defaultValue={data.defaultDocumentView}>
                  <option value="all">All documents</option><option value="exceptions">Exceptions</option><option value="review">My review queue</option>
                </select>
              </Field>
              <Field label="Session timeout" hint="Automatically end inactive sessions after this period.">
                <select name="sessionTimeoutMinutes" defaultValue={String(data.sessionTimeoutMinutes)}>
                  <option value="480">8 hours</option><option value="15">15 minutes</option><option value="30">30 minutes</option><option value="60">60 minutes</option>
                </select>
              </Field>
              <div className="settings-toggle-list">
                <label><input name="emailAssignmentNotifications" type="checkbox" defaultChecked={data.emailAssignmentNotifications} /> <span><strong>Assignment notifications</strong><small>Show alerts when documents are assigned to you.</small></span></label>
                <label><input name="processingStatusNotifications" type="checkbox" defaultChecked={data.processingStatusNotifications} /> <span><strong>Processing updates</strong><small>Show status changes as documents move through extraction.</small></span></label>
                <label><input name="allowCameraUploads" type="checkbox" defaultChecked={data.allowCameraUploads} /> <span><strong>Camera uploads</strong><small>Allow document capture from supported devices.</small></span></label>
              </div>
              <button className="btn primary">Save preferences</button>
            </form>
          </Panel>
        )}
      </div>
      {profile && (
        <Panel title="Change my password">
          <form
            className="panel-body"
            onSubmit={async (e) => {
              e.preventDefault();
              const form = new FormData(e.currentTarget);
              const nextPassword = String(form.get("new-password"));
              try {
                await resetPassword(
                  nextPassword,
                  String(form.get("confirm-password")),
                );
                setMessage("Password change recorded successfully.");
                e.currentTarget.reset();
              } catch (e) {
                setFailure((e as Error).message);
              }
            }}
          >
            {failure && <ErrorState message={failure} />}
            <Field
              label="New password *"
              hint="Use 8+ characters with uppercase, lowercase, number, and symbol."
            >
              <input
                name="new-password"
                type="password"
                required
                autoComplete="new-password"
              />
            </Field>
            <Field label="Confirm new password *">
              <input
                name="confirm-password"
                type="password"
                required
                autoComplete="new-password"
              />
            </Field>
            <button className="btn primary">Change password</button>
          </form>
        </Panel>
      )}
      <Toast message={message} />
    </>
  );
}
