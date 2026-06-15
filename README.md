# SemanticGateway

**AI That Knows Its Grain**

A governed semantic layer that translates natural language into validated Snowflake SQL — without hallucinated joins, metric misuse, or grain violations.

Live demo: [semanticgateway.vercel.app](https://semanticgateway.vercel.app) · Backend: [semantic-gateway-api.onrender.com](https://semanticgateway-api.onrender.com)

---

## What it does

You type "Show me MRR by plan type for the last 3 months." SemanticGateway figures out the right metric, picks the right dimensions, generates SQL that won't blow up your grain, runs it against Snowflake, and comes back with a number you can trust — plus a one-sentence narrative that tells you what's actually interesting about it.

It doesn't guess. Every query goes through a semantic validator before a single byte hits Snowflake. If the dimension doesn't belong to the metric, it tells you that instead of returning garbage.

---

## Why I built this

Most "AI analytics" demos do one of two things: they let the LLM write raw SQL (which hallucinates joins and gets grain wrong), or they hardcode a handful of queries and call it a product.

SemanticGateway is neither. The LLM handles intent extraction and narrative generation only. The actual SQL comes from MetricFlow — a governed semantic layer that knows your metric definitions, entity relationships, and valid dimension combinations. The LLM never touches SQL directly.

That separation is the whole point.

---

## Architecture

```
User Query
    │
    ▼
Intent Classifier (Gemini Flash)
    │  classifies: metric_query / metadata / out_of_scope
    ▼
RAG Retrieval (ChromaDB + all-MiniLM-L6-v2)
    │  finds the 5 most relevant metrics from 15 certified
    ▼
Intent Extractor (Gemini → Llama fallback)
    │  extracts: metric, dimensions, time range
    ▼
Semantic Validator
    │  checks dimension is valid for that metric
    ▼
SQL Template Cache (L1)
    │  HIT → skip MetricFlow, inject time range, execute
    │  MISS ↓
    ▼
MetricFlow CLI
    │  generates governed SQL from semantic manifest
    ▼
SQL Reviewer (Gemini)
    │  checks for grain violations, missing filters, fan-out risk
    │  revises SQL if issues found
    ▼
Snowflake Execution (connection pool, size=15)
    │
    ▼
Result Cache (L2, SHA256-keyed, TTL 8h)
    │
    ▼
Narrative Generator (Gemini)
    │  2-sentence summary anchored to actual result data
    ▼
React Frontend
```

---

## Stack

**Backend**
- FastAPI — API layer with request logging middleware and UUID-traced requests
- MetricFlow — semantic layer, governs all SQL generation
- dbt — 23 models, 79/79 tests passing, STREAMING_ANALYTICS schema on Snowflake
- Snowflake — data warehouse, connection pool of 15
- Gemini Flash Lite — intent classification and narrative generation
- Llama 3.1 8B (Groq) — fallback intent extractor
- ChromaDB — vector store for RAG metric retrieval
- all-MiniLM-L6-v2 — sentence embeddings

**Frontend**
- React + Vite
- Claymorphism design system (teal color family, Nunito + DM Sans)
- Gateway Chat panel with grain metadata, SQL viewer, and lineage tags

**Data**
- Synthetic Netflix-style dataset: 1.8M+ rows, 15,000 subscribers, 2023–2026
- 15 certified metrics across 5 semantic models
- Dimensions include plan type, country, cohort month, acquisition channel, device type, referral source, churn reason, payment method

---

## Certified Metrics

| Metric | Description |
|---|---|
| `mrr` | Monthly recurring revenue |
| `new_mrr` | New MRR from first-time subscribers |
| `expansion_mrr` | MRR from plan upgrades |
| `churn_rate` | % of subscribers who churned |
| `churned_subscribers` | Count of churned subscribers |
| `total_subscribers` | Active subscriber count |
| `avg_watch_time` | Average session duration in minutes |
| `total_watch_time` | Total watch minutes |
| `total_sessions` | Session count |
| `engagement_rate` | % completion rate per session |
| `ltv` | Lifetime value per subscriber |
| `recommendation_ctr` | Click-through rate on recommendations |
| `total_recommendations` | Recommendations served |
| `clicked_recommendations` | Recommendations clicked |
| `avg_buffering_events` | Average buffering events per session |

---

## What "governed" actually means here

Every query goes through three layers before hitting Snowflake:

**1. Semantic validation** — dimensions are checked against the metric registry. `avg_watch_time` won't accept `country` because that's not in its semantic model. You get a clear error, not a wrong answer.

**2. SQL review** — Gemini reviews the generated SQL for grain mismatches, missing hygiene filters (`is_active = TRUE`, `country IS NOT NULL`, valid `plan_type` values), and full table scans. If it finds issues, it revises the SQL before execution.

**3. Grain metadata** — every response includes the grain of the underlying query (e.g. "One row per session, keyed on session_id") and the dbt lineage path (`raw → stg → fct → metric`). The user always knows what they're looking at.

---

## Two-layer cache

**L1 — SQL Template Cache**
Keyed on `metric × dimensions`. When a cache hit occurs, MetricFlow is skipped entirely. Time ranges are injected into the cached template at query time. TTL: 24 hours. Survives server restarts.

**L2 — Result Cache**
Keyed on `SHA256(metric + dimensions + time_range + filters)`. Stores full query results in memory with disk persistence. TTL: 8 hours. 500-entry cap.

Cold query (first run, no cache): ~30–40 seconds (MetricFlow CLI + Snowflake)
Warm query (SQL cache hit): ~1–3 seconds
Result cache hit: ~2ms

---

## Query examples that work

```
Show me MRR by plan type
Churn rate by country for last 6 months
LTV by payment method
Avg watch time by device type
Total subscribers by acquisition channel
Engagement rate by plan type in Q1 2025
Churned subscribers by churn reason
```

---

## Running locally

**Prerequisites:** Python 3.11, Node 18+, Snowflake account, dbt project compiled

```bash
# Clone
git clone https://github.com/albertnsql/semantic-gateway.git
cd semantic-gateway

# Backend
cd gateway
pip install -r requirements.txt
cp .env.example .env
# Fill in SNOWFLAKE_*, GEMINI_API_KEY in .env
uvicorn main:app --reload --port 8000

# Frontend (separate terminal)
cd ../frontend
npm install
cp .env.example .env
# Set VITE_API_URL=http://localhost:8000
npm run dev
```

Frontend runs on `localhost:5173`, backend on `localhost:8000`.

---

## Project structure

```
semantic-gateway/
├── gateway/
│   ├── main.py                     # FastAPI app, middleware, startup
│   ├── api/routes/
│   │   ├── query.py                # Main query endpoint
│   │   ├── dashboard.py            # Dashboard widget endpoints
│   │   └── lineage.py              # Lineage explorer endpoint
│   ├── core/
│   │   ├── intent_extractor.py     # RAG + LLM intent extraction
│   │   ├── metric_registry.py      # Loads semantic manifest, maps dims
│   │   ├── sql_generator.py        # MetricFlow CLI + SQL reviewer
│   │   ├── sql_template_cache.py   # L1 cache
│   │   ├── semantic_validator.py   # Dimension/metric validation
│   │   ├── snowflake_pool.py       # Connection pool
│   │   └── cache_warmer.py         # Startup pre-warm for 22 combinations
│   ├── rag/
│   │   └── embedder.py             # ChromaDB + SentenceTransformer
│   └── skills/
│       ├── streaming_analytics.md  # Schema knowledge injected into prompts
│       └── sql_reviewer.md         # SQL review rules
├── frontend/
│   └── src/
│       ├── App.jsx
│       ├── pages/
│       │   ├── Overview.jsx
│       │   ├── Dashboard.jsx
│       │   └── QueryInterface.jsx
│       └── components/
│           ├── GatewayChat.jsx
│           ├── MetricCard.jsx
│           └── LineageExplorer.jsx
└── dbt_streaming_analytics/
    └── streaming_analytics/
        ├── models/                 # 23 dbt models
        ├── tests/                  # 79 tests, all passing
        └── semantic_models/        # MetricFlow semantic definitions
```

---

## Design decisions worth explaining

**Why MetricFlow instead of custom SQL builder?**
MetricFlow gives you a governed semantic manifest — metric definitions, entity relationships, and dimension validity are declared once and enforced everywhere. A custom SQL builder would just move the hallucination problem into Python. The goal was a system where the LLM literally cannot generate a fan-out join, not one where it probably won't.

**Why two LLMs?**
Gemini Flash Lite handles classification and narrative — it's fast and cheap for high-frequency calls. Llama 3.1 via Groq is the fallback for intent extraction when Gemini rate-limits or fails. Gemini 2.5 Flash is the tertiary for complex intent. All three are abstracted behind a single interface so swapping providers costs one line.

**Why RAG for metric retrieval?**
With 15 metrics and growing, passing all of them into every prompt wastes context and confuses the model. RAG retrieves the 5 most semantically similar metrics to the query, which keeps the extraction prompt focused and accurate.

**Why a skills system?**
The `streaming_analytics.md` skill injects schema knowledge (physical column names, hygiene filters, grain definitions) into the SQL reviewer prompt at runtime. This is what catches things like `is_active = TRUE` being missing, or `country IS NOT NULL` being skipped. Without it, the reviewer would have no grounding in the actual schema.

---

## Skills / Domain knowledge

Two markdown skills are loaded at startup and injected into LLM prompts:

`streaming_analytics.md` — Physical column references for all 5 fact/dimension tables, valid values for categorical columns, required hygiene filters per table, grain definitions, join keys.

`sql_reviewer.md` — Rules for what constitutes a valid SQL query in this schema. Used by the speculative review step to catch grain mismatches, fan-out risk, and missing filters before execution.

---

## About the data

The dataset is synthetic, generated to mirror a real Netflix-style streaming SaaS business. 15,000 subscribers, 1.8M+ rows across session, payment, subscription, and recommendation events. Date range: January 2023 – May 2026. Built to support realistic churn, cohort, and engagement analysis.

---

## About me

I'm Albert Nadar, a Senior Analytics Engineer based in Mumbai with 6 years building data platforms at National Pen (Cimpress). I work across dbt, Snowflake, Looker/LookML, and Python. 
I built SemanticGateway to show what a production-grade semantic layer looks like when you treat data governance as a first-class concern rather than an afterthought.


[LinkedIn](https://linkedin.com/in/albertn97) · [GitHub](https://github.com/albertnsql) · albertnsql@gmail.com

---

## Also built

**LookML Auditor** — Static analysis tool for LookML projects. 9 lint rules, ratio-based health score, GitHub URL ingestion. Live at [lookml-auditor-web.vercel.app](https://lookml-auditor-web.vercel.app)
