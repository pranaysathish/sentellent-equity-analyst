# Technical Interview Prep

Everything you need to explain this project confidently. Read the **One-Minute
Pitch** and **The Five Things** first — those carry most interviews.

> A note on honesty: you built this with AI assistance, and Sentellent's own
> brief says that's fine ("we don't care if you write every line from scratch
> or if you orchestrate AI to do it for you"). What they'll test is whether you
> *understand* it. So don't claim you hand-typed every line. Do be able to
> explain every decision and defend the trade-offs. That's what this document
> is for.

---

## One-Minute Pitch

> "It's a research assistant for Indian stocks. You log in with Google, follow
> tickers like RELIANCE or TCS, and the system pulls that company's
> fundamentals from screener.in plus recent news from eight Indian financial
> RSS feeds. It chunks and embeds all of that into pgvector, and an LLM pass
> tags each article's sentiment and materiality per stock.
>
> Then you chat with it. Every answer is built only from retrieved sources and
> carries citations back to the exact article or fundamentals row, with figures
> in rupees. If the data doesn't support an answer, it says so instead of
> inventing a number.
>
> It also learns who you are. Tell it you're conservative and avoid high-debt
> companies, and that becomes durable memory — stored as facts, factor weights,
> and a machine-readable screening rule. Ask for recommendations later and it
> ranks your stocks against that profile and excludes ones your own rules rule
> out.
>
> It runs on AWS, built entirely with Terraform, and deploys itself from
> GitHub Actions on every push to main."

---

## The Five Things

If you remember nothing else, remember these. They're the decisions that
distinguish this from a wrapper around an LLM call.

### 1. Grounding is structural, not a prompt instruction

**The claim:** the agent cannot hallucinate an answer, by construction.

**Why:** in `agent.py`, the `answer` node checks whether retrieval returned
anything. If it returned nothing, it returns a fixed "I don't have that in the
data" message and **never calls the model at all**. You can't hallucinate a
response you were never asked to write.

Layered on top: sources are numbered `[1]`, `[2]` in the prompt, the same
numbers come back to the UI as clickable links, and the frontend only renders a
citation marker if that source number actually exists in the response.

**If they push:** "A prompt saying 'don't make things up' is a request. Cutting
off the model's ability to be called is a guarantee. I wanted the second one."

### 2. Applying the persona is arithmetic, not an LLM call

**The claim:** ranking 50 stocks against your profile costs **zero** model calls.

**Why:** there's a clean split in `persona.py`.
- *Learning* the persona is a language problem — "I avoid high-debt companies"
  has to become `{field: debt_to_equity, op: "<=", value: 0.5}`. An LLM does
  that **once**, when you say something new about yourself.
- *Applying* it is arithmetic. Six factor scores (growth, value, stability,
  momentum, quality, income) computed from stored fundamentals, multiplied by
  your weights, summed. Hard rules are filters.

**If they push:** "The brief explicitly warned against a brute-force LLM call
per stock per query. Beyond cost, the arithmetic version is deterministic and
unit-testable — I have tests asserting a dividend-focused investor and a growth
investor get different rankings from the same two stocks."

### 3. Deduplication has two layers, because one isn't enough

**The claim:** the same story published by Moneycontrol, ET, and Mint is
embedded once.

**Why:**
- **Exact:** `sha256(normalised_title + canonical_url)` with a UNIQUE
  constraint. Catches the same article reappearing across feed polls. URL
  canonicalisation strips tracking parameters; title normalisation strips
  outlet suffixes and possessives.
- **Semantic:** a syndicated story has a *different* title and URL, so hashing
  can't catch it. Lead paragraphs are compared by cosine distance against a
  3-day window; a near-duplicate is linked to the original rather than
  re-indexed.

**If they push:** "Why 3 days? Because quarterly results legitimately recur —
I don't want this quarter's earnings story collapsed into last quarter's."

### 4. Ingestion is idempotent and race-safe

**The claim:** run it twice, nothing duplicates. Run two at once, nothing corrupts.

