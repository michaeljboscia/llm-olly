# Drift detection and canary testing for persona-based email generation on Claude

**Silent quality degradation is the dominant failure mode for LLM production systems, and traditional monitoring (HTTP 200, normal latency) is completely blind to it.** A 2023 Stanford/Berkeley study documented GPT-4's prime number identification accuracy dropping from 97.6% to 2.4% between API snapshots — without any alert triggering. For a 22-persona cold email system pinned to Claude API versions, the risk is not that the system breaks loudly but that persona fidelity erodes quietly over weeks until reply rates crater. What follows is a practical implementation guide built around Prefect, FastAPI, Supabase, and OSS tooling — no SaaS eval platforms required.

---

## 1. Canary sets are versioned contracts, not just test data

The industry has converged on the "golden set" pattern: a fixed collection of known-good inputs run through your pipeline on a schedule, with outputs graded against explicit criteria. The critical insight from production teams is that each canary case is a **contract** — input payload plus scoring rubric plus assertions — not merely an example input.

For a 22-persona email system with only ~26 hand-written exemplars, the bootstrapping problem is real. The recommended approach uses a three-tier coverage strategy. First, create **one canary case per persona** (22 total) using characteristic-based assertions rather than reference text comparison — this works even for personas without exemplars. Second, for the ~5 personas with hand-written exemplars, add 2–3 cases each with varied prospect industries and signals, enabling embedding similarity scoring against reference outputs. Third, add edge cases: prospects with minimal signal data, uncommon industries, very short company descriptions. The target is **44–66 total canary cases**, which falls within the 50–200 range that research indicates is "small enough to run on every change but large enough to detect regressions with statistical confidence."

Each test fixture should capture the full generation context as a structured record:

```json
{
  "case_id": "persona-cfo-fintech-001",
  "persona_module": "cfo_strategic",
  "prospect_input": {
    "company_name": "Acme Payments",
    "domain": "acmepayments.com",
    "industry": "fintech",
    "employee_count": 250,
    "recent_signal": "Series B funding announced",
    "contact_title": "CFO"
  },
  "assertions": {
    "tone": "executive-level, strategic, not salesy",
    "must_include": ["reference to funding round", "ROI framing"],
    "must_not_include": ["Dear Sir/Madam", "unsubscribe", "[placeholder]"],
    "word_count_range": [60, 150],
    "structure": ["hook", "relevance_bridge", "value_prop", "soft_cta"],
    "subject_line_max_chars": 60
  },
  "reference_exemplar": "full text if available, null otherwise"
}
```

**Scoring should be layered, cheapest first.** Layer 1 is deterministic checks (free, instant): word count, forbidden phrases, CTA keyword presence, subject line length, structural markers like paragraph count. Layer 2 is embedding cosine similarity against reference exemplars where available, using a local model like `all-MiniLM-L6-v2` with a threshold of **≥0.65** — below this indicates significant semantic drift. Layer 3 is LLM-as-judge with binary pass/fail per dimension (tone, personalization, structure, CTA quality), which is expensive but necessary for persona fidelity. A critical finding from Hamel Husain and other practitioners: **binary pass/fail scoring is more reliable than Likert scales** because it prevents gaming with verbosity. The judge model version must also be pinned — judge drift is a documented failure mode where scoring changes get mistaken for product improvement.

For scheduling cadence, production teams converge on three rhythms: a **daily heartbeat** running one case per persona (22 API calls), a **full suite on any prompt or model version change**, and a **weekly full suite** with all scoring layers active. Buildo documented using Prefect's Artifacts feature to persist generation tuples as markdown reports for comparison, specifically leveraging nested flows aligned with module segregation — directly analogous to 22 persona modules.

---

## 2. Edit distance as an early warning system

The delta between what the AI drafts and what the human actually sends is one of the highest-signal quality metrics available — and the easiest to collect. If an operator consistently rewrites 60%+ of a specific persona's output, that persona module is functionally broken.

