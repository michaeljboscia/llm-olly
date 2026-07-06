# Full-Stack AI Observability — Design Spec

**Date:** 2026-03-30
**Status:** Draft
**Working Directory:** /Users/you/llm-olly
**Author:** Mike Boscia + Claude

---

## 1. Problem Statement

**The symptom:** When an Oracle call returns `[Tool result missing due to internal error]`, there is zero visibility into whether:
- The Oracle is actually processing (Gemini thinking)
- The MCP transport dropped the connection
- The Oracle hit a context limit
- The daemon died silently

This is the failure mode across the entire AI stack — silent failures with no way to diagnose.

**What's needed:**
- **Request tracing** — every `ask_oracle` / `ask_daemon` call gets a trace ID with timestamps (sent, acknowledged, processing, responded, errored)
- **Heartbeat / status** — `oracle_status(session_id)` returns idle | processing | hung | dead with last-activity timestamp
- **Timeout surfacing** — if the inter-agent MCP server hit a timeout internally, that comes back as a structured error, not a generic "internal error"
- **JSONL event log** — every MCP tool call -> response pair logged with latency, so you can spot patterns (e.g., "Pythia oracle calls over 30s always fail")

**Current state of the three AI systems:**
- **Pythia** (LCS + Oracle): 110 console.* calls, no metrics, no traces, no structured logs
- **Inter-agent MCP** (Gemini + Codex orchestration): outbox files and progress events, nothing exported
- **Narrative Generator** (Claude API cold emails): Supabase tables + 3 Grafana dashboards, but no trace-level visibility, no quality scoring, no drift detection

GPU workloads run on hand-built golden VM images that are hard to version, observe, or scale.

Infrastructure monitoring exists (Prometheus, Grafana, Loki, Tempo, OTel Collector on server) but no application is instrumented to emit telemetry to it.

**Prior art:** The Tellus spatial query observability schema (`observability.function_executions`, `observability.log_execution`) is exactly the right pattern — structured logging with trace IDs, not silent failures. This project applies that pattern to the inter-agent and Pythia MCP layers.

---

## 2. Goals

1. `REQ-001` Every in-scope core AI system (Pythia, inter-agent, narrative generator) emits OTel traces, Prometheus metrics, and structured JSON logs
2. `REQ-002` All LLM calls (Gemini CLI, Claude API) are tracked in Langfuse with prompt/response/latency/cost
3. `REQ-003` Narrative generator output is quality-scored by MOE eval judges on a schedule
4. `REQ-004` GPU workloads run as containerized pods on GKE with scale-to-zero
5. `REQ-005` Server remains the observability hub — all telemetry flows back to localhost
6. `REQ-006` Total cloud spend on observability infrastructure: $0

---

## 2.1 Requirements Index

**Problem Statement (Section 1):**
`REQ-007` Request tracing with trace IDs and timestamps |
`REQ-008` Heartbeat/status endpoint for oracle sessions |
`REQ-009` Timeout surfacing as structured errors |
`REQ-010` JSONL event log for every MCP tool call

**Pythia (Section 4.1):**
`REQ-011` OTel traces (8 span types) |
`REQ-012` Prometheus /metrics (11 metrics) |
`REQ-013` Structured JSON logging (replace console.*) |
`REQ-014` Langfuse generation traces for Oracle |
`REQ-015` Telemetry is fire-and-forget

**Inter-Agent (Section 4.2):**
`REQ-016` OTel traces (7 span types) |
`REQ-017` Prometheus /metrics (10 metrics) |
`REQ-018` Structured JSON logging |
`REQ-019` Langfuse generation traces for daemons |
`REQ-020` Cross-system trace propagation

**Narrative Generator (Section 4.3):**
`REQ-021` OTel traces (3 span types) |
`REQ-022` Langfuse integration (prompts, generations, scores, datasets) |
`REQ-023` MOE eval engine scoring on schedule |
`REQ-024` New Prometheus metrics (cache, version, MOE scores, drift) |
`REQ-025` Drift calculation (7d vs 30d rolling avg, alert at -0.1)

