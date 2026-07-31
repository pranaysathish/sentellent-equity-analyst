"""Hybrid retrieval over the news + fundamentals corpus.

Vector search alone is weak on the things Indian equity questions turn on:
exact tickers, rupee figures, and quarter labels. Keyword search alone misses
paraphrase. So both run, and their ranks are fused.

Retrieval is also *scoped* — a user only ever searches inside the stocks they
follow. That keeps result sets small and relevant, and it means the vector
index does less work as the corpus grows.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from . import db, llm
from .config import settings

log = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    chunk_id: int
    kind: str
    content: str
    ticker: str | None
    stock_name: str | None
    title: str | None
    url: str | None
    source: str | None
    published_at: Any
    score: float
    vector_rank: int | None = None
    keyword_rank: int | None = None

    def citation_label(self) -> str:
        if self.kind == "news":
            return self.title or self.url or "news article"
        if self.kind == "fundamentals":
            return f"{self.stock_name or self.ticker} fundamentals (screener.in)"
        return "investor profile"


async def retrieve(
    query: str,
    *,
    user_id: str,
    tickers: Sequence[str] | None = None,
    top_k: int | None = None,
) -> list[RetrievedChunk]:
    """Fetch the most relevant chunks for a query, restricted to followed stocks.

    Returns [] rather than raising when the user follows nothing — the agent
    turns that into an honest "you haven't followed anything yet" answer.
    """
    top_k = top_k or settings.retrieval_top_k
    candidate_k = settings.retrieval_candidate_k

    query_vector = (await llm.embed([query], interactive=True))[0]
    ts_query = _to_tsquery(query)

    rows = await db.fetch(
        """
        WITH scope AS (
            SELECT s.id, s.ticker, s.name
              FROM stocks s
              JOIN follows f ON f.stock_id = s.id AND f.user_id = $1::uuid
             WHERE ($4::text[] IS NULL OR s.ticker = ANY($4::text[]))
        ),
        vector_hits AS (
            SELECT c.id,
                   ROW_NUMBER() OVER (ORDER BY c.embedding <=> $2) AS rank,
                   c.embedding <=> $2 AS distance
              FROM doc_chunks c
             WHERE c.embedding IS NOT NULL
               AND (
                     c.stock_id IN (SELECT id FROM scope)
                     OR (c.kind = 'persona' AND c.user_id = $1::uuid)
                   )
             ORDER BY c.embedding <=> $2
             LIMIT $5
        ),
        keyword_hits AS (
            SELECT c.id,
                   ROW_NUMBER() OVER (
                       ORDER BY ts_rank_cd(c.tsv, query) DESC
                   ) AS rank
              FROM doc_chunks c, to_tsquery('english', $3) AS query
             WHERE $3 <> ''
               AND c.tsv @@ query
               AND (
                     c.stock_id IN (SELECT id FROM scope)
                     OR (c.kind = 'persona' AND c.user_id = $1::uuid)
                   )
             LIMIT $5
        ),
        fused AS (
            SELECT COALESCE(v.id, k.id) AS id,
                   v.rank AS vector_rank,
                   k.rank AS keyword_rank,
                   -- Reciprocal rank fusion: robust to the two scores living
                   -- on totally different scales, and needs no tuning.
                   COALESCE(1.0 / (60 + v.rank), 0) * (1 - $6)
                 + COALESCE(1.0 / (60 + k.rank), 0) * $6 AS score
              FROM vector_hits v
              FULL OUTER JOIN keyword_hits k ON k.id = v.id
        )
        SELECT c.id AS chunk_id, c.kind, c.content, c.metadata,
               s.ticker, s.name AS stock_name,
               a.title, a.url, a.source, a.published_at,
               f.score, f.vector_rank, f.keyword_rank
          FROM fused f
          JOIN doc_chunks c ON c.id = f.id
          LEFT JOIN stocks s ON s.id = c.stock_id
          LEFT JOIN news_articles a ON a.id = c.article_id
         WHERE a.id IS NULL OR a.duplicate_of IS NULL
         ORDER BY f.score DESC
         LIMIT $7
        """,
        user_id,
        query_vector,
        ts_query,
        list(tickers) if tickers else None,
        candidate_k,
        settings.hybrid_keyword_weight,
        top_k,
    )

    results: list[RetrievedChunk] = []
    for row in rows:
        metadata = row["metadata"] or {}
        if isinstance(metadata, str):
            import json

            metadata = json.loads(metadata)
        results.append(
            RetrievedChunk(
                chunk_id=row["chunk_id"],
                kind=row["kind"],
                content=row["content"],
                ticker=row["ticker"],
                stock_name=row["stock_name"],
                title=row["title"] or metadata.get("title"),
                url=row["url"] or metadata.get("url") or metadata.get("source_url"),
                source=row["source"] or metadata.get("source"),
                published_at=row["published_at"],
                score=float(row["score"] or 0.0),
                vector_rank=row["vector_rank"],
                keyword_rank=row["keyword_rank"],
            )
        )
    return results


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_STOPWORDS = {
    "what",
    "which",
    "should",
    "would",
    "could",
    "about",
    "there",
    "their",
    "this",
    "that",
    "these",
    "those",
    "with",
    "from",
    "have",
    "has",
    "the",
    "and",
    "for",
    "are",
    "you",
    "your",
    "any",
    "how",
    "why",
    "when",
    "week",
    "tell",
    "give",
    "please",
    "stock",
    "stocks",
    "share",
    "shares",
}


def _to_tsquery(query: str) -> str:
    """Build a lenient OR-tsquery from the meaningful words in a question.

    OR rather than AND deliberately: this half only needs to surface plausible
    keyword candidates, and rank fusion sorts out which ones actually matter.
    """
    words = [w.lower() for w in _WORD.findall(query) if len(w) > 2 and w.lower() not in _STOPWORDS]
    return " | ".join(dict.fromkeys(words)) if words else ""


async def resolve_tickers(user_id: str, text: str) -> list[str]:
    """Find which of the user's followed tickers a question is about.

    Matches on the symbol and on distinctive words from the company name, so
    "how is Reliance doing" and "RELIANCE" both resolve. Returns [] when the
    question is general, which the caller reads as "search everything".
    """
    rows = await db.fetch(
        """
        SELECT s.ticker, s.name
          FROM follows f JOIN stocks s ON s.id = f.stock_id
         WHERE f.user_id = $1::uuid
        """,
        user_id,
    )
    haystack = text.lower()
    matched: list[str] = []
    for row in rows:
        ticker = row["ticker"]
        if re.search(rf"\b{re.escape(ticker.lower())}\b", haystack):
            matched.append(ticker)
            continue
        for token in _significant_name_tokens(row["name"]):
            if token in haystack:
                matched.append(ticker)
                break
    return list(dict.fromkeys(matched))


_NAME_NOISE = {
    "ltd",
    "limited",
    "india",
    "indian",
    "company",
    "corporation",
    "corp",
    "industries",
    "enterprises",
    "group",
    "bank",
    "services",
    "the",
}


def _significant_name_tokens(name: str) -> list[str]:
    tokens = [t.lower() for t in _WORD.findall(name) if len(t) > 3 and t.lower() not in _NAME_NOISE]
    return tokens[:2]


def build_context(chunks: Sequence[RetrievedChunk]) -> tuple[str, list[dict[str, Any]]]:
    """Render retrieved chunks as numbered sources for the prompt.

    The numbering is the contract with the model: it is told to cite [1], [2],
    and the same numbers come back to the frontend as clickable links. Anything
    the model asserts without one of these numbers is, by construction,
    ungrounded.
    """
    blocks: list[str] = []
    citations: list[dict[str, Any]] = []

    for index, chunk in enumerate(chunks, start=1):
        published = (
            chunk.published_at.strftime("%d %b %Y")
            if getattr(chunk.published_at, "strftime", None)
            else "date unknown"
        )
        header = (
            f"[{index}] {chunk.citation_label()} — {chunk.source or 'screener.in'}, {published}"
        )
        if chunk.ticker:
            header += f" (about {chunk.ticker})"
        blocks.append(f"{header}\n{chunk.content}")

        citations.append(
            {
                "n": index,
                "kind": chunk.kind,
                "title": chunk.citation_label(),
                "url": chunk.url,
                "source": chunk.source or "screener.in",
                "ticker": chunk.ticker,
                "published_at": (
                    chunk.published_at.isoformat()
                    if getattr(chunk.published_at, "isoformat", None)
                    else None
                ),
            }
        )

    return "\n\n".join(blocks), citations
