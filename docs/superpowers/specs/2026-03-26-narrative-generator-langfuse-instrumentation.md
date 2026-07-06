# Narrative Generator Langfuse Instrumentation

**Date:** 2026-03-26
**Status:** Approved
**llm-olly Step:** 9 of 10
**Target:** `~/gtm-machine-infrastructure/supabase/functions/generate-narrative/`

---

## Problem

The Narrative Generator (Supabase Edge Function) produces cold email sequences via Anthropic Claude but has zero external observability. Token usage, cache hit rates, generation latency, validation failure rates, and per-persona quality are invisible. llm-olly's Langfuse stack is running on server but not wired into the production generation pipeline.

## Success Criteria

1. Every `anthropic.messages.create()` call in the Narrative Generator is auto-traced to Langfuse (input, output, tokens, cache metrics, latency)
2. Each trace includes business context (persona, angle, framework, model, prompt version)
3. Validation results and generation attempt counts are attached as Langfuse scores
4. `trace_id` is returned in the edge function response and persisted in `narrative_generations` table
5. Phase 2 post-send scoring contract is documented and stubbed
6. Instrumentation is zero-impact when Langfuse env vars are not set (graceful no-op)

## Out of Scope

- Post-send scoring pipeline (HubSpot/Instantly engagement data) — Phase 2, documented but not built
- Edit distance scoring (human edits vs AI draft) — Phase 2
- Changes to generation logic, prompts, or validation
- Changes to the Python bridge in llm-olly (stays as-is for Promptfoo/canary)
- Changes to Langfuse Docker stack

---

## Architecture

```
Edge Function (Deno)                          Server (localhost)
+---------------------------------+           +------------------------+
| generate-narrative/             |           | Langfuse :3300         |
|                                 | OTel spans|                        |
|  telemetry.ts (init module)     |---------->|  Traces                |
|  - OTel SDK                     |           |  Generations (auto)    |
|  - LangfuseSpanProcessor       |           |  Scores (manual)       |
|  - AnthropicInstrumentation     |           |                        |
|                                 |           |  Dashboards:           |
|  Auto-traced:                   |           |  - Per-persona quality |
|  - Every messages.create()      |           |  - Token cost + cache  |
|  - Token usage + cache metrics  |           |  - Validation rates    |
|  - Latency                      |           +------------------------+
|                                 |                      ^
|  Manual spans:                  |           +----------+--------------+
|  - Persona/angle/framework      |           | Phase 2 (future):      |
|  - Validation scores            |           | Prefect flow receives  |
|  - Generation attempt count     |           | engagement data ->     |
|                                 |           | score_generation()     |
|  Returns: trace_id in response  |           | via trace_id lookup    |
+---------------------------------+           +------------------------+
         |
         | trace_id stored in
         v narrative_generations
+---------------------------------+
| Supabase                        |
| narrative_generations table     |
| + langfuse_trace_id column      |
+---------------------------------+
```

---

## Technical Design

### 1. Telemetry Init Module

**File:** `generate-narrative/telemetry.ts` (new, ~50 lines)

**Purpose:** Initialize OTel tracing with Langfuse exporter and Anthropic auto-instrumentation. Exports a function to create an instrumented Anthropic client.

**IMPORTANT (C1):** Cannot use `@opentelemetry/sdk-node` — it depends on Node.js-only APIs (`async_hooks`, `process`, `perf_hooks`) that don't exist in Deno Edge Runtime. Use `@opentelemetry/sdk-trace-base` with `BasicTracerProvider` instead.

**IMPORTANT (C2):** `AnthropicInstrumentation.manuallyInstrument()` takes a **client instance**, not the class constructor. Since `index.ts` currently creates the Anthropic client at module top-level (line 40), this module must export a factory function that creates an instrumented client.

