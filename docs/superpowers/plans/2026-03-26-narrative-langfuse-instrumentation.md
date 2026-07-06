# Narrative Generator Langfuse Instrumentation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Working Directory:** /Users/you/gtm-machine-infrastructure
**Git Branch:** feature/langfuse-instrumentation (create from main)
**Session Log:** (set at execution time)
**Spec:** /Users/you/llm-olly/docs/superpowers/specs/2026-03-26-narrative-generator-langfuse-instrumentation.md

**Goal:** Add Langfuse tracing to the Narrative Generator edge function so every LLM call is observable with business context, validation scores, and a trace_id for future engagement correlation.

**Architecture:** OTel auto-instrumentation via `AnthropicInstrumentation` patches the Anthropic client instance. A parent span wraps each request with business metadata. `SimpleSpanProcessor` exports spans to Langfuse on server. Graceful no-op when env vars unset.

**Tech Stack:** Deno (Supabase Edge Functions), TypeScript, OTel (`sdk-trace-base`, NOT `sdk-node`), Langfuse JS SDK (`@langfuse/otel`), `@arizeai/openinference-instrumentation-anthropic`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `supabase/functions/generate-narrative/telemetry.ts` | **Create** | OTel init, Langfuse processor, Anthropic client factory |
| `supabase/functions/generate-narrative/index.ts` | **Modify** | Import telemetry, replace Anthropic client init, wrap handler in parent span, add trace_id to response + Supabase insert |
| `supabase/functions/generate-narrative/v71/persistence.ts` | **Modify** | Accept + persist `langfuse_trace_id` in v71 insert path |
| `supabase/functions/generate-narrative/v71/orchestrator.ts` | **Modify** | Accept + pass `langfuse_trace_id` to persistence |

---

## Wave 0: Contracts & Schema

<task id="T-001" req="REQ-004" wave="0" depends="">
  <description>Add langfuse_trace_id column to narrative_generations</description>
  <files>Supabase migration (SQL)</files>
  <contract>ALTER TABLE narrative_generations ADD COLUMN langfuse_trace_id TEXT;</contract>
  <verify>SELECT column_name FROM information_schema.columns WHERE table_name = 'narrative_generations' AND column_name = 'langfuse_trace_id';</verify>
</task>

### Task 1: Add langfuse_trace_id column to narrative_generations

**Files:**
- Supabase SQL migration (run via dashboard or `supabase db push`)

- [ ] **Step 1: Run the migration**

```sql
ALTER TABLE narrative_generations ADD COLUMN langfuse_trace_id TEXT;
COMMENT ON COLUMN narrative_generations.langfuse_trace_id IS 'OTel trace ID from Langfuse instrumentation. Correlation key for Phase 2 post-send scoring.';
```

Run via: `supabase db push` or Supabase SQL Editor

- [ ] **Step 2: Verify column exists**

```sql
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'narrative_generations' AND column_name = 'langfuse_trace_id';
```

Expected: 1 row, `text`, `YES`

- [ ] **Step 3: Commit migration**

```bash
git add -A && git commit -m "schema: add langfuse_trace_id to narrative_generations"
```

---

## Wave 1: Telemetry Module (No Dependencies on Existing Code)

<task id="T-002" req="REQ-001,REQ-006" wave="1" depends="T-001">
  <description>Create telemetry.ts — OTel init + instrumented Anthropic client factory</description>
  <files>supabase/functions/generate-narrative/telemetry.ts</files>
  <contract>export { createInstrumentedClient, langfuseProcessor, langfuseEnabled, tracer, SpanStatusCode }</contract>
  <verify>deno check supabase/functions/generate-narrative/telemetry.ts</verify>
</task>

### Task 2: Create telemetry.ts

**Files:**
- Create: `supabase/functions/generate-narrative/telemetry.ts`

- [ ] **Step 1: Write telemetry.ts**

