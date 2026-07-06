# Langfuse Wiring — End-to-End Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Working Directory:** `/Users/you/llm-olly`
**Git Branch:** `main`
**Session Log:** none yet

**Goal:** Wire Langfuse observability end-to-end so that every Narrative Generator run produces a Langfuse trace, canary heartbeat runs daily, and edit distance is captured when emails are sent.

**Architecture:** The Narrative Generator is Deno/TypeScript — it needs the Langfuse TypeScript SDK (`npm:langfuse`) injected directly into `email-generator.ts`. The Python `langfuse_wrapper.py` in `bridge/` handles the Promptfoo evaluation bridge (separate path). Prefect drives the canary heartbeat and drift reporting on a cron schedule against the `monitoring` Postgres DB.

**Tech Stack:** Deno + TypeScript (Narrative Generator), Python 3.12 (bridge + Prefect flows), Langfuse v3 (Docker, server:3300), Promptfoo (Docker, server:3200), Postgres `monitoring` DB (server:54332 inside `llm-olly-postgres` container), Prefect 2.x (existing `scripting-host-prefect-server-1`)

---

## REQ Definitions

- **REQ-001** — Promptfoo evaluations flow to Langfuse (bridge credentials wired)
- **REQ-002** — Canary fixtures seeded into `monitoring.canary_cases` (22 rows)
- **REQ-003** — Every `generateSequence()` call produces a Langfuse trace with token usage
- **REQ-004** — Prefect canary heartbeat deployed and runnable on server
- **REQ-005** — Edit distance captured in `email_generations` when human-sent email is recorded
- **REQ-006** — End-to-end smoke test passes: generate 1 domain → trace visible in Langfuse UI

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `server:~/llm-olly-promptfoo/.env` | Modify | Fill in LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY |
| `bridge/seed_canary_cases.py` | Create | Load 22 JSON fixtures → insert into monitoring.canary_cases |
| `server:~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70/langfuse_tracer.ts` | Create | Thin TypeScript wrapper: init Langfuse client, start/end traces |
| `server:~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70/email-generator.ts` | Modify | Import tracer, wrap `messages.create()` call with Langfuse generation span |
| `server:~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70/generate_batch.ts` | Modify | Init Langfuse tracer once per run, pass to `generateSequence()`, flush at end |
| `server:~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70/orchestrator.ts` | Modify | Accept optional `tracer` param, pass down to `generateEmailTouch()` |
| `prefect/requirements.txt` | Modify | Add `langfuse>=2.51.0` |
| `prefect/_llm_olly_common.py` | Rename from `common.py` | Avoid collision with existing `_*_common.py` in Prefect flows dir |
| `prefect/canary_heartbeat.py` | Modify | Update import `from _llm_olly_common import` |
| `prefect/drift_report.py` | Modify | Update import `from _llm_olly_common import` |
| `prefect/model_change_trigger.py` | Modify | Update import `from _llm_olly_common import` |
| `bridge/capture_send.py` | Create | CLI: given domain + sequence_id, records human_sent email + computes edit distance |
| `server:~/server-infrastructure/scripting-host/flows/` | Copy | Flow files deployed via host-mounted volume (not docker cp) |
| `server:~/server-infrastructure/scripting-host/docker-compose.yml` | Modify | Add `llm-olly` external network to prefect-worker |
| `server:~/server-infrastructure/scripting-host/docker/prefect/requirements.txt` | Modify | Add `langfuse rapidfuzz` to baked-in dependencies |
| `server:~/llm-olly-langfuse/docker-compose.yml` | Modify | Add bind mount for ClickHouse `disable_system_logs.xml` |
| `server:~/llm-olly-langfuse/disable_system_logs.xml` | Create | ClickHouse config to disable system log tables |

---

## Wave 0 — Contracts

### Task 0: Define Langfuse Tracer Interface (TypeScript)

**Files:**
- Create: `server:~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70/langfuse_tracer.ts`

<task id="T-000" req="REQ-003" wave="0" depends="">
  <description>Define TypeScript Langfuse tracer interface — contracts for generate_batch + email-generator to code against</description>
  <files>gtm-machine-infrastructure/supabase/functions/generate-narrative/v70/langfuse_tracer.ts</files>
  <contract>
    LangfuseTracer {
      startSequenceTrace(params: SequenceTraceParams): string  // returns traceId
      logEmailGeneration(traceId: string, params: GenerationParams): void
      flush(): Promise&lt;void&gt;
    }
    SequenceTraceParams { domain, persona, model, sequenceId }
    GenerationParams { position, inputTokens, outputTokens, cacheCreation, cacheRead, subject, body }
  </contract>
  <verify>tsc --noEmit (Deno: deno check langfuse_tracer.ts)</verify>
</task>

- [ ] **Step 1: SSH to server and create the tracer file**