**GKE + GPU (Section 5):**
`REQ-026` GKE zonal cluster provisioned |
`REQ-027` Ollama on T4 GPU spot |
`REQ-028` TEI on L4 GPU spot |
`REQ-029` KEDA scale-to-zero |
`REQ-030` vmagent on free e2-micro |
`REQ-031` Cloudflare tunnel route for remote write

**Langfuse (Section 6):**
`REQ-032` Auth config per system |
`REQ-033` Project structure (prompts, datasets, score configs)

**MOE (Section 7):**
`REQ-034` Clone and adapt for cold email scoring |
`REQ-035` Model decision (CPU/Gemini CLI/L4) |
`REQ-036` Prefect flow for scheduled eval |
`REQ-037` Grafana dashboard for quality trends

**Discrete — Tellus (Section 8):**
`REQ-038` pg_stat_statements enabled |
`REQ-039` Spatial batch job metrics via Pushgateway |
`REQ-040` Tellus alerts (KNN, dead tuples, connection pool)

**Discrete — Alerting (Section 9):**
`REQ-041` Alertmanager receivers configured |
`REQ-042` Alert rules deployed (7 defined)

**Discrete — k3s (Section 10):**
`REQ-043` k3s installed on server |
`REQ-044` Docker Compose to k3s migration path

**Discrete — Cloudflare (Section 11):**
`REQ-045` Tunnel metrics scraped by Prometheus

---

## 3. Architecture

```
Local (laptop — where Pythia + inter-agent already run)
    +-- pythia (monolith, as-is)
        +-- /metrics endpoint (new)
        +-- OTel traces --> push to Server OTel Collector
        +-- JSON logs --> push to Server Loki
        +-- Langfuse generations --> push to langfuse.e5btools.com
    +-- inter-agent-mcp (monolith, as-is)
        +-- /metrics endpoint (new)
        +-- OTel traces --> push to Server OTel Collector
        +-- JSON logs --> push to Server Loki
        +-- Langfuse generations --> push to langfuse.e5btools.com

GKE Cluster (us-central1, zonal, free tier — GPU workloads only)
    +-- ollama pod (T4 GPU, spot, KEDA scale-to-zero)
    +-- TEI pod (L4 GPU, spot, KEDA scale-to-zero)
    +-- DCGM exporter (GPU metrics on :9400)
    +-- future: Pythia + inter-agent pods (when/if migrated)

e2-micro (FREE, same VPC as GKE)
    +-- vmagent (single Go binary, ~50MB RAM)
        +-- scrapes GKE pod /metrics (in-VPC, fast)
        +-- scrapes DCGM exporter (GPU util, VRAM, temp)
        +-- 30GB disk buffer (survives server outages)
        +-- remote-writes --> Cloudflare tunnel --> Server Prometheus

Server localhost (OBSERVABILITY HUB — already running)
    +-- Prometheus v3.8.0 (scrapes laptop + receives vmagent remote write)
    +-- Grafana v11.5.2 (single pane of glass)
    +-- Loki 3.0.0 (logs from all sources)
    +-- Tempo 2.6.1 (distributed traces via OTel)
    +-- OTel Collector 0.114.0 (receives from laptop + GKE, ports 4317/4318)
    +-- Langfuse (running — langfuse.e5btools.com via Cloudflare tunnel)
    +-- Alertmanager v0.31.0
    +-- Scrapes: Supabase, node_exporter, cAdvisor, Cloudflare tunnel
    +-- Receives push from laptop via tunnel endpoints:
        +-- loki.e5btools.com (logs, exists)
        +-- otel.e5btools.com (traces, new route)
        +-- pushgateway.e5btools.com (metrics, new route)
        +-- langfuse.e5btools.com (AI traces, exists)
```

**Pythia and inter-agent stay local. No Dockerfiles needed yet.** They push telemetry directly to server. GKE is for GPU workloads only. Dockerfiles and GKE migration for Pythia/inter-agent become a discrete section — do it when there's a reason (24/7 availability, in-cluster GPU proximity, etc.).

### Cost Model