```typescript
/**
 * Telemetry — OTel + Langfuse init for Narrative Generator
 *
 * MUST be imported before any Anthropic client is created.
 * Uses BasicTracerProvider (NOT NodeSDK — incompatible with Deno).
 * Uses SimpleSpanProcessor (NOT batched — edge functions are short-lived).
 *
 * Graceful no-op when LANGFUSE_PUBLIC_KEY is not set.
 */

import { BasicTracerProvider, SimpleSpanProcessor as OTelSimpleSpanProcessor } from "npm:@opentelemetry/sdk-trace-base";
import { LangfuseSpanProcessor } from "npm:@langfuse/otel";
import { AnthropicInstrumentation } from "npm:@arizeai/openinference-instrumentation-anthropic";
import Anthropic from "npm:@anthropic-ai/sdk";
import { trace, SpanStatusCode } from "npm:@opentelemetry/api";

const langfuseEnabled = !!Deno.env.get("LANGFUSE_PUBLIC_KEY");

let langfuseProcessor: LangfuseSpanProcessor | null = null;

if (langfuseEnabled) {
  langfuseProcessor = new LangfuseSpanProcessor({
    publicKey: Deno.env.get("LANGFUSE_PUBLIC_KEY")!,
    secretKey: Deno.env.get("LANGFUSE_SECRET_KEY")!,
    baseUrl: Deno.env.get("LANGFUSE_HOST") ?? "http://localhost:3300",
  });

  const provider = new BasicTracerProvider();
  provider.addSpanProcessor(langfuseProcessor);
  provider.register();
}

const tracer = trace.getTracer("generate-narrative");

/**
 * Create an Anthropic client with Langfuse auto-instrumentation.
 * When LANGFUSE_PUBLIC_KEY is not set, returns a plain client.
 */
export function createInstrumentedClient(apiKey: string): Anthropic {
  const client = new Anthropic({ apiKey });
  if (langfuseEnabled) {
    const instrumentation = new AnthropicInstrumentation();
    instrumentation.manuallyInstrument(client);
  }
  return client;
}

export { langfuseProcessor, langfuseEnabled, tracer, SpanStatusCode };
```

- [ ] **Step 2: Verify it type-checks**

```bash
cd /Users/you/gtm-machine-infrastructure
deno check supabase/functions/generate-narrative/telemetry.ts
```

Expected: No errors. If `npm:` imports fail, check Deno version (`deno --version` should be 2.1+).

- [ ] **Step 3: Commit**

```bash
git add supabase/functions/generate-narrative/telemetry.ts
git commit -m "feat: add telemetry.ts — OTel + Langfuse init for Deno edge function"
```

---

## Wave 2: Wire Telemetry into index.ts

<task id="T-003" req="REQ-001,REQ-002,REQ-003,REQ-004,REQ-006" wave="2" depends="T-002">
  <description>Modify index.ts — import telemetry, replace Anthropic client, wrap handler, add trace_id</description>
  <files>supabase/functions/generate-narrative/index.ts:1-4111</files>
  <contract>Parent span per request, auto-instrumented LLM calls, trace_id in response + Supabase insert</contract>
  <verify>supabase functions serve generate-narrative --debug; curl test</verify>
</task>

### Task 3: Replace Anthropic client initialization

**Files:**
- Modify: `supabase/functions/generate-narrative/index.ts:33-41`

- [ ] **Step 1: Add telemetry import (BEFORE Anthropic import)**

At line 33 of `index.ts`, add the telemetry import BEFORE the existing Anthropic import:

```typescript
// Line 33 — ADD THIS FIRST (must be before Anthropic import):
import { createInstrumentedClient, langfuseProcessor, langfuseEnabled, tracer, SpanStatusCode } from "./telemetry.ts";
```

- [ ] **Step 2: Replace module-level Anthropic client**

Change line 40 from:
```typescript
const anthropic = new Anthropic({ apiKey: anthropicApiKey });
```

To:
```typescript
const anthropic = createInstrumentedClient(anthropicApiKey!);
```

- [ ] **Step 3: Verify type-check**

```bash
deno check supabase/functions/generate-narrative/index.ts
```

- [ ] **Step 4: Commit**

```bash
git add supabase/functions/generate-narrative/index.ts
git commit -m "feat: replace Anthropic client with instrumented client from telemetry.ts"
```

### Task 4: Add parent span to v67 handler path

