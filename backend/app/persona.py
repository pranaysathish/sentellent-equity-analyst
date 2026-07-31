"""Investor persona: learning it from chat, and scoring stocks against it.

Two halves that deliberately sit on opposite sides of a line:

* **Learning** the persona is a language problem — "I avoid high-debt names"
  has to become a machine-readable rule — so an LLM does that, once, when the
  user says something new about themselves.
* **Applying** the persona is arithmetic, so it is arithmetic. Factor scores
  come from stored fundamentals, hard rules are SQL-style filters, and the
  ranking is a weighted sum. No LLM call per stock, per query.

That split is the point. Recommending across 50 followed tickers costs zero
model calls and is fast, deterministic, and unit-testable.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from . import db, llm

log = logging.getLogger(__name__)

# The five factors the brief names, plus income which Indian retail investors
# ask about constantly ("dividend-focused" is the example in the spec itself).
FACTORS = ("growth", "value", "stability", "momentum", "quality", "income")

NEUTRAL_WEIGHTS: dict[str, float] = dict.fromkeys(FACTORS, 0.5)

# Fields a hard rule may reference. Restricting this is a safety measure: the
# rule values come from an LLM reading user text, and they end up in a SQL-ish
# filter, so anything outside this set is dropped rather than trusted.
RULE_FIELDS = {
    "debt_to_equity",
    "pe",
    "pb",
    "roe",
    "roce",
    "dividend_yield",
    "market_cap_cr",
    "promoter_holding",
    "volatility_1y",
    "sentiment",
}
RULE_OPS = {"<=", "<", ">=", ">", "==", "!="}

# Maximum amount news sentiment may move a stock's score, in either direction.
# Kept small on purpose: sentiment is noisy and LLM-derived, while the factor
# scores come from audited fundamentals. See `score_stock`.
SENTIMENT_TILT = 0.05


@dataclass
class Persona:
    summary: str = ""
    weights: dict[str, float] = field(default_factory=lambda: dict(NEUTRAL_WEIGHTS))
    rules: list[dict[str, Any]] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """Human-readable persona, used inside the answer prompt."""
        if not self.facts and not self.rules:
            return "No investor profile learned yet."
        parts = []
        if self.summary:
            parts.append(self.summary)
        if self.facts:
            parts.append("Known preferences: " + "; ".join(self.facts))
        if self.rules:
            readable = ", ".join(f"{r['field']} {r['op']} {r['value']}" for r in self.rules)
            parts.append(f"Hard rules: {readable}")
        leaning = sorted(self.weights.items(), key=lambda kv: -kv[1])[:3]
        parts.append("Emphasis: " + ", ".join(f"{k} {v:.2f}" for k, v in leaning))
        return " | ".join(parts)


# --------------------------------------------------------------------------- #
# Learning the persona from conversation
# --------------------------------------------------------------------------- #
EXTRACTOR_SYSTEM = f"""You extract a durable investor profile from what a user says.

Return ONLY JSON:
{{"facts": [{{"fact": "<short statement about the investor>",
             "category": "risk|style|sector|constraint|goal",
             "structured": {{...}} }}],
  "weights": {{"growth": 0.0-1.0, "value": 0.0-1.0, "stability": 0.0-1.0,
               "momentum": 0.0-1.0, "quality": 0.0-1.0, "income": 0.0-1.0}}}}

"structured" is optional and may be either:
  {{"type": "rule", "field": <one of {sorted(RULE_FIELDS)}>,
    "op": "<= | < | >= | > | == | !=", "value": <number>}}
  {{"type": "preference", "note": "<text>"}}

