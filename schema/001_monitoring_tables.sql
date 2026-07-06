-- LLM Observability Monitoring Schema
-- Target: Dedicated Supabase local instance (port 54332)
-- Purpose: Canary testing, edit tracking, prompt versioning, A/B experiments, drift scoring
--
-- Run with: psql postgresql://postgres:postgres@localhost:54332/postgres -f 001_monitoring_tables.sql

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================
-- PROMPT VERSION MANAGEMENT
-- ============================================
CREATE TABLE IF NOT EXISTS prompt_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_type VARCHAR(20) NOT NULL CHECK (prompt_type IN ('base', 'persona')),
    persona_name VARCHAR(100),  -- NULL for base prompts
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,  -- SHA-256 for dedup
    is_active BOOLEAN DEFAULT FALSE,
    model_version VARCHAR(100),  -- e.g., 'claude-sonnet-4-20250514'
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(prompt_type, persona_name, version)
);

CREATE INDEX IF NOT EXISTS idx_prompt_active
    ON prompt_versions(prompt_type, persona_name, is_active)
    WHERE is_active = TRUE;

-- ============================================
-- CANARY TEST INFRASTRUCTURE
-- ============================================
CREATE TABLE IF NOT EXISTS canary_cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id VARCHAR(100) UNIQUE NOT NULL,  -- e.g., 'persona-cfo-fintech-001'
    persona_module VARCHAR(100) NOT NULL,
    prospect_input JSONB NOT NULL,
    assertions JSONB NOT NULL,  -- must_include, must_not_include, etc.
    reference_exemplar TEXT,  -- hand-written exemplar if available
    reference_embedding vector(384),  -- MiniLM-L6-v2 embedding
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS canary_runs (
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

CREATE TABLE IF NOT EXISTS canary_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES canary_runs(id),
    case_id VARCHAR(100) NOT NULL REFERENCES canary_cases(case_id),
    persona_module VARCHAR(100) NOT NULL,
    persona_version INTEGER NOT NULL,
    generated_output TEXT NOT NULL,
    output_embedding vector(384),
    -- Scoring dimensions
    deterministic_pass BOOLEAN NOT NULL,
    deterministic_details JSONB,  -- {word_count: 95, forbidden_found: []}
    embedding_similarity FLOAT,  -- cosine sim vs reference
    llm_judge_scores JSONB,  -- {tone: "PASS", personalization: "FAIL", ...}
    llm_judge_pass BOOLEAN,
    overall_pass BOOLEAN NOT NULL,
    -- Cost tracking
    input_tokens INTEGER,
    output_tokens INTEGER,
    judge_tokens INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_canary_results_persona
    ON canary_results(persona_module, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_canary_results_run
    ON canary_results(run_id);

-- ============================================
-- EMAIL GENERATION & EDIT TRACKING
-- ============================================
CREATE TABLE IF NOT EXISTS email_generations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Generation context
    persona_module VARCHAR(100) NOT NULL,
    persona_version INTEGER NOT NULL,
    base_prompt_version INTEGER NOT NULL,
    model_version VARCHAR(100) NOT NULL,
    prospect_domain VARCHAR(255) NOT NULL,
    prospect_data JSONB NOT NULL,
    -- A/B experiment tracking
    experiment_id UUID,
    variant VARCHAR(10),
    -- Content
    ai_draft_subject TEXT NOT NULL,
    ai_draft_body TEXT NOT NULL,
    ai_draft_embedding vector(384),
    -- Human-edited version (populated after send)
    human_sent_subject TEXT,
    human_sent_body TEXT,
    sent_at TIMESTAMPTZ,
    -- Edit metrics (computed at send time)
    edit_metrics JSONB,  -- full metrics object
    editing_effort FLOAT,  -- composite 0-1 score
    subject_changed BOOLEAN,
    cta_changed BOOLEAN,
    body_diff_ratio FLOAT,
    -- Outcome tracking
    opened BOOLEAN DEFAULT FALSE,
    replied BOOLEAN DEFAULT FALSE,
    reply_sentiment VARCHAR(20),  -- positive, negative, neutral, bounce
    reply_received_at TIMESTAMPTZ,
    -- Quality proxy
    llm_quality_score FLOAT,  -- LLM-as-judge quality score at generation
    -- Metadata
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gen_persona_time
    ON email_generations(persona_module, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gen_experiment
    ON email_generations(experiment_id, variant);
CREATE INDEX IF NOT EXISTS idx_gen_editing_effort
    ON email_generations(editing_effort) WHERE editing_effort IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_gen_reply
    ON email_generations(replied, persona_module) WHERE replied = TRUE;

-- ============================================
-- A/B EXPERIMENTS
-- ============================================
CREATE TABLE IF NOT EXISTS experiments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    test_level VARCHAR(20) CHECK (test_level IN ('base_prompt', 'persona')),
    target_persona VARCHAR(100),  -- NULL for base prompt tests
    variant_a_config JSONB NOT NULL,
    variant_b_config JSONB NOT NULL,
    status VARCHAR(20) DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'completed', 'rolled_back')),
    -- Bayesian tracking
    bayesian_state JSONB DEFAULT '{"a": {"alpha": 3, "beta": 97}, "b": {"alpha": 3, "beta": 97}}',
    prob_b_better FLOAT,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    ended_at TIMESTAMPTZ
);

-- ============================================
-- DRIFT SCORING (MATERIALIZED VIEWS)
-- ============================================
CREATE MATERIALIZED VIEW IF NOT EXISTS persona_health_7d AS
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

CREATE MATERIALIZED VIEW IF NOT EXISTS canary_health_7d AS
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

-- ============================================
-- HELPER: Persona degradation alert query
-- ============================================
COMMENT ON MATERIALIZED VIEW persona_health_7d IS
'Refresh daily via Prefect: REFRESH MATERIALIZED VIEW CONCURRENTLY persona_health_7d;
Alert query: SELECT persona_module, avg_editing_effort, reply_rate FROM persona_health_7d WHERE avg_editing_effort > 0.4 OR subject_change_rate > 0.7 ORDER BY avg_editing_effort DESC;';
