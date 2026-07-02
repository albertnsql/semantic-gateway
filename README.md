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
IntentClassifier (OpenRouter → Gemini fallback)
    │  classifies: metric_query / metadata / out_of_scope
    ▼
RAG Retrieval (ChromaDB + all-MiniLM-L6-v2)
    │  finds the 5 most relevant metrics from 15 certified
    ▼
IntentExtractor (OpenRouter → Groq → Gemini tertiary)
    │  extracts: metric, dimensions, time range, filters
    ▼
SemanticValidator
    │  checks dimension is valid for that metric
    ▼
SQL Template Cache (L1)
    │  HIT → skip MetricFlow, inject time range, execute
    │  MISS ↓
    ▼
MetricFlow CLI
    │  generates governed SQL from semantic manifest
    ▼
SQL Reviewer (OpenRouter / Gemini)
    │  checks for grain violations, missing filters, fan-out risk
    │  revises SQL if issues found
    ▼
Snowflake Execution (connection pool, dynamically sized)
    │
    ▼
Result Cache (L2, SHA256-keyed, TTL 8h, disk-persisted)
    │
    ▼
ResponseBuilder + Narrative Generator
    │  2-sentence summary anchored to actual result data
    ▼
React Frontend
```

---

## Stack

**Backend (gateway/)**
- FastAPI — API layer with UUID-traced request logging middleware
- MetricFlow — semantic layer, governs all SQL generation
- ManifestParser — parses the compiled dbt manifest for lineage resolution
- LineageResolver — resolves `raw → stg → int → mart → metric` lineage per query
- ResponseBuilder — assembles the final structured response with grain metadata
- IntentClassifier — two-stage routing (metric_query / metadata / out_of_scope)
- dbt — 30 models across 4 layers, 79/79 tests passing, STREAMING_ANALYTICS schema on Snowflake
- Snowflake — data warehouse, dynamically-sized connection pool (default: 5, configurable via `SNOWFLAKE_POOL_SIZE`)
- OpenRouter (Gemini 2.5 Flash) — primary: intent classification, extraction, SQL review, and narrative generation
- Groq (Llama 3.1 8B Instant) — fallback intent extractor
- Google Gemini Flash Lite — tertiary fallback for intent extraction
- ChromaDB — vector store for RAG metric retrieval
- all-MiniLM-L6-v2 — sentence embeddings (HuggingFace, cached locally)
- pydantic-settings — typed configuration from `.env`

**Backend (backend/)**
- `evals/` — offline accuracy evaluation harness for the IntentExtractor
- `skills/` — domain knowledge loader (`streaming_analytics.md`, `sql_reviewer.md`)
- `core/skill_loader.py` — loads markdown skill files and injects them into LLM prompts at startup

**Frontend**
- React + Vite + TailwindCSS
- Claymorphism design system (teal color family, DM Sans + Nunito)
- 7 pages: Landing, How It Works, Dashboard, Query, Metrics Catalog, Lineage Explorer, Demo Scenarios
- Key components: `QueryResultPanel`, `ChatPanel`, `ChatMessage`, `LineageGraph`, `SqlViewer`, `KpiTile`, `Sidebar`, `StatusBadge`

**Data**
- Synthetic Netflix-style dataset: 1.8M+ rows, 15,000 subscribers, 2023–2026
- 18 certified metrics across 5 semantic models
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
| `total_buffering_events` | Total buffering events across all sessions |
| `net_mrr_growth` | Net MRR growth (new + expansion − churn) |
| `retention_rate` | % of subscribers retained month-over-month |

---

## What "governed" actually means here

Every query goes through three layers before hitting Snowflake:

**1. Semantic validation** — dimensions are checked against the metric registry. `avg_watch_time` won't accept `country` because that's not in its semantic model. You get a clear error, not a wrong answer.

**2. SQL review** — the LLM reviews the generated SQL for grain mismatches, missing hygiene filters (`is_active = TRUE`, `country IS NOT NULL`, valid `plan_type` values), and full table scans. If it finds issues, it revises the SQL before execution.

**3. Grain metadata** — every response includes the grain of the underlying query (e.g. "One row per session, keyed on session_id") and the dbt lineage path (`raw → stg → int → fct → metric`). The user always knows what they're looking at.

---

## Two-layer cache

**L1 — SQL Template Cache**
Keyed on `metric × dimensions`. When a cache hit occurs, MetricFlow is skipped entirely. Time ranges are injected into the cached template at query time. TTL: 24 hours. Survives server restarts. Cap: 200 entries (configurable via `SQL_TEMPLATE_CACHE_MAXSIZE`).

**L2 — Result Cache**
Keyed on `SHA256(metric + dimensions + time_range + filters)`. Stores full query results in memory with disk persistence (`.query_cache.json`). TTL: 8 hours. Cap: 500 entries (configurable via `QUERY_CACHE_MAXSIZE`).

Cold query (first run, no cache): ~30–40 seconds (MetricFlow CLI + Snowflake)
Warm query (SQL cache hit): ~1–3 seconds
Result cache hit: ~2ms

> **Note for memory-constrained hosts (e.g. Render free tier, 512 MB RAM):** Set `QUERY_CACHE_MAXSIZE=100` and `SQL_TEMPLATE_CACHE_MAXSIZE=50` and keep the `warmup_matrix` to ≤ 6 combinations.

---

## Cache Warmer

On startup, the `CacheWarmer` pre-builds SQL templates for the most common metric + dimension combinations, so the first real user query is never cold. The default warmup matrix covers 12 combinations across 11 metrics (MRR, subscribers, churn, LTV, engagement, recommendations, watch time, etc.). Disable with `DISABLE_CACHE_WARMER=true`.

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

## Offline Evals

The `backend/evals/` directory contains an accuracy harness that runs every case in `golden_set.json` through the real `IntentExtractor` (using live credentials from `gateway/.env`) and scores the result against pinned expected values.

```bash
# Run all cases
python backend/evals/run_evals.py

