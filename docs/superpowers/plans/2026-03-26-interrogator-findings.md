# Ruthless Interrogator Findings — Langfuse Wiring Plan

**Date:** 2026-03-26
**Purpose:** Research findings from 10 interrogation questions, with Gemini search results and local verification.

---

## Answer Summary

### Q1: Does npm:langfuse work in Deno?

**YES, officially supported.** Langfuse docs have Deno-specific examples.

**Import syntax must be corrected in the plan:**
```typescript
// WRONG (plan currently has):
import Langfuse from "npm:langfuse";

// CORRECT:
import { Langfuse } from "npm:langfuse@latest";
```
Named export `{ Langfuse }`, not default import.

**Known risk:** GitHub issue reports `TypeError: Relative import path "fs"` in some versions. The `langfuse-core` dependency uses Node's `fs` module. Deno 2.0+ should handle this via `node:` compat, but needs testing during Wave 0.

**Action:** Add `deno run --allow-net --allow-env --allow-read` to all run commands (already in generate_batch.ts). Pin a specific version, don't use `@latest`.

---

### Q2: Are INIT keys the same as runtime SDK keys?

**YES — but with a nuance.** The `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` and `LANGFUSE_INIT_PROJECT_SECRET_KEY` are used during headless initialization to **create** the project's API keys. Once created, those same key values become the runtime API keys for SDK authentication.

So `pk-lf-4415ecd76d6e09c34218d849` (from the `.env`) IS the correct public key for SDK use. The plan's approach of copying these into `LANGFUSE_PUBLIC_KEY` env vars is correct.

**Risk:** If someone logs into the Langfuse UI and regenerates the API keys, the init keys will no longer work.

**Action:** Verify the project exists in the UI before wiring (add as Wave 1 prereq check: `curl http://localhost:3300/api/public/health`).

---

### Q3: Does flushAsync hang if server is unresponsive?

**NO — it logs errors and retries, never throws.** Designed for fire-and-forget. Configurable via:
- `flushAt`: max events before auto-send
- `flushInterval`: max seconds between sends
- `requestTimeout`: overall request timeout (default ~5s)

**Known bug (March 2025 GitHub issue):** `flushAsync()` may return immediately even when traces haven't been persisted yet. Race condition with `Deno.exit()` or `process.exit()`.

**Action:** Add a small `setTimeout(1000)` delay after `flushAsync()` before `Deno.exit()` in `generate_batch.ts`. Or use `shutdownAsync()` instead of `flushAsync()` (waits for all pending requests).

---

### Q4: Docker networking — Prefect worker → llm-olly-postgres (CRITICAL)

**CONFIRMED PROBLEM.** The containers are on different Docker networks:
- Prefect worker: `scripting-host_internal` (172.18.x.x)
- llm-olly-postgres: `llm-olly` (172.26.x.x)

They **cannot** reach each other by container hostname. The plan's DB URL `postgresql://langfuse:...@localhost:54332/monitoring` works from the **host** (port mapping), but NOT from inside the Prefect worker container.

**Three fix options:**
1. **Connect Prefect worker to the `llm-olly` network:** `docker network connect llm-olly scripting-host-prefect-worker-1` — then use `postgresql://langfuse:...@llm-olly-postgres:5432/monitoring`
2. **Use the host's IP from inside the container:** `postgresql://langfuse:...@172.18.0.1:54332/monitoring` (gateway IP = host)
3. **Use Docker's host.docker.internal:** `postgresql://langfuse:...@host.docker.internal:54332/monitoring` (if Docker is configured for it on Linux — not guaranteed)

**Recommended:** Option 1 (connect networks). Most reliable, no firewall concerns.

---

### Q5: Prefect flow deployment — pip persistence + common.py collision

**pip persistence:** Packages installed via `docker exec ... pip install` are lost on container restart. The worker container likely has a volume mount for `/opt/prefect` but pip packages go to `/usr/local/lib/python3.10/site-packages/`. Need to either:
1. Add to the Dockerfile and rebuild
2. Mount a persistent volume for site-packages
3. Add a `startup` script that re-installs on container start

