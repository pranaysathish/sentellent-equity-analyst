/**
 * Thin API client.
 *
 * In production the SPA and the API share an origin (CloudFront routes /api/*
 * to the backend), so requests are relative and the session cookie rides along
 * automatically. Locally, NEXT_PUBLIC_API_BASE points at the dev server on
 * :8000 and `credentials: "include"` keeps the cookie working cross-port.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}/api${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
    } catch {
      /* non-JSON error body; the status text is enough */
    }
    throw new ApiError(detail, response.status);
  }

  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

// --- Types ------------------------------------------------------------------
export interface User {
  id: string;
  email: string;
  name: string | null;
  picture_url: string | null;
}

export interface Follow {
  ticker: string;
  name: string;
  sector: string | null;
  current_price: number | null;
  pe: number | null;
  roe: number | null;
  debt_to_equity: number | null;
  dividend_yield: number | null;
  market_cap_cr: number | null;
  return_1m: number | null;
  return_1y: number | null;
  volatility_1y: number | null;
  /** Weekly closes, oldest first — drives the sparkline. */
  close_series: number[];
  sentiment: number;
  article_count: number;
  last_ingest_at: string | null;
}

export interface Citation {
  n: number;
  kind: string;
  title: string;
  url: string | null;
  source: string;
  ticker: string | null;
  published_at: string | null;
}

export interface ChatReply {
  session_id: string;
  answer: string;
  citations: Citation[];
  intent: string;
  grounded: boolean;
  persona_updated: boolean;
  tickers: string[];
}

export interface PersonaFact {
  id: number;
  fact: string;
  category: string;
  created_at: string;
}

export interface PersonaView {
  summary: string;
  weights: Record<string, number>;
  rules: { field: string; op: string; value: number }[];
  facts: PersonaFact[];
}

// --- Calls ------------------------------------------------------------------
export const api = {
  me: () => request<User>("/me"),
  logout: () => request<{ status: string }>("/auth/logout", { method: "POST" }),

  follows: () => request<Follow[]>("/follows"),
  follow: (ticker: string) =>
    request<{ ticker: string; status: string }>("/follows", {
      method: "POST",
      body: JSON.stringify({ ticker }),
    }),
  unfollow: (ticker: string) =>
    request<{ status: string }>(`/follows/${ticker}`, { method: "DELETE" }),
  refresh: (ticker: string) =>
    request<{ status: string }>(`/follows/${ticker}/refresh`, { method: "POST" }),

  persona: () => request<PersonaView>("/persona"),
  forgetFact: (id: number) =>
    request<{ status: string }>(`/persona/facts/${id}`, { method: "DELETE" }),

  chat: (message: string, sessionId: string | null) =>
    request<ChatReply>("/chat", {
      method: "POST",
      body: JSON.stringify({ message, session_id: sessionId }),
    }),
};

export const loginUrl = `${API_BASE}/api/auth/google/login`;

// --- Formatting -------------------------------------------------------------

/**
 * Coerce an API value to a finite number, or null.
 *
 * Numeric columns can legitimately reach the browser as strings — Postgres
 * `numeric` becomes a Python Decimal, which JSON encoders often render as
 * `"24.5"` to protect precision. The backend now normalises that, but calling
 * `.toFixed()` on whatever arrives is how a single unexpected field took the
 * whole React tree down and produced a blank "page couldn't load". Parsing
 * defensively here means a surprising value degrades to an em dash instead.
 */
export function num(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

/** All money in this app is INR, so formatting lives in exactly one place. */
export function formatINR(value: unknown): string {
  const n = num(value);
  if (n === null) return "—";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(n);
}

export function formatCrore(value: unknown): string {
  const n = num(value);
  if (n === null) return "—";
  if (n >= 100000) return `₹${(n / 100000).toFixed(2)} lakh cr`;
  return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })} cr`;
}

export function formatPercent(value: unknown, alreadyPercent = false): string {
  const n = num(value);
  if (n === null) return "—";
  const pct = alreadyPercent ? n : n * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
}

/** Fixed-decimal display for ratios, tolerant of nulls and stringly numbers. */
export function formatNumber(value: unknown, decimals = 2, suffix = ""): string {
  const n = num(value);
  return n === null ? "—" : `${n.toFixed(decimals)}${suffix}`;
}

export function sentimentLabel(value: unknown): { label: string; tone: string } {
  const score = num(value) ?? 0;
  if (score > 0.25) return { label: "Positive", tone: "pos" };
  if (score < -0.25) return { label: "Negative", tone: "neg" };
  return { label: "Neutral", tone: "neu" };
}