| Line Item | Monthly Cost | Covered By |
|-----------|-------------|-----------|
| GKE control plane | $0 | Always Free tier ($74.40/mo credit) |
| e2-micro relay (vmagent) | $0 | Always Free tier |
| GKE worker node (e2-small) | ~$15 | Ultra $100/mo GCP credit |
| GPU spot pods (T4/L4, scale-to-zero) | Pay-per-use | Ultra credit |
| Server (all observability) | $0 | Already running, owned hardware |
| Langfuse | $0 | Self-hosted on server |
| MOE eval compute (if GPU) | Pay-per-use | Ultra credit (Option C only) |
| **Total observability infra** | **$0** (Options A/B for MOE) | |

---

## 4. Core: Three-System Instrumentation

### 4.1 Pythia

**Current state:** Singleton daemon, 5K+ TS, zero external telemetry.

**OTel Traces:**

| Span | What It Captures |
|------|-----------------|
| `pythia.search` | Full hybrid search: query -> embed -> vector -> FTS -> RRF -> rerank -> return |
| `pythia.search.embed_query` | Query embedding latency + model info |
| `pythia.search.rerank` | Reranker latency, result count in/out, score distribution |
| `pythia.oracle.ask` | Oracle query: prompt -> Gemini CLI -> response. Token counts if available |
| `pythia.oracle.spawn` | Session creation, MADR reconstitution time |
| `pythia.index.file` | Per-file indexing: parse -> chunk -> embed -> write |
| `pythia.index.batch` | Batch embedding: chunks in, vectors out, duration, backpressure events |
| `pythia.thought.process` | Thought worker cycle: poll -> claim -> embed -> store |

**Prometheus Metrics (/metrics):**

| Metric | Type | Labels |
|--------|------|--------|
| `pythia_search_duration_seconds` | Histogram | intent, workspace |
| `pythia_search_results_total` | Counter | intent, workspace |
| `pythia_embed_duration_seconds` | Histogram | model |
| `pythia_rerank_duration_seconds` | Histogram | - |
| `pythia_oracle_tokens_total` | Counter | direction (prompt/completion) |
| `pythia_oracle_duration_seconds` | Histogram | model |
| `pythia_oracle_sessions_active` | Gauge | - |
| `pythia_index_files_total` | Counter | status (success/failure), workspace |
| `pythia_index_chunks_total` | Counter | workspace |
| `pythia_thought_processed_total` | Counter | status (indexed/duplicate/error) |
| `pythia_db_size_bytes` | Gauge | workspace |

**Structured Logging:**
Replace 110 console.* calls with structured JSON logger (e.g., `pino` + `pino-loki` transport). Fields: `timestamp`, `level`, `component` (embedder/reranker/oracle/search/indexer), `workspace`, `trace_id`, `span_id`. Transport: direct HTTP push to Loki at `http://localhost:3100/loki/api/v1/push`. Pythia runs locally (not in a container), so there is no container log driver — push directly.

**Resilience:** All telemetry emission (metrics, traces, logs, Langfuse) is fire-and-forget. Application latency is never gated on observability infrastructure availability. If Loki/Tempo/Langfuse is down, the application continues normally and telemetry is dropped.

**Langfuse:**
Wrap Oracle Gemini CLI calls with manual Langfuse generation traces. Input prompt, output response, model, latency. Token counts when available from CLI JSON response.

**Hook points:** EventEmitter on supervisor (batchComplete, fileFailed, fatal) and parallel-supervisor (fileComplete, fileFailed, progress, edgesComplete). Attach metric/span emitters as listeners — minimal changes to core logic.

**Cost protection (implemented 2026-03-30):** `GEMINI_API_KEY` env var no longer activates SDK mode. SDK requires explicit `mode: "sdk"` in config. Pythia stays $0.

---

### 4.2 Inter-Agent MCP

**Current state:** Unified MCP server v2.0.0, ~10K TS. Outbox logger, progress events, OTel-compatible trace fields defined but not exported.

**OTel Traces:**

| Span | What It Captures |
|------|-----------------|
| `inter_agent.spawn_daemon` | Target, model, bootstrap duration, resumed flag |
| `inter_agent.ask_daemon` | Daemon ID, question length, response length, model, duration |
| `inter_agent.send_message` | Target, request_type, SYN/ACK timing, job_id |
| `inter_agent.get_response` | Job ID, poll count, total wait time, success/timeout |
| `inter_agent.model_fallback` | Which model failed, fallback target, reason |
| `inter_agent.cli_exec` | PID, command, exit code, duration, retried flag |
| `inter_agent.thought_capture` | Thought blocks extracted, type (observation/insight/decision) |