Research from Devatine and Abraham (2024) found that traditional metrics like Levenshtein distance and BLEU "often fail to accurately measure the effort required for post-editing, especially when edits involve block operations" like cut/paste/restructure, which are common in email editing. Their compression-based distance metric correlates **0.87** with human edit time. Meanwhile, the EditLens paper established practical embedding distance thresholds: a cosine distance of **0.03** is too small to detect, while **0.15** indicates essentially rewritten text.

For cold emails specifically, the most actionable approach is **section-level diffing** rather than whole-text metrics. Parse each email into subject line, greeting, body, CTA, and sign-off, then compute diff ratios per section. This tells you *what* is being rewritten, not just *how much*. If CTAs are being changed 50%+ of the time across a persona, the CTA framing is systematically wrong. If subject lines are rewritten 70%+ of the time, subject line generation needs prompt adjustment.

A practical multi-metric implementation combines word-level diff ratio via Python's `difflib.SequenceMatcher`, character-level fuzzy matching via `rapidfuzz`, and semantic similarity via a local sentence-transformer model. The composite "editing effort" score weights these: 40% word-level diff, 30% semantic distance, 30% character distance. Interpretation thresholds from both research and the machine translation industry suggest **0–15%** means essentially accepted as-is, **15–40%** is normal light editing, **40–60%** warrants investigation, and **60%+** means the persona is effectively broken.

The capture mechanism matters. **None of the major outreach tools — Apollo, SalesLoft, Outreach — natively expose draft-vs-sent edit data.** They track engagement metrics but not editing deltas. The cleanest implementation for a solo operator is building the editing interface directly in FastAPI: display the AI draft, let the operator edit inline, and capture both versions at send time. The `email_generations` table stores both `ai_draft_body` and `human_sent_body`, with edit metrics computed and persisted on every send event.

The feedback loop then runs as a daily Prefect flow querying the rolling 7-day average editing effort per persona. When any persona exceeds the 0.4 threshold, it fires a Slack notification. When a model version change causes >15% increase in editing effort across any persona, that's an immediate investigation trigger.

---

## 3. Why Bayesian methods are mandatory for A/B testing at this volume

Traditional frequentist A/B testing is mathematically impossible at 35 emails per week with a 3% reply rate. The numbers are unforgiving: detecting a **50% relative lift** (3% → 4.5%) requires ~5,022 total emails — about **2.8 years** at current volume. Even detecting a **doubling** of reply rate (3% → 6%) needs ~1,490 emails, or roughly **10 months**. Cold email industry sources confirm this: Instantly.ai recommends a minimum of 250 contacts per variant.

**Bayesian A/B testing using Beta-Binomial conjugate priors is the only viable approach.** It works with any sample size, provides intuitive outputs ("88% probability Variant B is better"), allows continuous monitoring without peeking penalties, and incorporates prior knowledge. An informative prior of Beta(3, 97) encodes the operator's belief in a ~3% baseline reply rate. After 70 emails per variant, if Variant A has 2 replies (2.9%) and Variant B has 5 replies (7.1%), the posterior yields approximately 88% probability that B is better. The decision rule: act when P(B better) exceeds 90% or drops below 10%.

**Thompson Sampling / multi-armed bandit is even better** for an ongoing optimization context because it minimizes regret — lost replies sent using the worse variant. Start with a 50/50 split, update Beta distributions after each batch, and let the algorithm naturally shift traffic toward the winning variant. This is especially valuable when "the cost of poor variations is so high that businesses hesitate to run A/B tests," as VWO notes.

For the 22-persona system, the architectural recommendation is to **test one module at a time against a control**, using consistent hashing for deterministic variant assignment. Each prospect domain should always see the same variant to prevent style inconsistency across touchpoints. Route assignment uses a simple hash: `hash(f"{experiment_id}:{prospect_domain}") % 100 < 50` determines variant A vs B.

Three practical tactics compensate for low reply volume. First, use **LLM-as-judge proxy metrics** — score every generated email on personalization, relevance, and persuasiveness to get 35 quality data points per week instead of ~1 reply. Second, **test dramatically different approaches** (completely different persona tones, different email structures) where effect sizes might be large enough to detect quickly. A Mailshake case study found switching from "pitch" to "question" style yielded **97% more appointments** — effects this large are detectable even at modest volumes. Third, treat initial 4-week results as directional signals and make decisions at the 90% Bayesian credible level rather than demanding 95%.

