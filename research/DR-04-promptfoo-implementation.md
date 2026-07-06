# Promptfoo implementation guide for modular cold email evaluation

**A single YAML config with a JavaScript prompt function, array variable expansion, and layered assertions is the optimal architecture for evaluating 22 persona variants in Promptfoo.** This approach avoids config duplication, auto-generates the full test matrix (22 personas x N payloads), and supports deterministic checks alongside Claude-as-judge scoring—all in one eval run.

---

## 1. One config with a prompt function scales best

The critical architectural decision is whether to create one config with 22 prompts, 22 separate configs, or a single prompt function approach. **The prompt function approach wins decisively** for this use case.

When `vars` values are **arrays**, Promptfoo automatically expands them into a cartesian product. Listing 22 personas as an array with N payloads generates 22xN test cases without manual repetition. A JavaScript prompt function dynamically assembles the base prompt + persona module at runtime, eliminating 22 duplicate prompt files.

**Recommended directory structure:**
```
cold-email-eval/
├── promptfooconfig.yaml       # Single config
├── generate_prompt.js          # Dynamic prompt assembly
├── prompts/base_system.txt     # Shared base prompt
├── personas/                   # 22 persona JSON modules
│   ├── c_suite.json
│   ├── vp_sales.json
│   └── ...
├── payloads/                   # Test JSON payloads
│   ├── saas_startup.json
│   └── enterprise_fintech.json
└── assertions/                 # Reusable assertion scripts
    ├── validate_sequence.js
    └── no_hallucinated_numbers.js
```

**Core promptfooconfig.yaml:**
```yaml
# yaml-language-server: $schema=https://promptfoo.dev/config-schema.json
description: "Cold Email Persona Evaluation - 22 Personas"

prompts:
  - file://generate_prompt.js

providers:
  - id: anthropic:messages:claude-sonnet-4-20250514
    label: "Claude Sonnet 4"
    config:
      temperature: 0.7
      max_tokens: 4096

defaultTest:
  assert:
    - type: is-json
    - type: javascript
      value: file://assertions/validate_sequence.js
    - type: cost
      threshold: 0.01

tests:
  - vars:
      persona:
        - c_suite
        - cto
        - vp_sales
        - vp_marketing
        - vp_engineering
        - cfo
        - coo
        - cio
        - ciso
        - hr_director
        - product_director
        - ops_director
        - it_director
        - procurement_director
        - customer_success_director
        - sales_director
        - marketing_director
        - engineering_manager
        - data_director
        - growth_director
        - partnerships_director
        - innovation_director
      payload: file://payloads/saas_startup.json
  - vars:
      persona:
        - c_suite
        - cto
        - cfo
        - vp_sales
        - vp_engineering
      payload: file://payloads/enterprise_fintech.json

commandLineOptions:
  maxConcurrency: 5
```

---

## 2. Five assertion patterns for cold email validation

### (a) Word count per email: built-in `word-count`

Promptfoo has a **native `word-count` assertion** supporting min/max ranges:

```yaml
assert:
  - type: word-count
    value:
      min: 25
      max: 50
```

### (b) Banned CTA phrases: `not-icontains-any`

```yaml
assert:
  - type: not-icontains-any
    value:
      - 'click here'
      - 'schedule a call'
      - 'book a meeting'
      - 'sign up now'
      - 'buy now'
      - 'act now'
      - 'limited time offer'
      - 'free trial'
```

### (c) No hallucinated numbers: custom JavaScript assertion

```javascript
// assertions/no_hallucinated_numbers.js
module.exports = (output, context) => {
  function extractNumbers(text) {
    const matches = text.match(/\d+\.?\d*/g);
    return matches ? [...new Set(matches.map(Number))] : [];
  }
  const inputPayload = typeof context.vars.payload === 'string'
    ? context.vars.payload : JSON.stringify(context.vars.payload);
  const inputNumbers = extractNumbers(inputPayload);
  const outputNumbers = extractNumbers(output);
  const hallucinated = outputNumbers.filter(n => !inputNumbers.includes(n));
  if (hallucinated.length === 0) {
    return { pass: true, score: 1, reason: `All ${outputNumbers.length} numbers trace to input` };
  }
  return { pass: false, score: 0, reason: `Hallucinated numbers: ${hallucinated.join(', ')}` };
};
```

### (d) Subject line format: JavaScript validation

```yaml
assert:
  - type: javascript
    value: |
      const emails = JSON.parse(output).emails;
      const pattern = /^[a-z]+( [a-z]+){0,2}$/;
      for (let i = 0; i < emails.length; i++) {
        if (!pattern.test(emails[i].subject)) {
          return { pass: false, score: 0,
            reason: `Email ${i+1} subject "${emails[i].subject}" violates format rules` };
        }
      }
      return { pass: true, score: 1, reason: 'All subjects valid' };
```

### (e) E2 contains no new signal data beyond E1: combined approach

