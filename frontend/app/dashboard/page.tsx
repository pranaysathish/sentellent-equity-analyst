"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  Citation,
  Follow,
  PersonaView,
  User,
  api,
  formatCrore,
  formatINR,
  formatNumber,
  formatPercent,
  num,
  sentimentLabel,
} from "@/lib/api";

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
  const [follows, setFollows] = useState<Follow[]>([]);
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
  }, [turns]);

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
      // A turn can teach the agent something new about the investor, so the
      // profile panel is refreshed rather than left stale.
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
      <div className="login-wrap">
        <p className="empty">
          <span className="spinner" /> Loading…
        </p>
      </div>
    );
  }

  return (
    <>
      <header className="topbar">
        <div className="brand">
          Sentellent <span>Equity Analyst</span>
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
        <aside>
          <WatchlistPanel follows={follows} onChange={reload} />
          <PersonaPanel persona={persona} onChange={reload} />
        </aside>

        <main className="panel chat">
          <h2>Research chat</h2>

          <div className="messages" ref={messagesRef}>
            {turns.length === 0 && (
              <div className="empty" style={{ marginBottom: 14 }}>
                Ask about the stocks you follow. Every answer is built only from
                ingested fundamentals and news, and each claim is cited.
              </div>
            )}
            {turns.map((turn, i) => (
              <TurnView key={i} turn={turn} />
            ))}
            {sending && (
              <div className="msg">
                <div className="role">Analyst</div>
                <div className="empty">
                  <span className="spinner" /> Retrieving sources and composing a
                  grounded answer…
                </div>
              </div>
            )}
          </div>

          {turns.length === 0 && (
            <div className="suggestions">
              {STARTERS.map((s) => (
                <button key={s} onClick={() => send(s)}>
                  {s}
                </button>
              ))}
            </div>
          )}

          <div className="composer">
            <textarea
              value={draft}
              placeholder="e.g. What's the sentiment on TCS this week?"
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
      </div>
    </>
  );
}