**common.py collision:** No collision risk — existing common files are all prefixed: `_adyntel_common.py`, `_crux_common.py`, etc. A bare `common.py` won't collide. However, all existing flows use `from _psi_common import ...` etc. If any flow does `import common` generically, it would now pick up the llm-olly one. **Low risk but should rename to `_llm_olly_common.py` for safety.**

---

### Q6: Canary fixture seeding — no upsert, no force

**Intentional for now.** The seed script has a `COUNT(*) > 0 → skip` guard. To update fixtures, you'd need to TRUNCATE and re-seed. This is fine for v1 — fixtures change rarely.

**Action:** Document that fixture updates require `TRUNCATE canary_cases CASCADE` first. Add a `--force` flag in a future iteration.

---

### Q7: capture_send.py — who runs it and when?

**Manual for now.** No HubSpot webhook or Instantly callback exists yet. The user (Mike) would run it after reviewing and editing each email.

**Schema confirms:** `experiment_id` is NULLABLE (no FK constraint), so `None` inserts are fine. BUT `persona_version` and `base_prompt_version` are NOT NULL — the `capture_send.py` script in the plan does NOT pass these, so the INSERT will fail.

**Action:** Either:
1. Add `--persona-version` and `--base-prompt-version` CLI args to `capture_send.py`
2. Set defaults (e.g., `persona_version=1`, `base_prompt_version=1`)
3. ALTER the table to make them nullable

---

### Q8: Promptfoo bridge verification

**No verification step in the plan.** Promptfoo logs won't show Langfuse connection status on startup — it's a config-level integration, not a live connection.

**Action:** After filling keys (Task 1), run a quick Promptfoo eval on 1 fixture, then run `promptfoo_to_langfuse.py` on the result. Check Langfuse UI for the trace.

---

### Q9: Generator code blast radius — try/catch

**The plan does NOT wrap Langfuse calls in try/catch.** If `tracer.logEmailGeneration()` throws (e.g., SDK version mismatch, Deno compat issue), the entire generation fails.

**Action:** Wrap ALL tracer calls in try/catch in email-generator.ts:
```typescript
try {
  tracer?.logEmailGeneration({ ... });
} catch (e) {
  console.error("[langfuse] trace failed, continuing:", e);
}
```

The `v70_test.ts` does reference `OrchestratorParams` — adding `tracer?` (optional) won't break existing test code since TypeScript allows omitting optional fields.

---

### Q10: Observability of the observability

**No monitoring exists for the monitoring.** If Langfuse crashes, traces silently stop. If canary heartbeat fails, the Slack alert comes from the thing that just failed.

**Suggested post-plan:** Add a simple `last_canary_run` check to the existing infrastructure monitoring stack (Grafana on server). Query: `SELECT MAX(completed_at) FROM canary_runs` — alert if >26 hours ago.

---

## Critical Plan Changes Required

| # | Issue | Severity | Fix |
|---|-------|----------|-----|
| 1 | Import syntax: `{ Langfuse }` not `Langfuse` default | **Blocker** | Fix langfuse_tracer.ts |
| 2 | Docker network isolation: Prefect worker can't reach llm-olly-postgres | **Blocker** | `docker network connect llm-olly scripting-host-prefect-worker-1` |
| 3 | `capture_send.py` missing NOT NULL columns: `persona_version`, `base_prompt_version` | **Blocker** | Add CLI args or defaults |
| 4 | No try/catch around Langfuse SDK calls in email-generator.ts | **High** | Wrap in try/catch |
| 5 | `flushAsync()` race with `Deno.exit()` | **High** | Use `shutdownAsync()` + delay |
| 6 | pip packages lost on container restart | **Medium** | Document; add to Dockerfile later |
| 7 | `common.py` should be `_llm_olly_common.py` for safety | **Low** | Rename |