Prompt version management should be **database-backed in Supabase with git for templates**. Store prompt text in YAML files under version control for review history, but load from Supabase at runtime for dynamic switching. The `experiments` table stores variant configs (which base prompt version + which persona module version), and `email_generations` logs the full lineage: experiment ID, variant, all version numbers, and the generated output.

---

## 4. Promptfoo is the clear winner for CI/CD prompt testing

After evaluating seven tools against the constraint of self-hosted/OSS with no SaaS dependencies, the landscape is clear:

- **Promptfoo** (star): Fully open source (MIT), 100% self-hosted, first-class Anthropic support, YAML-driven configs, CI/CD integration. The unambiguous best choice.
- **DeepEval**: Open source core (Apache 2.0), but the regression comparison dashboard requires their paid Confident AI cloud platform. Strong backup if Promptfoo falters.
- **Langfuse**: Fully OSS and self-hostable (MIT), excellent for observability and prompt management, but it's a monitoring platform, not a testing framework. Good complement to Promptfoo but requires Docker Compose with PostgreSQL + ClickHouse + Redis — heavy for a solo operator.
- **Braintrust**: Proprietary SaaS. Self-hosting only via paid Enterprise. **Disqualified.**
- **LangSmith**: Proprietary SaaS. Self-hosting requires Enterprise license. **Disqualified.**
- **Ragas**: OSS but purpose-built for RAG pipeline evaluation. Metrics assume retrieval context that doesn't exist in cold email. **Not applicable.**
- **DSPy**: Fully OSS but focused on prompt optimization/compilation, not regression testing. Could complement testing by auto-optimizing persona prompts, but steep learning curve and high token cost. **Not the right tool for this job.**

**Promptfoo runs entirely locally with no data leaving the machine.** Disable all outbound requests with environment variables: `PROMPTFOO_DISABLE_TELEMETRY=1`, `PROMPTFOO_SELF_HOSTED=1`. It talks directly to the Anthropic API using provider syntax like `anthropic:messages:claude-sonnet-4-20250514`. Install via `npm install -g promptfoo` or `pip install promptfoo`.

One important caveat: **Promptfoo was acquired by OpenAI in March 2026** but explicitly remains MIT-licensed with multi-provider support. Monitor this — if Anthropic support degrades in future versions, DeepEval is the fallback.

---

## 5. The minimum viable monitoring setup for a solo operator

The absolute minimum to detect silent degradation across 22 personas with ~35 prospect domains requires four components running on Prefect + Supabase + FastAPI.

**Component 1: Daily canary heartbeat** (Prefect scheduled flow, `cron: 0 2 * * *`). Runs 1 canary case per persona = 22 Claude API calls. Applies deterministic checks (free) + one LLM-as-judge call per output (22 additional calls). Total daily cost: ~44 API calls ~ **$0.15-0.30/day** at Claude Sonnet pricing. Stores results in `canary_results` table. Alerts via Slack webhook if any persona's pass rate drops below 80%.

**Component 2: Edit distance capture** (FastAPI middleware on the email-sending endpoint). Captures both AI draft and human-sent version on every send event. Computes word-level diff ratio + section-level diffs inline. Stores in `email_generations` table. Zero additional API cost — uses `difflib` which is Python stdlib.

**Component 3: Weekly drift report** (Prefect scheduled flow, `cron: 0 8 * * 1`). Queries rolling 7-day metrics from Supabase: average editing effort per persona, canary pass rates per persona, output length distribution changes. Compares against stored baselines. Sends a single summary notification — either "all clear" or a list of personas requiring attention.

**Component 4: Model version change trigger** (Prefect flow, triggered manually or via webhook when switching model strings). Runs the full canary suite (44-66 cases) against the new model version. Compares results against the last run on the previous version. Blocks deployment if pass rate drops >10%.

