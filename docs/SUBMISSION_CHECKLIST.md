# Submission Checklist

Every requirement from the challenge PDF, what satisfies it, and how to prove
it on the live site. Work through the **Test** column in order — that sequence
also doubles as the video walkthrough script.

Live URL: `https://rnfl1abgkf.execute-api.ap-south-1.amazonaws.com`

---

## Mandatory stack

| PDF requirement | Status | Where |
|---|---|---|
| Frontend: React.js / Next.js | ✅ | Next.js 16, static export |
| Backend: Python (FastAPI or Flask) | ✅ | FastAPI |
| AI framework: LangChain / LangGraph | ✅ | LangGraph state machine, `agent.py` |
| Embeddings model + vector store | ✅ | Gemini embeddings, pgvector on RDS |
| *"Build the chunk, embed, retrieve pipeline yourself"* | ✅ | `ingest.py`, `retrieval.py` — no framework retriever |
| Fundamentals: screener.in or equivalent | ✅ | `sources.fetch_fundamentals` |
| *"store NSE & BSE IDs per stock"* | ✅ | `stocks.nse_id`, `stocks.bse_id` |
| News: Indian financial RSS | ✅ | ET, Moneycontrol, Mint, Business Standard |
| Prices: NSE India / yfinance `.NS` | ✅ | `.NS` tickers via Yahoo's chart endpoint |
| Cloud: AWS | ✅ | EC2, RDS, API Gateway, S3, ECR, VPC, IAM, CloudWatch |
| GCP for OAuth login only | ✅ | No Gmail/Calendar scopes requested |
| Terraform | ✅ | 11 files, ~55 resources |
| CI/CD: GitHub Actions, push to main deploys | ✅ | `.github/workflows/` |

---

## Phase 1 — The Foundation

| Requirement | Test on the live site |
|---|---|
| User logs in via OAuth | Open the URL → **Sign in with Google** → lands on the dashboard |
| Sentellent reviewers can log in | Google Console → OAuth consent screen → Test users includes `harisankar@sentellent.com` and `naga@sentellent.com` |
| DB + vector store provisioned | AWS Console → RDS → `sentellent-db` running |
| Terraform provisions the resources | `cd infra && terraform plan` shows no changes |
| CI/CD deploys frontend and backend | GitHub → Actions → latest run green |
| DB migrations | Migrations run on boot under an advisory lock; RDS shows 13 tables |
| **Goal: one grounded, cited answer in INR** | Follow `RELIANCE` → ask *"What's the sentiment on Reliance this week?"* → answer cites `[1]`, `[2]`, prices in ₹ |

## Phase 2 — The Integration

| Requirement | Test on the live site |
|---|---|
| Follow ticker → fetch, chunk, embed, index | Type `ITC` → **Follow** → wait ~90s → refresh → price, P/E, RoE, yield populate |
| Fundamentals source connected | Watchlist shows ₹ price, P/E, RoE, D/E, yield, market cap |
| Indian news RSS connected | Article count on the stock card is > 0 |
| LLM tags sentiment / impact / event per stock | Sentiment badge moves off *Neutral* after ingestion |
| Retrieval tool: relevant news + fundamentals | Ask about a followed ticker → sources list shows both news and fundamentals rows |
| Citation tool: every claim links to source | Click any `[n]` → jumps to the source → link opens the real article |
| Scheduled job refreshes news | `cron` every 6h on the instance → `/api/internal/refresh` |

## Phase 3 — The Brain

| Requirement | Test on the live site |
|---|---|
| Learns persona from chat | Say *"I'm a conservative, dividend-focused investor and I avoid companies with debt-to-equity above 0.5"* → **Investor Profile** panel fills in |
| Stores persona as a vector | `user_persona.embedding`, rebuilt on every change |
| Extracts facts from data automatically | Ingestion tags each article and updates rolling sentiment with no user action |
| Matches/scores stocks to persona | Ask *"What should I buy for my profile?"* → ranked picks, one-line reason each |
| Screens out names the rules exclude | A stock breaching your stated D/E rule is listed as excluded, with the reason |
| **Anti-hallucination** | Ask something absent from the data (e.g. *"What is Reliance's 2027 revenue guidance?"*) → replies *"I don't have that in the data I've ingested"* |
| Grounding, citations, INR correctness | Every figure carries `[n]`; all money reads `Rs.` / `₹` |

---

## The demo sequence

Do these in order. Step 7 is the one worth lingering on.

1. Open the live URL → **Sign in with Google**
2. Follow `RELIANCE` — point out it is fetching fundamentals and news *now*
3. Follow `ITC` — a second stock is needed for ranking to mean anything
4. Wait ~90 seconds, refresh — show the populated cards: ₹ price, P/E, RoE, yield, sentiment
5. Ask **"What's the sentiment on ITC this week?"** — click a citation, land on the real Economic Times article
6. Say **"I'm a conservative, dividend-focused investor and I avoid companies with debt-to-equity above 0.5"** — show the Investor Profile panel update
7. Ask **"What should I buy for my profile?"** — show ranked picks and anything excluded by your own rule
8. Ask **"What is Reliance's revenue guidance for 2027?"** — show it *declining to answer*

Step 8 matters most. Anyone can demo an answer. Demonstrating a **refusal** is
the hard part and the thing the brief explicitly grades.

---

## Known gaps — state these honestly

**Debt-to-equity is sometimes blank.** screener.in does not publish it on every
company's ratio strip. A missing field never excludes a stock from
recommendations — a data gap is a data gap, not grounds to hide a name.

**CloudFront is written but disabled.** AWS blocks distribution creation on
unverified new accounts. The Terraform is complete behind
`var.enable_cloudfront`; API Gateway serves the same role and is named in the
brief's own AWS list.

Being upfront about these is stronger than hoping nobody clicks. Each one has a
reason and a mitigation.

---

## Before submitting

- [ ] Screenshot: AWS Console → EC2 → instance running *(set region to Mumbai)*
- [ ] Screenshot: AWS Console → RDS → `sentellent-db` available
- [ ] Screenshot: AWS Console → API Gateway → the API
- [ ] Screenshot: AWS Console → S3 → the frontend bucket
- [ ] Screenshot: GitHub → Actions → a green "Deploy to AWS" run, all jobs passing
- [ ] Screenshot: the live app, logged in, with a cited answer on screen
- [ ] Video walkthrough — follow the demo sequence above
- [ ] Upload screenshots + video to Google Drive, sharing set to **Anyone with the link**
- [ ] Resume on Google Drive, same sharing
- [ ] Form: tick **AWS** and **GCP** under Cloud Platforms
- [ ] Form: tick **all three phases**
- [ ] Rotate the Gemini key and OAuth client secret once review is done
- [ ] `terraform destroy` after Sentellent has reviewed