```typescript
import { BasicTracerProvider, SimpleSpanProcessor } from "npm:@opentelemetry/sdk-trace-base";
import { LangfuseSpanProcessor } from "npm:@langfuse/otel";
import { AnthropicInstrumentation } from "npm:@arizeai/openinference-instrumentation-anthropic";
import Anthropic from "npm:@anthropic-ai/sdk";
import { trace, context, SpanStatusCode } from "npm:@opentelemetry/api";

const langfuseEnabled = !!Deno.env.get("LANGFUSE_PUBLIC_KEY");

let langfuseProcessor: LangfuseSpanProcessor | null = null;
const tracer = trace.getTracer("generate-narrative");

if (langfuseEnabled) {
  langfuseProcessor = new LangfuseSpanProcessor({
    publicKey: Deno.env.get("LANGFUSE_PUBLIC_KEY")!,
    secretKey: Deno.env.get("LANGFUSE_SECRET_KEY")!,
    baseUrl: Deno.env.get("LANGFUSE_HOST") ?? "http://localhost:3300",
  });

  // Use SimpleSpanProcessor (not batched) — edge functions are short-lived,
  // batching risks dropped spans on isolate freeze (S2)
  const provider = new BasicTracerProvider();
  provider.addSpanProcessor(langfuseProcessor);
  provider.register();
}

/** Create an instrumented Anthropic client (C2 fix) */
export function createInstrumentedClient(apiKey: string): Anthropic {
  const client = new Anthropic({ apiKey });
  if (langfuseEnabled) {
    const instrumentation = new AnthropicInstrumentation();
    instrumentation.manuallyInstrument(client);
  }
  return client;
}

export { langfuseProcessor, langfuseEnabled, tracer, context, SpanStatusCode };
```

**Key behaviors:**
- **Graceful no-op:** If `LANGFUSE_PUBLIC_KEY` is not set, nothing is initialized. `createInstrumentedClient` returns a plain Anthropic client. Zero overhead.
- **SimpleSpanProcessor:** Uses unbatched export — each span is sent immediately. Edge functions are short-lived; batching risks dropped spans on isolate freeze.
- **Client factory:** `index.ts` must switch from module-level `new Anthropic()` to calling `createInstrumentedClient()`. This is the only breaking change to existing code structure.

**CAVEAT (C3):** v71 uses `client.beta.messages.create()`. The `AnthropicInstrumentation` may only patch `client.messages.create()`, not the beta namespace. During implementation, verify by checking traces for v71 calls. If missing, add manual spans around `beta.messages.create()` calls in v71's email-generator.ts.

### 2. Manual Trace in Request Handler

**File:** `generate-narrative/index.ts` (modify Deno.serve handler, ~40 lines added)

**What changes:**
- Import `telemetry.ts` at top of file (before Anthropic import)
- After parsing request body, start a Langfuse trace with business context
- Before returning response, attach scores and flush
- Include `trace_id` in response JSON

**Trace metadata (manual span attributes):**

| Attribute | Source | Example |
|-----------|--------|---------|
| `persona_id` | `${department}_${seniority}` | `"ecommerce_vp"` |
| `domain` | request body | `"acme.com"` |
| `primary_angle` | `effectiveAngle` | `"performance"` |
| `secondary_angle` | request body | `"competitive"` |
| `framework` | `selectedFramework` | `"challenger"` |
| `model` | resolved model ID | `"claude-sonnet-4-20250514"` |
| `prompt_type` | request body | `"sequence_full"` |
| `pipeline_version` | request body or default (I4: type is `"v67" \| "v68"` — v71 is routed separately but should report as `"v71"`) | `"v67"` |
| `contact_role` | request body | `"technical"` |

**Scores attached at end of request:**

| Score Name | Type | Value |
|------------|------|-------|
| `validation_passed` | boolean as 0/1 | `1.0` or `0.0` |
| `generation_attempts` | integer | `1` to `max_retries` |
| `validation_errors` | integer | count of failed rules |

**Manual trace creation (I3 — decided, not TBD):**

Use the OTel `tracer.startActiveSpan()` API directly. This creates a parent span that auto-instrumented Anthropic calls will automatically nest under via OTel context propagation. Business metadata goes as span attributes. Scores go as span events (Langfuse maps OTel events to scores when attribute names match its conventions).