**Prometheus Metrics (/metrics):**

| Metric | Type | Labels |
|--------|------|--------|
| `inter_agent_daemon_spawn_duration_seconds` | Histogram | target, model |
| `inter_agent_daemon_ask_duration_seconds` | Histogram | target, model |
| `inter_agent_daemons_active` | Gauge | target |
| `inter_agent_messages_total` | Counter | target, request_type, status |
| `inter_agent_jobs_active` | Gauge | - |
| `inter_agent_model_fallbacks_total` | Counter | from_model, to_model |
| `inter_agent_cli_exec_duration_seconds` | Histogram | target |
| `inter_agent_cli_exec_retries_total` | Counter | target |
| `inter_agent_cli_exec_timeouts_total` | Counter | target |
| `inter_agent_thoughts_captured_total` | Counter | type |

**Structured Logging:**
JSON logger replacing ad-hoc console output. Keep outbox file audit trail as-is. Every log line gets `trace_id`, `span_id`, `target`, `daemon_id`.

**Langfuse:**
Manual generation traces around Gemini and Codex `ask_daemon` calls. Same pattern as Pythia Oracle.

**Hook points:** OnProgress callback in cli-executor.ts (spawned/heartbeat/stdout_data/timeout/retry/done). Attach OTel span listeners to existing callback chain.

**Cross-system trace propagation:** Use W3C `TRACEPARENT` environment variable to propagate trace context across CLI subprocess boundaries. Inter-agent sets `TRACEPARENT` before spawning Gemini CLI; Pythia reads it from env to attach search spans to the same trace. One request across Claude -> inter-agent -> Pythia = one distributed trace in Tempo.

---

### 4.3 Narrative Generator

**Current state:** Supabase Edge Function, Claude API. 3 Grafana dashboards already exist (cost, operations, quality). Most instrumented system.

**OTel Traces (new):**

| Span | What It Captures |
|------|-----------------|
| `narrative.generate` | Persona, domain, signals used, prompt version, cache hit/miss |
| `narrative.generate.claude_api` | Model, input/output/cache tokens, latency |
| `narrative.generate.validate` | Pass/fail, which rules failed |

**Langfuse Integration (the big add):**
- Prompt management: Git repo is canonical source of truth for 22 persona prompts. GitHub Actions syncs to Langfuse on push to main. No ad-hoc Langfuse UI edits.
- Generations: every Claude API call with full prompt + response
- Scores: quality scores from MOE judges attached to generations
- Datasets: canary test fixtures (44-66 cases from DR-04) as Langfuse datasets

**MOE Eval Engine Integration:**
- Clone `michaeljboscia/moe-eval-engine` locally
- Scheduled via Prefect (daily or weekly)
- Pulls recent generations from Langfuse
- 11 specialized judges score each: information gap, credibility, data specificity, voice authenticity, forwarding likelihood, loss framing, sense-making, illusory truth, loss aversion, transparency, committee design
- Scores written back to Langfuse as generation scores
- Grafana dashboard shows quality trends over time

**New Prometheus Metrics:**

| Metric | Type | Labels |
|--------|------|--------|
| `narrative_cache_hit_ratio` | Gauge | persona |
| `narrative_prompt_version` | Info | persona |
| `narrative_moe_score` | Gauge | judge, persona |
| `narrative_moe_drift` | Gauge | judge, persona |

**Drift Calculation:** `narrative_moe_drift` = rolling 7-day average score minus rolling 30-day average score, per judge per persona. A negative value means recent quality is worse than the longer baseline. Alert threshold: -0.1 (10% degradation). Start with n=35 (7-day window at 5/day), tune thresholds based on real signal quality.

**Narrative Generator OTel Feasibility:** Supabase Edge Functions (Deno Deploy) have limited OTel support. If native OTel is not feasible, instrument the Prefect flow that triggers generation instead — the calling code, not the Edge Function itself. Move to Discrete if blocked.

---

