"use client";

import { useState, type ChangeEvent, type FormEvent } from "react";
import { Camera, UserRound } from "lucide-react";
import type { User } from "@/types";
import { updateOwnProfile } from "@/lib/api/user.service";
import { changeOwnPassword, resetPassword, generateBackupCodes } from "@/lib/api/auth.service";
import { ErrorState, Field, PageHeader, Panel, Toast } from "@/components/common/ui";

export function ProfilePage({ user }: { user: User }) {
  const [name, setName] = useState(user.name);
  const [avatar, setAvatar] = useState(user.avatar);
  const [message, setMessage] = useState("");
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  const [backupCodes, setBackupCodes] = useState<string[]>([]);

  function chooseAvatar(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!file.type.startsWith("image/") || file.size > 1024 * 1024) {
      setFailure("Choose a JPG, PNG, or WebP image smaller than 1 MB.");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => setAvatar(String(reader.result));
    reader.readAsDataURL(file);
  }

  async function saveProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    try {
      await updateOwnProfile(user, name, avatar);
      setFailure("");
      setMessage("Your profile has been updated.");
    } catch (error) {
      setFailure((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="My profile" description="Manage your personal details and profile image." />
      <div className="profile-layout">
        <Panel title="Profile details">
          <form className="panel-body profile-form" onSubmit={saveProfile}>
            {failure && <ErrorState message={failure} />}
            <div className="avatar-editor">
              <span className="profile-avatar-large">
                {avatar ? (
                  // Profile avatar is stored as a data: URL — next/image cannot optimize it.
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={avatar} alt="Profile" />
                ) : (
                  <UserRound size={38} />
                )}
              </span>
              <div>
                <strong>Profile image</strong>
                <p>JPG, PNG, or WebP · maximum 1 MB</p>
                <label className="btn small">
                  <Camera size={15} /> Choose image
                  <input type="file" accept="image/png,image/jpeg,image/webp" onChange={chooseAvatar} hidden />
                </label>
                {avatar && <button className="text-link danger-text" type="button" onClick={() => setAvatar(undefined)}>Remove</button>}
              </div>
            </div>
            <Field label="Display name *">
              <input value={name} onChange={(event) => setName(event.target.value)} required minLength={2} />
            </Field>
            <Field label="Company email">
              <input value={user.email} disabled />
            </Field>
            <Field label="Role">
              <input value={user.role.replaceAll("_", " ")} disabled />
            </Field>
            <button className="btn primary" disabled={busy}>{busy ? "Saving…" : "Save profile"}</button>
          </form>
        </Panel>
        {user.role === "ACCOUNTANT" ? (
          <Panel title="Change password">
            <form
              className="panel-body"
              onSubmit={async (event) => {
                event.preventDefault();
                const form = new FormData(event.currentTarget);
                try {
                  await changeOwnPassword(
                    user,
                    String(form.get("password")),
                    String(form.get("confirmation")),
                  );
                  setMessage("Password updated. You will not be asked again at sign-in.");
                  setFailure("");
                  event.currentTarget.reset();
                } catch (error) {
                  setFailure((error as Error).message);
                }
              }}
            >
              <Field
                label="New password *"
                hint="Use uppercase, lowercase, a number, and a symbol. You can change it here any time."
              >
                <input name="password" type="password" autoComplete="new-password" required minLength={8} />
              </Field>
              <Field label="Confirm new password *">
                <input name="confirmation" type="password" autoComplete="new-password" required minLength={8} />
              </Field>
              <button className="btn primary">Change password</button>
            </form>
          </Panel>
        ) : (
          <Panel title="Change my password">
            <form className="panel-body" onSubmit={async (event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              try {
                await resetPassword(String(form.get("password")), String(form.get("confirmation")));
                setMessage("Password change recorded successfully.");
                event.currentTarget.reset();
              } catch (error) { setFailure((error as Error).message); }
            }}>
              <Field label="New password *" hint="Use uppercase, lowercase, number, and symbol.">
                <input name="password" type="password" autoComplete="new-password" required minLength={8} />
              </Field>
              <Field label="Confirm new password *">
                <input name="confirmation" type="password" autoComplete="new-password" required minLength={8} />
              </Field>
              <button className="btn primary">Change password</button>
            </form>
          </Panel>
        )}
        {user.role !== "ACCOUNTANT" && <Panel title="Authenticator recovery" subtitle="Generate one-time codes for the event you lose access to your authenticator.">
          <div className="panel-body">
            {backupCodes.length ? <>
              <p>Save these codes now. They will not be shown again.</p>
              <pre aria-label="Backup codes">{backupCodes.join("\n")}</pre>
              <button className="btn" type="button" onClick={() => navigator.clipboard?.writeText(backupCodes.join("\n"))}>Copy codes</button>
            </> : <p>No active backup codes are displayed.</p>}
            <button className="btn primary" type="button" disabled={busy} onClick={async () => { setBusy(true); setFailure(""); try { const result = await generateBackupCodes(); setBackupCodes(result.codes); setMessage("New backup codes generated. Save them somewhere safe."); } catch (error) { setFailure((error as Error).message); } finally { setBusy(false); } }}>Generate new backup codes</button>
          </div>
        </Panel>}
      </div>
      <Toast message={message} />
    </>
  );
}
