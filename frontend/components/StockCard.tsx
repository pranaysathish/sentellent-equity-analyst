"use client";

import { useState } from "react";
import {
  Follow,
  api,
  formatCrore,
  formatINR,
  formatNumber,
  formatPercent,
  num,
  sentimentLabel,
} from "@/lib/api";
import { Sparkline } from "./Sparkline";

export function StockCard({
  stock,
  index,
  onChange,
}: {
  stock: Follow;
  index: number;
  onChange: () => void;
}) {
  const [busy, setBusy] = useState<"refresh" | "unfollow" | null>(null);
  const tone = sentimentLabel(stock.sentiment);
  const change = num(stock.return_1y);
  const direction = change === null ? "flat" : change > 0 ? "up" : change < 0 ? "down" : "flat";

  async function refresh() {
    setBusy("refresh");
    try {
      await api.refresh(stock.ticker);
      // Ingestion runs in the background, so poll once rather than leaving the
      // card looking unchanged.
      setTimeout(onChange, 12000);
    } finally {
      setTimeout(() => setBusy(null), 1200);
    }
  }

  async function unfollow() {
    setBusy("unfollow");
    await api.unfollow(stock.ticker);
    onChange();
  }

  return (
    <div
      className="stock"
      // Staggered so a watchlist assembles rather than appearing at once.
      // Capped, or a long list would take noticeably longer to settle.
      style={{ animationDelay: `${Math.min(index * 45, 300)}ms` }}
    >
      <div className="stock-head">
        <div>
          <span className="stock-ticker">{stock.ticker}</span>
          <span className="stock-name">{stock.name}</span>
        </div>
        <span className={`badge ${tone.tone}`}>
          <span className="badge-dot" />
          {tone.label}
        </span>
      </div>

      <div className="stock-price-row">
        <span className="stock-price num">{formatINR(stock.current_price)}</span>
        <span className={`delta num ${direction}`}>
          {change === null ? "—" : `${formatPercent(change)} 1Y`}
        </span>
      </div>

      {stock.close_series?.length > 1 && (
        <Sparkline series={stock.close_series} positive={direction === "up"} />
      )}

      <div className="metrics">
        <div className="metric">
          <span className="metric-label">P/E</span>
          <span className="metric-value num">{formatNumber(stock.pe, 1)}</span>
        </div>
        <div className="metric">
          <span className="metric-label">RoE</span>
          <span className="metric-value num">{formatNumber(stock.roe, 1, "%")}</span>
        </div>
        <div className="metric">
          <span className="metric-label">Yield</span>
          <span className="metric-value num">{formatNumber(stock.dividend_yield, 2, "%")}</span>
        </div>
        <div className="metric">
          <span className="metric-label">Mcap</span>
          <span className="metric-value num">{formatCrore(stock.market_cap_cr)}</span>
        </div>
      </div>

      <div className="stock-foot">
        <span className="meta">
          {stock.article_count > 0
            ? `${stock.article_count} article${stock.article_count === 1 ? "" : "s"} indexed`
            : "no news indexed yet"}
        </span>
        <div className="stock-actions">
          <button
            className="ghost"
            onClick={refresh}
            disabled={busy !== null}
            title="Re-fetch fundamentals and news"
          >
            {busy === "refresh" ? <span className="spinner" /> : "Refresh"}
          </button>
          <button
            className="ghost danger"
            onClick={unfollow}
            disabled={busy !== null}
            title="Stop following"
          >
            Remove
          </button>
        </div>
      </div>
    </div>
  );
}

/** Shown while the watchlist is loading, matching the real card's geometry. */
export function StockCardSkeleton() {
  return (
    <div className="stock" aria-hidden>
      <div className="stock-head">
        <div style={{ flex: 1 }}>
          <div className="skeleton" style={{ width: 78, height: 14 }} />
          <div className="skeleton" style={{ width: 118, height: 10, marginTop: 6 }} />
        </div>
        <div className="skeleton" style={{ width: 62, height: 18, borderRadius: 999 }} />
      </div>
      <div className="skeleton" style={{ width: 108, height: 20, marginTop: 11 }} />
      <div className="skeleton" style={{ width: "100%", height: 34, marginTop: 10 }} />
      <div className="skeleton" style={{ width: "100%", height: 42, marginTop: 11 }} />
    </div>
  );
}