```bash
ssh user@localhost
cat > ~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70/langfuse_tracer.ts << 'EOF'
/**
 * Langfuse TypeScript Tracer — Narrative Generator v70
 *
 * Thin wrapper around the Langfuse SDK for use inside Deno edge functions.
 * One LangfuseTracer instance per batch run.
 * Each domain/persona generates one top-level trace; each email is a generation span.
 */

import { Langfuse } from "npm:langfuse@3.38.6";

export interface SequenceTraceParams {
  domain: string;
  persona: string;
  tier: string;
  model: string;
  sequenceId: string;
}

export interface GenerationParams {
  traceId: string;
  position: number;
  systemPrompt: string;
  userPrompt: string;
  output: string;
  inputTokens: number;
  outputTokens: number;
  cacheCreationTokens: number;
  cacheReadTokens: number;
  latencyMs: number;
  model: string;
}

export class LangfuseTracer {
  private client: Langfuse;

  constructor() {
    this.client = new Langfuse({
      publicKey: Deno.env.get("LANGFUSE_PUBLIC_KEY") ?? "",
      secretKey: Deno.env.get("LANGFUSE_SECRET_KEY") ?? "",
      baseUrl: Deno.env.get("LANGFUSE_BASE_URL") ?? "http://localhost:3300",
    });
  }

  startSequenceTrace(params: SequenceTraceParams): string | undefined {
    try {
      const trace = this.client.trace({
        name: `sequence:${params.domain}:${params.persona}`,
        id: params.sequenceId,
        metadata: {
          domain: params.domain,
          persona: params.persona,
          tier: params.tier,
          model: params.model,
        },
        tags: [params.tier, params.model, "v70"],
      });
      return trace.id;
    } catch (e) {
      console.error("[langfuse] trace start failed, continuing:", e);
      return undefined;
    }
  }

  logEmailGeneration(params: GenerationParams): void {
    try {
      const gen = this.client.generation({
        traceId: params.traceId,
        name: `email:position:${params.position}`,
        model: params.model,
        input: [
          { role: "system", content: params.systemPrompt },
          { role: "user", content: params.userPrompt },
        ],
        output: params.output,
        usage: {
          input: params.inputTokens,
          output: params.outputTokens,
          unit: "TOKENS",
          inputCost: undefined,
          outputCost: undefined,
        },
        metadata: {
          cache_creation_input_tokens: params.cacheCreationTokens,
          cache_read_input_tokens: params.cacheReadTokens,
          latency_ms: params.latencyMs,
        },
      });
      gen.end();
    } catch (e) {
      console.error("[langfuse] generation log failed, continuing:", e);
    }
  }

  async shutdown(): Promise<void> {
    try {
      await this.client.shutdownAsync();
    } catch (e) {
      console.error("[langfuse] shutdown failed:", e);
    }
  }
}
EOF
echo "Created langfuse_tracer.ts"
```

Expected: `Created langfuse_tracer.ts`

- [ ] **Step 2: Verify Deno can parse it**

```bash
ssh user@localhost "cd ~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70 && deno check langfuse_tracer.ts 2>&1 | head -20"
```

Expected: No errors (or only warnings about unused imports — acceptable at this stage)

- [ ] **Step 3: Commit**

```bash
# On server
cd ~/gtm-machine-infrastructure && git add supabase/functions/generate-narrative/v70/langfuse_tracer.ts
git commit -m "feat(observability): add Langfuse TypeScript tracer for v70 Narrative Generator"
```

---

## Wave 1 — Quick Fixes (Parallel)

### Task 1: Wire Promptfoo → Langfuse Credentials (REQ-001)

**Files:**
- Modify: `server:~/llm-olly-promptfoo/.env`

<task id="T-001" req="REQ-001" wave="1" depends="">
  <description>Copy SDK keys from langfuse .env into promptfoo .env so bridge can post results to Langfuse</description>
  <files>server:~/llm-olly-promptfoo/.env</files>
  <contract>LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are non-empty in promptfoo .env</contract>
  <verify>docker restart llm-olly-promptfoo && docker logs llm-olly-promptfoo 2>&1 | grep -i langfuse</verify>
</task>

- [ ] **Step 1: Write the test (verify keys are empty)**

```bash
ssh user@localhost "grep 'LANGFUSE_PUBLIC_KEY\|LANGFUSE_SECRET_KEY' ~/llm-olly-promptfoo/.env"
```

Expected: Both lines show empty values.

- [ ] **Step 2: Copy keys from langfuse .env to promptfoo .env**

```bash
ssh user@localhost "
PK=\$(grep LANGFUSE_INIT_PROJECT_PUBLIC_KEY ~/llm-olly-langfuse/.env | cut -d= -f2)
SK=\$(grep LANGFUSE_INIT_PROJECT_SECRET_KEY ~/llm-olly-langfuse/.env | cut -d= -f2)
sed -i \"s|LANGFUSE_PUBLIC_KEY=.*|LANGFUSE_PUBLIC_KEY=\${PK}|\" ~/llm-olly-promptfoo/.env
sed -i \"s|LANGFUSE_SECRET_KEY=.*|LANGFUSE_SECRET_KEY=\${SK}|\" ~/llm-olly-promptfoo/.env
echo 'done'
"
```

Expected: `done`

- [ ] **Step 3: Verify keys are set (without displaying values)**

```bash
ssh user@localhost "
test -n \"\$(grep LANGFUSE_PUBLIC_KEY ~/llm-olly-promptfoo/.env | cut -d= -f2)\" && echo 'PUBLIC_KEY: set' || echo 'PUBLIC_KEY: EMPTY'
test -n \"\$(grep LANGFUSE_SECRET_KEY ~/llm-olly-promptfoo/.env | cut -d= -f2)\" && echo 'SECRET_KEY: set' || echo 'SECRET_KEY: EMPTY'
"
```

Expected:
```
PUBLIC_KEY: set
SECRET_KEY: set
```

- [ ] **Step 4: Restart Promptfoo to pick up new env**

```bash
ssh user@localhost "docker restart llm-olly-promptfoo && sleep 5 && docker ps | grep promptfoo"
```

Expected: Container shows `(healthy)` status after restart.

---

### Task 2: Seed Canary Cases (REQ-002)

**Files:**
- Create: `bridge/seed_canary_cases.py`