**Why, in layers:**
- `pg_try_advisory_lock` per ticker — a concurrent second run returns
  `skipped` immediately rather than competing.
- Every write is an upsert. Article insert is `ON CONFLICT DO NOTHING`, and the
  **loser of a race reads the winner's row** instead of failing.
- A UNIQUE index on `(kind, content_hash, article_id, stock_id)` makes
  double-indexing a chunk impossible at the database level.
- Migrations run inside an advisory lock, so parallel container boots are safe.

**If they push:** "The advisory lock is an optimisation — it avoids wasted work.
The unique constraints are the actual guarantee. I didn't want correctness to
depend on the lock being held."

### 5. Embeddings are content-addressed

**The claim:** each distinct piece of text is embedded exactly once, ever.

**Why:** the cache is keyed on `(sha256(text), model)`, not on article ID. So
the same story relevant to three followed tickers, or seen by five different
users, costs one embedding call total. Fundamentals get a content hash too, so
unchanged numbers are never re-embedded on a refresh.

---

## Architecture Walkthrough

```
Browser → API Gateway (HTTPS) → nginx → ┬→ static frontend files
                                        └→ FastAPI → RDS PostgreSQL + pgvector
                                                   → Gemini (chat + embeddings)
GitHub Actions → ECR + S3 → SSM Run Command → the instance
```

**Why one front door?** The SPA and the API share an origin, so the session
cookie is first-party, there's no CORS, and Google OAuth gets the HTTPS
redirect URI it demands — without buying a domain or certificate.

**Why API Gateway rather than CloudFront?** Honest answer, and a good one:
CloudFront was the original design and is still in the repo behind
`var.enable_cloudfront`. AWS blocks CloudFront on unverified new accounts and
only Support can lift it. API Gateway gives the same trusted HTTPS endpoint
with no such gate, and it's named in the brief's own AWS service list. The
switch is one variable because every URL derives from a single
`local.public_base_url`.

### The agent graph (LangGraph)

```
understand → retrieve → [rank] → answer
```

| Node | Does | Calls an LLM? |
|---|---|---|
| `understand` | Writes persona memory, resolves tickers, classifies intent | Only if you said something about yourself |
| `retrieve` | Hybrid search: vector + keyword, fused | No (one embedding) |
| `rank` | Scores stocks against persona. Recommendation questions only | **No** |
| `answer` | Writes the cited response | Yes — and only if sources exist |

**Why a graph rather than one prompt?** Different questions need different work.
"What's the sentiment on TCS" needs retrieval. "What should I buy" needs
retrieval *and* ranking. "What do you know about me" needs neither — it's
answered from memory with no model call at all.

### Retrieval: why hybrid

Vector search alone is weak on exactly what Indian equity questions turn on —
ticker symbols, rupee figures, quarter labels. Keyword search alone misses
paraphrase ("rising borrowings" vs "debt problems").

Both run; results are fused with **reciprocal rank fusion** — combine by
*rank position* rather than raw score. That matters because cosine similarity
and BM25 live on completely different scales and normalising between them is
guesswork.

---

## Questions They Will Probably Ask

**"Walk me through what happens when I follow RELIANCE."**
> The endpoint checks whether we know the ticker. If not, it scrapes screener.in
> synchronously so a typo returns a clean 404 rather than failing silently in
> the background. Then the follow row is written and ingestion runs in the
> background — scraping takes tens of seconds, far too long to hold a request
> open. Ingestion takes a per-ticker advisory lock, pulls fundamentals, a year
> of prices from yfinance, and articles from eight feeds. It dedupes, fetches
> article bodies, checks for near-duplicates by embedding distance, chunks,
> embeds through the cache, and indexes. Then one batched LLM call tags up to a
> dozen articles for sentiment and materiality, and a SQL aggregate recomputes
> the stock's time-decayed sentiment score.

**"How do you stop it hallucinating?"**
> See Thing #1. Lead with "no sources means no model call."

**"Why pgvector rather than Pinecone?"**
> One datastore instead of two. The relational data and the vectors are queried
> together — retrieval is scoped to the stocks a user follows, which is a join.
> With a separate vector database that becomes two round trips and a
> consistency problem. It's also one less service to provision and pay for.

