# 👁️ llm-olly

**A self-hostable, full-stack observability + evaluation rig for LLM applications.** Because "it looked fine when I tested it" is not a monitoring strategy.

`olly` = observability. The extra `l` is for the second L in LLM. We don't make the rules.

---

## The problem

Your LLM feature works. Probably. You changed the prompt last Tuesday and it *feels* better. Cost is... some number? Quality is... vibes? And when the model provider silently ships a new version, you'll find out from an angry user, not a dashboard.

That's a distributed-systems problem disguised as a feelings problem. llm-olly wires up the three things you actually need — **tracing, pre-deploy evaluation, and drift detection** — into one Docker-deployable stack you own end to end. No per-token SaaS pricing, no shipping your prompts to someone else's cloud.

## The three pillars

```
        Your LLM app
             │
   ┌─────────┼─────────────────────────────┐
   │         │                             │
 TRACE     EVAL (pre-deploy)          MONITOR (ongoing)
 Langfuse   Promptfoo                  Prefect
   │         │                             │
 every      22 personas × N payloads    daily canary heartbeat
 call:      deterministic assertions    weekly drift report
 cost,      + LLM-as-judge rubrics      model-change → full re-test
 latency,   + golden-set similarity
 tokens,
 cache,
 judge
 scores
```

| Directory | What's inside |
|---|---|
| `langfuse/` | Docker Compose + env template + deploy docs for **Langfuse v3** (traces every call: tokens, cost, latency, prompt-version tracking, cache hit/miss, LLM-as-judge auto-eval, human approval scores) |
| `promptfoo/` | Docker Compose + eval configs + assertion scripts — a **persona × payload test matrix** with deterministic checks (word count, banned CTAs, data accuracy) *and* model-as-judge rubrics (persona fidelity, tone, anti-patterns) |
| `canary/` | 44–66 canary fixtures + scoring rubrics — the smoke test you run every day so drift can't sneak up on you |
| `bridge/` | Promptfoo → Langfuse bridge, so eval results land in the same place as production traces |
| `prefect/` | Orchestration flows: daily heartbeat, weekly drift report, and a **model-change trigger** that fires the full canary suite when your provider moves the ground under you |
| `schema/` | Supabase/Postgres migrations for the monitoring tables (`canary_runs`, `canary_results`, `experiments`, `prompt_versions`, …) |

A healthy pipeline stays healthy right up until something quietly regresses while you're not looking. This is how you look.

## The best trick in here (and it's free)

From the research baked into this repo (`research/DR-04`): **edit-distance tracking is the highest signal-per-engineering-hour metric you can add, at zero API cost.** Track how much a human edits the model's output before shipping it. Edits go up → quality went down. No judge model, no extra tokens, no dashboard subscription. Just diff and count. The cheapest instrument on the rack is also one of the sharpest.

## What you get out of it

- **Before deploy:** "Did my prompt change make things better, or just different?" — answered by Promptfoo, not by feel.
- **In production:** every call traced, costed, and scored in Langfuse.
- **Over time:** drift reports and a canary that screams the day the model's behavior shifts under you.

## Stack

Langfuse v3 (Postgres + ClickHouse + Redis + MinIO) · Promptfoo · Prefect · Supabase/Postgres · all Docker Compose, all yours.

> Deployment defaults point at `localhost` — set your own hosts/ports in the env templates. See `CLAUDE.md` for the full architecture and `langfuse/`, `promptfoo/` for the compose files.

---

*Observability: knowing your LLM is wrong before your users do.*