```typescript
// At top of file:
import {
  createInstrumentedClient,
  langfuseProcessor,
  langfuseEnabled,
  tracer,
  SpanStatusCode,
} from "./telemetry.ts";

// Replace module-level Anthropic client:
const anthropic = createInstrumentedClient(Deno.env.get("ANTHROPIC_API_KEY")!);

// Inside Deno.serve handler, after parsing requestBody:
async function handleRequest(requestBody: RequestBody): Promise<Response> {
  const persona_id = `${requestBody.department || "unknown"}_${requestBody.seniority}`;

  // Start parent span with business context
  return tracer.startActiveSpan("generate-narrative", async (span) => {
    try {
      span.setAttributes({
        "narrative.persona_id": persona_id,
        "narrative.domain": requestBody.domain,
        "narrative.prompt_type": requestBody.prompt_type,
        "narrative.pipeline_version": requestBody.pipeline_version || "v67",
        "narrative.model": requestBody.model || "sonnet",
        "narrative.contact_role": requestBody.contact_role || "",
      });

      // ... existing generation logic ...
      // Auto-instrumented anthropic.messages.create() calls
      // automatically nest under this parent span

      // After generation completes, add angle/framework (only known post-decision-tree):
      span.setAttributes({
        "narrative.primary_angle": effectiveAngle,
        "narrative.secondary_angle": effectiveSecondaryAngle || "",
        "narrative.framework": selectedFramework,
      });

      // Attach scores as span events (Langfuse convention):
      span.addEvent("score", {
        "langfuse.score.name": "validation_passed",
        "langfuse.score.value": validationResult.valid ? 1.0 : 0.0,
      });
      span.addEvent("score", {
        "langfuse.score.name": "generation_attempts",
        "langfuse.score.value": attempts,
      });

      const traceId = span.spanContext().traceId;
      span.setStatus({ code: SpanStatusCode.OK });

      // Flush before response (I2: for streaming, flush inside ReadableStream)
      if (langfuseEnabled && langfuseProcessor) {
        await langfuseProcessor.forceFlush();
      }

      return new Response(JSON.stringify({
        success: true,
        output: parsedOutput,
        trace_id: traceId,  // NEW — correlation key for Phase 2
        metadata: { /* existing metadata */ }
      }));
    } catch (error) {
      span.setStatus({ code: SpanStatusCode.ERROR, message: String(error) });
      throw error;
    } finally {
      span.end();
    }
  });
}
```

**v71 streaming response handling (I2):** v71 returns a `ReadableStream`. The `forceFlush()` call must happen inside the stream's completion callback, NOT before `return new Response()`:

```typescript
// In v71 streaming path:
const stream = new ReadableStream({
  async start(controller) {
    // ... generate and enqueue chunks ...
    controller.close();
    // Flush AFTER stream completes, BEFORE isolate freezes:
    if (langfuseEnabled && langfuseProcessor) {
      await langfuseProcessor.forceFlush();
    }
  }
});
return new Response(stream, { headers: { "content-type": "text/event-stream" } });
```

### 3. trace_id Persistence

**Files:**
- `generate-narrative/index.ts` — v67 insert path (~line 3995)
- `generate-narrative/v71/persistence.ts` — v71 insert path (~line 47)

**IMPORTANT (I1):** v71 has its own persistence layer at `v71/persistence.ts`. The `langfuse_trace_id` must be added to ALL insert paths, not just the v67 path in `index.ts`. v68 routes through the v67 insert path so it's covered.

**What changes:** Add `langfuse_trace_id` to both insert locations.

```typescript
// v67 path in index.ts (around line 3995):
const { error: insertError } = await supabase
  .from('narrative_generations')
  .insert({
    domain,
    prompt_type,
    // ... existing fields ...
    langfuse_trace_id: traceId,  // NEW
  });

// v71 path in v71/persistence.ts (around line 47):
const { error: insertError } = await supabase
  .from('narrative_generations')
  .insert({
    // ... existing v71 fields ...
    langfuse_trace_id: traceId,  // NEW — passed from orchestrator
  });
```