## 5. Core: GKE Cluster Setup

### 5.1 Cluster Spec

- **Type:** Zonal Standard (free tier eligible)
- **Zone:** us-central1-a (or -b, -c — check GPU availability)
- **Node pool (GPU-T4):** 0-1x n1-standard-4 + T4, spot, KEDA scale-to-zero (Ollama inference, batch)
- **Node pool (GPU-L4):** 0-1x g2-standard-4 + L4, spot with on-demand fallback, KEDA scale-to-zero (TEI embedding, batch reindexing)
- **No CPU node pool needed initially** — Pythia and inter-agent stay local
- **No always-on GPU** — TEI is batch-only (reindexing). Pythia embeds locally via GGUF on CPU. Future: if Pythia moves to GKE, it can spin up GPU pods on-demand for heavy embedding.
- **Provisioning:** Terraform (reuse patterns from infrastructure-monitoring/terraform/modules/)

### 5.2 Container Images (GPU workloads only for now)

**Ollama:**
- Base: `ollama/ollama:latest`
- Models: pull via init container or pre-baked
- GPU resource request: `nvidia.com/gpu: 1`

**TEI:**
- Base: `ghcr.io/huggingface/text-embeddings-inference:turing-1.7` (T4) or `89-1.7` (L4)
- Model: mount from GCS via init container
- GPU resource request: `nvidia.com/gpu: 1`

### 5.3 Deferred: Pythia + Inter-Agent Dockerfiles

**Not needed for Phase 1-3.** Both run locally, push telemetry directly to server. Containerize when:
- You need 24/7 availability (laptop sleeps)
- You need in-cluster proximity to GPU pods
- You want to run them on k3s or GKE

### 5.4 vmagent Relay (e2-micro)

- **Provisioning:** Terraform or startup script
- **Install:** Download vmagent binary (~15MB)
- **Config:** YAML with scrape targets pointing to GKE pod service endpoints
- **Remote write:** Requires a new Cloudflare tunnel route from e2-micro to server Prometheus. Add `vmagent-relay.e5btools.com` route pointing to `http://host.docker.internal:9091` (Prometheus remote write receiver). Enable `--web.enable-remote-write-receiver` on server Prometheus.
- **Disk buffer:** Enabled, 30GB persistent disk (free tier)
- **Monitoring:** vmagent's own `/metrics` endpoint scraped by itself (meta-monitoring)

---

## 6. Core: Langfuse Wiring

**Deployment:** Already running on server (`llm-olly-langfuse-web`, `llm-olly-langfuse-worker`). Accessible at `langfuse.e5btools.com`.

**SDK Integration Points:**

| System | SDK | Pattern |
|--------|-----|---------|
| Pythia Oracle | `langfuse` npm package | Manual `generation()` around Gemini CLI calls |
| Inter-agent daemons | `langfuse` npm package | Manual `generation()` around ask_daemon calls |
| Narrative generator | `langfuse` JS/TS SDK (Deno-compatible) | Manual trace in Supabase Edge Function |
| MOE eval engine | `langfuse` Python SDK | Read generations, write scores back |

**Langfuse Auth Config:**
Each system needs `LANGFUSE_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_BASE_URL` (= `https://langfuse.e5btools.com`).
- Pythia + inter-agent (TypeScript, local): set in `.env` files, loaded via `dotenv` or shell env
- Narrative generator (Supabase Edge Function): `supabase secrets set LANGFUSE_SECRET_KEY=... LANGFUSE_PUBLIC_KEY=... LANGFUSE_BASE_URL=...`
- MOE eval engine (Python, local): `.env` file loaded by `python-dotenv`

**Langfuse Project Structure:**
- Project: `gtm-machine` (single project, tag by system — simpler correlation)
- Prompts: 22 persona prompts versioned in Langfuse
- Datasets: canary test fixtures (44-66 cases)
- Score configs: one per MOE judge (11 total)

---

## 7. Core: MOE Eval Engine Integration

**Repo:** `michaeljboscia/moe-eval-engine` (GitHub, not yet cloned locally)

**Current state:** 11 expert judges, AHP-weighted TOPSIS aggregation, JSONL checkpointing, statistical validation. Built for evaluating sales decks via Qwen 2.5 32B on A100.