```yaml
assert:
  - type: javascript
    value: |
      const emails = JSON.parse(output).emails;
      const e1 = JSON.stringify(emails[0]);
      const e2 = JSON.stringify(emails[1]);
      const extractNums = t => (t.match(/\d+\.?\d*/g) || []).map(Number);
      const e1Nums = extractNums(e1);
      const newNums = extractNums(e2).filter(n => !e1Nums.includes(n));
      if (newNums.length > 0) {
        return { pass: false, score: 0, reason: `E2 introduces new numbers: ${newNums.join(', ')}` };
      }
      return { pass: true, score: 1, reason: 'E2 has no new numbers beyond E1' };

  - type: llm-rubric
    value: |
      The output is a JSON array of emails. Compare Email 2 to Email 1.
      RULE: Email 2 must NOT introduce any new signal data, statistics, or factual
      assertions not already present in Email 1.
      Score 1.0 if compliant, 0.0 if not.
    threshold: 0.9
```

---

## 3. Claude works natively as an LLM judge

```yaml
defaultTest:
  options:
    provider:
      id: anthropic:messages:claude-sonnet-4-20250514
      config:
        temperature: 0.0
        max_tokens: 1024
  assert:
    - type: llm-rubric
      value: |
        Evaluate PERSONA FIDELITY: Does this cold email sequence consistently
        reflect the priorities, vocabulary, and concerns of a {{persona}} buyer?
      weight: 3
      metric: persona_fidelity
      threshold: 0.7

    - type: llm-rubric
      value: |
        Evaluate TONE MATCH: appropriate formality, jargon, communication style.
      weight: 2
      metric: tone_match
      threshold: 0.7

    - type: llm-rubric
      value: |
        Check ANTI-PATTERN COMPLIANCE. No fake urgency, superlatives,
        presumptuous familiarity, generic value props, or multiple CTAs per email.
      weight: 3
      metric: anti_pattern_compliance
      threshold: 1.0

derivedMetrics:
  - name: 'overall_quality'
    value: '(persona_fidelity * 0.4 + tone_match * 0.3 + anti_pattern_compliance * 0.3)'
```

---

## 4. CI/CD integration

**GitHub Actions:**
```yaml
name: 'Prompt Evaluation'
on:
  pull_request:
    paths: ['prompts/**', 'personas/**']
jobs:
  evaluate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: promptfoo/promptfoo-action@v1
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          config: 'promptfooconfig.yaml'
```

**Prefect integration:**
```python
from prefect import flow, task
import subprocess, json, os

@task(name="run-promptfoo-eval")
def run_eval(config_path: str, output_path: str = "results.json"):
    result = subprocess.run(
        ["npx", "promptfoo@latest", "eval",
         "-c", config_path, "-o", output_path,
         "--no-progress-bar", "--no-table"],
        capture_output=True, text=True, env={**os.environ}
    )
    if result.returncode not in (0, 100):
        raise RuntimeError(f"promptfoo failed: {result.stderr}")
    with open(output_path) as f:
        return json.load(f)

@task(name="check-pass-rate")
def check_results(results: dict, threshold: float = 0.8):
    stats = results.get("results", {}).get("stats", {})
    total = stats.get("successes", 0) + stats.get("failures", 0)
    pass_rate = stats["successes"] / total if total > 0 else 0
    if pass_rate < threshold:
        raise ValueError(f"Pass rate {pass_rate:.0%} below {threshold:.0%}")
    return pass_rate

@flow(name="llm-eval-pipeline")
def eval_pipeline(config: str = "promptfooconfig.yaml"):
    results = run_eval(config)
    return check_results(results)
```

---

## 5. Golden datasets

```yaml
assert:
  - type: similar
    value:
      - file://golden/c_suite_exemplar_01.txt
      - file://golden/c_suite_exemplar_02.txt
      - file://golden/c_suite_exemplar_03.txt
    threshold: 0.75
```

Supported metrics: `similar` (cosine), `similar:dot`, `similar:euclidean`, `rouge-n`, `bleu`, `levenshtein`, `factuality`, `answer-relevance`.

---

## 6. Key gotchas

- **Caching**: 14-day TTL at `~/.promptfoo/cache`. Never use `--no-cache` unless prompts changed.
- **Rate limiting**: AIMD adaptive scheduler. Start `maxConcurrency: 5`.
- **Grading provider**: Must explicitly set `defaultTest.options.provider` or `llm-rubric` assertions fail silently.
- **`contains` is case-sensitive** — use `icontains` for case-insensitive.
- **Cost assertions**: `- type: cost` with `threshold: 0.01` gates per-call spending.
- **Promptfoo acquired by OpenAI March 2026** — still MIT, monitor Anthropic support.

---

## 7. Langfuse bridge (Promptfoo -> Langfuse)

No native export. Custom bridge script required:

```python
import json
from langfuse import Langfuse

langfuse = Langfuse()

with open("results.json") as f:
    data = json.load(f)

for output in data["results"]["outputs"]:
    trace = langfuse.trace(
        name=f"promptfoo-eval-{data.get('timestamp', '')}",
        input=output.get("prompt", {}).get("raw", ""),
        output=output.get("text", ""),
        metadata={
            "provider": output.get("provider", ""),
            "vars": output.get("vars", {}),
        }
    )
    for i, grading in enumerate(output.get("gradingResults", [])):
        trace.score(
            name=grading.get("assertion", {}).get("type", f"assertion-{i}"),
            value=grading.get("score", 0),
            comment=grading.get("reason", ""),
        )
langfuse.flush()
```
