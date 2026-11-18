import Link from "next/link";
import { ArrowLeft } from "lucide-react";

export function NotFound() {
  return (
    <main className="not-found-page">
      <section className="not-found-content">
        <div className="not-found-face-container" aria-hidden="true">
          <svg
            className="not-found-face"
            viewBox="0 0 320 380"
            role="presentation"
          >
            <g
              fill="none"
              stroke="currentColor"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="25"
            >
              <g
                className="not-found-face__eyes"
                transform="translate(0,112.5)"
              >
                <g transform="translate(15,0)">
                  <polyline
                    className="not-found-face__eye-lid"
                    points="37,0 0,120 75,120"
                  />

                  <polyline
                    className="not-found-face__pupil"
                    points="55,120 55,155"
                    strokeDasharray="35 35"
                  />
                </g>

                <g transform="translate(230,0)">
                  <polyline
                    className="not-found-face__eye-lid"
                    points="37,0 0,120 75,120"
                  />

                  <polyline
                    className="not-found-face__pupil"
                    points="55,120 55,155"
                    strokeDasharray="35 35"
                  />
                </g>
              </g>

              <rect
                className="not-found-face__nose"
                x="132.5"
                y="112.5"
                width="55"
                height="155"
                rx="4"
                ry="4"
              />

              <g
                transform="translate(65,334)"
                strokeDasharray="102 102"
              >
                <path
                  className="not-found-face__mouth-left"
                  d="M 0 30 C 0 30 40 0 95 0"
                />

                <path
                  className="not-found-face__mouth-right"
                  d="M 95 0 C 150 0 190 30 190 30"
                />
              </g>
            </g>
          </svg>
        </div>

        <div className="not-found-copy">
          <span className="not-found-eyebrow">
            404 · PAGE NOT FOUND
          </span>

          <h1>
            This page seems to have
            <span> wandered off.</span>
          </h1>

          <p>
            The page you&apos;re looking for doesn&apos;t exist, has moved,
            or is no longer available.
          </p>

          <Link className="not-found-button" href="/">
            <ArrowLeft size={15} />
            Return home
          </Link>
        </div>
      </section>
    </main>
  );
}