"""
Model version change flow — triggered manually.

Runs the full canary suite (44-66 cases) against a specified model version,
compares results to the last run on the previous model, and blocks if pass
rate drops more than 10%. Reports results via Slack.
"""

import json
import os
from datetime import datetime, timezone

import anthropic
import numpy as np
import psycopg2.extras
from prefect import flow, get_run_logger, task

from common import (
    get_monitoring_db,
    run_deterministic_checks,
    run_llm_judge,
    send_slack_alert,
)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@task(name="load-all-canary-cases", retries=2, retry_delay_seconds=5)
def load_all_cases() -> list[dict]:
    """Load ALL active canary cases (full suite, 44-66 cases)."""
    logger = get_run_logger()
    conn = get_monitoring_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT case_id, persona_module, prospect_input, assertions,
                       reference_exemplar, reference_embedding
                FROM canary_cases
                WHERE is_active = TRUE
                ORDER BY persona_module, case_id
            """)
            rows = [dict(r) for r in cur.fetchall()]
        logger.info(f"Loaded {len(rows)} active canary cases (full suite)")
        return rows
    finally:
        conn.close()


@task(name="get-previous-run")
def get_previous_run(current_model: str) -> dict | None:
    """Fetch the most recent full_suite or model_change run on a different model.

    Returns dict with run_id, model_version, pass_rate, per-persona stats,
    or None if no prior run exists.
    """
    logger = get_run_logger()
    conn = get_monitoring_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Find the most recent completed run on a different model
            cur.execute("""
                SELECT id, model_version, total_cases, passed, failed, pass_rate,
                       completed_at
                FROM canary_runs
                WHERE model_version != %s
                  AND run_type IN ('full_suite', 'model_change')
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT 1
            """, (current_model,))
            run_row = cur.fetchone()

            if not run_row:
                logger.info("No previous model run found for comparison")
                return None

            run = dict(run_row)

            # Get per-persona pass rates for comparison
            cur.execute("""
                SELECT persona_module,
                       COUNT(*) AS total,
                       SUM(CASE WHEN overall_pass THEN 1 ELSE 0 END) AS passed,
                       AVG(CASE WHEN overall_pass THEN 1.0 ELSE 0.0 END) AS pass_rate,
                       AVG(embedding_similarity) AS avg_embedding_sim
                FROM canary_results
                WHERE run_id = %s
                GROUP BY persona_module
                ORDER BY persona_module
            """, (run["id"],))
            run["persona_stats"] = {
                r["persona_module"]: dict(r) for r in cur.fetchall()
            }

            logger.info(
                f"Previous run: {run['model_version']} — "
                f"{run['passed']}/{run['total_cases']} ({run['pass_rate']:.0%})"
            )
            return run
    finally:
        conn.close()


@task(name="generate-and-evaluate", retries=1, retry_delay_seconds=10)
def generate_and_evaluate(case: dict, model_version: str) -> dict:
    """Generate an email and run full evaluation suite.

    Includes: deterministic checks, embedding similarity, LLM-as-judge.
    """
    logger = get_run_logger()
    persona = case["persona_module"]
    prospect = case["prospect_input"]
    assertions = case["assertions"]

    # --- Generate ---
    system_prompt = (
        f"You are a cold-email copywriter using the '{persona}' persona module. "
        f"Framework: {assertions.get('framework', 'challenger')}. "
        f"Tone: {assertions.get('tone', 'professional')}. "
        f"Write a cold outreach email for the prospect below. "
        f"Keep it concise — target word count: "
        f"{assertions.get('word_count_range', [40, 80])}."
    )

    user_content = (
        f"Prospect data:\n{json.dumps(prospect, indent=2)}\n\n"
        f"Write the email now. First line is the subject line, "
        f"then a blank line, then the body."
    )

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model_version,
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    output = message.content[0].text.strip()

    # --- Deterministic checks ---
    det = run_deterministic_checks(output, assertions)

    # --- Embedding similarity ---
    embedding_sim = None
    ref_embedding = case.get("reference_embedding")
    if ref_embedding is not None:
        try:
            # Compute output embedding via a lightweight approach:
            # use the anthropic SDK to get a hash-based proxy, or skip if
            # no embedding model is available. For now, we store None and
            # rely on deterministic + judge scoring.
            embedding_sim = None
        except Exception:
            embedding_sim = None

    # --- LLM-as-judge ---
    judge = run_llm_judge(
        output=output,
        persona_module=persona,
        prospect_input=prospect,
        assertions=assertions,
        model=model_version,
    )

    overall_pass = det["passed"] and judge["overall_pass"]

    logger.info(
        f"  {case['case_id']}: det={'PASS' if det['passed'] else 'FAIL'} "
        f"judge={'PASS' if judge['overall_pass'] else 'FAIL'} "
        f"overall={'PASS' if overall_pass else 'FAIL'}"
    )

    return {
        "case_id": case["case_id"],
        "persona_module": persona,
        "generated_output": output,
        "deterministic_pass": det["passed"],
        "deterministic_details": det["details"],
        "embedding_similarity": embedding_sim,
        "llm_judge_scores": judge["scores"],
        "llm_judge_pass": judge["overall_pass"],
        "overall_pass": overall_pass,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "judge_tokens": judge["input_tokens"] + judge["output_tokens"],
    }


@task(name="insert-model-change-run", retries=2, retry_delay_seconds=5)
def insert_run(results: list[dict], model_version: str) -> str:
    """Insert a model_change canary run + all results. Returns run_id."""
    logger = get_run_logger()
    conn = get_monitoring_db()

    total = len(results)
    passed = sum(1 for r in results if r["overall_pass"])

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO canary_runs
                    (run_type, model_version, base_prompt_version,
                     total_cases, passed, failed, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                "model_change", model_version, 1,
                total, passed, total - passed,
                datetime.now(timezone.utc),
            ))
            run_id = str(cur.fetchone()[0])

            for r in results:
                cur.execute("""
                    INSERT INTO canary_results
                        (run_id, case_id, persona_module, persona_version,
                         generated_output, deterministic_pass,
                         deterministic_details, embedding_similarity,
                         llm_judge_scores, llm_judge_pass, overall_pass,
                         input_tokens, output_tokens, judge_tokens)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    run_id, r["case_id"], r["persona_module"], 1,
                    r["generated_output"], r["deterministic_pass"],
                    json.dumps(r["deterministic_details"]),
                    r["embedding_similarity"],
                    json.dumps(r["llm_judge_scores"]),
                    r["llm_judge_pass"], r["overall_pass"],
                    r["input_tokens"], r["output_tokens"], r["judge_tokens"],
                ))

        conn.commit()
        logger.info(f"Inserted model_change run {run_id}: {passed}/{total}")
        return run_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@task(name="compare-and-report")
