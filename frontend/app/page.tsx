"use client";

import { useEffect, useState } from "react";
import { api, loginUrl } from "@/lib/api";

export default function LoginPage() {
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // If a valid session cookie is already set, skip the login screen.
    api
      .me()
      .then(() => {
        window.location.href = "/dashboard/";
      })
      .catch(() => setChecking(false));

    // The OAuth callback redirects back here with ?error=... when the
    // round-trip fails, so surface that instead of silently showing login.
    const params = new URLSearchParams(window.location.search);
    const err = params.get("error");
    if (err) {
      setError(
        err === "invalid_state"
          ? "Login session expired or was tampered with. Please try again."
          : "Google sign-in did not complete. Please try again.",
      );
    }
  }, []);

  return (
    <div className="login-wrap">
      <div className="panel login-card">
        <h1>
          Sentellent <span style={{ color: "var(--accent)" }}>Equity Analyst</span>
        </h1>
        <p>Your research chief of staff for the NSE &amp; BSE.</p>

        <ul>
          <li>Follow Indian tickers — RELIANCE, TCS, HDFCBANK.</li>
          <li>
            Fundamentals from screener.in and news from Indian financial media are
            ingested, chunked and embedded into a vector store.
          </li>
          <li>
            Every claim is grounded in a retrieved source and cited. Figures are in
            INR. If it isn&apos;t in the data, the agent says so.
          </li>
          <li>
            Tell it how you invest and it remembers — then screens your stocks
            against your own rules.
          </li>
        </ul>

        {checking ? (
          <p className="empty">
            <span className="spinner" /> Checking your session…
          </p>
        ) : (
          <a href={loginUrl}>
            <button className="primary" style={{ width: "100%" }}>
              Sign in with Google
            </button>
          </a>
        )}

        {error && <p className="error">{error}</p>}
      </div>
    </div>
  );
}