**Adaptation for continuous eval:**
1. Clone repo to server
2. Replace "sales deck slide" input with "cold email generation" input from Langfuse
3. Judges are already persona-agnostic (psychology + content quality dimensions)
4. **Model decision:** Option A — server CPU via Ollama (~41 min/day, acceptable for scheduled batch). $0 cost.
5. **Judge set:** The 11 sales-deck judges are NOT right for cold email. Phase 3 prerequisite: research existing email quality judge frameworks, then design email-specific judge set (subject line strength, CTA clarity, personalization authenticity, spam/deliverability risk, etc.) before wiring MOE into the pipeline.
6. Prefect flow: daily -> pull ALL generations since last run from Langfuse API -> run judges -> write scores back to Langfuse -> push summary metrics to Prometheus Pushgateway
6. Grafana dashboard: quality score trends per judge per persona over time

**Cost note:** If MOE judges run on GPU spot pods (Option C), that is non-zero compute drawn from Ultra credits. Options A and B are truly $0.

---

## 8. Discrete: Tellus/PostGIS Observability

**Independent of core instrumentation. Execute whenever.**

**pg_stat_statements:**
- Enable extension on Supabase (if not already)
- Scrape via Postgres exporter or direct query from Grafana
- Track: top 10 slowest queries, query frequency, rows returned/affected
- Special attention: KNN queries (`<->` operator), GiST index scans

**Spatial Batch Job Metrics:**
- Prefect flow metadata: duration, rows processed, worker count, rows/sec
- Push to Prometheus via Pushgateway (same pattern as pain sensor metrics)
- Grafana dashboard: job duration trends, throughput, failure rates

**Alerts:**
- KNN query time > 500ms (p95)
- Dead tuple ratio > 10% (needs VACUUM)
- Connection pool exhaustion

---

## 9. Discrete: Alerting

**Independent. Configure receivers and rules when ready.**

**Alertmanager Receivers (replace placeholders):**
- **Decision:** Email as default receiver. Add Slack only if/when actively used.
- PagerDuty/Opsgenie is overkill for solo operator.

**Alert Rules Worth Having:**

| Alert | Condition | Severity |
|-------|-----------|----------|
| PythiaSearchSlow | `pythia_search_duration_seconds` p95 > 5s for 10m | WARNING |
| InterAgentDaemonTimeout | `inter_agent_cli_exec_timeouts_total` increase > 3 in 1h | WARNING |
| NarrativeQualityDrift | `narrative_moe_drift` < -0.1 for any judge | WARNING |
| GKENodeNotReady | kube_node_status_condition{condition="Ready",status="true"} == 0 | CRITICAL |
| GPUPodOOM | container_oom_kills_total increase > 0 | CRITICAL |
| VmagentRemoteWriteFailing | vmagent_remotewrite_send_duration_seconds_count increase == 0 for 15m | CRITICAL |
| HomeLokiDown | up{job="loki"} == 0 for 5m | CRITICAL |

---

## 10. Discrete: k3s on Server

**Independent. Run local K8s for services that benefit from container orchestration without cloud dependency.**

**Use cases:**
- Langfuse (already Docker Compose — move to k3s pod for consistency)
- OTel Collector
- Prometheus + Grafana stack (currently Docker Compose)
- Local dev/test versions of Pythia and inter-agent before pushing to GKE

**Setup:**
- Install: `curl -sfL https://get.k3s.io | sh -`
- Single node, no HA needed
- Disable Traefik ingress (use Cloudflare tunnel instead)
- Coexists with existing Docker Compose stacks during migration
- Same kubectl, same manifests, same Helm charts as GKE

**Migration path:** Move Docker Compose services to k3s one at a time. Validate each before removing the Compose version. No big bang.

---

## 11. Discrete: Cloudflare Tunnel Metrics

**Quick win. One scrape config addition.**

Cloudflared exposes Prometheus metrics on container port 2000. Add to server `prometheus.yml`:

```yaml
- job_name: 'cloudflare-tunnel'
  static_configs:
    - targets: ['tunnel-cloudflared-1:2000']  # container-internal on server-net Docker network
  metrics_path: '/metrics'
```

