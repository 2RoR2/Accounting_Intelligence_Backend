"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, Check, LockKeyhole } from "lucide-react";
import type { User } from "@/types";
import {
  changeOwnPassword,
  logout,
  passwordChecks,
} from "@/lib/api/auth.service";
import { ErrorState, Field } from "@/components/common/ui";
import { roleHome } from "@/lib/permissions";
import Image from "next/image";
import accordAiLogo from "@/images/AccordAI_logo.png";

const REQUIREMENTS = [
  "At least 8 characters",
  "Uppercase letter",
  "Lowercase letter",
  "Number",
  "Special character",
];

export function ForcePasswordChange({ user }: { user: User }) {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);

  async function leave() {
    await logout();
    router.push("/login");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setFailure("");
    try {
      await changeOwnPassword(
        user,
        password,
        String(new FormData(event.currentTarget).get("confirmation")),
      );
      router.push(`/welcome?name=${encodeURIComponent(user.name)}&home=${encodeURIComponent(roleHome(user.role))}`);
    } catch (error) {
      setFailure((error as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="home-wash auth-wash">
      <header className="hp-header">
        <Link href="/" className="hp-brand">
          <Image src={accordAiLogo} alt="AccordAI" width={150} height={46} priority />
        </Link>
        <nav>
          <button type="button" className="hp-login" onClick={() => void leave()}>
            Switch account
          </button>
        </nav>
      </header>
      <main className="hp-hero auth-hero">
        <div>
          <p className="hp-eyebrow">Company accounting intelligence</p>
          <p className="hp-quiet">First sign-in · asked once</p>
          <h1>
            Choose a new <span>password.</span>
          </h1>
          <p className="hp-lead">Then the workspace opens.</p>
          <p className="hp-copy">
            {user.name.split(" ")[0]}, this is required only the first time you
            sign in. After you save it, later changes stay on your profile.
          </p>
        </div>
        <form className="auth-card login-card" onSubmit={submit}>
          <span className="auth-icon">
            <LockKeyhole size={20} />
          </span>
          <p className="hp-eyebrow">Account access</p>
          <h2>Set your password</h2>
          {failure && <ErrorState message={failure} />}
          <Field
            label="New password *"
            hint="Use 8+ characters with uppercase, lowercase, a number, and a symbol."
          >
            <input
              name="password"
              type="password"
              required
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>
          <Field label="Confirm new password *">
            <input
              name="confirmation"
              type="password"
              required
              autoComplete="new-password"
            />
          </Field>
          <div className="password-checks" aria-live="polite">
            <span className="password-checks-title">Password requirements</span>
            {REQUIREMENTS.map((text, index) => (
              <span
                className={passwordChecks(password)[index] ? "passed" : ""}
                key={text}
              >
                <Check size={13} />
                {text}
              </span>
            ))}
          </div>
          <button className="btn primary" disabled={busy}>
            {busy ? "Saving…" : "Save password"} <ArrowRight size={16} />
          </button>
          <p className="form-bottom">
            <button type="button" className="form-link" onClick={() => void leave()}>
              Switch account
            </button>
          </p>
        </form>
      </main>
    </div>
  );
}