**Schema change required:** Add `langfuse_trace_id TEXT` column to `narrative_generations` table. Nullable (backwards-compatible with existing rows).

**Phase 2 contract stub (as code comment):**

```typescript
// PHASE 2: Post-Send Scoring Contract
// =====================================
// When engagement data arrives from HubSpot/Instantly:
// 1. Look up langfuse_trace_id from narrative_generations by id (primary key)
//    Note: v71 writes one row per email touch, so use the specific generation row ID
//    passed through HubSpot custom properties, NOT domain + timestamp (ambiguous)
// 2. Call Langfuse API to attach scores:
//    - score_generation(trace_id, "opened", 1/0)
//    - score_generation(trace_id, "clicked", 1/0)
//    - score_generation(trace_id, "replied", 1/0)
//    - score_generation(trace_id, "bounced", 1/0)
//    - log_edit_metrics(trace_id, ai_draft, human_sent)  // if human edited before send
// 3. This uses the Python LangfuseNarrativeTracer in llm-olly/bridge/
//    (already implemented, needs trace_id + engagement data as inputs)
// 4. Trigger: Prefect flow on webhook or scheduled poll
// =====================================
```

### 4. v68/v71 Orchestrator Passthrough

**Files:**
- `generate-narrative/v68/orchestrator.ts` (~5 lines)
- `generate-narrative/v71/orchestrator.ts` (~5 lines)

**What changes:** The auto-instrumentation handles the LLM calls automatically (since AnthropicInstrumentation patches the Anthropic class globally). These orchestrators do NOT need tracer parameters passed in — the OTel context propagation handles nesting.

**However:** If we want business-context spans (persona, angle) on v68/v71 traces, we need to propagate the parent span context. This is standard OTel context propagation — the active span set in the handler automatically becomes the parent of spans created in called functions.

**Verification needed during implementation:** Confirm that OTel context propagation works across async function calls in Deno isolates. If it doesn't, we fall back to explicitly passing a trace ID.

### 5. Environment Variables

**Add to Supabase Edge Function secrets:**

| Variable | Value | Required |
|----------|-------|----------|
| `LANGFUSE_PUBLIC_KEY` | Project public key from Langfuse UI | Yes (for tracing to be active) |
| `LANGFUSE_SECRET_KEY` | Project secret key from Langfuse UI | Yes (for tracing to be active) |
| `LANGFUSE_HOST` | `http://localhost:3300` | No (defaults to this value) |

**Set via:**
```bash
supabase secrets set LANGFUSE_PUBLIC_KEY=pk-lf-...
supabase secrets set LANGFUSE_SECRET_KEY=sk-lf-...
supabase secrets set LANGFUSE_HOST=http://localhost:3300
```

---

## Dependencies (npm: imports for Deno)

| Package | Purpose |
|---------|---------|
| `npm:@opentelemetry/sdk-trace-base` | `BasicTracerProvider` + `SimpleSpanProcessor` (Deno-compatible, NOT sdk-node) |
| `npm:@opentelemetry/api` | `trace`, `context`, `SpanStatusCode` — OTel trace API |
| `npm:@langfuse/otel` | `LangfuseSpanProcessor` — exports spans to Langfuse |
| `npm:@arizeai/openinference-instrumentation-anthropic` | Auto-instruments Anthropic SDK client instances |

**Compatibility:** Langfuse JS SDK explicitly supports Deno (cookbooks + roadmap). Supabase Edge Runtime supports Deno 2.1+ with `npm:` specifier as of April 2025.