Key metrics: `cloudflared_tunnel_ha_connections` (expect 4 for healthy), `cloudflared_tunnel_total_requests`, `cloudflared_tunnel_request_errors`, `cloudflared_tunnel_response_by_code`.

---

## 12. Implementation Phases

### Phase 1: Plumbing (get telemetry flowing — local first)
- [ ] Verify Langfuse reachable from laptop (`langfuse.e5btools.com`), create project and API keys
- [ ] Set up static DHCP reservation for laptop on home router (stable IP for Prometheus scraping)
- [ ] Add Prometheus `/metrics` endpoint to Pythia (local, server scrapes laptop)
- [ ] Add Prometheus `/metrics` endpoint to inter-agent (local, server scrapes laptop)
- [ ] Add structured JSON logger to Pythia (replace console.*, `pino` + `pino-loki` push to Loki)
- [ ] Add structured JSON logger to inter-agent (push to Loki)
- [ ] Add Pythia + inter-agent scrape targets to server prometheus.yml
- [ ] Verify: metrics visible in Grafana from local services
- [ ] Verify: logs land in Loki from local services
- [ ] Add Cloudflare tunnel scrape config (quick win)

### Phase 2: Depth (trace-level visibility)
- [ ] OTel trace instrumentation in Pythia (8 span types)
- [ ] OTel trace instrumentation in inter-agent (7 span types)
- [ ] OTel trace instrumentation in narrative generator (3 span types — or instrument calling code if Edge Function OTel not feasible)
- [ ] Wire Langfuse SDK into Pythia Oracle calls
- [ ] Wire Langfuse SDK into inter-agent daemon asks
- [ ] Wire Langfuse into narrative generator Claude API calls
- [ ] Cross-system trace propagation (Claude -> inter-agent -> Pythia)
- [ ] Verify: traces visible in Tempo via Grafana
- [ ] Grafana dashboards for new metrics

### Phase 3: Intelligence (quality + drift)
- [ ] Clone and adapt MOE eval engine for cold email scoring
- [ ] Prefect flow: pull generations from Langfuse -> MOE judges -> scores back
- [ ] Load canary test fixtures into Langfuse datasets
- [ ] Drift detection dashboard (score trends over time)
- [ ] Narrative generator prompt versioning in Langfuse

### Discrete (execute independently, any order)
- [ ] Tellus/PostGIS: enable pg_stat_statements, Grafana dashboard
- [ ] Alerting: configure Alertmanager receivers, deploy alert rules
- [ ] k3s: install on server, migrate one service as proof of concept
- [ ] GKE cluster: provision zonal cluster, GPU node pools, vmagent relay on e2-micro
- [ ] Dockerfiles: containerize Pythia + inter-agent (when 24/7 or in-cluster proximity needed)

---

## 13. Success Criteria

1. **Grafana shows metrics from all three AI systems** — searchable, graphable, alertable
2. **Tempo shows distributed traces** across Claude -> inter-agent -> Pythia in one view
3. **Loki shows structured logs** from Pythia and inter-agent, correlated to traces via trace_id
4. **Langfuse shows every LLM call** — Gemini (Oracle + daemons) and Claude (narrative gen) with prompt/response/cost
5. **MOE quality scores** visible in Langfuse and Grafana, trending over time
6. **GPU inference runs on GKE** as spot pods with scale-to-zero, GPU metrics in Prometheus
7. **vmagent relay works** — metrics flow from GKE through e2-micro to server without gaps
8. **Total cloud observability spend: $0**

---

## 14. Open Questions

1. ~~MOE eval engine: can judges run on T4?~~ **Resolved:** No — Qwen 32B doesn't fit T4 (16GB). See Section 7 for three options (server CPU, Gemini CLI, L4 spot).
2. Narrative generator OTel: Supabase Edge Functions support custom OTel? Test early in Phase 2. Fallback: instrument the Prefect flow that triggers generation.
3. Cross-system trace propagation: pass trace_id as HTTP header, CLI argument, or environment variable? Decide during Phase 2 implementation.
4. ~~Laptop IP stability~~ **Resolved:** Use static DHCP reservation on home router. Added to Phase 1 checklist.