<task id="T-002" req="REQ-002" wave="1" depends="">
  <description>Load all 22 JSON fixtures from canary/fixtures/ into monitoring.canary_cases table</description>
  <files>bridge/seed_canary_cases.py</files>
  <contract>canary_cases table has exactly 22 rows, one per persona, is_active=true</contract>
  <verify>psql -c "SELECT COUNT(*) FROM canary_cases" → 22</verify>
</task>

- [ ] **Step 1: Write the failing test**

```bash
ssh user@localhost "docker exec llm-olly-postgres bash -c 'psql -U \$POSTGRES_USER -d monitoring -c \"SELECT COUNT(*) FROM canary_cases\"' 2>&1"
```

Expected: `0`

- [ ] **Step 2: Create the seed script**

Create `/Users/you/llm-olly/bridge/seed_canary_cases.py`:

```python
#!/usr/bin/env python3
"""
Seed canary_cases table from canary/fixtures/*.json

Usage:
    python bridge/seed_canary_cases.py

Reads fixtures from canary/fixtures/ (relative to repo root).
Connects to monitoring DB via LLM_OLLY_DB_URL env var (matches common.py).
"""

import json
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

FIXTURES_DIR = Path(__file__).parent.parent / "canary" / "fixtures"
DB_URL = os.environ["LLM_OLLY_DB_URL"]


def load_fixtures() -> list[dict]:
    fixtures = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        with path.open() as f:
            data = json.load(f)
        fixtures.append(data)
    return fixtures


def seed(fixtures: list[dict]) -> None:
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM canary_cases")
            existing = cur.fetchone()[0]
            if existing > 0:
                print(f"canary_cases already has {existing} rows — skipping insert")
                return

            for fixture in fixtures:
                cur.execute(
                    """
                    INSERT INTO canary_cases (
                        case_id, persona_module, prospect_input, assertions,
                        reference_exemplar, is_active
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        fixture["case_id"],
                        fixture["persona_module"],
                        Json(fixture["prospect_input"]),
                        Json(fixture["assertions"]),
                        fixture.get("reference_exemplar"),
                        True,
                    ),
                )
            conn.commit()
            print(f"Inserted {len(fixtures)} fixtures into canary_cases")
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


if __name__ == "__main__":
    fixtures = load_fixtures()
    print(f"Loaded {len(fixtures)} fixtures from {FIXTURES_DIR}")
    seed(fixtures)
```

- [ ] **Step 3: Copy script to server and run it**

```bash
scp /Users/you/llm-olly/bridge/seed_canary_cases.py user@localhost:~/llm-olly/bridge/
ssh user@localhost "
cd ~/llm-olly
pip install psycopg2-binary -q
LLM_OLLY_DB_URL='postgresql://langfuse:\$(grep POSTGRES_PASSWORD ~/llm-olly-langfuse/.env | cut -d= -f2)@localhost:54332/monitoring' \
  python bridge/seed_canary_cases.py
"
```

Expected: `Loaded 22 fixtures from .../canary/fixtures` then `Inserted 22 fixtures into canary_cases`

- [ ] **Step 4: Verify 22 rows exist**

```bash
ssh user@localhost "docker exec llm-olly-postgres bash -c 'psql -U \$POSTGRES_USER -d monitoring -c \"SELECT persona_module, case_id FROM canary_cases ORDER BY persona_module\"' 2>&1"
```

Expected: 22 rows, one per persona.

- [ ] **Step 5: Commit seed script**

```bash
cd /Users/you/llm-olly
git add bridge/seed_canary_cases.py
git commit -m "feat(canary): add seed script for canary_cases table"
```

---

## Wave 2 — Core: Instrument Narrative Generator (REQ-003)

> ⚠️ Files modified in this wave are on server inside `~/gtm-machine-infrastructure/`.
> After changes: sync back to local and commit to gtm-machine-infrastructure.

### Task 3: Integrate Tracer into email-generator.ts

**Files:**
- Modify: `server:~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70/email-generator.ts`
- Modify: `server:~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70/orchestrator.ts`

<task id="T-003" req="REQ-003" wave="2" depends="T-000">
  <description>Inject LangfuseTracer into email-generator generateEmailTouch() — wrap the Anthropic API call with a generation span</description>
  <files>email-generator.ts, orchestrator.ts</files>
  <contract>generateEmailTouch() accepts optional tracer param; on each successful Claude call, calls tracer.logEmailGeneration() with full token usage</contract>
  <verify>Run generate_batch.ts --dry-run, then 1-domain run, check Langfuse UI for trace</verify>
</task>

- [ ] **Step 1: Read current generateEmailTouch signature**

```bash
ssh user@localhost "grep -n 'generateEmailTouch\|function generate\|export.*function' ~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70/email-generator.ts | head -20"
```

Note the exact function signature and param types.

- [ ] **Step 2: Add tracer param to generateEmailTouch**

In `email-generator.ts`, add the optional tracer import and param:

```typescript
// Add at top of file (after existing imports):
import type { LangfuseTracer } from "./langfuse_tracer.ts";

// Modify generateEmailTouch signature to accept optional tracer:
// Before: async function generateEmailTouch(params: ...)
// After:  async function generateEmailTouch(params: ..., tracer?: LangfuseTracer)
```

- [ ] **Step 3: Wrap the Anthropic call with timing + tracer log**

In the `else` branch (~line 446) where `anthropicClient.beta.messages.create()` is called, add timing and tracer call:

```typescript
const t0 = Date.now();
const raw = await (anthropicClient.beta as any).messages.create({ /* existing params */ });
const latencyMs = Date.now() - t0;

// Track token usage (existing code stays identical)
const u = raw.usage;
const callUsage: TokenUsage = {
  input_tokens: u.input_tokens,
  output_tokens: u.output_tokens,
  cache_creation_input_tokens: (u as any).cache_creation_input_tokens ?? 0,
  cache_read_input_tokens: (u as any).cache_read_input_tokens ?? 0,
};
cumulativeUsage = addUsage(cumulativeUsage, callUsage);

// NEW: log to Langfuse if tracer is provided
if (tracer && traceId) {
  tracer.logEmailGeneration({
    traceId,
    position: touchSpec.position,
    systemPrompt,
    userPrompt,
    output: JSON.stringify(parsed),
    inputTokens: callUsage.input_tokens,
    outputTokens: callUsage.output_tokens,
    cacheCreationTokens: callUsage.cache_creation_input_tokens,
    cacheReadTokens: callUsage.cache_read_input_tokens,
    latencyMs,
    model: modelId,
  });
}
```

- [ ] **Step 4: Pass traceId through the call chain**

`generateEmailTouch` needs access to `traceId` (string from `tracer.startSequenceTrace()`). This gets passed from `generateSequence()` in `orchestrator.ts`.

In `orchestrator.ts`, modify `generateSequence()`:

```typescript
// Add to OrchestratorParams interface:
tracer?: LangfuseTracer;

// At start of generateSequence(), before the loop:
let traceId: string | undefined;
if (params.tracer && params.sequenceId) {
  traceId = params.tracer.startSequenceTrace({
    domain: params.domain,
    persona: `${params.persona.seniority}_${params.persona.department}`,
    tier: params.tier,
    model: params.modelOverride ?? "claude-haiku-4-5-20251001",
    sequenceId: params.sequenceId,
  });
}

// Pass tracer + traceId to generateEmailTouch calls inside the loop
```

- [ ] **Step 5: Run type check**

```bash
ssh user@localhost "cd ~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70 && deno check orchestrator.ts 2>&1 | head -30"
```

Expected: No type errors.

---

### Task 4: Wire Tracer into generate_batch.ts

**Files:**
- Modify: `server:~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70/generate_batch.ts`

<task id="T-004" req="REQ-003" wave="2" depends="T-003">
  <description>Init LangfuseTracer once per batch run in generate_batch.ts, pass to generateSequence(), flush at end</description>
  <files>generate_batch.ts</files>
  <contract>If LANGFUSE_PUBLIC_KEY env var is set, tracer is created and every sequence call receives it. If not set, tracer is undefined (no-op path).</contract>
  <verify>Run generate_batch.ts on 1 domain, check Langfuse UI</verify>
</task>

- [ ] **Step 1: Add tracer init to generate_batch.ts**

After the existing env/arg setup at top of file, add:

```typescript
import { LangfuseTracer } from "./langfuse_tracer.ts";

// Init tracer only if keys are configured (makes Langfuse opt-in)
const tracer = Deno.env.get("LANGFUSE_PUBLIC_KEY")
  ? new LangfuseTracer()
  : undefined;

if (tracer) {
  console.log("[langfuse] Tracer initialized — traces will be sent to Langfuse");
} else {
  console.log("[langfuse] LANGFUSE_PUBLIC_KEY not set — tracing disabled");
}
```

- [ ] **Step 2: Pass tracer to generateSequence calls**

Find the `generateSequence(...)` call(s) in `generate_batch.ts` and add `tracer` to the params:

```typescript
const result = await generateSequence({
  // ... existing params ...
  tracer,  // ADD THIS
});
```

- [ ] **Step 3: Flush tracer at end of batch**

Before the final `Deno.exit(0)` or after the domain loop completes:

```typescript
if (tracer) {
  console.log("[langfuse] Shutting down tracer...");
  await tracer.shutdown();
  // Small delay to ensure all HTTP requests complete before Deno exits
  await new Promise((r) => setTimeout(r, 1000));
  console.log("[langfuse] Tracer shutdown complete");
}
```

- [ ] **Step 4: Run a 1-domain smoke test with Langfuse enabled**

```bash
ssh user@localhost "
cd ~/gtm-machine-infrastructure/supabase/functions/generate-narrative/v70
export ANTHROPIC_API_KEY=\$(cat ~/.env | grep ANTHROPIC_API_KEY | cut -d= -f2)
export LANGFUSE_PUBLIC_KEY=\$(grep LANGFUSE_INIT_PROJECT_PUBLIC_KEY ~/llm-olly-langfuse/.env | cut -d= -f2)
export LANGFUSE_SECRET_KEY=\$(grep LANGFUSE_INIT_PROJECT_SECRET_KEY ~/llm-olly-langfuse/.env | cut -d= -f2)
export SUPABASE_URL=https://PROJECT_REF.supabase.co
export SUPABASE_SERVICE_ROLE_KEY=\$(cat ~/.env | grep SUPABASE_SERVICE_ROLE_KEY | cut -d= -f2)
deno run --allow-net --allow-env --allow-write --allow-read generate_batch.ts \
  --model haiku --pass 1 --domain examplestore.com 2>&1 | tail -30
"
```

Expected: `[langfuse] Tracer initialized`, generation output, `[langfuse] Traces flushed`

- [ ] **Step 5: Verify trace in Langfuse UI**

Open `http://localhost:3300` → project `narrative-generator` → Traces.
Expected: 1 trace with name matching `sequence:examplestore.com:*`, containing 5 generation spans with token usage.

- [ ] **Step 6: Commit**

