# LLM Observability (llm-olly)

**Repo:** `michaeljboscia/llm-olly`
**Purpose:** Observability, evaluation, canary testing, and drift detection for the GTM Machine Narrative Generator.

---

## What This Repo Contains

| Directory | Purpose |
|-----------|---------|
| `langfuse/` | Docker Compose, .env template, deployment docs for Langfuse v3 |
| `promptfoo/` | Docker Compose, evaluation configs, assertion scripts |
| `canary/` | Canary test fixtures (44-66 cases), scoring rubrics |
| `bridge/` | Promptfoo → Langfuse bridge script |
| `prefect/` | Prefect flows: daily heartbeat, weekly drift report, model change trigger |
| `schema/` | Supabase migration SQL for monitoring tables |

---

## Architecture

```
Narrative Generator (v70-email-corpus repo)
    │
    ├── Generates emails via Claude API
    │
    ├── Langfuse traces every call ──→ Langfuse (Docker, server)
    │   ├── Token usage, cost, latency
    │   ├── Prompt version tracking
    │   ├── Cache hit/miss rates
    │   ├── LLM-as-judge auto-eval
    │   └── Human approval scores (via API)
    │
    ├── Promptfoo evaluates before deploy ──→ Promptfoo (Docker, server)
    │   ├── 22 persona × N payload test matrix
    │   ├── Deterministic assertions (word count, banned CTAs, data accuracy)
    │   ├── Claude-as-judge rubrics (persona fidelity, tone, anti-patterns)
    │   └── Golden dataset similarity scoring
    │
    └── Prefect orchestrates monitoring ──→ Prefect (existing, server)
        ├── Daily canary heartbeat (22 API calls)
        ├── Weekly drift report (materialized view refresh)
        └── Model change trigger (full canary suite)
```

---

## Infrastructure

- **Langfuse v3:** Docker on server (localhost), port 3300. Uses dedicated Supabase local Postgres instance + ClickHouse + Redis + MinIO.
- **Promptfoo:** Docker on server, port 3200. SQLite for persistence.
- **Supabase monitoring tables:** `canary_cases`, `canary_runs`, `canary_results`, `email_generations`, `experiments`, `prompt_versions` — in a separate local Supabase database instance on server.
- **Prefect:** Existing deployment on server, add new flows for canary/drift.

---

## Key Research Findings (from DR-04)

- **Edit distance tracking** = highest signal per engineering hour, zero API cost
- **3-shot exemplars per persona** = 90-95% of quality gains (DR-01)
- **Single-step generation** = validated, no Brain→Voice split needed (DR-02)
- **Persona-bucketed prompt caching** = $0.0032/request, 85% cheaper than mega-shot (DR-01-original)
- **Bayesian A/B testing** = only viable approach at 35 emails/week (DR-04)
- **91% of ML models degrade over time** — monitoring is mandatory, not optional
- **Promptfoo acquired by OpenAI March 2026** — still MIT, monitor Anthropic support

---

## Related Repos

- **gtm-machine-infrastructure** — Pain sensors, CE/AE, rank_signals_for_persona()
- **v70-email-corpus worktree** — Modular prompts (base + 22 persona modules), exemplars, research

---

## Build Order

1. Deploy Langfuse Docker stack on server
2. Deploy Promptfoo Docker on server
3. Create Supabase local DB instance for Langfuse + monitoring tables
4. Apply DR-04 schema (canary infrastructure, experiments, email_generations)
5. Write canary test fixtures (1 per persona = 22 minimum)
6. Write Promptfoo assertion scripts
7. Build Promptfoo → Langfuse bridge
8. Create Prefect flows (daily heartbeat, weekly drift, model change)
9. Instrument Narrative Generator with Langfuse SDK
10. Build edit distance capture in send endpoint