**What to NOT build initially:** embedding drift detection (adds embedding API cost and complexity), a dashboard UI (use Supabase's built-in SQL editor for ad-hoc queries), real-time monitoring (daily is sufficient for ~35 emails/week), or Langfuse integration (add later if monitoring needs grow).

The total ongoing cost of this setup is approximately **$2-5/week** in additional Claude API calls, with zero infrastructure cost beyond the existing Prefect + Supabase stack. The highest-value signal per engineering effort is the edit distance tracking — it captures real operator behavior with no API cost and no separate infrastructure.

---

## 6. Real failures that prove monitoring matters

**The GPT-4 degradation study** (Chen, Zaharia, Zou, July 2023) remains the landmark case. Between March and June 2023 snapshots, GPT-4's code generation success rate dropped from **52% to 10%** and chain-of-thought reasoning stopped generating intermediate steps. Users had reported subjective quality drops for months before this objective evidence emerged. The study triggered industry-wide recognition that model providers silently update models and prompts optimized for one version may break on another.

**The support ticket triage case** (documented by base14/Scout) is the most directly relevant to persona-based systems. A GPT-4o-powered ticket routing system worked perfectly at launch but three months later silently began misclassifying billing tickets as technical issues. Two root causes: OpenAI pushed a model update behind the same identifier with no visible changelog, and the company launched a new product that blurred category boundaries.

**The GPT-4o retirement cascade** (February-August 2026) is the most sobering example for anyone pinning to specific model versions. When OpenAI retired GPT-4o and related models, replacement models introduced three distinct failure modes: stricter JSON schema enforcement broke prompts relying on loose schemas, strict instruction hierarchies broke prompts with implicit conventions, and different verbosity calibration meant "Be concise" instructions overshot.

A widely-cited NannyML study found that **91% of ML model performance degrades over time**, establishing degradation as the norm, not the exception.

---

## 7. Complete Supabase schema for the monitoring stack

The schema below provides the full data layer needed for canary testing, edit tracking, prompt versioning, A/B experiments, and drift scoring. Supabase's built-in pgvector extension enables embedding storage for similarity comparisons.

```sql
-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================
-- PROMPT VERSION MANAGEMENT
-- ============================================
CREATE TABLE prompt_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_type VARCHAR(20) NOT NULL CHECK (prompt_type IN ('base', 'persona')),
    persona_name VARCHAR(100),
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    is_active BOOLEAN DEFAULT FALSE,
    model_version VARCHAR(100),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(prompt_type, persona_name, version)
);

CREATE INDEX idx_prompt_active
    ON prompt_versions(prompt_type, persona_name, is_active)
    WHERE is_active = TRUE;

-- ============================================
-- CANARY TEST INFRASTRUCTURE
-- ============================================
CREATE TABLE canary_cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id VARCHAR(100) UNIQUE NOT NULL,
    persona_module VARCHAR(100) NOT NULL,
    prospect_input JSONB NOT NULL,
    assertions JSONB NOT NULL,
    reference_exemplar TEXT,
    reference_embedding vector(384),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE canary_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_type VARCHAR(20) NOT NULL CHECK
        (run_type IN ('heartbeat', 'full_suite', 'model_change', 'prompt_change')),
    model_version VARCHAR(100) NOT NULL,
    base_prompt_version INTEGER NOT NULL,
    total_cases INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    pass_rate FLOAT GENERATED ALWAYS AS
        (passed::float / NULLIF(total_cases, 0)) STORED,
    metadata JSONB DEFAULT '{}',
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE canary_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES canary_runs(id),
    case_id VARCHAR(100) NOT NULL REFERENCES canary_cases(case_id),
    persona_module VARCHAR(100) NOT NULL,
    persona_version INTEGER NOT NULL,
    generated_output TEXT NOT NULL,
    output_embedding vector(384),
    deterministic_pass BOOLEAN NOT NULL,
    deterministic_details JSONB,
    embedding_similarity FLOAT,
    llm_judge_scores JSONB,
    llm_judge_pass BOOLEAN,
    overall_pass BOOLEAN NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    judge_tokens INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_canary_results_persona
    ON canary_results(persona_module, created_at DESC);
CREATE INDEX idx_canary_results_run
    ON canary_results(run_id);

-- ============================================
-- EMAIL GENERATION & EDIT TRACKING
-- ============================================
CREATE TABLE email_generations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    persona_module VARCHAR(100) NOT NULL,
    persona_version INTEGER NOT NULL,
    base_prompt_version INTEGER NOT NULL,
    model_version VARCHAR(100) NOT NULL,
    prospect_domain VARCHAR(255) NOT NULL,
    prospect_data JSONB NOT NULL,
    experiment_id UUID,
    variant VARCHAR(10),
    ai_draft_subject TEXT NOT NULL,
    ai_draft_body TEXT NOT NULL,
    ai_draft_embedding vector(384),
    human_sent_subject TEXT,
    human_sent_body TEXT,
    sent_at TIMESTAMPTZ,
    edit_metrics JSONB,
    editing_effort FLOAT,
    subject_changed BOOLEAN,
    cta_changed BOOLEAN,
    body_diff_ratio FLOAT,
    opened BOOLEAN DEFAULT FALSE,
    replied BOOLEAN DEFAULT FALSE,
    reply_sentiment VARCHAR(20),
    reply_received_at TIMESTAMPTZ,
    llm_quality_score FLOAT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_gen_persona_time
    ON email_generations(persona_module, created_at DESC);
CREATE INDEX idx_gen_experiment
    ON email_generations(experiment_id, variant);
CREATE INDEX idx_gen_editing_effort
    ON email_generations(editing_effort) WHERE editing_effort IS NOT NULL;
CREATE INDEX idx_gen_reply
    ON email_generations(replied, persona_module) WHERE replied = TRUE;

-- ============================================
-- A/B EXPERIMENTS
-- ============================================
CREATE TABLE experiments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    test_level VARCHAR(20) CHECK (test_level IN ('base_prompt', 'persona')),
    target_persona VARCHAR(100),
    variant_a_config JSONB NOT NULL,
    variant_b_config JSONB NOT NULL,
    status VARCHAR(20) DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'completed', 'rolled_back')),
    bayesian_state JSONB DEFAULT '{"a": {"alpha": 3, "beta": 97},
                                    "b": {"alpha": 3, "beta": 97}}',
    prob_b_better FLOAT,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    ended_at TIMESTAMPTZ
);

-- ============================================
-- DRIFT SCORING (MATERIALIZED VIEWS)
-- ============================================
CREATE MATERIALIZED VIEW persona_health_7d AS
SELECT
    persona_module,
    COUNT(*) FILTER (WHERE editing_effort IS NOT NULL) AS emails_with_edits,
    AVG(editing_effort) AS avg_editing_effort,
    PERCENTILE_CONT(0.5) WITHIN GROUP
        (ORDER BY editing_effort) AS median_editing_effort,
    AVG(CASE WHEN subject_changed THEN 1 ELSE 0 END) AS subject_change_rate,
    AVG(CASE WHEN cta_changed THEN 1 ELSE 0 END) AS cta_change_rate,
    AVG(body_diff_ratio) AS avg_body_diff,
    COUNT(*) FILTER (WHERE replied) AS reply_count,
    COUNT(*) FILTER (WHERE sent_at IS NOT NULL) AS sent_count,
    CASE WHEN COUNT(*) FILTER (WHERE sent_at IS NOT NULL) > 0
        THEN COUNT(*) FILTER (WHERE replied)::float /
             COUNT(*) FILTER (WHERE sent_at IS NOT NULL)
        ELSE 0 END AS reply_rate
FROM email_generations
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY persona_module;

CREATE MATERIALIZED VIEW canary_health_7d AS
SELECT
    cr.persona_module,
    COUNT(*) AS total_tests,
    SUM(CASE WHEN cr.overall_pass THEN 1 ELSE 0 END) AS passed,
    AVG(CASE WHEN cr.overall_pass THEN 1.0 ELSE 0.0 END) AS pass_rate,
    AVG(cr.embedding_similarity) AS avg_embedding_sim
FROM canary_results cr
JOIN canary_runs crun ON cr.run_id = crun.id
WHERE crun.started_at > NOW() - INTERVAL '7 days'
GROUP BY cr.persona_module;
```