```bash
# On server
cd ~/gtm-machine-infrastructure
git add supabase/functions/generate-narrative/v70/langfuse_tracer.ts \
        supabase/functions/generate-narrative/v70/email-generator.ts \
        supabase/functions/generate-narrative/v70/orchestrator.ts \
        supabase/functions/generate-narrative/v70/generate_batch.ts
git commit -m "feat(observability): instrument v70 generator with Langfuse TypeScript SDK

- Add LangfuseTracer wrapper (npm:langfuse) for Deno environment
- Wrap Anthropic API call with generation span including cache token fields
- Trace opt-in: only activates when LANGFUSE_PUBLIC_KEY env var is set
- Flush at end of batch run to ensure all spans are sent"
```

- [ ] **Step 7: Sync changes back to local Mac** ← **Required: do not skip**

The TypeScript changes were committed on server. Pull them to the Mac so the local `gtm-machine-infrastructure` repo stays in sync.

```bash
# On Mac — pull the server commits
cd ~/gtm-machine-infrastructure
git pull
# Verify the new files are present locally
ls supabase/functions/generate-narrative/v70/langfuse_tracer.ts
git log --oneline -3
```

Expected: `langfuse_tracer.ts` exists locally, `git log` shows the Wave 2 commit.

---

## Wave 3 — Prefect Flows Deployment (REQ-004)

> **Key discovery:** The Prefect worker builds from a custom Dockerfile (`./docker/prefect/`) and mounts flows from `./flows:/opt/prefect/flows`. This means:
> - **Flow files** go directly on the host at `~/server-infrastructure/scripting-host/flows/` — no `docker cp` needed
> - **Dependencies** are baked into the Dockerfile — no ephemeral `pip install`
> - **Network** is declared in docker-compose.yml — no runtime `docker network connect`

### Task 5: Deploy Canary Heartbeat to Prefect

**Files:**
- Rename: `prefect/common.py` → `prefect/_llm_olly_common.py` (in repo)
- Modify: `prefect/canary_heartbeat.py`, `prefect/drift_report.py`, `prefect/model_change_trigger.py` (update imports)
- Modify: `server:~/server-infrastructure/scripting-host/docker-compose.yml` (add llm-olly network)
- Modify: `server:~/server-infrastructure/scripting-host/docker/prefect/` (add dependencies to Dockerfile)

<task id="T-005" req="REQ-004" wave="3" depends="T-002">
  <description>Deploy canary flows to Prefect: rename common.py in repo, add llm-olly network to compose, bake deps into Dockerfile, copy flows to host-mounted dir, rebuild worker, deploy flows</description>
  <files>prefect/_llm_olly_common.py, scripting-host/docker-compose.yml, scripting-host/docker/prefect/</files>
  <contract>Prefect deployment shows 3 flows. Worker container has llm-olly network. Dependencies survive container recreate. Manual heartbeat run completes.</contract>
  <verify>docker exec worker pip show langfuse rapidfuzz; docker exec worker python -c "from _llm_olly_common import get_monitoring_db"; prefect deployment ls | grep llm-olly</verify>
</task>

- [ ] **Step 1: Rename common.py in the repo and update imports**

Rename the file and update all imports at the source level — no deploy-time sed.

```bash
cd /Users/you/llm-olly
mv prefect/common.py prefect/_llm_olly_common.py
# Update imports in all flow files
sed -i '' 's/from common import/from _llm_olly_common import/g' \
  prefect/canary_heartbeat.py \
  prefect/drift_report.py \
  prefect/model_change_trigger.py
git add prefect/
git commit -m "refactor(prefect): rename common.py → _llm_olly_common.py to avoid collision with existing flow modules"
```

- [ ] **Step 2: Add `llm-olly` external network to scripting-host docker-compose**

The Prefect worker needs to reach `llm-olly-postgres:5432` on the `llm-olly` Docker network. Add it as an external network in `~/server-infrastructure/scripting-host/docker-compose.yml`:

```bash
ssh user@localhost "cd ~/server-infrastructure/scripting-host && cp docker-compose.yml docker-compose.yml.bak"
```

Then edit `docker-compose.yml` on server:

**In the `prefect-worker` service, add `- llm-olly` to its networks list:**
```yaml
  prefect-worker:
    # ... existing config ...
    networks:
      - internal
      - llm-olly                 # access llm-olly-postgres for canary monitoring DB
```

**In the top-level `networks:` section, add:**
```yaml
networks:
  internal:
    driver: bridge
  server-net:
    external: true
  llm-olly:
    external: true               # created by llm-olly-langfuse docker-compose
    name: llm-olly
```

- [ ] **Step 3: Add llm-olly dependencies to the Prefect worker Dockerfile**

```bash
ssh user@localhost "cat ~/server-infrastructure/scripting-host/docker/prefect/Dockerfile"
```

Check if there's a `requirements.txt` or inline `pip install`. Add these packages:
```
langfuse>=2.51.0
rapidfuzz>=3.6.0
```

If the Dockerfile uses a requirements file, append to it. If it uses inline `RUN pip install`, add to that line.

- [ ] **Step 4: Add llm-olly env vars to the scripting-host .env**

The worker reads its `.env` file at container start. Add the monitoring DB URL and Langfuse keys:

```bash
ssh user@localhost "
PG_PASS=\$(grep POSTGRES_PASSWORD ~/llm-olly-langfuse/.env | cut -d= -f2)
PK=\$(grep LANGFUSE_INIT_PROJECT_PUBLIC_KEY ~/llm-olly-langfuse/.env | cut -d= -f2)
SK=\$(grep LANGFUSE_INIT_PROJECT_SECRET_KEY ~/llm-olly-langfuse/.env | cut -d= -f2)

cat >> ~/server-infrastructure/scripting-host/.env << EOF

# llm-olly monitoring (added by Langfuse wiring plan 2026-03-26)
LLM_OLLY_DB_URL=postgresql://langfuse:\${PG_PASS}@llm-olly-postgres:5432/monitoring
LANGFUSE_PUBLIC_KEY=\${PK}
LANGFUSE_SECRET_KEY=\${SK}
LANGFUSE_HOST=http://llm-olly-langfuse-web:3000
EOF
echo 'env vars appended'
"
```

