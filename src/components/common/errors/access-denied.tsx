import Link from "next/link";
import { ArrowLeft, LockKeyhole } from "lucide-react";

export function AccessDenied({ homeHref }: { homeHref: string }) {
  return (
    <main
      className="access-denied-page"
      aria-labelledby="access-denied-title"
    >
      <section className="access-denied-content">
        <div className="access-denied-face-container" aria-hidden="true">
          <svg
            className="access-denied-face"
            viewBox="0 0 320 320"
            role="presentation"
          >
            <g
              fill="none"
              stroke="currentColor"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path
                className="access-denied-brow access-denied-brow-left"
                d="M 55 92 Q 95 70 125 92"
                strokeWidth="18"
              />

              <path
                className="access-denied-brow access-denied-brow-right"
                d="M 195 92 Q 225 70 265 92"
                strokeWidth="18"
              />

              <path
                className="access-denied-eye"
                d="M 55 135 Q 90 112 125 135"
                strokeWidth="16"
              />

              <path
                className="access-denied-eye"
                d="M 195 135 Q 230 112 265 135"
                strokeWidth="16"
              />

              <circle
                className="access-denied-pupil access-denied-pupil-left"
                cx="93"
                cy="130"
                r="8"
                fill="currentColor"
                stroke="none"
              />

              <circle
                className="access-denied-pupil access-denied-pupil-right"
                cx="233"
                cy="130"
                r="8"
                fill="currentColor"
                stroke="none"
              />

              <path
                className="access-denied-nose"
                d="M 160 132 L 148 195 L 174 195"
                strokeWidth="15"
              />

              <path
                className="access-denied-mouth"
                d="M 110 245 Q 160 230 210 245"
                strokeWidth="18"
              />
            </g>
          </svg>

          <div className="access-denied-lock">
            <LockKeyhole size={16} strokeWidth={2} />
          </div>
        </div>

        <div className="access-denied-copy">
          <span className="access-denied-eyebrow">
            403 · ACCESS RESTRICTED
          </span>

          <h1 id="access-denied-title">
            This area is
            <span> off limits.</span>
          </h1>

          <p>
            You don&apos;t have permission to access this page in your current
            workspace. If you believe you should have access, contact your
            administrator.
          </p>

          <Link className="access-denied-button" href={homeHref}>
            <ArrowLeft size={15} />
            Go to my dashboard
          </Link>
        </div>
      </section>
    </main>
  );
}