def compare_and_report(
    results: list[dict],
    run_id: str,
    model_version: str,
    previous_run: dict | None,
) -> dict:
    """Compare current results against previous model run.

    Blocks (returns blocked=True) if pass rate drops >10%.
    Sends Slack report either way.
    """
    logger = get_run_logger()

    total = len(results)
    passed = sum(1 for r in results if r["overall_pass"])
    current_rate = passed / total if total else 0

    # Per-persona stats for current run
    persona_stats: dict[str, dict] = {}
    for r in results:
        p = r["persona_module"]
        if p not in persona_stats:
            persona_stats[p] = {"total": 0, "passed": 0}
        persona_stats[p]["total"] += 1
        if r["overall_pass"]:
            persona_stats[p]["passed"] += 1

    blocked = False
    comparison_lines = []

    if previous_run:
        prev_rate = previous_run["pass_rate"] or 0
        delta = current_rate - prev_rate

        if delta < -0.10:
            blocked = True

        comparison_lines.append(
            f"Previous model: *{previous_run['model_version']}* — "
            f"{previous_run['passed']}/{previous_run['total_cases']} ({prev_rate:.0%})"
        )
        comparison_lines.append(
            f"Current model:  *{model_version}* — "
            f"{passed}/{total} ({current_rate:.0%})"
        )
        comparison_lines.append(f"Delta: {delta:+.0%}")

        # Per-persona comparison
        prev_stats = previous_run.get("persona_stats", {})
        degraded = []
        for persona, stats in sorted(persona_stats.items()):
            curr_p_rate = stats["passed"] / stats["total"] if stats["total"] else 0
            prev_p = prev_stats.get(persona, {})
            prev_p_rate = prev_p.get("pass_rate", 0) or 0
            p_delta = curr_p_rate - prev_p_rate
            if p_delta < -0.05:
                degraded.append(
                    f"  - *{persona}*: {prev_p_rate:.0%} -> {curr_p_rate:.0%} ({p_delta:+.0%})"
                )

        if degraded:
            comparison_lines.append("\nDegraded personas:")
            comparison_lines.extend(degraded)
    else:
        comparison_lines.append("No previous model run found for comparison.")
        comparison_lines.append(
            f"Current model: *{model_version}* — {passed}/{total} ({current_rate:.0%})"
        )

    # Build Slack message
    if blocked:
        icon = ":no_entry:"
        header = f"{icon} *Model Change BLOCKED* — {model_version}"
        footer = (
            "\n:rotating_light: Pass rate dropped >10%. "
            "Do NOT deploy this model version without investigation."
        )
    else:
        icon = ":white_check_mark:"
        header = f"{icon} *Model Change Report* — {model_version}"
        footer = "\nModel version is safe to deploy."

    msg = header + "\n" + "\n".join(comparison_lines) + footer
    send_slack_alert(msg)

    if blocked:
        logger.warning(f"MODEL CHANGE BLOCKED: {model_version} — pass rate drop >10%")
    else:
        logger.info(f"Model change OK: {model_version} — {current_rate:.0%}")

    return {
        "blocked": blocked,
        "current_rate": round(current_rate, 4),
        "previous_rate": round(previous_run["pass_rate"] or 0, 4) if previous_run else None,
        "previous_model": previous_run["model_version"] if previous_run else None,
    }


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------