# Write a dated JSON snapshot
python backend/evals/run_evals.py --snapshot

# Run only hallucination-resistance cases
python backend/evals/run_evals.py --category hallucination_resistance --verbose

# Fail CI if pass rate drops below 90%
python backend/evals/run_evals.py --fail-under 90
```

Scores: metric match, dimension match, time range extraction, aggregation level, filter presence, and clarification flag. Supports `partial_match_ok` for soft-fail cases.

---

## Running locally

**Prerequisites:** Python 3.11, Node 18+, Snowflake account, dbt project compiled

```bash
# Clone
git clone https://github.com/albertnsql/semantic-gateway.git
cd semantic-gateway

# Backend (gateway)
cd gateway
pip install -r requirements.txt
cp .env.example .env
# Fill in SNOWFLAKE_*, OPENROUTER_API_KEY (primary), OPENAI_API_KEY (Groq fallback),
# GOOGLE_API_KEY (tertiary fallback) in .env
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

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Primary LLM provider (Gemini 2.5 Flash via OpenRouter) |
| `OPENAI_API_KEY` | — | Groq API key (Llama 3.1 8B fallback) |
| `GOOGLE_API_KEY` | — | Google Gemini tertiary fallback |
| `SNOWFLAKE_ACCOUNT` | — | Snowflake account identifier |
| `SNOWFLAKE_USER` | — | Snowflake user |
| `SNOWFLAKE_PASSWORD` | — | Snowflake password |
| `SNOWFLAKE_WAREHOUSE` | `compute_wh` | Virtual warehouse |
| `SNOWFLAKE_ROLE` | `transformer` | Snowflake role |
| `SNOWFLAKE_POOL_SIZE` | `5` | Connection pool size (lower on 512 MB hosts) |
| `CACHE_TTL_SECONDS` | `28800` | Result cache TTL (8 h) |
| `SQL_TEMPLATE_CACHE_TTL_SECONDS` | `86400` | SQL template cache TTL (24 h) |
| `QUERY_CACHE_MAXSIZE` | `500` | Result cache entry cap |
| `SQL_TEMPLATE_CACHE_MAXSIZE` | `200` | SQL template cache entry cap |
| `DISABLE_CACHE_WARMER` | `false` | Set `true` to skip startup pre-warm |
| `DISABLE_RAG` | `false` | Set `true` to skip ChromaDB and inject all metrics |
| `ADMIN_SECRET_KEY` | `""` | Required to call `POST /api/v1/cache/clear` in production |
| `GATEWAY_ENV` | `development` | Runtime environment label |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## Project structure

