"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import Image from "next/image";
import accordAiLogo from "@/images/AccordAI_logo.png";
import welcomeBackground from "@/images/successful-access_bg/welcome_bg.png";
import { QRCodeSVG } from "qrcode.react";
import {
  ArrowRight,
  ArrowLeft,
  Check,
  CheckCircle2,
  LockKeyhole,
  ShieldCheck,
  Building2,
  CalendarDays,
  UserRound,
  X,
  Eye,
  EyeOff,
  CircleHelp,
} from "lucide-react";
import { LandingPage } from "@/features/auth/landing-page";
import { Badge, Field, ErrorState, LoadingState } from "@/components/common/ui";
import { HelpCenter } from "@/features/common/help/help-center";
import {
  activateAccount,
  getAccounts,
  login,
  verifyMfa,
  resendMfa,
  verifyResetCode,
  passwordChecks,
  requestPasswordReset,
  resetPassword,
  getCurrentUser,
  AUTH_IDENTITY_CONTRACT,
  requestLostAuthenticatorRecovery,
  recoverAuthenticator,
} from "@/lib/api/auth.service";
import { requestEnterpriseAccess } from "@/lib/api/tenant.service";
import { roleHome } from "@/lib/permissions";
import { useResource } from "@/features/common/hooks/use-resource";