**"How does this scale?"**
> Be honest about limits. "Today it's a single instance and a single RDS
> instance — right-sized for a review deployment. The pieces that would need to
> change first: ingestion should move to a queue with workers rather than
> FastAPI background tasks, because a background task dies with the process.
> The retrieval path scales further than that — HNSW indexing plus per-user
> scoping keeps queries bounded. And the embedding cache means the marginal
> cost of a new user following an already-covered stock is nearly zero."

**"What's the weakest part?"**
> Have a real answer ready — deflecting reads badly.
> "The screener.in scraper. It parses their HTML by CSS selector, so a layout
> change breaks it. I made it degrade rather than crash — missing fields become
> nulls, and a null never excludes a stock from recommendations — but it's the
> piece most likely to need maintenance. If this were long-lived I'd want a
> paid fundamentals API behind the same interface."

**"Why did you choose Gemini?"**
> "Cost, and it was swappable anyway. The provider layer takes chat and
> embeddings behind one interface with three implementations, chosen by an
> environment variable. Gemini's free tier covers both. There's also an offline
> fallback so the whole app runs end-to-end with no API key — which is how the
> test suite runs in CI."

**"Tell me about a bug you had to debug."**
> Use the OIDC one — it's the best story in the project. See below.

---

## The Best Debugging Story

Every deployment failed at "log in to AWS" with:

```
Not authorized to perform sts:AssumeRoleWithWebIdentity
```

The error names no claim, no policy, nothing actionable. The GitHub secrets
were correct, the trust policy looked correct, the repo name matched.

Rather than re-paste credentials hoping something would change, I pulled
**CloudTrail** — AWS's audit log records the actual claims presented on a
failed call. It showed:

```
presented: repo:pranaysathish@127191655/sentellent-equity-analyst@1318255008:ref:refs/heads/main
expected:  repo:pranaysathish/sentellent-equity-analyst:*
```

GitHub had started issuing the OIDC subject with **immutable numeric IDs**
embedded. They did that for a good reason: with the old format, deleting a repo
and letting someone else claim the name would hand them your cloud trust. The
IDs survive renames and transfers, so they can't be spoofed that way.

The fix matches both patterns, so it works whichever form GitHub sends.

**Why this is a good story:** it shows you go to ground truth instead of
guessing, and that you understood *why* the upstream change was made rather
than just working around it.

---

## Things To Be Careful About

**Don't oversell.** If asked something you don't know, say "I'd have to look at
that." Confident wrongness is worse than a gap.

**Know your numbers.**
- 6 factor scores, weighted per user
- 8 RSS feeds
- 768-dimension embeddings
- 34 tests
- 13 tables
- ~47 AWS resources in Terraform
- One LLM call per ~12 articles for tagging

**If they ask about AI assistance**, be straight: "I used AI heavily — their
brief said that was fine. What I own is the architecture decisions and the
trade-offs, and I can walk you through any of them."

**Have the code open.** Know where things live: `agent.py` for the graph,
`ingest.py` for the pipeline, `persona.py` for scoring, `retrieval.py` for
hybrid search, `infra/` for Terraform.

---

## Two-Minute Demo Script

1. Log in with Google
2. Follow **RELIANCE** — note it's fetching fundamentals and news right now
3. Show the watchlist populate: price in ₹, P/E, debt-to-equity, sentiment
4. Ask: *"What's the sentiment on Reliance this week?"* — point at the
   citations, click one, land on the actual article
5. Say: *"I'm a conservative, dividend-focused investor and I avoid companies
   with debt-to-equity above 0.5"* — show the profile panel update
6. Ask: *"What should I buy for my profile?"* — show ranked picks and anything
   excluded by your own rule, with the reason
7. Ask something not in the data — show it declining to answer rather than
   inventing one

Step 7 is the one worth lingering on. Anyone can demo an answer. Demonstrating
a *refusal* to answer is the harder thing to build and the thing they asked for.
