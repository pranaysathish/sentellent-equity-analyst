-- ---------------------------------------------------------------------------
-- 001_init: core schema for the agentic Indian-equity analyst.
--
-- Design notes that matter for correctness:
--   * Every externally-sourced row carries a deterministic `content_hash`
--     with a UNIQUE constraint, so re-running ingestion is a no-op upsert
--     rather than a duplicate insert.
--   * Embeddings are cached by (content_hash, model) and reused across
--     stocks/users, so following a second ticker never re-embeds shared news.
--   * Vector columns are fixed at 768 dims (see app/config.EMBED_DIM).
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- --------------------------------------------------------------------------
-- Identity
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    google_sub    text UNIQUE,                 -- Google's stable subject id
    email         text NOT NULL UNIQUE,
    name          text,
    picture_url   text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

-- --------------------------------------------------------------------------
-- Universe: stocks, the tickers users follow, and their fundamentals
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stocks (
    id           bigserial PRIMARY KEY,
    ticker       text NOT NULL UNIQUE,          -- NSE symbol, e.g. RELIANCE
    name         text NOT NULL,
    nse_id       text,                          -- NSE symbol (== ticker, kept explicit)
    bse_id       text,                          -- BSE scrip code, e.g. 500325
    sector       text,
    industry     text,
    isin         text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS follows (
    user_id    uuid   NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stock_id   bigint NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, stock_id)
);
CREATE INDEX IF NOT EXISTS follows_stock_idx ON follows (stock_id);

-- One live fundamentals row per stock; history is not required by the brief,
-- so ingestion upserts in place and stamps `as_of`.
CREATE TABLE IF NOT EXISTS fundamentals (
    stock_id          bigint PRIMARY KEY REFERENCES stocks(id) ON DELETE CASCADE,
    as_of             date,
    current_price     numeric(18,2),            -- INR
    market_cap_cr     numeric(18,2),            -- INR crore
    pe                numeric(12,2),
    pb                numeric(12,2),
    roe               numeric(12,2),            -- percent
    roce              numeric(12,2),            -- percent
    debt_to_equity    numeric(12,3),
    dividend_yield    numeric(12,3),            -- percent
    eps               numeric(18,2),            -- INR
    book_value        numeric(18,2),            -- INR
    face_value        numeric(18,2),            -- INR
    sales_growth_3y   numeric(12,2),            -- percent CAGR
    profit_growth_3y  numeric(12,2),            -- percent CAGR
    promoter_holding  numeric(12,2),            -- percent
    high_52w          numeric(18,2),            -- INR
    low_52w           numeric(18,2),            -- INR
    raw               jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_url        text,
    source_name       text,
    fetched_at        timestamptz NOT NULL DEFAULT now(),
    content_hash      text                       -- skips re-embedding unchanged data
);

-- Momentum/volatility derived from price history (yfinance ".NS").
CREATE TABLE IF NOT EXISTS price_metrics (
    stock_id       bigint PRIMARY KEY REFERENCES stocks(id) ON DELETE CASCADE,
    last_close     numeric(18,2),               -- INR
    return_1m      numeric(12,4),               -- fraction, 0.05 = +5%
    return_3m      numeric(12,4),
    return_6m      numeric(12,4),
    return_1y      numeric(12,4),
    volatility_1y  numeric(12,4),               -- annualised stdev of daily returns
    drawdown_1y    numeric(12,4),
    fetched_at     timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- News corpus
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news_articles (
    id            bigserial PRIMARY KEY,
    -- sha256 over (normalised title + canonical url); the idempotency key.
    content_hash  text NOT NULL UNIQUE,
    url           text NOT NULL,
    canonical_url text,
    title         text NOT NULL,
    source        text NOT NULL,               -- 'Moneycontrol', 'Economic Times', ...
    author        text,
    published_at  timestamptz,
    summary       text,
    body          text,
    language      text DEFAULT 'en',
    -- Set when this article is judged a near-duplicate of an earlier one.
    duplicate_of  bigint REFERENCES news_articles(id) ON DELETE SET NULL,
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    tagged_at     timestamptz                  -- when the LLM sentiment pass ran
);
CREATE INDEX IF NOT EXISTS news_published_idx  ON news_articles (published_at DESC);
CREATE INDEX IF NOT EXISTS news_duplicate_idx  ON news_articles (duplicate_of);
CREATE INDEX IF NOT EXISTS news_untagged_idx   ON news_articles (tagged_at)
    WHERE tagged_at IS NULL;
CREATE INDEX IF NOT EXISTS news_title_trgm_idx ON news_articles
    USING gin (title gin_trgm_ops);

-- LLM-extracted, per-(article, stock) signal. This is the "from data" memory.
CREATE TABLE IF NOT EXISTS article_stock_signals (
    article_id  bigint NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
    stock_id    bigint NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
    sentiment   numeric(4,3) NOT NULL,          -- -1.000 .. +1.000
    impact      numeric(4,3) NOT NULL DEFAULT 0.5,  -- 0 .. 1 materiality
    event_type  text,                           -- earnings | debt | order_win | ...
    rationale   text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, stock_id)
);
CREATE INDEX IF NOT EXISTS signals_stock_idx ON article_stock_signals (stock_id);