@flow(name="model-change-trigger", log_prints=True)
def model_change_trigger(model_version: str) -> dict:
    """Full canary suite evaluation for a new model version.

    Args:
        model_version: The Claude model version to test
            (e.g., "claude-sonnet-4-20250514").

    Returns:
        {
            "status": "completed" | "blocked",
            "run_id": str,
            "model_version": str,
            "total_cases": int,
            "passed": int,
            "failed": int,
            "pass_rate": float,
            "blocked": bool,
            "comparison": dict,
        }
    """
    logger = get_run_logger()
    logger.info(f"Starting model change evaluation for {model_version}")

    # Load full suite
    cases = load_all_cases()
    if not cases:
        logger.warning("No active canary cases found — aborting")
        return {"status": "no_cases", "model_version": model_version, "total_cases": 0}

    # Get previous run for comparison
    previous_run = get_previous_run(model_version)

    # Run full evaluation
    results = []
    for case in cases:
        try:
            result = generate_and_evaluate(case, model_version)
            results.append(result)
        except Exception as exc:
            logger.error(f"Failed on {case['case_id']}: {exc}")
            results.append({
                "case_id": case["case_id"],
                "persona_module": case["persona_module"],
                "generated_output": f"ERROR: {exc}",
                "deterministic_pass": False,
                "deterministic_details": {"error": str(exc)},
                "embedding_similarity": None,
                "llm_judge_scores": {},
                "llm_judge_pass": False,
                "overall_pass": False,
                "input_tokens": 0,
                "output_tokens": 0,
                "judge_tokens": 0,
            })

    # Persist
    run_id = insert_run(results, model_version)

    # Compare and report
    comparison = compare_and_report(results, run_id, model_version, previous_run)

    passed = sum(1 for r in results if r["overall_pass"])
    total = len(results)

    return {
        "status": "blocked" if comparison["blocked"] else "completed",
        "run_id": run_id,
        "model_version": model_version,
        "total_cases": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0,
        "blocked": comparison["blocked"],
        "comparison": comparison,
    }


if __name__ == "__main__":
    import sys
    model = sys.argv[1] if len(sys.argv) > 1 else "claude-sonnet-4-20250514"
    model_change_trigger(model_version=model)