Expected: `env vars appended`

> **Note:** `LANGFUSE_HOST` uses the internal container hostname `llm-olly-langfuse-web:3000` (not the host-mapped port 3300), since the worker is now on the `llm-olly` network.

- [ ] **Step 5: Copy flow files to host-mounted flows directory**

```bash
scp /Users/you/llm-olly/prefect/_llm_olly_common.py user@localhost:~/server-infrastructure/scripting-host/flows/
scp /Users/you/llm-olly/prefect/canary_heartbeat.py user@localhost:~/server-infrastructure/scripting-host/flows/
scp /Users/you/llm-olly/prefect/drift_report.py user@localhost:~/server-infrastructure/scripting-host/flows/
scp /Users/you/llm-olly/prefect/model_change_trigger.py user@localhost:~/server-infrastructure/scripting-host/flows/
```

These are instantly available inside the container via the `./flows:/opt/prefect/flows` bind mount — no `docker cp` needed.

- [ ] **Step 6: Rebuild and restart the Prefect worker**

```bash
ssh user@localhost "
cd ~/server-infrastructure/scripting-host
docker compose up -d --build prefect-worker 2>&1 | tail -10
"
```

Expected: Worker rebuilds with new dependencies, starts, connects to both `internal` and `llm-olly` networks.

- [ ] **Step 7: Verify dependencies and network**

```bash
ssh user@localhost "
docker exec scripting-host-prefect-worker-1 pip show langfuse rapidfuzz 2>&1 | grep -E 'Name:|Version:'
docker exec scripting-host-prefect-worker-1 python -c 'from _llm_olly_common import get_monitoring_db; print(\"import OK\")'
docker exec scripting-host-prefect-worker-1 ping -c1 llm-olly-postgres 2>&1 | head -2
"
```

Expected: `langfuse` and `rapidfuzz` versions shown, `import OK`, ping succeeds.

- [ ] **Step 8: Deploy all flows using Prefect 2.x deployment apply**

```bash
ssh user@localhost "
docker exec scripting-host-prefect-worker-1 bash -c '
# Canary heartbeat
cat > /tmp/canary-heartbeat-deploy.yaml << EOF
name: canary-heartbeat-daily
description: Daily canary health check across 22 personas
work_queue_name: default
work_pool_name: default-process-pool
tags: [llm-olly, canary, daily]
parameters: {}
schedules:
  - cron: \"0 2 * * *\"
    timezone: America/New_York
    active: true
flow_name: canary-heartbeat
manifest_path: null
storage: null
path: /opt/prefect/flows
entrypoint: canary_heartbeat.py:canary_heartbeat
EOF
prefect deployment apply /tmp/canary-heartbeat-deploy.yaml 2>&1

# Drift report
cat > /tmp/drift-report-deploy.yaml << EOF
name: drift-report-weekly
description: Weekly persona drift report with materialized view refresh
work_queue_name: default
work_pool_name: default-process-pool
tags: [llm-olly, drift, weekly]
parameters: {}
schedules:
  - cron: \"0 8 * * 1\"
    timezone: America/New_York
    active: true
flow_name: drift-report
manifest_path: null
storage: null
path: /opt/prefect/flows
entrypoint: drift_report.py:drift_report
EOF
prefect deployment apply /tmp/drift-report-deploy.yaml 2>&1

# Model change trigger (manual only)
cat > /tmp/model-change-deploy.yaml << EOF
name: model-change-trigger
description: Run full canary suite when Claude model version changes
work_queue_name: default
work_pool_name: default-process-pool
tags: [llm-olly, model-change]
parameters:
  model_version: \"claude-sonnet-4-6\"
schedules: []
flow_name: model-change-trigger
manifest_path: null
storage: null
path: /opt/prefect/flows
entrypoint: model_change_trigger.py:model_change_trigger
EOF
prefect deployment apply /tmp/model-change-deploy.yaml 2>&1
'
"
```

Expected: 3 deployment success messages.

- [ ] **Step 9: Run heartbeat once manually to verify**

```bash
ssh user@localhost "
docker exec scripting-host-prefect-worker-1 bash -c '
cd /opt/prefect/flows
python -c \"
import asyncio
from canary_heartbeat import canary_heartbeat
asyncio.run(canary_heartbeat())
\" 2>&1 | tail -30
'
"
```

Expected: `status: completed`, `total_cases: 22`, `pass_rate: <some value>`, no Python exceptions.

- [ ] **Step 10: Commit**

```bash
cd /Users/you/llm-olly
git add prefect/
git commit -m "feat(prefect): deploy canary flows with _llm_olly_common rename, host-mounted volumes"
```

---

## Wave 4 — Edit Distance Capture (REQ-005)

### Task 6: Build send capture CLI

**Files:**
- Create: `bridge/capture_send.py`

<task id="T-006" req="REQ-005" wave="4" depends="T-002">
  <description>CLI script that accepts human-sent email content, inserts into email_generations, computes edit distance vs AI draft, stores edit_metrics</description>
  <files>bridge/capture_send.py</files>
  <contract>Running capture_send.py with domain + sequence_id + human_sent content inserts a row in email_generations with edit_metrics populated</contract>
  <verify>python bridge/capture_send.py --help; then run with test data, verify row in email_generations</verify>
