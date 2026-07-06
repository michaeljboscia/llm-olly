# Self-hosting Langfuse for Claude API cold email observability

**Langfuse v3 is the right choice and will handle your workload with ease, but connecting it to Supabase Cloud Postgres requires careful configuration to avoid the most common self-hosting failure: Prisma migration errors.**

---

## 1. Supabase Postgres works, but the schema is the trap

**The public schema is hardcoded.** Langfuse uses Prisma ORM and operates exclusively in the `public` schema — not configurable (GitHub Discussion #5462). If existing tables or extensions exist in `public`, Prisma throws **Error P3005: "The database schema is not empty"**.

**Solution: dedicated, clean database for Langfuse** — never share a database with existing objects.

**Connection pooling requires dual connection strings.** Prisma Migrate cannot run through any connection pooler — fails with `"prepared statement 's0' already exists"`.

---

## 2. Deploy v3 — the only version that matters

Latest: **v3.160.0**. v2 is obsolete. v3 requires: ClickHouse, Redis, MinIO, separate worker container.

Key v3 features:
- Arbitrary usage type tracking (`cache_read_input_tokens`, `cache_creation_input_tokens`)
- Built-in LLM-as-a-Judge evaluators
- Annotation queues for human review
- Prompt composability
- Custom dashboards
- Context-dependent pricing tiers

---

## 3. Anthropic SDK instrumentation

**Manual instrumentation with `@observe` decorator is safest** to avoid OTel double-counting bug (GitHub issue #12306).

```python
from langfuse import observe, get_client
import anthropic

langfuse = get_client()
anthropic_client = anthropic.Anthropic()

@observe(as_type="generation")
def generate_cold_email(persona_id: str, prospect_data: dict, hubspot_contact_id: str):
    base = langfuse.get_prompt("base-cold-email")
    persona = langfuse.get_prompt(f"persona-{persona_id}")

    system_prompt = base.compile(persona_instructions=persona.prompt, company_name=prospect_data["company"])
    messages = [{"role": "user", "content": prospect_data["context"]}]

    langfuse.update_current_generation(
        model="claude-sonnet-4-20250514",
        input=messages,
        metadata={"hubspot_contact_id": hubspot_contact_id, "persona_id": persona_id}
    )

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
    )

    langfuse.update_current_generation(
        output=response.content[0].text,
        usage_details={
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
            "cache_read_input_tokens": response.usage.cache_read_input_tokens or 0,
            "cache_creation_input_tokens": response.usage.cache_creation_input_tokens or 0,
        },
    )
    return response.content[0].text
```

---

## 4. Prompt management: 23 Langfuse prompts

One `base-cold-email` + 22 `persona-*` prompts. Independently versioned with label-based deployment (`production`, `staging`).

```python
base = langfuse.get_prompt("base-cold-email")
persona = langfuse.get_prompt(f"persona-{persona_id}")
final_prompt = base.compile(persona_instructions=persona.prompt, **other_vars)
```

Benefits: non-engineering iteration, per-persona performance dashboards, client-side caching with TTL.

---

## 5. Evaluation strategy

- **Langfuse LLM-as-Judge** for production (auto-evaluate every generation)
- **Langfuse Annotation Queues** for human review
- **Promptfoo** for pre-deployment red teaming only
- Promptfoo cannot push results back to Langfuse (GitHub Discussion #3375) — requires custom bridge script

---

## 6. Three failure modes

1. **Prisma migration failures**: Failed row in `_prisma_migrations` blocks all subsequent migrations. Pin to specific version tags, not `:latest`.
2. **Memory**: Set `NODE_OPTIONS=--max-old-space-size=2048`. Budget 2GB for ClickHouse.
3. **Operational**: All components must run in UTC. `ENCRYPTION_KEY` must be 64 hex chars. Enable data retention policies.

---

## 7. Cache token tracking

`usageDetails` object supports `cache_read_input_tokens` and `cache_creation_input_tokens` as first-class citizens. No built-in cache hit rate dashboard — compute manually via API queries grouped by persona tags.

---

## 8. Docker Compose configuration

See `langfuse/docker-compose.yml` in this repo for the production-ready config adapted for server port conflicts.
