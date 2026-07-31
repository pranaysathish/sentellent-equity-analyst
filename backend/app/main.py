"""FastAPI application: routes, lifespan, and the scheduled-refresh hook.

Everything is mounted under /api because CloudFront routes that path prefix to
this service and serves the SPA for everything else. Same origin means the
session cookie is first-party and there is no CORS surface in production.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import agent, auth, db, ingest
from . import persona as persona_mod
from .auth import CurrentUser, get_current_user
from .config import settings

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await db.run_migrations()
    log.info(
        "started: env=%s llm=%s embeddings=%s",
        settings.environment,
        settings.llm_provider,
        settings.embedding_provider,
    )
    yield
    await db.disconnect()


app = FastAPI(
    title="Sentellent Equity Analyst",
    description="Agentic RAG analyst for NSE/BSE equities.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# Only needed for local development, where the Next.js dev server runs on a
# different port. In production CloudFront makes everything same-origin.
if not settings.is_prod:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_base_url, "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
def jsonable_row(row: Any) -> dict[str, Any]:
    """Convert a database record into JSON-friendly primitives.

    Postgres `numeric` columns arrive from asyncpg as `Decimal`, and Pydantic
    serialises `Decimal` to a JSON *string* to avoid float precision loss.
    That is defensible for money, but it means every ratio on the wire is
    `"24.5"` rather than `24.5`, and the browser then throws
    `toFixed is not a function` the moment it formats one — taking the whole
    React tree down with it.

    Precision is not a concern here: these are display ratios and prices
    already rounded to two decimals in the schema, well inside float64's exact
    range. So they are converted once, at the boundary, rather than every
    consumer having to defend against a string.
    """
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, dt.datetime):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


class FollowRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=32)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    citations: list[dict[str, Any]]
    intent: str
    grounded: bool
    persona_updated: bool
    tickers: list[str]


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/api/health")
async def health() -> dict[str, str]:
    """Liveness — deliberately does not touch the database."""
    return {"status": "ok", "environment": settings.environment}


@app.get("/api/health/ready")
async def ready() -> dict[str, Any]:
    """Readiness — the load balancer uses this, so it proves the DB is reachable."""
    try:
        await db.fetchval("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"database unavailable: {exc}"
        ) from exc
    return {"status": "ready", "database": "ok"}


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@app.get("/api/auth/google/login")
async def google_login() -> RedirectResponse:
    if not settings.google_oauth_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Google OAuth is not configured on this deployment.",
        )
    state = auth.make_state()
    response = RedirectResponse(auth.build_authorize_url(state))
    response.set_cookie(
        auth.STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=settings.is_prod,
        path="/",
    )
    return response


@app.get("/api/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = ""):
    cookie_state = request.cookies.get(auth.STATE_COOKIE)
    if not auth.verify_state(state, cookie_state):
        # Mismatched state means a forged or replayed callback.
        return RedirectResponse(f"{settings.frontend_base_url}/?error=invalid_state")
    if not code:
        return RedirectResponse(f"{settings.frontend_base_url}/?error=no_code")

    profile = await auth.exchange_code_for_profile(code)
    user = await auth.upsert_user(profile)

    response = RedirectResponse(f"{settings.frontend_base_url}/dashboard")
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_session(user.id, user.email),
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.is_prod,
        path="/",
    )
    response.delete_cookie(auth.STATE_COOKIE, path="/")
    return response


@app.post("/api/auth/dev-login")
async def dev_login(email: str = Query(default="dev@example.com")):
    """Passwordless login for local development and tests.

    Hard-disabled in prod by `Settings.dev_login_enabled`, so it cannot become
    an authentication bypass on the deployed app.
    """
    if not settings.dev_login_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    user = await auth.upsert_user({"email": email, "name": "Local Developer"})
    response = JSONResponse({"id": user.id, "email": user.email})
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_session(user.id, user.email),
        max_age=3600 * 24,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.get("/api/me")
async def me(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "picture_url": user.picture_url,
    }


# --------------------------------------------------------------------------- #
# Follows and stock data
# --------------------------------------------------------------------------- #
@app.get("/api/follows")
async def list_follows(user: CurrentUser = Depends(get_current_user)) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT s.ticker, s.name, s.sector, s.bse_id, s.nse_id,
               f.current_price, f.pe, f.roe, f.debt_to_equity, f.dividend_yield,
               f.market_cap_cr,
               p.return_1m, p.return_1y, p.volatility_1y,
               COALESCE(p.close_series, '[]'::jsonb) AS close_series,
               COALESCE(ss.score, 0) AS sentiment,
               COALESCE(ss.article_count, 0) AS article_count,
               fo.created_at AS followed_at,
               (SELECT max(finished_at) FROM ingest_runs r
                 WHERE r.stock_id = s.id AND r.status = 'ok') AS last_ingest_at
          FROM follows fo
          JOIN stocks s ON s.id = fo.stock_id
          LEFT JOIN fundamentals f ON f.stock_id = s.id
          LEFT JOIN price_metrics p ON p.stock_id = s.id
          LEFT JOIN stock_sentiment ss ON ss.stock_id = s.id
         WHERE fo.user_id = $1::uuid
         ORDER BY fo.created_at DESC
        """,
        user.id,
    )
    return [jsonable_row(r) for r in rows]