**Files:**
- Modify: `supabase/functions/generate-narrative/index.ts:3261-3290` (handler setup)
- Modify: `supabase/functions/generate-narrative/index.ts:3995-4022` (Supabase insert)
- Modify: `supabase/functions/generate-narrative/index.ts:4074-4110` (return statement)

This task wraps the ENTIRE request handler in a parent OTel span. Auto-instrumented Anthropic calls nest underneath automatically.

- [ ] **Step 1: Add traceId variable and parent span start**

After the request body is parsed and validated (after line 3288), add span creation. The span wraps the full handler. Add these attributes immediately (known from request body):

```typescript
// After line 3288 (after validation of domain/prompt_type/seniority):
const persona_id = `${department || "unknown"}_${seniority}`;

return await tracer.startActiveSpan("generate-narrative", async (span) => {
  try {
    span.setAttributes({
      "narrative.persona_id": persona_id,
      "narrative.domain": domain,
      "narrative.prompt_type": prompt_type,
      "narrative.pipeline_version": requestBody.pipeline_version || "v67",
      "narrative.model": model,
      "narrative.seniority": seniority,
      "narrative.department": department || "",
      "narrative.contact_role": contact_role || "",
    });
```

**NOTE:** The v68/v71 branch checks (lines 3296, 3334) are INSIDE the span — their LLM calls will auto-nest under this parent.

- [ ] **Step 2: Add post-generation attributes (angle, framework, scores)**

After the generation logic completes and before the Supabase insert (~line 3993), add:

```typescript
    // After parsedOutput = normalizeContent(parsedOutput); (~line 3993)
    span.setAttributes({
      "narrative.primary_angle": effectiveAngle,
      "narrative.secondary_angle": effectiveSecondaryAngle || "",
      "narrative.framework": selectedFramework,
    });

    span.addEvent("score", {
      "langfuse.score.name": "validation_passed",
      "langfuse.score.value": isFullSequence
        ? (sequenceValidation?.valid ? 1.0 : 0.0)
        : (validationResult?.valid ? 1.0 : 0.0),
    });
    span.addEvent("score", {
      "langfuse.score.name": "generation_attempts",
      "langfuse.score.value": attempts,
    });

    const traceId = langfuseEnabled ? span.spanContext().traceId : undefined;
```

- [ ] **Step 3: Add trace_id to narrative_generations insert**

In the Supabase insert at line 3995, add after `parameters_used`:

```typescript
      langfuse_trace_id: traceId ?? null,
```

- [ ] **Step 4: Add trace_id to response JSON**

In the return object (~line 4074), add:

```typescript
    trace_id: traceId ?? null,
```

- [ ] **Step 5: Add span end, flush, and error handling**

Wrap the closing of the return in span lifecycle:

```typescript
    span.setStatus({ code: SpanStatusCode.OK });
    if (langfuseEnabled && langfuseProcessor) {
      await langfuseProcessor.forceFlush();
    }
    span.end();

    return result; // the existing return object
  } catch (error) {
    span.setStatus({ code: SpanStatusCode.ERROR, message: String(error) });
    span.end();
    if (langfuseEnabled && langfuseProcessor) {
      await langfuseProcessor.forceFlush();
    }
    throw error;
  }
}); // end tracer.startActiveSpan
```

- [ ] **Step 6: Verify type-check**

```bash
deno check supabase/functions/generate-narrative/index.ts
```

- [ ] **Step 7: Commit**

```bash
git add supabase/functions/generate-narrative/index.ts
git commit -m "feat: wrap handler in OTel parent span with business context + scores"
```

### Task 5: Add flush to v71 streaming path

**Files:**
- Modify: `supabase/functions/generate-narrative/index.ts:3334-3369`

- [ ] **Step 1: Add forceFlush inside the v71 stream completion**

At line 3356 (after `controller.enqueue(encoder.encode(JSON.stringify(result)));` and before `controller.close();`):

```typescript
          // Flush telemetry before stream closes (isolate may freeze after)
          if (langfuseEnabled && langfuseProcessor) {
            await langfuseProcessor.forceFlush();
          }
```

Also add the same in the error path at line 3364 (before the error `controller.close()`).