/* ------------------------------------------------------------------------- */
function TurnView({ turn }: { turn: Turn }) {
  if (turn.role === "user") {
    return (
      <div className="msg user">
        <div className="role">You</div>
        <div className="body">{turn.content}</div>
      </div>
    );
  }

  return (
    <div className="msg">
      <div className="role">
        Analyst
        {turn.personaUpdated && (
          <span className="pill pos" style={{ marginLeft: 8 }}>
            profile updated
          </span>
        )}
      </div>
      <div className={`body${turn.grounded === false ? " ungrounded" : ""}`}>
        {renderAnswer(turn.content, turn.citations ?? [])}
      </div>
      {turn.citations && turn.citations.length > 0 && (
        <div className="sources">
          <h4>Sources</h4>
          {turn.citations.map((c) => (
            <div className="source" key={c.n} id={`cite-${c.n}`}>
              <span className="num">[{c.n}]</span>
              <span>
                {c.url ? (
                  <a href={c.url} target="_blank" rel="noreferrer noopener">
                    {c.title}
                  </a>
                ) : (
                  c.title
                )}
                <span className="meta">
                  {" — "}
                  {c.source}
                  {c.ticker ? ` · ${c.ticker}` : ""}
                  {c.published_at ? ` · ${new Date(c.published_at).toLocaleDateString("en-IN")}` : ""}
                </span>
              </span>
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
 * findings. Rendered as raw text that surfaced as literal `**` around every
 * company name, which reads as broken output. This handles the small subset
 * the analyst prompt actually produces (bold, bullets, nesting) rather than
 * pulling in a full Markdown library and its sanitiser for four constructs.
 *
 * Everything still passes through the citation linker, so `**TCS** [3]` keeps
 * both its emphasis and its clickable source.
 */
function renderAnswer(text: string, citations: Citation[]) {
  const lines = text.split("\n");

  return lines.map((line, i) => {
    if (!line.trim()) return <div key={i} style={{ height: "0.5em" }} />;

    // Leading asterisks or dashes denote a bullet; the indent before them
    // denotes nesting depth.
    const bullet = line.match(/^(\s*)[*-]\s+(.*)$/);
    if (bullet) {
      const depth = Math.min(Math.floor(bullet[1].length / 2), 3);
      return (
        <div key={i} className="md-bullet" style={{ marginLeft: depth * 16 }}>
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
  // Split on **bold** and *italic*, keeping the delimiters so they can be
  // matched and replaced with real elements.
  const parts = line.split(/(\*\*[^*]+\*\*)/g);

  return parts.map((part, i) => {
    const bold = part.match(/^\*\*([^*]+)\*\*$/);
    if (bold) {
      return (
        <strong key={i}>{renderWithCitations(bold[1], citations)}</strong>
      );
    }
    return <span key={i}>{renderWithCitations(part, citations)}</span>;
  });
}

/**
 * Turn inline [1] / [2, 3] markers into links that jump to the source list.
 * The numbering comes straight from the retrieval layer, so a marker with no
 * matching source is left as plain text rather than linking nowhere.
 */
function renderWithCitations(text: string, citations: Citation[]) {
  const valid = new Set(citations.map((c) => c.n));
  const parts = text.split(/(\[\d+(?:\s*,\s*\d+)*\])/g);

  return parts.map((part, i) => {
    const match = part.match(/^\[(\d+(?:\s*,\s*\d+)*)\]$/);
    if (!match) return <span key={i}>{part}</span>;

    const numbers = match[1].split(",").map((n) => parseInt(n.trim(), 10));
    if (!numbers.every((n) => valid.has(n))) return <span key={i}>{part}</span>;

    return (
      <span
        key={i}
        className="cite-marker"
        onClick={() =>
          document
            .getElementById(`cite-${numbers[0]}`)
            ?.scrollIntoView({ behavior: "smooth", block: "center" })
        }
      >
        {part}
      </span>
    );
  });
}

/* ------------------------------------------------------------------------- */
function WatchlistPanel({ follows, onChange }: { follows: Follow[]; onChange: () => void }) {
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
      // Ingestion runs in the background; poll once so fundamentals and the
      // first news batch appear without the user having to reload.
      setTimeout(onChange, 12000);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not follow that ticker");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <h2>Watchlist</h2>
      <div className="add-row">
        <input
          value={ticker}
          placeholder="NSE symbol, e.g. RELIANCE"
          onChange={(e) => setTicker(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
        />
        <button onClick={add} disabled={busy || !ticker.trim()}>
          {busy ? <span className="spinner" /> : "Follow"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}

      {follows.length === 0 ? (
        <p className="empty">
          No stocks followed yet. Add one to trigger ingestion of its fundamentals
          and recent Indian financial news.
        </p>
      ) : (
        follows.map((f) => {
          const tone = sentimentLabel(f.sentiment);
          return (
            <div className="stock" key={f.ticker}>
              <div className="stock-head">
                <div>
                  <span className="stock-ticker">{f.ticker}</span>
                  <span className="stock-name">{f.name}</span>
                </div>
                <span className={`pill ${tone.tone}`}>{tone.label}</span>
              </div>
              <div className="stock-metrics">
                <span>
                  <b>{formatINR(f.current_price)}</b>
                </span>
                <span>
                  P/E <b>{formatNumber(f.pe, 1)}</b>
                </span>
                <span>
                  RoE <b>{formatNumber(f.roe, 1, "%")}</b>
                </span>
                <span>
                  D/E <b>{formatNumber(f.debt_to_equity, 2)}</b>
                </span>
                <span>
                  Yield <b>{formatNumber(f.dividend_yield, 2, "%")}</b>
                </span>
                <span>
                  Mcap <b>{formatCrore(f.market_cap_cr)}</b>
                </span>
                <span>
                  1Y <b>{formatPercent(f.return_1y)}</b>
                </span>
                <span>{f.article_count} articles</span>
              </div>
              <div className="stock-actions">
                <button className="ghost" onClick={() => api.refresh(f.ticker).then(() => setTimeout(onChange, 10000))}>
                  Refresh
                </button>
                <button
                  className="ghost danger"
                  onClick={() => api.unfollow(f.ticker).then(onChange)}
                >
                  Unfollow
                </button>
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}

/* ------------------------------------------------------------------------- */
function PersonaPanel({ persona, onChange }: { persona: PersonaView | null; onChange: () => void }) {
  if (!persona) return null;

  const hasProfile = persona.facts.length > 0 || persona.rules.length > 0;

  return (
    <div className="panel">
      <h2>Investor profile</h2>

      {!hasProfile ? (
        <p className="empty">
          Nothing learned yet. Say something like &ldquo;I&apos;m conservative and
          dividend-focused, and I avoid companies with debt-to-equity above
          0.5&rdquo; — it will be remembered and applied to every recommendation.
        </p>
      ) : (
        <>
          {persona.facts.map((f) => (
            <div className="fact" key={f.id}>
              <span>{f.fact}</span>
              <button
                className="ghost danger"
                title="Forget this"
                onClick={() => api.forgetFact(f.id).then(onChange)}
              >
                ×
              </button>
            </div>
          ))}

          {persona.rules.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <h2 style={{ fontSize: 12 }}>Screening rules</h2>
              {persona.rules.map((r, i) => (
                <div key={i} className="fact">
                  <code style={{ fontSize: 12.5 }}>
                    {r.field.replace(/_/g, " ")} {r.op} {formatNumber(r.value, 2)}
                  </code>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      <div style={{ marginTop: 14 }}>
        <h2 style={{ fontSize: 12 }}>Factor emphasis</h2>
        {Object.entries(persona.weights)
          .sort((a, b) => b[1] - a[1])
          .map(([factor, weight]) => (
            <div className="weight" key={factor}>
              <div className="label">
                <span>{factor}</span>
                <span>{formatNumber(weight, 2)}</span>
              </div>
              <div className="bar">
                <div style={{ width: `${Math.round((num(weight) ?? 0) * 100)}%` }} />
              </div>
            </div>
          ))}
      </div>
    </div>
  );
}