</task>

- [ ] **Step 1: Write the failing test**

```bash
ssh user@localhost "docker exec llm-olly-postgres bash -c 'psql -U \$POSTGRES_USER -d monitoring -c \"SELECT COUNT(*) FROM email_generations\"' 2>&1"
```

Expected: `0`

- [ ] **Step 2: Create capture_send.py**

Create `/Users/you/llm-olly/bridge/capture_send.py`:

```python
#!/usr/bin/env python3
"""
Capture human-sent email and compute edit distance vs AI draft.

Usage:
    # Record a sent email (provide AI draft + human-sent version):
    python bridge/capture_send.py \\
        --domain examplestore.com \\
        --persona executive \\
        --model claude-haiku-4-5-20251001 \\
        --ai-subject "search visibility drop" \\
        --ai-body "Your organic traffic fell 27% last quarter..." \\
        --human-subject "search drop" \\
        --human-body "Your organic fell 27%..." \\
        --sequence-id <uuid>

Computes edit metrics using bridge/edit_distance.py and inserts into email_generations.
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

# Add bridge dir to path for edit_distance import
sys.path.insert(0, str(Path(__file__).parent))
from edit_distance import compute_edit_metrics

DB_URL = os.environ["LLM_OLLY_DB_URL"]


def parse_args():
    p = argparse.ArgumentParser(description="Capture human-sent email + compute edit distance")
    p.add_argument("--domain", required=True)
    p.add_argument("--persona", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--ai-subject", required=True)
    p.add_argument("--ai-body", required=True)
    p.add_argument("--human-subject", required=True)
    p.add_argument("--human-body", required=True)
    p.add_argument("--persona-version", type=int, default=1, help="Persona prompt version (NOT NULL in DB)")
    p.add_argument("--base-prompt-version", type=int, default=1, help="Base prompt version (NOT NULL in DB)")
    p.add_argument("--sequence-id", default=None)
    p.add_argument("--experiment-id", default=None)
    p.add_argument("--prospect-data", default="{}", help="JSON string of prospect metadata")
    return p.parse_args()


def main():
    args = parse_args()

    ai_draft = f"Subject: {args.ai_subject}\n\n{args.ai_body}"
    human_sent = f"Subject: {args.human_subject}\n\n{args.human_body}"

    metrics = compute_edit_metrics(ai_draft, human_sent)

    prospect_data = json.loads(args.prospect_data)

    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO email_generations (
                    persona_module, persona_version, base_prompt_version,
                    model_version, prospect_domain, prospect_data,
                    experiment_id,
                    ai_draft_subject, ai_draft_body,
                    human_sent_subject, human_sent_body,
                    edit_metrics, editing_effort, subject_changed, body_diff_ratio
                ) VALUES (
                    %s, %s, %s,
                    %s, %s, %s,
                    %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s, %s
                ) RETURNING id
                """,
                (
                    args.persona,
                    args.persona_version,
                    args.base_prompt_version,
                    args.model,
                    args.domain,
                    Json(prospect_data),
                    args.experiment_id,
                    args.ai_subject,
                    args.ai_body,
                    args.human_subject,
                    args.human_body,
                    # compute_edit_metrics() returns a plain dict — use dict access
                    Json(metrics),
                    metrics["editing_effort"],
                    metrics.get("section_diffs", {}).get("subject", {}).get("changed", False),
                    metrics.get("section_diffs", {}).get("body", {}).get("word_diff_ratio", 0.0),
                ),
            )
            row_id = cur.fetchone()[0]
            conn.commit()
            print(f"Inserted email_generation row: {row_id}")
            print(f"editing_effort: {metrics['editing_effort']:.3f}")
            print(f"interpretation: {metrics['interpretation']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Test with dummy data on server**

```bash
scp /Users/you/llm-olly/bridge/capture_send.py user@localhost:~/llm-olly/bridge/
ssh user@localhost "
cd ~/llm-olly
LLM_OLLY_DB_URL='postgresql://langfuse:\$(grep POSTGRES_PASSWORD ~/llm-olly-langfuse/.env | cut -d= -f2)@localhost:54332/monitoring' \
python bridge/capture_send.py \
  --domain examplestore.com \
  --persona executive \
  --model claude-haiku-4-5-20251001 \
  --ai-subject 'search visibility drop' \
  --ai-body 'Your organic traffic fell 27% last quarter — Dunn Lumber site shows LCP at 4.2s on mobile.' \
  --human-subject 'search drop' \
  --human-body 'Your organic fell 27%.' 2>&1
"
```

Expected: `Inserted email_generation row: <uuid>` and `editing_effort: <value>`

- [ ] **Step 4: Verify row in DB**

```bash
ssh user@localhost "docker exec llm-olly-postgres bash -c 'psql -U \$POSTGRES_USER -d monitoring -c \"SELECT prospect_domain, persona_module, editing_effort, subject_changed FROM email_generations\"' 2>&1"
```

Expected: 1 row with `examplestore.com`, `executive`, and numeric `editing_effort`.

- [ ] **Step 5: Commit**

```bash
cd /Users/you/llm-olly
git add bridge/capture_send.py
git commit -m "feat(edit-distance): add capture_send CLI to record human-sent emails and compute edit metrics"
```

---

## Wave 5 — End-to-End Smoke Test (REQ-006)

### Task 7: Full Stack Verification

<task id="T-007" req="REQ-006" wave="5" depends="T-004,T-005,T-006">
  <description>Run full end-to-end: generate 1 domain, verify Langfuse trace, run Promptfoo bridge, verify DB state</description>
  <files>n/a — verification only</files>
  <contract>Langfuse UI shows trace; monitoring DB shows canary_cases=22; email_generations has 1 test row; Prefect shows heartbeat deployment</contract>
  <verify>All checks below pass</verify>
</task>

- [ ] **Check 1: Langfuse UI reachable**

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:3300
```