```
semantic-gateway/
├── gateway/                            # FastAPI application
│   ├── main.py                         # App entry point, startup/shutdown, middleware
│   ├── config.py                       # pydantic-settings config (reads .env)
│   ├── classifier.py                   # IntentClassifier (metric_query / metadata / out_of_scope)
│   ├── cache.py                        # L2 result cache (SHA256-keyed, disk-persisted)
│   ├── requirements.txt
│   ├── api/routes/
│   │   ├── query.py                    # Main query endpoint
│   │   ├── dashboard.py                # Dashboard widget endpoints
│   │   ├── lineage.py                  # Lineage explorer endpoint
│   │   ├── metrics.py                  # Metric registry / catalog endpoint
│   │   └── health.py                   # Health check endpoint
│   ├── core/
│   │   ├── intent_extractor.py         # RAG + multi-LLM intent extraction
│   │   ├── metric_registry.py          # Loads semantic manifest, maps dims
│   │   ├── manifest_parser.py          # Parses compiled dbt manifest.json
│   │   ├── lineage_resolver.py         # raw → stg → int → fct → metric lineage
│   │   ├── response_builder.py         # Assembles structured API response
│   │   ├── sql_generator.py            # MetricFlow CLI + SQL reviewer
│   │   ├── sql_template_cache.py       # L1 SQL template cache
│   │   ├── semantic_validator.py       # Dimension/metric validation
│   │   ├── snowflake_pool.py           # Connection pool
│   │   ├── cache_warmer.py             # Startup pre-warm for top metric+dim combos
│   │   └── exceptions.py              # Typed gateway exceptions
│   └── rag/
│       └── embedder.py                 # ChromaDB + SentenceTransformer
│
├── backend/                            # Offline tooling
│   ├── core/
│   │   └── skill_loader.py             # Loads markdown skill files
│   ├── skills/
│   │   ├── streaming_analytics.md      # Schema knowledge injected into prompts
│   │   └── sql_reviewer.md             # SQL review rules
│   ├── evals/
│   │   ├── run_evals.py                # Eval harness CLI
│   │   ├── golden_set.json             # Pinned expected outputs
│   │   └── snapshots/                  # Dated JSON eval results
│   └── tests/
│
├── frontend/
│   └── src/
│       ├── App.jsx                     # Router + claymorphism layout
│       ├── pages/
│       │   ├── LandingPage.jsx
│       │   ├── HowItWorksPage.jsx
│       │   ├── DashboardPage.jsx
│       │   ├── QueryPage.jsx
│       │   ├── MetricsCatalogPage.jsx
│       │   ├── LineageExplorerPage.jsx
│       │   └── DemoScenariosPage.jsx
│       ├── components/
│       │   ├── QueryResultPanel.jsx
│       │   ├── Sidebar.jsx
│       │   ├── LineageGraph.jsx
│       │   ├── SqlViewer.jsx
│       │   ├── KpiCard.jsx
│       │   ├── StatusBadge.jsx
│       │   ├── TopBar.jsx
│       │   ├── LoadingSpinner.jsx
│       │   ├── ErrorState.jsx
│       │   └── dashboard/
│       │       ├── ChatPanel.jsx
│       │       ├── ChatMessage.jsx
│       │       ├── KpiTile.jsx
│       │       └── ChartCard.jsx
│       └── hooks/
│           └── useTheme.js
│
└── dbt_streaming_analytics/
    └── streaming_analytics/
        ├── models/
        │   ├── staging/                # 10 stg_ models (sources)
        │   ├── intermediate/           # 6 int_ models
        │   ├── marts/                  # 8 fct_/dim_ models + time spine
        │   └── semantic/               # 5 MetricFlow semantic YAMLs
        ├── tests/                      # 79 tests, all passing
        └── dbt_project.yml
```

---

## Design decisions worth explaining

**Why MetricFlow instead of custom SQL builder?**
MetricFlow gives you a governed semantic manifest — metric definitions, entity relationships, and dimension validity are declared once and enforced everywhere. A custom SQL builder would just move the hallucination problem into Python. The goal was a system where the LLM literally cannot generate a fan-out join, not one where it probably won't.

**Why three LLMs?**
OpenRouter (Gemini 2.5 Flash) is the primary for all LLM calls — classification, intent extraction, SQL review, and narrative. Groq (Llama 3.1 8B Instant) is the first fallback for intent extraction when OpenRouter rate-limits or fails. Google Gemini Flash Lite is the tertiary fallback. All three are abstracted behind a single OpenAI-compatible interface, so swapping providers costs one line.

**Why RAG for metric retrieval?**
With 15 metrics and growing, passing all of them into every prompt wastes context and confuses the model. RAG retrieves the 5 most semantically similar metrics to the query, which keeps the extraction prompt focused and accurate. Disable with `DISABLE_RAG=true` if RAM is constrained.

**Why a skills system?**
The `streaming_analytics.md` skill injects schema knowledge (physical column names, hygiene filters, grain definitions) into the SQL reviewer prompt at runtime. This is what catches things like `is_active = TRUE` being missing, or `country IS NOT NULL` being skipped. Without it, the reviewer would have no grounding in the actual schema.

**Why a separate `backend/` directory?**
The `gateway/` directory is the deployable FastAPI service. `backend/` is the offline layer — eval harness, skill loader, and tests — that never ships to production but is critical for maintaining accuracy and schema knowledge. Keeping them separate avoids polluting the deployed bundle with eval dependencies.

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