@app.post("/api/follows", status_code=status.HTTP_202_ACCEPTED)
async def follow(
    body: FollowRequest,
    background: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Follow a ticker and kick off ingestion.

    Ingestion runs in the background because scraping plus embedding takes tens
    of seconds — far too long to hold an HTTP request open. The endpoint is
    safe to call repeatedly: the advisory lock inside `ingest_ticker` makes a
    concurrent second call a no-op.
    """
    ticker = body.ticker.upper().strip()
    if not ticker.isalnum() and "-" not in ticker and "&" not in ticker:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid ticker format")

    existing = await db.fetchrow("SELECT id FROM stocks WHERE ticker = $1", ticker)
    if existing is None:
        # First-ever follow of this ticker: fetch synchronously so a typo comes
        # back as a clean 404 rather than a silent background failure.
        fundamentals = await ingest.sources.fetch_fundamentals(ticker)
        if fundamentals is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"Could not find {ticker} on screener.in. Use the NSE symbol, e.g. RELIANCE.",
            )
        async with db.pool().acquire() as conn:
            stock_id = await ingest.upsert_stock(conn, fundamentals)
    else:
        stock_id = existing["id"]

    await db.execute(
        "INSERT INTO follows (user_id, stock_id) VALUES ($1::uuid, $2) ON CONFLICT DO NOTHING",
        user.id,
        stock_id,
    )
    background.add_task(_ingest_safely, ticker, "follow")
    return {"ticker": ticker, "status": "following", "ingest": "started"}


async def _ingest_safely(ticker: str, trigger: str) -> None:
    try:
        result = await ingest.ingest_ticker(ticker, trigger=trigger)
        log.info("ingest %s: %s %s", ticker, result.status, result.as_stats())
    except Exception:  # noqa: BLE001
        log.exception("background ingest failed for %s", ticker)


@app.delete("/api/follows/{ticker}")
async def unfollow(ticker: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, str]:
    await db.execute(
        "DELETE FROM follows WHERE user_id = $1::uuid AND stock_id = "
        "(SELECT id FROM stocks WHERE ticker = $2)",
        user.id,
        ticker.upper(),
    )
    return {"ticker": ticker.upper(), "status": "unfollowed"}


@app.post("/api/follows/{ticker}/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh(
    ticker: str,
    background: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, str]:
    owned = await db.fetchval(
        "SELECT 1 FROM follows f JOIN stocks s ON s.id = f.stock_id "
        "WHERE f.user_id = $1::uuid AND s.ticker = $2",
        user.id,
        ticker.upper(),
    )
    if not owned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You do not follow that ticker")
    background.add_task(_ingest_safely, ticker.upper(), "manual")
    return {"ticker": ticker.upper(), "status": "refresh_started"}


@app.get("/api/stocks/{ticker}")
async def stock_detail(
    ticker: str, user: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    row = await db.fetchrow(
        """
        SELECT s.ticker, s.name, s.sector, s.industry, s.nse_id, s.bse_id,
               f.current_price, f.market_cap_cr, f.pe, f.pb, f.roe, f.roce,
               f.debt_to_equity, f.dividend_yield, f.eps, f.book_value,
               f.high_52w, f.low_52w, f.promoter_holding, f.source_url,
               f.fetched_at,
               p.return_1m, p.return_3m, p.return_6m, p.return_1y,
               p.volatility_1y,
               COALESCE(ss.score, 0) AS sentiment,
               COALESCE(ss.confidence, 0) AS sentiment_confidence,
               COALESCE(ss.article_count, 0) AS article_count
          FROM stocks s
          LEFT JOIN fundamentals f ON f.stock_id = s.id
          LEFT JOIN price_metrics p ON p.stock_id = s.id
          LEFT JOIN stock_sentiment ss ON ss.stock_id = s.id
         WHERE s.ticker = $1
        """,
        ticker.upper(),
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown ticker")

    news = await db.fetch(
        """
        SELECT a.title, a.url, a.source, a.published_at,
               sig.sentiment, sig.impact, sig.event_type, sig.rationale
          FROM article_stock_signals sig
          JOIN news_articles a ON a.id = sig.article_id
          JOIN stocks s ON s.id = sig.stock_id
         WHERE s.ticker = $1 AND a.duplicate_of IS NULL
         ORDER BY a.published_at DESC NULLS LAST
         LIMIT 20
        """,
        ticker.upper(),
    )
    return {**jsonable_row(row), "news": [jsonable_row(n) for n in news]}


# --------------------------------------------------------------------------- #
# Persona and recommendations
# --------------------------------------------------------------------------- #
@app.get("/api/persona")
async def get_persona(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    p = await persona_mod.load_persona(user.id)
    facts = await db.fetch(
        "SELECT id, fact, category, structured, created_at FROM persona_facts "
        "WHERE user_id = $1::uuid AND active ORDER BY updated_at DESC",
        user.id,
    )
    return {
        "summary": p.summary,
        "weights": p.weights,
        "rules": p.rules,
        "facts": [jsonable_row(f) for f in facts],
    }


@app.delete("/api/persona/facts/{fact_id}")
async def forget_fact(
    fact_id: int, user: CurrentUser = Depends(get_current_user)
) -> dict[str, str]:
    """Soft-delete so the user stays in control of what the agent remembers.

    Clearing the derived weights when the last fact goes is handled in the
    persona module, alongside the logic that set them.
    """
    await persona_mod.forget_fact(user.id, fact_id)
    return {"status": "forgotten"}


@app.delete("/api/persona")
async def reset_persona(user: CurrentUser = Depends(get_current_user)) -> dict[str, str]:
    """Forget everything learned about this investor.

    Exposed because the profile is built from things the user said, so they
    should be able to take it back — and because derived state can otherwise
    outlive its inputs, which is exactly the inconsistency this fixes.
    """
    await persona_mod.reset_persona(user.id)
    return {"status": "reset"}


@app.get("/api/recommendations")
async def recommendations(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    p = await persona_mod.load_persona(user.id)
    ranked, excluded = await persona_mod.rank_followed_stocks(user.id, p)
    return {
        "persona": p.describe(),
        "ranked": [vars(s) for s in ranked],
        "excluded": [vars(s) for s in excluded],
    }


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
@app.get("/api/sessions")
async def list_sessions(user: CurrentUser = Depends(get_current_user)) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT id, title, created_at, updated_at FROM chat_sessions "
        "WHERE user_id = $1::uuid ORDER BY updated_at DESC LIMIT 50",
        user.id,
    )
    return [jsonable_row(r) for r in rows]


@app.get("/api/sessions/{session_id}/messages")
async def session_messages(
    session_id: str, user: CurrentUser = Depends(get_current_user)
) -> list[dict[str, Any]]:
    owned = await db.fetchval(
        "SELECT 1 FROM chat_sessions WHERE id = $1::uuid AND user_id = $2::uuid",
        session_id,
        user.id,
    )
    if not owned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown session")
    rows = await db.fetch(
        "SELECT role, content, citations, created_at FROM chat_messages "
        "WHERE session_id = $1::uuid ORDER BY id",
        session_id,
    )
    return [jsonable_row(r) for r in rows]


@app.post("/api/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, user: CurrentUser = Depends(get_current_user)) -> ChatResponse:
    session_id = body.session_id
    if session_id:
        owned = await db.fetchval(
            "SELECT 1 FROM chat_sessions WHERE id = $1::uuid AND user_id = $2::uuid",
            session_id,
            user.id,
        )
        if not owned:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown session")
    else:
        session_id = str(
            await db.fetchval(
                "INSERT INTO chat_sessions (user_id, title) VALUES ($1::uuid, $2) RETURNING id",
                user.id,
                body.message[:80],
            )
        )

    history = [
        {"role": r["role"], "content": r["content"]}
        for r in await db.fetch(
            "SELECT role, content FROM chat_messages WHERE session_id = $1::uuid "
            "ORDER BY id DESC LIMIT 6",
            session_id,
        )
    ][::-1]

    await db.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES ($1::uuid, 'user', $2)",
        session_id,
        body.message,
    )

    state = await agent.run(user_id=user.id, question=body.message, history=history)

    import json

    await db.execute(
        "INSERT INTO chat_messages (session_id, role, content, citations, meta) "
        "VALUES ($1::uuid, 'assistant', $2, $3::jsonb, $4::jsonb)",
        session_id,
        state.get("answer", ""),
        json.dumps(state.get("citations", [])),
        json.dumps(
            {
                "intent": state.get("intent"),
                "grounded": state.get("grounded"),
                "tickers": state.get("tickers", []),
            }
        ),
    )
    await db.execute("UPDATE chat_sessions SET updated_at = now() WHERE id = $1::uuid", session_id)

    return ChatResponse(
        session_id=session_id,
        answer=state.get("answer", ""),
        citations=state.get("citations", []),
        intent=state.get("intent", "general"),
        grounded=bool(state.get("grounded")),
        persona_updated=bool(state.get("persona_updated")),
        tickers=state.get("tickers", []),
    )


# --------------------------------------------------------------------------- #
# Scheduled refresh
# --------------------------------------------------------------------------- #
@app.post("/api/internal/refresh")
async def internal_refresh(request: Request) -> dict[str, Any]:
    """Refresh every followed ticker. Called by the scheduled task.

    Protected by a shared secret rather than a user session, since the caller
    is a machine. Returns per-ticker outcomes so a failed refresh is visible
    in the scheduler's logs instead of silently dropped.
    """
    expected = os.getenv("INTERNAL_REFRESH_TOKEN", "")
    provided = request.headers.get("x-internal-token", "")
    if not expected or provided != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid internal token")

    results = await ingest.refresh_all_followed()
    return {
        "refreshed": len(results),
        "results": [{"ticker": r.ticker, "status": r.status, **r.as_stats()} for r in results],
    }