- [ ] **Step 2: Add trace_id to v71 result**

The v71 result is serialized at line 3355. The `orchestrateV71` function returns the result — we need the traceId from the active span. Add before the JSON.stringify:

```typescript
          const traceId = langfuseEnabled ? trace.getActiveSpan()?.spanContext().traceId : undefined;
          const v71WithTrace = { ...result, trace_id: traceId ?? null };
          controller.enqueue(encoder.encode(JSON.stringify(v71WithTrace)));
```

(Replace the existing line 3355.)

- [ ] **Step 3: Commit**

```bash
git add supabase/functions/generate-narrative/index.ts
git commit -m "feat: add telemetry flush to v71 streaming path + trace_id in response"
```

---

## Wave 3: v71 Persistence (Depends on Wave 2)

<task id="T-004" req="REQ-004" wave="3" depends="T-003">
  <description>Pass langfuse_trace_id through v71 persistence path</description>
  <files>supabase/functions/generate-narrative/v71/persistence.ts, v71/orchestrator.ts</files>
  <contract>PersistParams includes langfuse_trace_id; written to narrative_generations</contract>
  <verify>deno check; v71 test generates row with trace_id</verify>
</task>

### Task 6: Add trace_id to v71 persistence

**Files:**
- Modify: `supabase/functions/generate-narrative/v71/persistence.ts:13-24,47-74`
- Modify: `supabase/functions/generate-narrative/v71/orchestrator.ts` (pass trace_id to persist)

- [ ] **Step 1: Add langfuse_trace_id to PersistParams interface**

In `v71/persistence.ts`, add to the `PersistParams` interface (after line 23):

```typescript
  langfuse_trace_id?: string | null;
```

- [ ] **Step 2: Add langfuse_trace_id to the insert**

In the `narrative_generations` insert loop (line 47-74), add inside the insert object:

```typescript
        langfuse_trace_id: params.langfuse_trace_id ?? null,
```

- [ ] **Step 3: Pass trace_id from v71 orchestrator to persist**

In `v71/orchestrator.ts`, where `persistV71Results` is called, add the trace_id. The active span's traceId is available via `trace.getActiveSpan()`:

```typescript
import { trace } from "npm:@opentelemetry/api";

// In the persistV71Results call:
const traceId = trace.getActiveSpan()?.spanContext().traceId;

await persistV71Results({
  // ... existing params ...
  langfuse_trace_id: traceId ?? null,
});
```

- [ ] **Step 4: Verify type-check**

```bash
deno check supabase/functions/generate-narrative/v71/persistence.ts
deno check supabase/functions/generate-narrative/v71/orchestrator.ts
```

- [ ] **Step 5: Commit**

```bash
git add supabase/functions/generate-narrative/v71/persistence.ts supabase/functions/generate-narrative/v71/orchestrator.ts
git commit -m "feat: persist langfuse_trace_id in v71 pipeline path"
```

### Task 7: Add Phase 2 contract stub

**Files:**
- Modify: `supabase/functions/generate-narrative/index.ts` (after the v67 insert, ~line 4022)

- [ ] **Step 1: Add the Phase 2 stub comment**

After the `narrative_generations` insert block (after line 4022):

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

- [ ] **Step 2: Commit**

```bash
git add supabase/functions/generate-narrative/index.ts
git commit -m "docs: add Phase 2 post-send scoring contract stub"
```

---

## Wave 4: Smoke Test + Verification

<task id="T-005" req="REQ-001,REQ-002,REQ-003,REQ-004,REQ-006" wave="4" depends="T-003,T-004">
  <description>End-to-end verification — deploy, test, verify traces in Langfuse</description>
  <files>None (testing only)</files>
  <contract>All success criteria verified</contract>
  <verify>Langfuse UI shows traces; narrative_generations has trace_id; no-op when env unset</verify>
</task>

### Task 8: Set Langfuse env vars

- [ ] **Step 1: Get Langfuse API keys**

Open Langfuse UI at `http://localhost:3300`, create a project (or use existing), copy the public + secret keys.

- [ ] **Step 2: Set edge function secrets**