Only include a weight when the message genuinely implies it; omit the rest.
Only extract durable traits about the investor — never one-off questions about
a specific stock. If the message says nothing about who the investor is,
return {{"facts": [], "weights": {{}}}}."""


async def learn_from_message(user_id: str, message: str) -> bool:
    """Extract and persist any persona signal in a user message.

    Returns True when something was learned, so the caller can tell the user
    their profile was updated. Failures here are swallowed: a bad extraction
    must never break the chat.
    """
    if len(message.strip()) < 12:
        return False

    try:
        completion = await llm.complete(
            EXTRACTOR_SYSTEM,
            [llm.Message(role="user", content=message[:4000])],
            json_mode=True,
            max_tokens=2000,
            interactive=True,
        )
        payload = llm.parse_json(completion.text)
    except (llm.LLMError, llm.LLMRefusal) as exc:
        log.warning("persona extraction failed: %s", exc)
        return False

    if not isinstance(payload, dict):
        return False

    facts = payload.get("facts") or []
    weights = payload.get("weights") or {}
    if not facts and not weights:
        return False

    learned = False
    async with db.pool().acquire() as conn, conn.transaction():
        for entry in facts if isinstance(facts, list) else []:
            if await _store_fact(conn, user_id, entry):
                learned = True
        if isinstance(weights, dict) and weights and await _blend_weights(conn, user_id, weights):
            learned = True
        if learned:
            await _rebuild_persona_vector(conn, user_id)
    return learned


async def _store_fact(conn: asyncpg.Connection, user_id: str, entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    fact = str(entry.get("fact") or "").strip()
    if not fact:
        return False

    structured = entry.get("structured")
    structured = _sanitise_structured(structured)

    # Hashing the normalised text means repeating yourself doesn't pile up
    # duplicate rows — it just refreshes the existing one.
    fact_hash = hashlib.sha256(" ".join(fact.lower().split()).encode()).hexdigest()

    await conn.execute(
        """
        INSERT INTO persona_facts
            (user_id, fact, category, structured, fact_hash, source)
        VALUES ($1::uuid, $2, $3, $4::jsonb, $5, 'chat')
        ON CONFLICT (user_id, fact_hash) DO UPDATE
            SET active = true,
                structured = EXCLUDED.structured,
                updated_at = now()
        """,
        user_id,
        fact[:500],
        str(entry.get("category") or "preference")[:40],
        json.dumps(structured),
        fact_hash,
    )
    return True


def _sanitise_structured(structured: Any) -> dict[str, Any]:
    """Only let through rules we recognise, with numeric values.

    The value flows into a comparison against real fundamentals, so an
    unrecognised field or a non-numeric threshold is discarded rather than
    stored and half-applied later.
    """
    if not isinstance(structured, dict):
        return {}
    if structured.get("type") != "rule":
        return structured if structured.get("type") == "preference" else {}

    field_name = structured.get("field")
    op = structured.get("op")
    if field_name not in RULE_FIELDS or op not in RULE_OPS:
        log.debug("dropping unrecognised persona rule: %s", structured)
        return {}
    try:
        value = float(structured.get("value"))
    except (TypeError, ValueError):
        return {}
    return {"type": "rule", "field": field_name, "op": op, "value": value}


async def _blend_weights(conn: asyncpg.Connection, user_id: str, incoming: dict[str, Any]) -> bool:
    """Move stored weights toward what the user just said, rather than replacing.

    A single sentence shouldn't wipe out a profile built over many turns, but it
    should move the needle. An exponential blend does both, and keeps the
    profile stable when someone repeats themselves.
    """
    row = await conn.fetchrow("SELECT weights FROM user_persona WHERE user_id = $1::uuid", user_id)
    current = dict(NEUTRAL_WEIGHTS)
    if row and row["weights"]:
        stored = row["weights"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        current.update({k: float(v) for k, v in stored.items() if k in FACTORS})

    alpha = 0.6  # weight given to the new statement
    changed = False
    for factor, value in incoming.items():
        if factor not in FACTORS:
            continue
        try:
            target = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            continue
        blended = round((1 - alpha) * current[factor] + alpha * target, 4)
        if abs(blended - current[factor]) > 1e-6:
            changed = True
        current[factor] = blended

    if not changed:
        return False

    await conn.execute(
        """
        INSERT INTO user_persona (user_id, weights, updated_at)
        VALUES ($1::uuid, $2::jsonb, now())
        ON CONFLICT (user_id) DO UPDATE
            SET weights = EXCLUDED.weights,
                version = user_persona.version + 1,
                updated_at = now()
        """,
        user_id,
        json.dumps(current),
    )
    return True


async def _rebuild_persona_vector(conn: asyncpg.Connection, user_id: str) -> None:
    """Embed the persona so it can be compared against documents semantically.

    The numeric weights drive ranking; this vector is what lets retrieval bias
    toward material the investor actually cares about (say, dividend policy
    over order-book news) when their question is open-ended.
    """
    rows = await conn.fetch(
        "SELECT fact FROM persona_facts WHERE user_id = $1::uuid AND active "
        "ORDER BY updated_at DESC LIMIT 40",
        user_id,
    )
    if not rows:
        return

    summary = "Investor profile: " + "; ".join(r["fact"] for r in rows)
    vectors = await llm.embed([summary])

    await conn.execute(
        """
        INSERT INTO user_persona (user_id, summary, embedding, updated_at)
        VALUES ($1::uuid, $2, $3, now())
        ON CONFLICT (user_id) DO UPDATE
            SET summary = EXCLUDED.summary,
                embedding = EXCLUDED.embedding,
                version = user_persona.version + 1,
                updated_at = now()
        """,
        user_id,
        summary[:4000],
        vectors[0],
    )

    # The persona is also a retrievable document, so the agent can cite *why*
    # it believes something about the user.
    await conn.execute(
        """
        INSERT INTO doc_chunks
            (kind, user_id, chunk_index, content, content_hash, token_estimate,
             embedding, metadata)
        VALUES ('persona', $1::uuid, 0, $2, $3, $4, $5, '{}'::jsonb)
        ON CONFLICT (kind, content_hash, COALESCE(article_id, 0), COALESCE(stock_id, 0))
        DO NOTHING
        """,
        user_id,
        summary[:4000],
        hashlib.sha256(summary.encode()).hexdigest(),
        len(summary) // 4,
        vectors[0],
    )


async def load_persona(user_id: str) -> Persona:
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT summary, weights, rules FROM user_persona WHERE user_id = $1::uuid",
            user_id,
        )
        facts = await conn.fetch(
            "SELECT fact, structured FROM persona_facts "
            "WHERE user_id = $1::uuid AND active ORDER BY updated_at DESC LIMIT 40",
            user_id,
        )

    persona = Persona()
    if row:
        persona.summary = row["summary"] or ""
        stored = row["weights"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        if stored:
            persona.weights.update({k: float(v) for k, v in stored.items() if k in FACTORS})

    for record in facts:
        persona.facts.append(record["fact"])
        structured = record["structured"]
        if isinstance(structured, str):
            structured = json.loads(structured)
        if isinstance(structured, dict) and structured.get("type") == "rule":
            persona.rules.append(structured)

    return persona


# --------------------------------------------------------------------------- #
# Scoring stocks against the persona — pure arithmetic
# --------------------------------------------------------------------------- #
@dataclass
class StockScore:
    ticker: str
    name: str
    total: float
    factors: dict[str, float]
    excluded_by: str | None = None
    reason: str = ""
    sentiment: float = 0.0
    current_price: float | None = None


def _bounded(value: float | None, low: float, high: float, invert: bool = False) -> float | None:
    """Map a raw metric onto 0..1 by clamping to a sensible band.

    Returns None for missing data so it can be treated as "unknown" rather
    than silently scored as zero, which would punish thinly-covered stocks.
    """
    if value is None:
        return None
    clamped = max(low, min(high, float(value)))
    scaled = (clamped - low) / (high - low) if high > low else 0.0
    return 1.0 - scaled if invert else scaled


def compute_factors(row: dict[str, Any]) -> dict[str, float]:
    """Turn one stock's stored fundamentals into 0..1 factor scores.

    The bands are deliberately hard-coded and readable rather than fitted:
    this is a screening heuristic that a human can audit and a test can pin,
    which is what "efficient, testable logic" in the brief asks for.
    """

    def pick(*candidates: float | None) -> float:
        present = [c for c in candidates if c is not None]
        return sum(present) / len(present) if present else 0.5

    growth = pick(
        _bounded(row.get("sales_growth_3y"), -10, 40),
        _bounded(row.get("profit_growth_3y"), -10, 40),
    )
    # Low P/E and low P/B read as cheap, hence invert.
    value = pick(
        _bounded(row.get("pe"), 5, 80, invert=True),
        _bounded(row.get("pb"), 0.5, 15, invert=True),
    )
    stability = pick(
        _bounded(row.get("debt_to_equity"), 0, 2.5, invert=True),
        _bounded(row.get("volatility_1y"), 0.15, 0.75, invert=True),
        _bounded(row.get("drawdown_1y"), -0.6, 0.0),
    )
    momentum = pick(
        _bounded(row.get("return_3m"), -0.25, 0.35),
        _bounded(row.get("return_6m"), -0.35, 0.5),
        _bounded(row.get("return_1y"), -0.4, 0.7),
    )
    quality = pick(
        _bounded(row.get("roe"), 0, 35),
        _bounded(row.get("roce"), 0, 40),
        _bounded(row.get("promoter_holding"), 20, 75),
    )
    income = pick(_bounded(row.get("dividend_yield"), 0, 6))

    return {
        "growth": round(growth, 4),
        "value": round(value, 4),
        "stability": round(stability, 4),
        "momentum": round(momentum, 4),
        "quality": round(quality, 4),
        "income": round(income, 4),
    }


def check_rules(row: dict[str, Any], rules: Sequence[dict[str, Any]]) -> str | None:
    """Return the first rule this stock violates, or None if it passes.

    A rule referencing data we don't have does *not* exclude the stock —
    silently dropping a name because a field was missing would look like a
    recommendation failure rather than a data gap.
    """
    for rule in rules:
        field_name = rule.get("field")
        op = rule.get("op")
        threshold = rule.get("value")
        if field_name not in RULE_FIELDS or op not in RULE_OPS:
            continue

        actual = row.get(field_name)
        if actual is None:
            continue
        try:
            actual = float(actual)
            threshold = float(threshold)
        except (TypeError, ValueError):
            continue

        passes = {
            "<=": actual <= threshold,
            "<": actual < threshold,
            ">=": actual >= threshold,
            ">": actual > threshold,
            "==": actual == threshold,
            "!=": actual != threshold,
        }[op]
        if not passes:
            return f"{field_name} is {actual:g}, your rule requires {op} {threshold:g}"
    return None


def score_stock(row: dict[str, Any], persona: Persona) -> StockScore:
    """Score one stock: factor fit, sentiment tilt, then hard-rule veto."""
    factors = compute_factors(row)

    total_weight = sum(persona.weights.get(f, 0.5) for f in FACTORS) or 1.0
    fit = sum(factors[f] * persona.weights.get(f, 0.5) for f in FACTORS) / total_weight

    # Recent news breaks near-ties; it must not overturn the fundamentals.
    # The adjustment is bounded to ±SENTIMENT_TILT, so any pair of stocks
    # separated by more than 2 * SENTIMENT_TILT on fit keeps its ordering no
    # matter how the news reads. That invariant is pinned by a test.
    sentiment = float(row.get("sentiment") or 0.0)
    confidence = float(row.get("sentiment_confidence") or 0.0)
    total = fit + SENTIMENT_TILT * sentiment * confidence

    excluded_by = check_rules(row, persona.rules)

    top = sorted(
        ((f, factors[f] * persona.weights.get(f, 0.5)) for f in FACTORS),
        key=lambda kv: -kv[1],
    )[:2]
    reason = "strong on " + " and ".join(f"{name}" for name, _ in top)
    if sentiment > 0.15 and confidence > 0.2:
        reason += "; recent news skews positive"
    elif sentiment < -0.15 and confidence > 0.2:
        reason += "; recent news skews negative"

    return StockScore(
        ticker=row["ticker"],
        name=row["name"],
        total=round(max(0.0, min(1.5, total)), 4),
        factors=factors,
        excluded_by=excluded_by,
        reason=reason,
        sentiment=round(sentiment, 3),
        current_price=(
            float(row["current_price"]) if row.get("current_price") is not None else None
        ),
    )


async def rank_followed_stocks(
    user_id: str, persona: Persona, limit: int = 10
) -> tuple[list[StockScore], list[StockScore]]:
    """Score every stock the user follows. Returns (ranked, excluded).

    One query, then pure Python arithmetic — the cost is flat in the number of
    followed tickers and involves no model calls at all.
    """
    rows = await db.fetch(
        """
        SELECT s.ticker, s.name, s.sector,
               f.pe, f.pb, f.roe, f.roce, f.debt_to_equity, f.dividend_yield,
               f.market_cap_cr, f.promoter_holding, f.current_price,
               f.sales_growth_3y, f.profit_growth_3y,
               p.return_3m, p.return_6m, p.return_1y, p.volatility_1y,
               p.drawdown_1y,
               COALESCE(ss.score, 0)      AS sentiment,
               COALESCE(ss.confidence, 0) AS sentiment_confidence
          FROM follows fo
          JOIN stocks s            ON s.id = fo.stock_id
          LEFT JOIN fundamentals f ON f.stock_id = s.id
          LEFT JOIN price_metrics p ON p.stock_id = s.id
          LEFT JOIN stock_sentiment ss ON ss.stock_id = s.id
         WHERE fo.user_id = $1::uuid
        """,
        user_id,
    )

    scored = [score_stock(dict(r), persona) for r in rows]
    ranked = sorted((s for s in scored if s.excluded_by is None), key=lambda s: -s.total)
    excluded = [s for s in scored if s.excluded_by is not None]
    return ranked[:limit], excluded