export function PublicPage({ page }: { page: string }) {
  const router = useRouter();
  const params = useSearchParams();
  const { data: accounts } = useResource(getAccounts, "public");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [activation, setActivation] = useState(params.get("user") ?? "");
  const [otpStep, setOtpStep] = useState(false);
  const [mfaStep, setMfaStep] = useState(false);
  const [mfaCode, setMfaCode] = useState("");
  const [mfaMethod, setMfaMethod] = useState("authenticator");
  const [mfaEnrollment, setMfaEnrollment] = useState(false);
  const [mfaSecondsLeft, setMfaSecondsLeft] = useState(0);
  const [mfaResending, setMfaResending] = useState(false);
  const [totpSecret, setTotpSecret] = useState("");
  const [otpauthUrl, setOtpauthUrl] = useState("");
  const [showTotpSecret, setShowTotpSecret] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [passwordShake, setPasswordShake] = useState(false);
  const [recoveryReason, setRecoveryReason] = useState("");
  const [recoveryRequested, setRecoveryRequested] = useState(false);
  const [lostAuthenticator, setLostAuthenticator] = useState(false);
  const mfaInputRefs = useRef<Array<HTMLInputElement | null>>([]);
  const [otp, setOtp] = useState("");
  const [resetEmail, setResetEmail] = useState("");
  const [secondsLeft, setSecondsLeft] = useState(0);
  const [otpRejected, setOtpRejected] = useState(false);
  const { data: welcomeUser, error: welcomeError, loading: welcomeLoading, refresh: refreshWelcome } = useResource(
    () => page === "welcome" ? getCurrentUser() : Promise.resolve(null), `${AUTH_IDENTITY_CONTRACT}:welcome:${page}`,
  );
  const titles: Record<string, string> = {
    login: "Welcome back",
    welcome: "Welcome!",
    "request-access": "Register your company",
    activate: "Activate your account",
    "forgot-password": "Forgot your password?",
    "reset-password": "Create a new password",
    help: "Help & support",
  };
  const selected = accounts?.find((a) => a.id === activation);
  useEffect(() => {
    if (!otpStep) return;
    const timer = window.setInterval(() => {
      setSecondsLeft((current) => (current > 0 ? current - 1 : 0));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [otpStep]);
  useEffect(() => {
    if (!mfaStep || mfaSecondsLeft <= 0) return;
    const timer = window.setInterval(() => setMfaSecondsLeft((value) => Math.max(0, value - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [mfaStep, mfaSecondsLeft]);

  async function resendOtp() {
    if (!resetEmail || secondsLeft > 0 || busy) return;
    setBusy(true);
    setOtp("");
    setError("");
    setOtpRejected(false);
    try {
      await requestPasswordReset(resetEmail);
      setSecondsLeft(60);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to resend the code.");
    } finally {
      setBusy(false);
    }
  }

  async function continueReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await verifyResetCode(resetEmail, otp.trim());
      router.push("/reset-password");
    } catch (e) {
      setOtpRejected(true);
      setError(e instanceof Error ? e.message : "That code is not valid. Check the company email, or resend the code.");
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const f = new FormData(event.currentTarget);
    try {
      if (page === "login") {
        const result = await login(email, password);
        if (result.mfaRequired) {
          setMfaMethod(result.mfaMethod ?? "authenticator");
          setMfaEnrollment(result.enrollmentRequired ?? false);
          setTotpSecret(result.totpSecret ?? "");
          setOtpauthUrl(result.otpauthUrl ?? "");
          setMfaStep(true);
          setMfaSecondsLeft(result.mfaMethod === "email" ? 60 : 0);
          return;
        }
        if (!result.user) throw new Error("Sign-in did not return a user session.");
        const user = result.user;
        router.push(
          `/welcome?name=${encodeURIComponent(user.name)}&home=${encodeURIComponent(roleHome(user.role))}`,
        );
      } else if (page === "request-access") {
        await requestEnterpriseAccess({
          company: String(f.get("company")),
          registration: String(f.get("registration")),
          contact: String(f.get("contact")),
          email: String(f.get("email")),
          phone: String(f.get("phone")),
          accounts: Number(f.get("accounts")),
          employees: [],
          notes: String(f.get("notes")),
        });
        setSuccess(
          "Company access request submitted. Your request is awaiting AccordAI approval.",
        );
      } else if (page === "activate") {
        await activateAccount(
          activation,
          password,
          String(f.get("confirm")),
        );
        setSuccess(
          "Account activated successfully. You can now sign in using your company email.",
        );
      } else if (page === "forgot-password") {
        const entered = String(f.get("email"));
        await requestPasswordReset(entered);
        setResetEmail(entered);
        setOtp("");
        setOtpRejected(false);
        setSecondsLeft(60);
        setOtpStep(true);
      } else {
        await resetPassword(
          password,
          String(f.get("confirm")),
        );
        setSuccess(
          "Password reset completed successfully. You can now sign in with your new password.",
        );
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  async function submitMfa(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const user = await verifyMfa(mfaCode.trim());
      if (user.mustChangePassword) {
        router.push(roleHome(user.role));
        return;
      }
      router.push(`/welcome?name=${encodeURIComponent(user.name)}&home=${encodeURIComponent(roleHome(user.role))}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Verification failed.");
    } finally {
      setBusy(false);
    }
  }
  async function resendMfaCode() {
    if (mfaMethod !== "email" || mfaSecondsLeft > 0 || mfaResending) return;
    setMfaResending(true);
    setError("");
    try {
      const result = await resendMfa();
      setMfaCode("");
      setMfaSecondsLeft(result.expiresIn ?? 60);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to resend the code.");
    } finally {
      setMfaResending(false);
    }
  }
  async function requestLostAuthenticator() {
    if (!recoveryReason.trim()) { setError("Explain why you lost access to your authenticator."); return; }
    setBusy(true);
    setError("");
    try {
      await requestLostAuthenticatorRecovery(recoveryReason.trim());
      setRecoveryRequested(true);
      setRecoveryReason("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to submit the recovery request.");
    } finally { setBusy(false); }
  }
  async function submitLostAuthenticator() {
    setBusy(true); setError("");
    const form = new FormData();
    const recoveryEmail = document.querySelector<HTMLInputElement>('input[name="recoveryEmail"]');
    const recoveryPassword = document.querySelector<HTMLInputElement>('input[name="recoveryPassword"]');
    form.set("recoveryEmail", recoveryEmail?.value ?? email);
    form.set("recoveryPassword", recoveryPassword?.value ?? "");
    try {
      const result = await recoverAuthenticator(String(form.get("recoveryEmail")), String(form.get("recoveryPassword")));
      setTotpSecret(result.totpSecret); setOtpauthUrl(result.otpauthUrl); setMfaEnrollment(true); setMfaCode(""); setLostAuthenticator(false);
    } catch (e) { setError(e instanceof Error ? e.message : "Recovery failed."); } finally { setBusy(false); }
  }
  if (page === "help")
    return (
      <div className="home-wash auth-wash help-wash auth-no-nav">
        <main className="help-page-main">
          <Link href="/" className="auth-back-link"><ArrowLeft size={16} aria-hidden="true" />Back to home</Link>
          <div className="help-page-copy">
            <p className="hp-eyebrow">Support</p>
            <h1>
              Tutorial and <span>company helpline.</span>
            </h1>
            <p className="hp-lead">
              Learn the workflow, or ask AccordAI for help. The same guide opens inside the workspace.
            </p>
          </div>
          <div className="help-page-panel">
            <HelpCenter />
          </div>
        </main>
      </div>
    );

  if (!page) return <LandingPage />;
  const accessPage = page === "login" || page === "forgot-password";
  return (
    <div className={`home-wash auth-wash${page === "welcome" ? " welcome-wash" : ""}${page === "login" || page === "request-access" || page === "forgot-password" || page === "help" ? " auth-no-nav" : ""}`}>
      {page !== "login" && page !== "request-access" && page !== "forgot-password" && page !== "help" && <header className="hp-header">
        <Link href="/" className="hp-brand">
          <Image src={accordAiLogo} alt="AccordAI" width={150} height={46} priority />
        </Link>
        {page !== "login" && page !== "request-access" && page !== "forgot-password" && page !== "help" && <nav>
          <Link href="/help" className="hp-help-link" aria-label="Help and support" title="Help and support"><CircleHelp size={18} strokeWidth={2} /><span>Help</span></Link>
          <Link href="/request-access" className="hp-register">
            Register company
          </Link>
          <Link href="/login" className="hp-login">
            Log in
          </Link>
        </nav>}
      </header>}
      <main
        className={
          page === "request-access"
            ? "register-split"
            : mfaStep
              ? "auth-stage mfa-stage"
              : accessPage
              ? "hp-hero auth-hero"
              : page === "welcome"
                ? "welcome-split"
              : "auth-stage"
        }
      >
        {page !== "welcome" && (
          <Link href="/" className="auth-back-link">
            <ArrowLeft size={16} aria-hidden="true" />
            Back to home
          </Link>
        )}
        {page === "welcome" && (
          <aside
            className="welcome-brand-panel"
            style={{ backgroundImage: `url(${welcomeBackground.src})` }}
          >
            <Image src={accordAiLogo} alt="AccordAI Accounting Intelligence" width={180} height={55} priority />
            <div className="welcome-brand-copy">
              <h1>Smarter<br />Accounting<br />Starts Here</h1>
              <p>Automate document processing, improve accuracy and simplify your accounting workflow.</p>
            </div>
          </aside>
        )}
        {page === "request-access" && (
          <section className="register-story">
            <p className="hp-eyebrow">Company registration</p>
            <h1>
              Register your company and turn invoices into{" "}
              <span>trusted records.</span>
            </h1>
            <p className="hp-lead">
              Request a company workspace. AccordAI reviews it before anyone can sign in.
            </p>
            <p className="register-how">What the workspace is for</p>
            <ul className="register-points">
              <li>
                <Check size={16} strokeWidth={2.6} aria-hidden="true" />
                <span>Extract supplier, date, line items, and total from invoices, bills, and receipts.</span>
              </li>
              <li>
                <Check size={16} strokeWidth={2.6} aria-hidden="true" />
                <span>Send every mismatch to a person. Nothing questionable becomes a record on its own.</span>
              </li>
              <li>
                <Check size={16} strokeWidth={2.6} aria-hidden="true" />
                <span>Write one standard record shape, ready to export.</span>
              </li>
            </ul>
            <p className="register-note">
              No payment execution, tax filing, or external ERP posting. This request only opens a company workspace.
            </p>
          </section>
        )}
        {accessPage && !mfaStep && (
          <div>
            <p className="hp-eyebrow">Company accounting intelligence</p>
            <p className="hp-quiet">
              {mfaStep
                ? "Privileged account security"
                : page === "login"
                ? "AccordAI secure · company email opens the workspace"
                : otpStep
                  ? "Waiting for the code from the company email"
                  : "Reset stays inside this workspace"}
            </p>
            <h1>
              {mfaStep ? (
                <>Complete the <span>authenticator check.</span></>
              ) : page === "login" ? (
                <>
                  Sign in to the <span>workspace.</span>
                </>
              ) : otpStep ? (
                <>
                  Enter the <span>code.</span>
                </>
              ) : (
                <>
                  Forgot your <span>password?</span>
                </>
              )}
            </h1>
            <p className="hp-lead">
              {mfaStep
                ? "Your account requires one more security check."
                : page === "login"
                ? "From invoice to trusted record."
                : otpStep
                  ? "The code is valid for 15 minutes."
                  : "Use the company email on the account."}
            </p>
            <p className="hp-copy">
              {mfaStep
                ? "Scan the QR code with your authenticator app, then enter the current six-digit code to finish signing in."
                : page === "login"
                ? "Enter the company email you were issued. That address opens the right workspace. People are invited; there is no public self-signup."
                : otpStep
                  ? "Enter the code from the company email, then continue to choose a new password. Sending the email is handled later."
                  : "Enter the company email. This preview records the reset here, then asks for the code before the new password."}
            </p>
          </div>
        )}
        <div
          className={`auth-card${page === "request-access" ? " register-card" : ""}${page === "login" ? " login-card" : ""}${page === "forgot-password" ? " reset-card" : ""}`}
        >
          {!accessPage && page !== "request-access" && page !== "welcome" && (
            <>
              <p className="hp-eyebrow">
                {page === "request-access"
                  ? "Company registration"
                  : page === "reset-password"
                    ? "Account access"
                    : "Workspace sign in"}
              </p>
              <h1>{titles[page] ?? "Page not found"}</h1>
              <p>
                {page === "welcome"
                  ? "Your identity has been verified successfully."
                  : page === "request-access"
                    ? "Submit your company details to register for company access. AccordAI will review your request."
                    : page === "reset-password"
                      ? "Choose a strong password to keep your workspace secure."
                      : "Secure access starts with your company email."}
              </p>
            </>
          )}
          {page === "request-access" && !success && (
            <>
              <p className="hp-eyebrow">Company details</p>
              <h2>Register your company</h2>
              <p>Complete the form. AccordAI reviews the request before the workspace opens.</p>
            </>
          )}
          {accessPage && !success && !otpStep && !mfaStep && (
            <>
              <p className="hp-eyebrow">
                {page === "login" ? "Workspace sign in" : "Account access"}
              </p>
              <h2>{page === "login" ? "Company credentials" : "Request a reset"}</h2>
            </>
          )}
          {page === "login" && mfaStep ? (
            <form className="otp-wait" onSubmit={submitMfa}>
              <button type="button" className="mfa-back-button" onClick={() => { setMfaStep(false); setMfaCode(""); setError(""); }} aria-label="Back to sign in">
                <ArrowLeft size={18} />
              </button>
              <h2>Verify your sign-in</h2>
              <p>{mfaEnrollment ? "Scan the QR code with your authenticator app, then enter the generated six-digit code." : mfaMethod === "email" ? "Enter the six-digit code sent to your email." : "Enter the six-digit code from your authenticator app."}</p>
              {mfaMethod === "email" && <p className="otp-timer" aria-live="polite">{mfaSecondsLeft > 0 ? `You can request a new code in ${mfaSecondsLeft}s.` : "Your code may have expired. Request a new code."}</p>}
              {mfaEnrollment && (
                <div className="form-bottom">
                  <strong>Authenticator setup</strong>
                  <p>Scan the QR code with your authenticator app. If you cannot scan it, use the manual setup key.</p>
                  {otpauthUrl && <div style={{ display: "flex", justifyContent: "center", margin: "18px 0" }}><QRCodeSVG value={otpauthUrl} size={220} level="M" includeMargin aria-label="Authenticator QR code" /></div>}
                  {totpSecret && <button type="button" className="totp-manual-secret" onClick={() => setShowTotpSecret((visible) => !visible)} aria-label="Show or hide manual TOTP setup key">
                    <span>Manual setup key</span>
                    <input type={showTotpSecret ? "text" : "password"} readOnly value={totpSecret} aria-label="Manual TOTP setup key" tabIndex={-1} />
                  </button>}
                </div>
              )}
              {error && <div role="alert" style={{ position: "relative", margin: "12px 0", padding: "14px 42px 14px 14px", borderRadius: 10, border: "1px solid #efb4b4", background: "#fff4f4", color: "#8f1d1d" }}><strong>{error}</strong><button type="button" onClick={() => setError("")} aria-label="Close error" style={{ position: "absolute", top: 8, right: 8, border: 0, background: "transparent", color: "inherit", cursor: "pointer" }}><X size={18} /></button></div>}
              <Field label="Authenticator code *">
                <div className="mfa-code-boxes" aria-label="Six-digit authenticator code">
                  {Array.from({ length: 6 }, (_, index) => (
                    <input
                      key={index}
                      ref={(element) => { mfaInputRefs.current[index] = element; }}
                      inputMode="numeric"
                      maxLength={1}
                      value={mfaCode[index] ?? ""}
                      aria-label={`Authenticator digit ${index + 1}`}
                      onKeyDown={(event) => {
                        if (event.key !== "Backspace") return;
                        event.preventDefault();
                        const next = mfaCode.split("");
                        if (next[index]) {
                          next[index] = "";
                        } else if (index > 0) {
                          next[index - 1] = "";
                          mfaInputRefs.current[index - 1]?.focus();
                        }
                        setMfaCode(next.join("").slice(0, 6));
                      }}
                      onChange={(event) => {
                        const digit = event.target.value.replace(/\D/g, "").slice(-1);
                        const next = mfaCode.split("");
                        next[index] = digit;
                        setMfaCode(next.join("").slice(0, 6));
                        if (digit && index < 5) mfaInputRefs.current[index + 1]?.focus();
                      }}
                    />
                  ))}
                </div>
              </Field>
              <button className="btn primary full" type="submit" disabled={busy}>{busy ? "Verifying…" : "Verify and continue"} <ArrowRight size={16} /></button>
              {mfaMethod === "email" && <button type="button" className="otp-resend" onClick={resendMfaCode} disabled={mfaSecondsLeft > 0 || mfaResending}>{mfaResending ? "Sending a new code..." : mfaSecondsLeft > 0 ? `Resend code in ${mfaSecondsLeft}s` : "Resend code"}</button>}
              {mfaMethod !== "email" && !mfaEnrollment && <div className="form-bottom">
                <button type="button" className="otp-resend" onClick={() => setLostAuthenticator(true)}>Lost authenticator key?</button>
                {lostAuthenticator && typeof document !== "undefined" && createPortal(<div className="recovery-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setLostAuthenticator(false); }}>
                  <div className="recovery-modal" role="dialog" aria-modal="true" aria-labelledby="recovery-title">
                    <button type="button" className="recovery-modal-close" onClick={() => setLostAuthenticator(false)} aria-label="Close recovery dialog">×</button>
                    <h3 id="recovery-title">Recover your authenticator</h3>
                    <p>Retype your password to generate a new authenticator key.</p>
                    <Field label="Retype your password"><input name="recoveryPassword" type="password" required autoComplete="current-password" /></Field>
                    <button type="button" className="btn primary full" disabled={busy} onClick={submitLostAuthenticator}>{busy ? "Checking…" : "Generate new authenticator key"}</button>
                  </div>
                </div>, document.body)}
              </div>}
            </form>
          ) : page === "forgot-password" && otpStep && (
            <form className="otp-wait" onSubmit={continueReset}>
              <span className="auth-icon">
                <LockKeyhole size={20} />
              </span>
              <p className="hp-eyebrow">Account access</p>
              <h2>Enter the code</h2>
              <p>Waiting for the code sent to {resetEmail || "the company email"}. It stays valid for 15 minutes.</p>
              {error && <ErrorState message={error} />}
              <Field label="Code *">
                <input
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={6}
                  placeholder="6-digit code"
                  required
                  value={otp}
                  onChange={(event) => setOtp(event.target.value)}
                />
              </Field>
              <p className="otp-timer" aria-live="polite">
                {secondsLeft > 0 ? `Resend in ${secondsLeft}s` : "You can resend the code."}
              </p>
              <button className="btn primary full" type="submit">
                Continue reset password <ArrowRight size={16} />
              </button>
              <button
                className="otp-resend"
                type="button"
                disabled={secondsLeft > 0 && !otpRejected}
                onClick={resendOtp}
              >
                Resend code
              </button>
              <p className="form-bottom">
                Wrong email?{" "}
                <button type="button" className="form-link" onClick={() => { setOtpStep(false); setError(""); }}>
                  Change company email
                </button>
              </p>
            </form>
          )}
          {page === "welcome" && welcomeLoading ? <LoadingState /> : page === "welcome" && welcomeError ? <ErrorState message={welcomeError} retry={refreshWelcome} /> : page === "welcome" && welcomeUser ? (
            <div className="welcome-confirmation">
              <span className="welcome-check"><Check size={42} strokeWidth={2.4} /></span>
              <p className="hp-eyebrow">SIGN IN SUCCESSFUL</p>
              <h2>Welcome, {welcomeUser.name}</h2>
              <p className="welcome-lead">Your identity has been verified and you’ve successfully signed in to your workspace.</p>
              <div className="welcome-details">
                {welcomeUser.tenantCode && <div><Building2 size={22} /><span>Company code</span><strong>{welcomeUser.tenantCode}</strong></div>}
                <div><Building2 size={22} /><span>Company</span><strong>{welcomeUser.tenantName || (welcomeUser.role === "SUPER_ADMIN" ? "AccordAI Platform" : "Company workspace")}</strong></div>
                <div><UserRound size={22} /><span>Role</span><strong>{welcomeUser.role.replaceAll("_", " ")}</strong></div>
                <div><CalendarDays size={22} /><span>Last sign in</span><strong>{welcomeUser.lastLogin ? new Date(welcomeUser.lastLogin).toLocaleString("en-MY", { dateStyle: "medium", timeStyle: "short" }) : "Just now"}</strong></div>
              </div>
              <Link className="btn primary welcome-continue" href={roleHome(welcomeUser.role)}>
                Continue to workspace <ArrowRight size={20} />
              </Link>
              <Link className="welcome-switch" href="/login">Sign in with a different account</Link>
            </div>
          ) : page === "welcome" ? (
            <div className="welcome-session-error">
              <p className="hp-eyebrow">Session unavailable</p>
              <h2>Sign in to continue</h2>
              <p>
                No authenticated session is available. Please sign in to continue.
              </p>
              <Link
                className="btn primary"
                href="/login"
              >
                Return to sign in <ArrowRight size={16} />
              </Link>
            </div>
          ) : success ? (
            <div className="success-state">
              <CheckCircle2 size={40} />
              <h2>
                {page === "request-access"
                  ? "Request received"
                  : "You’re all set"}
              </h2>
              <p>{success}</p>
              {page === "request-access" && <Badge>PENDING</Badge>}
              <Link
                className="btn primary"
                href={page === "forgot-password" ? "/reset-password" : "/login"}
              >
                {page === "forgot-password"
                  ? "Continue password reset"
                  : "Continue to login"}
                <ArrowRight size={16} />
              </Link>
            </div>
          ) : otpStep || mfaStep ? null : (
            <form onSubmit={submit}>
              {error && <ErrorState message={error} />}
              {page === "login" && (
                <>
                  <Field label="Company email *">
                    <input
                      type="email"
                      autoComplete="username"
                      placeholder="you@company.com"
                      required
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                    />
                  </Field>
                  <Field
                    label="Password *"
                    hint="Enter your account password to continue."
                  >
                    <div className="password-input-wrap">
                      <input
                        type={showPassword ? "text" : "password"}
                        aria-label="Password *"
                        required
                        autoComplete="current-password"
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                      />
                      <button type="button" className="password-visibility" aria-label={showPassword ? "Hide password" : "Show password"} aria-pressed={showPassword} onPointerDown={(event) => event.preventDefault()} onClick={() => setShowPassword((visible) => !visible)}>
                        {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                      </button>
                    </div>
                  </Field>
                </>
              )}
              {page === "request-access" && (
                <>
                  <div className="form-grid">
                    <Field label="Company name *">
                      <input
                        name="company"
                        required
                        placeholder="Your company Sdn Bhd"
                      />
                    </Field>
                    <Field label="Registration number *">
                      <input
                        name="registration"
                        required
                        placeholder="202601234567"
                      />
                    </Field>
                    <Field label="Contact person / manager *">
                      <input name="contact" required />
                    </Field>
                    <Field label="Company email *">
                      <input
                        name="email"
                        type="email"
                        required
                        defaultValue={params.get("email") ?? ""}
                      />
                    </Field>
                    <Field label="Phone number">
                      <input name="phone" type="tel" />
                    </Field>
                    <Field label="Accounts required *">
                      <input
                        name="accounts"
                        type="number"
                        min="1"
                        max="1000"
                        defaultValue="10"
                        required
                      />
                    </Field>
                  </div>
                  <Field label="Reason / notes">
                    <textarea name="notes" rows={2} />
                  </Field>
                </>
              )}
              {page === "activate" && (
                <>
                  <Field label="Pending invitation">
                    <select
                      required
                      value={activation}
                      onChange={(e) => setActivation(e.target.value)}
                    >
                      <option value="">Select an invitation</option>
                      {accounts
                        ?.filter((a) =>
                          ["INVITED", "INVITATION_EXPIRED"].includes(a.status),
                        )
                        .map((a) => (
                          <option key={a.id} value={a.id}>
                            {a.name} · {a.email}
                          </option>
                        ))}
                    </select>
                  </Field>
                  {selected && (
                    <div className="info-box">
                      <strong>{selected.email}</strong>
                      <span>Company: {selected.tenantId}</span>
                      <Badge>{selected.role}</Badge>
                    </div>
                  )}
                </>
              )}
              {page === "forgot-password" && (
                <Field label="Company email *">
                  <input name="email" type="email" required />
                </Field>
              )}
              {["activate", "reset-password"].includes(page) && (
                <div className="password-setup">
                  <div className="password-setup-heading">
                    <strong>Set your password</strong>
                    <span>Use a password only you can guess.</span>
                  </div>
                  <Field label="Create password *">
                    <div className="password-input-wrap">
                      <input
                        required
                        type={showPassword ? "text" : "password"}
                        autoComplete="new-password"
                        value={password}
                        onChange={(e) => {
                          const nextPassword = e.target.value;
                          setPassword(nextPassword);
                          if (passwordChecks(nextPassword).some((valid) => !valid)) {
                            setPasswordShake((current) => !current);
                          }
                        }}
                      />
                      <button type="button" className="password-visibility" aria-label={showPassword ? "Hide password" : "Show password"} aria-pressed={showPassword} onPointerDown={(event) => event.preventDefault()} onClick={() => setShowPassword((visible) => !visible)}>
                        {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                      </button>
                    </div>
                  </Field>
                  <Field label="Confirm password *">
                    <div className="password-input-wrap">
                      <input
                        required
                        name="confirm"
                        type={showConfirmPassword ? "text" : "password"}
                        autoComplete="new-password"
                        value={confirmPassword}
                        onChange={(e) => setConfirmPassword(e.target.value)}
                      />
                      <button type="button" className="password-visibility" aria-label={showConfirmPassword ? "Hide password confirmation" : "Show password confirmation"} aria-pressed={showConfirmPassword} onPointerDown={(event) => event.preventDefault()} onClick={() => setShowConfirmPassword((visible) => !visible)}>
                        {showConfirmPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                      </button>
                    </div>
                  </Field>
                  <div className="password-checks" aria-live="polite">
                    <span className="password-checks-title">
                      Password requirements
                    </span>
                    {[
                      "At least 8 characters",
                      "Uppercase letter",
                      "Lowercase letter",
                      "Number",
                      "Special character",
                    ].map((text, i) => (
                      <span
                        className={`password-rule ${passwordChecks(password)[i] ? "passed" : `invalid ${passwordShake ? "shake" : ""}`}`}
                        key={text}
                      >
                        {passwordChecks(password)[i] ? <Check size={13} /> : <X size={13} />}
                        {text}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              <button className={`btn primary full${page === "reset-password" ? " reset-submit" : ""}`} disabled={busy}>
                {busy
                  ? "Please wait…"
                  : page === "login"
                    ? "Continue with email"
                    : page === "request-access"
                      ? "Submit company registration"
                      : page === "activate"
                        ? "Activate account"
                        : page === "forgot-password"
                          ? "Request reset"
                          : "Reset password"}
                <ArrowRight size={16} />
              </button>
              {page === "login" && (
                <>
                  <Link href="/forgot-password" className="form-link">
                    Forgot password?
                  </Link>
                  <p className="form-bottom">
                    New to Accounting Intelligence?{" "}
                    <Link href="/request-access">Register company</Link>
                    {" · "}
                    <Link href="/help">Need help?</Link>
                  </p>
                </>
              )}
              {page === "forgot-password" && (
                <p className="form-bottom">
                  Remembered it? <Link href="/login">Back to sign in</Link>
                </p>
              )}
              {page === "request-access" && (
                <p className="form-bottom">
                  Already registered? <Link href="/login">Sign in</Link>
                  {" · "}
                  <Link href="/help">Help & tutorial</Link>
                </p>
              )}
            </form>
          )}
          {page === "request-access" && <div className="secure-note">
            <ShieldCheck size={15} />
            <span>Company registration is reviewed by AccordAI before workspace activation.</span>
          </div>}
        </div>
        {(page === "login" || page === "request-access" || page === "forgot-password") && (
          <aside className="auth-preview-panel" aria-label="AccordAI workflow preview">
            <p className="auth-preview-kicker">AccordAI · Accounting intelligence</p>
            <h2>From documents to dependable records.</h2>
            <p className="auth-preview-copy">Review invoices, bills, and receipts in one clear workflow.</p>
            <div className="auth-preview-dashboard">
              <div className="auth-preview-stat"><span>Source documents</span><strong>PDF · JPG · PNG</strong><small>Original source retained for review</small></div>
              <div className="auth-preview-chart"><span>Evidence-led review</span><strong>Human approval</strong><p>Check extracted values against the source.</p></div>
              <div className="auth-preview-table"><span>Document workflow</span><b>Extract and validate</b><b>Correct and submit</b><b>Independent approval</b></div>
            </div>
          </aside>
        )}
      </main>
    </div>
  );
}