```bash
supabase secrets set LANGFUSE_PUBLIC_KEY=pk-lf-...
supabase secrets set LANGFUSE_SECRET_KEY=sk-lf-...
supabase secrets set LANGFUSE_HOST=http://localhost:3300
```

### Task 9: Smoke test — single cold email (v67)

- [ ] **Step 1: Serve locally**

```bash
supabase functions serve generate-narrative --debug
```

- [ ] **Step 2: Send test request**

```bash
curl -X POST http://localhost:54321/functions/v1/generate-narrative \
  -H "Authorization: Bearer $(supabase secrets list | grep SERVICE_ROLE)" \
  -H "Content-Type: application/json" \
  -d '{"domain":"test-domain.example.com","prompt_type":"cold_email","seniority":"vp","department":"ecommerce","debug":true}'
```

- [ ] **Step 3: Verify response includes trace_id**

Response JSON should have `"trace_id": "<32-char hex string>"` (or `null` if env vars not set).

- [ ] **Step 4: Verify trace in Langfuse UI**

Open `http://localhost:3300` → Traces. Look for a trace named `generate-narrative` with:
- Auto-captured generation span (input messages, output, tokens, cache metrics)
- Attributes: `narrative.persona_id`, `narrative.domain`, `narrative.primary_angle`, etc.
- Score events: `validation_passed`, `generation_attempts`

- [ ] **Step 5: Verify Supabase persistence**

```sql
SELECT langfuse_trace_id FROM narrative_generations
WHERE domain = 'test-domain.example.com'
ORDER BY created_at DESC LIMIT 1;
```

Expected: matches the trace_id from the response.

### Task 10: Test v71 pipeline

- [ ] **Step 1: Send v71 test request**

```bash
curl -X POST http://localhost:54321/functions/v1/generate-narrative \
  -H "Authorization: Bearer ..." \
  -H "Content-Type: application/json" \
  -d '{"domain":"test-domain.example.com","prompt_type":"cold_email","seniority":"vp","department":"ecommerce","pipeline_version":"v71","debug":true}'
```

- [ ] **Step 2: Verify v71 traces appear**

Check Langfuse UI. If traces are missing for v71 LLM calls (C3 — `beta.messages.create()` escape), note this as a known gap. Phase 2 fix: add manual spans in `v71/email-generator.ts`.

- [ ] **Step 3: Verify v71 persistence has trace_id**

```sql
SELECT langfuse_trace_id, pipeline_version FROM narrative_generations
WHERE domain = 'test-domain.example.com' AND pipeline_version = 'v71'
ORDER BY created_at DESC LIMIT 5;
```

### Task 11: Test graceful degradation

- [ ] **Step 1: Remove Langfuse env vars**

```bash
supabase secrets unset LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY LANGFUSE_HOST
```

- [ ] **Step 2: Send same test request**

Verify: edge function works normally, `trace_id` is `null` in response, `langfuse_trace_id` is `NULL` in Supabase, no errors in logs.

- [ ] **Step 3: Restore env vars and commit any fixes**

```bash
supabase secrets set LANGFUSE_PUBLIC_KEY=pk-lf-... # etc.
git add -A && git commit -m "test: verify Langfuse instrumentation end-to-end"
```

### Task 12: Measure cold start impact

- [ ] **Step 1: Cold start with Langfuse enabled**

Restart the function (`supabase functions serve` restart) and measure first-request latency.

- [ ] **Step 2: Cold start without Langfuse**

Remove env vars, restart, measure first-request latency.

- [ ] **Step 3: Compare**

Budget: < 2 seconds additional. If exceeded, investigate lazy initialization of OTel provider.

---

## Requirements Traceability

| REQ | Description | Tasks |
|-----|-------------|-------|
| REQ-001 | Every messages.create() auto-traced | T-002, T-003 |
| REQ-002 | Business context in traces | T-003 (Task 4) |
| REQ-003 | Validation scores attached | T-003 (Task 4) |
| REQ-004 | trace_id in response + Supabase | T-001, T-003 (Task 4), T-004 |
| REQ-005 | Phase 2 contract documented | T-003 (Task 7) |
| REQ-006 | Graceful no-op | T-002, T-003 (Task 11) |