Expected: `200`

- [ ] **Check 2: Monitoring DB has 22 canary cases**

```bash
ssh user@localhost "docker exec llm-olly-postgres bash -c 'psql -U \$POSTGRES_USER -d monitoring -c \"SELECT COUNT(*) FROM canary_cases\"'"
```

Expected: `22`

- [ ] **Check 3: Promptfoo health**

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:3200
```

Expected: `200`

- [ ] **Check 4: Langfuse has ≥1 trace**

Open `http://localhost:3300` → Traces. Verify at least 1 trace exists with 5 generation spans.

- [ ] **Check 5: Prefect shows 3 deployments**

```bash
ssh user@localhost "docker exec scripting-host-prefect-worker-1 bash -c 'prefect deployment ls 2>&1 | grep llm-olly'"
```

Expected: `canary-heartbeat-daily`, `drift-report-weekly`, `model-change-trigger`

- [ ] **Check 6: Promptfoo → Langfuse bridge works end-to-end (REQ-001)**

Run a single Promptfoo eval on one fixture, bridge the result to Langfuse, verify trace appears:

```bash
ssh user@localhost "
cd ~/llm-olly/promptfoo
docker exec llm-olly-promptfoo promptfoo eval \
  -c /app/configs/promptfooconfig.yaml \
  --filter-pattern 'executive' \
  -o /tmp/smoke-result.json 2>&1 | tail -10
docker cp llm-olly-promptfoo:/tmp/smoke-result.json /tmp/smoke-result.json
cd ~/llm-olly
python bridge/promptfoo_to_langfuse.py /tmp/smoke-result.json --tag smoke-test 2>&1
"
```

Then open `http://localhost:3300` → Traces → filter by tag `smoke-test`. Expected: 1 trace with assertion scores.

- [ ] **Final commit**

```bash
cd /Users/you/llm-olly
git add -A
git commit -m "chore: wire up complete observability stack — Langfuse traces, canary seeded, Prefect deployed"
```

---

## Wave 6 — Infrastructure Hardening (Survive Day 2)

> All changes in this wave ensure the stack survives `docker compose down/up`, container recreates, and host reboots.

### Task 8: Persist ClickHouse Config

<task id="T-008" req="REQ-003" wave="6" depends="T-007">
  <description>Bind-mount disable_system_logs.xml into ClickHouse container so it survives recreate</description>
  <files>server:~/llm-olly-langfuse/disable_system_logs.xml, server:~/llm-olly-langfuse/docker-compose.yml</files>
  <contract>After docker compose down && docker compose up, ClickHouse still has system logs disabled</contract>
  <verify>docker exec llm-olly-clickhouse cat /etc/clickhouse-server/config.d/disable_system_logs.xml</verify>
</task>

- [ ] **Step 1: Copy the config file from inside the container to the host**

```bash
ssh user@localhost "
docker cp llm-olly-clickhouse:/etc/clickhouse-server/config.d/disable_system_logs.xml ~/llm-olly-langfuse/disable_system_logs.xml
cat ~/llm-olly-langfuse/disable_system_logs.xml
"
```

Expected: The XML content we wrote earlier.

- [ ] **Step 2: Add bind mount to docker-compose.yml**

Edit `~/llm-olly-langfuse/docker-compose.yml` on server. In the `clickhouse` service, add one line to the `volumes:` section:

```yaml
  clickhouse:
    image: clickhouse/clickhouse-server:24.3
    container_name: llm-olly-clickhouse
    # ... existing config ...
    volumes:
      - ch_data:/var/lib/clickhouse
      - ch_logs:/var/log/clickhouse-server
      - ./disable_system_logs.xml:/etc/clickhouse-server/config.d/disable_system_logs.xml:ro
```

The `:ro` flag makes it read-only inside the container. The existing `docker_related_config.xml` in `config.d/` is NOT affected — single-file bind mounts overlay only that one file.

- [ ] **Step 3: Verify by recreating ClickHouse**

```bash
ssh user@localhost "
cd ~/llm-olly-langfuse
docker compose up -d clickhouse 2>&1 | tail -5
sleep 5
docker exec llm-olly-clickhouse cat /etc/clickhouse-server/config.d/disable_system_logs.xml
docker stats llm-olly-clickhouse --no-stream --format '{{.MemUsage}}'
"
```

Expected: Config file present after recreate. RAM stays under 600MB.

- [ ] **Step 4: Commit the compose change**

```bash
# On server
cd ~/llm-olly-langfuse
git add docker-compose.yml disable_system_logs.xml
git commit -m "ops(clickhouse): persist disable_system_logs.xml via bind mount — survives recreate"
```

---

## Known Gaps / Post-Plan

- **Slack webhook** for canary alerts: add `SLACK_WEBHOOK_URL` to scripting-host `.env` when Slack channel is set up
- **HubSpot webhook** for automated edit capture: when an email is marked sent in HubSpot, trigger `capture_send.py` automatically — this is a follow-on project
- **`embedding_similarity`** field in `canary_results`: `model_change_trigger.py` stubs this — pgvector embeddings are a follow-on
- **Langfuse SDK fallback**: if `npm:langfuse` has Deno compat issues at runtime, pivot to raw `fetch()` against Langfuse ingestion API (`POST /api/public/traces`) — native Deno fetch bypasses Node compat layer
