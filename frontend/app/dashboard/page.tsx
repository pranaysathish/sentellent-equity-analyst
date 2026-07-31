"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  Citation,
  Follow,
  PersonaView,
  User,
  api,
  formatNumber,
  num,
} from "@/lib/api";
import { StockCard, StockCardSkeleton } from "@/components/StockCard";

interface Turn {
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  grounded?: boolean;
  personaUpdated?: boolean;
}

const STARTERS = [
  "What's the sentiment on my stocks this week?",
  "I'm a conservative, dividend-focused investor and I avoid high-debt companies.",
  "What should I buy for my profile?",
  "What do you know about me?",
];

export default function Dashboard() {
  const [user, setUser] = useState<User | null>(null);
  const [follows, setFollows] = useState<Follow[] | null>(null);
  const [persona, setPersona] = useState<PersonaView | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [draft, setDraft] = useState("");
  const messagesRef = useRef<HTMLDivElement>(null);

  const reload = useCallback(async () => {
    const [f, p] = await Promise.all([api.follows(), api.persona()]);
    setFollows(f);
    setPersona(p);
  }, []);

  useEffect(() => {
    api
      .me()
      .then((u) => {
        setUser(u);
        return reload();
      })
      .catch(() => {
        window.location.href = "/";
      });
  }, [reload]);

  useEffect(() => {
    messagesRef.current?.scrollTo({
      top: messagesRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [turns, sending]);

  async function send(text: string) {
    const message = text.trim();
    if (!message || sending) return;

    setTurns((t) => [...t, { role: "user", content: message }]);
    setDraft("");
    setSending(true);

    try {
      const reply = await api.chat(message, sessionId);
      setSessionId(reply.session_id);
      setTurns((t) => [
        ...t,
        {
          role: "assistant",
          content: reply.answer,
          citations: reply.citations,
          grounded: reply.grounded,
          personaUpdated: reply.persona_updated,
        },
      ]);
      // A turn can teach the agent something new, so refresh the profile
      // rather than leaving the panel stale.
      if (reply.persona_updated) {
        api.persona().then(setPersona).catch(() => undefined);
      }
    } catch (err) {
      const detail = err instanceof ApiError ? err.message : "Request failed";
      setTurns((t) => [
        ...t,
        { role: "assistant", content: `Something went wrong: ${detail}`, grounded: false },
      ]);
    } finally {
      setSending(false);
    }
  }

  if (!user) {
    return (
      <div className="login">
        <span className="spinner" />
      </div>
    );
  }

  return (
    <>
      <header className="topbar">
        <div className="brand">
          {/* Plain <img>: the export is static, so next/image would only add
              a wrapper around the same request. */}
          <img className="brand-mark" src="/logo.png" alt="Sentellent" />
          Sentellent <span className="brand-sub">Equity Analyst</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span className="who">{user.email}</span>
          <button
            className="ghost"
            onClick={async () => {
              await api.logout();
              window.location.href = "/";
            }}
          >
            Sign out
          </button>
        </div>
      </header>

      <div className="shell">
        <div className="col">
          <Watchlist follows={follows} onChange={reload} />
        </div>

        <main className="col panel chat">
          <div className="panel-head">
            <h2 className="panel-title">Research chat</h2>
            <span className="meta">Answers cite only ingested sources</span>
          </div>

          <div className="messages" ref={messagesRef}>
            {turns.length === 0 && <ChatEmptyState hasStocks={(follows?.length ?? 0) > 0} />}
            {turns.map((turn, i) => (
              <TurnView key={i} turn={turn} />
            ))}
            {sending && <Thinking />}
          </div>

          {turns.length === 0 && (
            <div className="suggestions">
              {STARTERS.map((s, i) => (
                <button key={s} onClick={() => send(s)} style={{ animationDelay: `${i * 50}ms` }}>
                  {s}
                </button>
              ))}
            </div>
          )}

          <div className="composer">
            <textarea
              value={draft}
              placeholder="Ask about a stock you follow…"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send(draft);
                }
              }}
            />
            <button className="primary" disabled={sending || !draft.trim()} onClick={() => send(draft)}>
              Send
            </button>
          </div>
        </main>

        <div className="col col-profile">
          <ProfilePanel persona={persona} onChange={reload} />
        </div>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------------- */
function Watchlist({ follows, onChange }: { follows: Follow[] | null; onChange: () => void }) {
  const [ticker, setTicker] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function add() {
    const symbol = ticker.trim().toUpperCase();
    if (!symbol) return;
    setBusy(true);
    setError(null);
    try {
      await api.follow(symbol);
      setTicker("");
      await onChange();
      // Ingestion is a background job; check back once so the card fills in
      // without the user having to reload.
      setTimeout(onChange, 15000);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not follow that ticker");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel fill">
      <div className="panel-head">
        <h2 className="panel-title">Watchlist</h2>
        {follows && follows.length > 0 && <span className="meta">{follows.length} following</span>}
      </div>
      <div className="panel-body scroll">
        <div style={{ display: "flex", gap: 8, marginBottom: 13 }}>
          <input
            value={ticker}
            placeholder="NSE symbol — RELIANCE, TCS, ITC"
            onChange={(e) => setTicker(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && add()}
          />
          <button className="primary" onClick={add} disabled={busy || !ticker.trim()}>
            {busy ? <span className="spinner" /> : "Follow"}
          </button>
        </div>
        {error && <div className="error">{error}</div>}

        {follows === null ? (
          <>
            <StockCardSkeleton />
            <div style={{ height: 9 }} />
            <StockCardSkeleton />
          </>
        ) : follows.length === 0 ? (
          <div className="empty" style={{ padding: "18px 0" }}>
            <div className="empty-title">No stocks yet</div>
            Follow an NSE ticker to pull its screener.in fundamentals, a year of
            prices, and recent Indian financial news into the vector store.
          </div>
        ) : (
          follows.map((f, i) => (
            <StockCard key={f.ticker} stock={f} index={i} onChange={onChange} />
          ))
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------- */
function ProfilePanel({
  persona,
  onChange,
}: {
  persona: PersonaView | null;
  onChange: () => void;
}) {
  const [resetting, setResetting] = useState(false);
  if (!persona) return null;

  const hasProfile = persona.facts.length > 0 || persona.rules.length > 0;
  const weights = Object.entries(persona.weights).sort((a, b) => b[1] - a[1]);
  // Weights can be non-neutral with no facts left — earlier versions removed
  // the fact without the numbers it had produced. Offering reset whenever
  // *anything* is off-default means that state is recoverable from the UI
  // rather than needing the database.
  const hasAnything =
    hasProfile || weights.some(([, w]) => Math.abs((num(w) ?? 0.5) - 0.5) > 0.001);

  async function reset() {
    setResetting(true);
    try {
      await api.resetPersona();
      await onChange();
    } finally {
      setResetting(false);
    }
  }

  return (
    <div className="panel fill">
      <div className="panel-head">
        <h2 className="panel-title">Investor profile</h2>
        <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
          {hasProfile && <span className="badge accent">learned</span>}
          {hasAnything && (
            <button
              className="ghost danger"
              onClick={reset}
              disabled={resetting}
              title="Forget everything learned about you"
            >
              {resetting ? <span className="spinner" /> : "Reset"}
            </button>
          )}
        </div>
      </div>
      <div className="panel-body scroll">
        {!hasProfile ? (
          <div className="empty">
            <div className="empty-title">Nothing learned yet</div>
            Tell the analyst how you invest — &ldquo;I&apos;m conservative and
            dividend-focused, and I avoid companies with debt-to-equity above
            0.5&rdquo; — and it will remember, and apply it to every
            recommendation.
          </div>
        ) : (
          <>
            {persona.facts.map((f, i) => (
              <div className="fact" key={f.id} style={{ animationDelay: `${i * 40}ms` }}>
                <span>{f.fact}</span>
                <button
                  className="ghost danger icon"
                  title="Forget this"
                  onClick={() => api.forgetFact(f.id).then(onChange)}
                >
                  ×
                </button>
              </div>
            ))}

            {persona.rules.length > 0 && (
              <div style={{ marginTop: 14 }}>
                <div className="panel-title" style={{ marginBottom: 7 }}>
                  Screening rules
                </div>
                {persona.rules.map((r, i) => (
                  <div key={i}>
                    <span className="rule">
                      {r.field.replace(/_/g, " ")} {r.op} {formatNumber(r.value, 2)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}

        <div style={{ marginTop: hasProfile ? 18 : 16 }}>
          <div className="panel-title" style={{ marginBottom: 10 }}>
            Factor emphasis
          </div>
          {weights.map(([factor, weight]) => {
            const value = num(weight) ?? 0;
            // Anything still at the neutral default is not a preference, so it
            // is drawn muted — otherwise six identical bars read as six
            // deliberate choices.
            const isDefault = Math.abs(value - 0.5) < 0.001;
            return (
              <div className="weight" key={factor}>
                <div className="weight-label">
                  <span>{factor}</span>
                  <span className="num">{value.toFixed(2)}</span>
                </div>
                <div className={`bar${isDefault ? " muted" : ""}`}>
                  <div style={{ width: `${Math.round(value * 100)}%` }} />
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------- */
function ChatEmptyState({ hasStocks }: { hasStocks: boolean }) {
  return (
    <div className="empty" style={{ maxWidth: 460 }}>
      <div className="empty-title">
        {hasStocks ? "Ask anything about your watchlist" : "Follow a stock to begin"}
      </div>
      {hasStocks
        ? "Every answer is built only from ingested fundamentals and news, and each claim links back to its source. If the data doesn't support an answer, it will say so rather than guess."
        : "Add an NSE ticker on the left. Once its fundamentals and news are indexed, you can ask about sentiment, valuation, or what fits your investing style."}
    </div>
  );
}

function Thinking() {
  return (
    <div className="msg">
      <div className="msg-role">Analyst</div>
      <div className="empty" style={{ display: "flex", alignItems: "center", gap: 9 }}>
        <span className="spinner" />
        Retrieving sources and composing a grounded answer…
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------- */
function TurnView({ turn }: { turn: Turn }) {
  if (turn.role === "user") {
    return (
      <div className="msg user">
        <div className="msg-role">You</div>
        <div className="msg-body">{turn.content}</div>
      </div>
    );
  }

  return (
    <div className="msg">
      <div className="msg-role">
        Analyst
        {turn.personaUpdated && <span className="badge accent">profile updated</span>}
      </div>
      <div className={`msg-body${turn.grounded === false ? " ungrounded" : ""}`}>
        {renderAnswer(turn.content, turn.citations ?? [])}
      </div>
      {turn.citations && turn.citations.length > 0 && (
        <div className="sources">
          <div className="sources-title">
            {turn.citations.length} source{turn.citations.length === 1 ? "" : "s"}
          </div>
          {turn.citations.map((c) => (
            <div className="source" key={c.n} id={`cite-${c.n}`}>
              <span className="source-num">{c.n}</span>
              <div>
                {c.url ? (
                  <a
                    className="source-title"
                    href={c.url}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    {c.title}
                  </a>
                ) : (
                  <span className="source-title">{c.title}</span>
                )}
                <div className="source-meta">
                  {c.source}
                  {c.ticker ? ` · ${c.ticker}` : ""}
                  {c.published_at
                    ? ` · ${new Date(c.published_at).toLocaleDateString("en-IN", {
                        day: "numeric",
                        month: "short",
                        year: "numeric",
                      })}`
                    : ""}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Render an answer as structured content.
 *
 * The model replies in Markdown — bold for stock names, asterisk bullets for
 * findings. Rendering it raw surfaced literal `**` around every company name,
 * which reads as broken output. This handles the small subset the analyst
 * prompt actually produces rather than pulling in a full Markdown library and
 * its sanitiser for four constructs.
 */
function renderAnswer(text: string, citations: Citation[]) {
  return text.split("\n").map((line, i) => {
    if (!line.trim()) return <div key={i} style={{ height: "0.55em" }} />;

    const bullet = line.match(/^(\s*)[*-]\s+(.*)$/);
    if (bullet) {
      const depth = Math.min(Math.floor(bullet[1].length / 2), 3);
      return (
        <div key={i} className="md-bullet" style={{ marginLeft: depth * 15 }}>
          <span className="md-dot">•</span>
          <span>{renderInline(bullet[2], citations)}</span>
        </div>
      );
    }

    const heading = line.match(/^#{1,4}\s+(.*)$/);
    if (heading) {
      return (
        <div key={i} className="md-heading">
          {renderInline(heading[1], citations)}
        </div>
      );
    }

    return <div key={i}>{renderInline(line, citations)}</div>;
  });
}

/** Apply bold emphasis, then citation links, to a single line. */
function renderInline(line: string, citations: Citation[]) {
  return line.split(/(\*\*[^*]+\*\*)/g).map((part, i) => {
    const bold = part.match(/^\*\*([^*]+)\*\*$/);
    if (bold) {
      return <strong key={i}>{renderWithCitations(bold[1], citations)}</strong>;
    }
    return <span key={i}>{renderWithCitations(part, citations)}</span>;
  });
}

/**
 * Turn inline [1] / [2, 3] markers into links that jump to the source list.
 * The numbering comes from the retrieval layer, so a marker with no matching
 * source is left as plain text rather than linking nowhere.
 */
function renderWithCitations(text: string, citations: Citation[]) {
  const valid = new Set(citations.map((c) => c.n));

  return text.split(/(\[\d+(?:\s*,\s*\d+)*\])/g).map((part, i) => {
    const match = part.match(/^\[(\d+(?:\s*,\s*\d+)*)\]$/);
    if (!match) return <span key={i}>{part}</span>;

    const numbers = match[1].split(",").map((n) => parseInt(n.trim(), 10));
    if (!numbers.every((n) => valid.has(n))) return <span key={i}>{part}</span>;

    return (
      <span
        key={i}
        className="cite"
        onClick={() => {
          const el = document.getElementById(`cite-${numbers[0]}`);
          el?.scrollIntoView({ behavior: "smooth", block: "center" });
          // Brief highlight, so the eye lands on the right row after the jump.
          el?.animate(
            [
              { background: "var(--accent-subtle)" },
              { background: "transparent" },
            ],
            { duration: 900, easing: "ease-out" },
          );
        }}
      >
        {part}
      </span>
    );
  });
}