-- Rolling, time-decayed sentiment per stock. Updated by ingestion, never by
-- the chat path, so reads during a conversation are a single indexed lookup.
CREATE TABLE IF NOT EXISTS stock_sentiment (
    stock_id       bigint PRIMARY KEY REFERENCES stocks(id) ON DELETE CASCADE,
    score          numeric(5,4) NOT NULL DEFAULT 0,  -- -1 .. +1, impact-weighted
    confidence     numeric(5,4) NOT NULL DEFAULT 0,  -- 0 .. 1, grows with volume
    article_count  integer NOT NULL DEFAULT 0,
    window_days    integer NOT NULL DEFAULT 30,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- RAG: chunks + the embedding cache
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc_chunks (
    id            bigserial PRIMARY KEY,
    kind          text   NOT NULL CHECK (kind IN ('news', 'fundamentals', 'persona')),
    article_id    bigint REFERENCES news_articles(id) ON DELETE CASCADE,
    stock_id      bigint REFERENCES stocks(id) ON DELETE CASCADE,
    user_id       uuid   REFERENCES users(id) ON DELETE CASCADE,
    chunk_index   integer NOT NULL DEFAULT 0,
    content       text   NOT NULL,
    content_hash  text   NOT NULL,
    token_estimate integer NOT NULL DEFAULT 0,
    embedding     vector(768),
    metadata      jsonb  NOT NULL DEFAULT '{}'::jsonb,
    -- Precomputed lexical vector for the keyword half of hybrid retrieval.
    tsv           tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- The idempotency guarantee for indexing: the same chunk text for the same
-- (kind, article, stock) can only ever exist once, so concurrent ingestion
-- runs collide on the constraint and ON CONFLICT DO NOTHING keeps the store clean.
CREATE UNIQUE INDEX IF NOT EXISTS doc_chunks_dedupe_idx
    ON doc_chunks (kind, content_hash, COALESCE(article_id, 0), COALESCE(stock_id, 0));

CREATE INDEX IF NOT EXISTS doc_chunks_stock_idx ON doc_chunks (stock_id);
CREATE INDEX IF NOT EXISTS doc_chunks_tsv_idx   ON doc_chunks USING gin (tsv);
-- HNSW beats IVFFlat here: no training step, and recall stays good as the
-- corpus grows incrementally (which is exactly our write pattern).
CREATE INDEX IF NOT EXISTS doc_chunks_embedding_idx ON doc_chunks
    USING hnsw (embedding vector_cosine_ops);

-- Content-addressed embedding cache. Two users following the same stock, or a
-- story syndicated across three outlets, cost exactly one embedding call.
CREATE TABLE IF NOT EXISTS embedding_cache (
    content_hash text NOT NULL,
    model        text NOT NULL,
    embedding    vector(768) NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (content_hash, model)
);

-- --------------------------------------------------------------------------
-- Long-term memory: investor persona
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS persona_facts (
    id           bigserial PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fact         text NOT NULL,
    category     text NOT NULL DEFAULT 'preference',
    -- Machine-usable form of the fact, e.g.
    --   {"type":"rule","field":"debt_to_equity","op":"<=","value":0.5}
    --   {"type":"weight","factor":"dividend","value":0.9}
    structured   jsonb NOT NULL DEFAULT '{}'::jsonb,
    confidence   numeric(4,3) NOT NULL DEFAULT 0.8,
    fact_hash    text NOT NULL,                 -- dedupes repeated statements
    active       boolean NOT NULL DEFAULT true,
    source       text NOT NULL DEFAULT 'chat',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS persona_facts_unique_idx
    ON persona_facts (user_id, fact_hash);
CREATE INDEX IF NOT EXISTS persona_facts_user_idx ON persona_facts (user_id)
    WHERE active;

CREATE TABLE IF NOT EXISTS user_persona (
    user_id     uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    summary     text NOT NULL DEFAULT '',
    -- Normalised 0..1 preference weights over the scoring factors.
    weights     jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Hard constraints applied as filters before ranking.
    rules       jsonb NOT NULL DEFAULT '[]'::jsonb,
    embedding   vector(768),
    version     integer NOT NULL DEFAULT 1,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- Conversations
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_sessions (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      text NOT NULL DEFAULT 'New conversation',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_sessions_user_idx
    ON chat_sessions (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         bigserial PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role       text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content    text NOT NULL,
    -- [{ "n":1, "kind":"news", "title":..., "url":..., "source":..., "published_at":... }]
    citations  jsonb NOT NULL DEFAULT '[]'::jsonb,
    meta       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_session_idx
    ON chat_messages (session_id, id);

-- --------------------------------------------------------------------------
-- Ingestion observability / idempotency ledger
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_runs (
    id          bigserial PRIMARY KEY,
    stock_id    bigint REFERENCES stocks(id) ON DELETE CASCADE,
    kind        text NOT NULL,                  -- fundamentals | news | prices | full
    status      text NOT NULL DEFAULT 'running',-- running | ok | skipped | error
    trigger     text NOT NULL DEFAULT 'manual', -- manual | follow | schedule
    stats       jsonb NOT NULL DEFAULT '{}'::jsonb,
    error       text,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS ingest_runs_stock_idx
    ON ingest_runs (stock_id, started_at DESC);
