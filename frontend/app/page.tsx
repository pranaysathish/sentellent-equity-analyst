"use client";

import { useEffect, useState } from "react";
import { api, loginUrl } from "@/lib/api";
import { GradientBackground } from "@/components/GradientBackground";

/**
 * Short enough to sit in a row. The previous version was three paragraphs of
 * body copy — that is a features page, not a sign-in. Nobody reads it, and it
 * pushed the button below the fold on a laptop.
 */
const PILLARS = [
  { label: "Grounded in real data", detail: "screener.in · NSE prices · Indian financial media" },
  { label: "Every claim cited", detail: "each figure links back to its source" },
  { label: "Learns how you invest", detail: "your rules, applied to every recommendation" },
];

const TICKERS = ["RELIANCE", "TCS", "ITC", "HDFCBANK", "INFY", "SBIN", "WIPRO", "MARUTI"];

export default function LoginPage() {
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // A valid session cookie means this screen is just a redirect.
    api
      .me()
      .then(() => {
        window.location.href = "/dashboard/";
      })
      .catch(() => setChecking(false));

    // The OAuth callback returns here with ?error=… when the round trip fails.
    const err = new URLSearchParams(window.location.search).get("error");
    if (err) {
      setError(
        err === "invalid_state"
          ? "That sign-in attempt expired. Please try again."
          : "Google sign-in didn't complete. Please try again.",
      );
    }
  }, []);

  return (
    <div className="login">
      <GradientBackground />

      <main className="hero">
        <img className="hero-mark" src="/logo.png" alt="Sentellent" />

        <h1 className="hero-title">Your equity research chief of staff</h1>

        <p className="hero-sub">
          An agentic analyst for the NSE and BSE. Follow Indian tickers and ask
          questions — every answer grounded in retrieved sources, cited, and
          priced in rupees.
        </p>

        <div className="pillars">
          {PILLARS.map((p, i) => (
            <div className="pillar" key={p.label} style={{ animationDelay: `${260 + i * 90}ms` }}>
              <span className="pillar-label">{p.label}</span>
              <span className="pillar-detail">{p.detail}</span>
            </div>
          ))}
        </div>

        <div className="hero-cta">
          {checking ? (
            <button className="google-btn" disabled>
              <span className="spinner" /> Checking your session…
            </button>
          ) : (
            <a href={loginUrl}>
              <button className="google-btn">
                <GoogleMark />
                Continue with Google
              </button>
            </a>
          )}
          {error && <div className="error">{error}</div>}
          <p className="hero-note">Sign-in only — no Gmail or Calendar access is requested.</p>
        </div>

        {/* A quiet reminder that this covers real listed companies. The list is
            duplicated so the loop has no visible seam at the wrap point. */}
        <div className="ticker-strip" aria-hidden>
          <div className="ticker-track">
            {[...TICKERS, ...TICKERS].map((t, i) => (
              <span className="ticker-item" key={i}>
                {t}
              </span>
            ))}
          </div>
        </div>
      </main>
    </div>
  );
}

/** Google's mark, inlined so the button needs no network request to render. */
function GoogleMark() {
  return (
    <svg width="17" height="17" viewBox="0 0 48 48" aria-hidden>
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
    </svg>
  );
}