**Deliberate divergence from Python (S5):** The Python `LangfuseNarrativeTracer` in `llm-olly/bridge/` intentionally avoids OTel (due to Langfuse double-counting bug #12306 with the Python SDK). The TypeScript side uses OTel because the JS SDK's `LangfuseSpanProcessor` was designed for it and doesn't have this bug. This is NOT an inconsistency to "fix" — two different SDKs, two correct approaches.

---

## Testing Plan

### Smoke Test
1. Deploy edge function with Langfuse env vars set
2. Generate a single cold email via curl/Postman
3. Check Langfuse UI at `http://localhost:3300` — verify trace appears with:
   - LLM generation span (auto-captured)
   - Input messages, output text, token counts
   - Cache metrics (cache_read_input_tokens, cache_creation_input_tokens)
   - Business context attributes (persona, angle, framework)
   - Validation score

### Pipeline Coverage
4. Generate a full 5-touch sequence — verify 5+ generation spans under one parent trace
5. Trigger a validation retry — verify attempt count score > 1
6. Test v68 pipeline — verify traces appear with correct pipeline_version
7. Test v71 pipeline — verify traces appear with correct pipeline_version

### Cold Start Budget (S1)
8. Measure cold start with Langfuse enabled vs disabled — budget is **< 2 seconds additional latency**. If exceeded, investigate lazy initialization.

### Graceful Degradation
9. Remove `LANGFUSE_PUBLIC_KEY` env var — verify edge function still works, no traces sent, no errors
10. Stop Langfuse Docker stack — verify edge function still works (OTel should buffer/drop, not block)
11. Verify `langfuse_trace_id` is NULL in `narrative_generations` when env vars are not set (S3)

### trace_id Persistence
10. After generation, query `narrative_generations` table — verify `langfuse_trace_id` is populated
11. Verify trace_id in response JSON matches the one in Langfuse UI

---

## Phase 2: Post-Send Scoring (NOT built, documented only)

### Data Flow

```
HubSpot/Instantly
  -> Webhook or scheduled poll
  -> Prefect flow (Python, on server)
  -> Lookup trace_id from narrative_generations
  -> LangfuseNarrativeTracer.score_generation(trace_id, metric, value)
  -> LangfuseNarrativeTracer.log_edit_metrics(trace_id, ai_draft, human_sent)
```

### Engagement Metrics to Score

| Metric | Source | Score Name |
|--------|--------|------------|
| Email opened | HubSpot | `opened` (0/1) |
| Link clicked | HubSpot | `clicked` (0/1) |
| Reply received | HubSpot/Instantly | `replied` (0/1) |
| Bounce | Instantly | `bounced` (0/1) |
| Human edit distance | Compare ai_draft vs human_sent | `editing_effort` (0-1 float) |

### Prerequisites for Phase 2

- HubSpot webhook or polling integration (may already exist in gtm-machine-infrastructure)
- Instantly API integration for bounce/delivery data
- Store ai_draft alongside human_sent for edit distance comparison
- Prefect flow to orchestrate the scoring pipeline

---

## Risks

| Risk | Mitigation |
|------|------------|
| OTel SDK adds latency to edge function cold start | Conditional init — only when env vars set. Measure cold start impact. |
| `npm:` imports fail in Supabase Edge Runtime | Verified compatible per Langfuse docs + Supabase Deno 2.1 support. Test before full rollout. |
| Langfuse server unreachable (server down) | OTel is fire-and-forget async. Spans buffer/drop. Generation continues unaffected. |
| OTel context propagation breaks in Deno async | Fallback: pass trace ID explicitly to orchestrators. |
| Deno isolate freezes before flush completes | Call `forceFlush()` before returning response. |

---

## References

- Langfuse JS SDK Deno cookbook: langfuse.com/docs/sdk/typescript/guide
- Langfuse Anthropic instrumentation: langfuse.com/docs/integrations/anthropic
- Python LangfuseNarrativeTracer: `/Users/you/llm-olly/bridge/langfuse_wrapper.py`
- Narrative Generator: `/Users/you/gtm-machine-infrastructure/supabase/functions/generate-narrative/index.ts`
- llm-olly build order: `/Users/you/llm-olly/CLAUDE.md` (Step 9)
- ADR-001 (split-brain architecture): `/Users/you/infrastructure-monitoring/docs/decisions/ADR-001-split-brain-architecture.md`
