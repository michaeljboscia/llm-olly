"""
Daily canary heartbeat flow.

Cron: 0 2 * * * (2 AM ET daily)

Loads one active canary case per persona (22 total), generates an email via
Claude, runs deterministic checks + LLM-as-judge, inserts results into the
monitoring DB, and alerts Slack if any persona drops below 80% pass rate.
"""

import json
import os
from datetime import datetime, timezone

import anthropic
import psycopg2.extras
from prefect import flow, get_run_logger, task

from common import (
    get_monitoring_db,
    run_deterministic_checks,
    run_llm_judge,
    send_slack_alert,
)

MODEL = "claude-sonnet-4-20250514"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@task(name="load-active-canary-cases", retries=2, retry_delay_seconds=5)
def load_active_cases() -> list[dict]:
    """Load one active canary case per persona from the monitoring DB."""
    logger = get_run_logger()
    conn = get_monitoring_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (persona_module)
                    case_id, persona_module, prospect_input, assertions,
                    reference_exemplar
                FROM canary_cases
                WHERE is_active = TRUE
                ORDER BY persona_module, created_at DESC
            """)
            rows = [dict(r) for r in cur.fetchall()]
        logger.info(f"Loaded {len(rows)} active canary cases")
        return rows
    finally:
        conn.close()


@task(name="generate-email", retries=1, retry_delay_seconds=10)
def generate_email(case: dict) -> dict:
    """Generate an email via Claude for a single canary case.

    Returns dict with generated_output, input_tokens, output_tokens.
    """
    logger = get_run_logger()
    persona = case["persona_module"]
    prospect = case["prospect_input"]
    assertions = case["assertions"]

    # Build the generation prompt
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
        model=MODEL,
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    output = message.content[0].text.strip()
    logger.info(f"Generated email for persona={persona} ({len(output.split())} words)")

    return {
        "generated_output": output,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }


@task(name="evaluate-case")
def evaluate_case(case: dict, generation: dict) -> dict:
    """Run deterministic checks + LLM-as-judge on a generated email.

    Returns a result dict ready for DB insertion.
    """
    logger = get_run_logger()
    output = generation["generated_output"]
    assertions = case["assertions"]
    persona = case["persona_module"]
    prospect = case["prospect_input"]

    # Deterministic checks
    det = run_deterministic_checks(output, assertions)

    # LLM-as-judge
    judge = run_llm_judge(
        output=output,
        persona_module=persona,
        prospect_input=prospect,
        assertions=assertions,
        model=MODEL,
    )

    overall_pass = det["passed"] and judge["overall_pass"]

    logger.info(
        f"  {persona}: det={'PASS' if det['passed'] else 'FAIL'} "
        f"judge={'PASS' if judge['overall_pass'] else 'FAIL'} "
        f"overall={'PASS' if overall_pass else 'FAIL'}"
    )

    return {
        "case_id": case["case_id"],
        "persona_module": persona,
        "generated_output": output,
        "deterministic_pass": det["passed"],
        "deterministic_details": det["details"],
        "llm_judge_scores": judge["scores"],
        "llm_judge_pass": judge["overall_pass"],
        "overall_pass": overall_pass,
        "input_tokens": generation["input_tokens"],
        "output_tokens": generation["output_tokens"],
        "judge_tokens": judge["input_tokens"] + judge["output_tokens"],
    }


@task(name="insert-canary-run", retries=2, retry_delay_seconds=5)
def insert_canary_run(results: list[dict]) -> str:
    """Insert a canary_run row + all canary_results rows. Returns run_id."""
    logger = get_run_logger()
    conn = get_monitoring_db()

    total = len(results)
    passed = sum(1 for r in results if r["overall_pass"])
    failed = total - passed

    try:
        with conn.cursor() as cur:
            # Insert the run
            cur.execute("""
                INSERT INTO canary_runs
                    (run_type, model_version, base_prompt_version,
                     total_cases, passed, failed, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                "heartbeat", MODEL, 1,
                total, passed, failed,
                datetime.now(timezone.utc),
            ))
            run_id = str(cur.fetchone()[0])

            # Insert results
            for r in results:
                cur.execute("""
                    INSERT INTO canary_results
                        (run_id, case_id, persona_module, persona_version,
                         generated_output, deterministic_pass,
                         deterministic_details, llm_judge_scores,
                         llm_judge_pass, overall_pass,
                         input_tokens, output_tokens, judge_tokens)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    run_id, r["case_id"], r["persona_module"], 1,
                    r["generated_output"], r["deterministic_pass"],
                    json.dumps(r["deterministic_details"]),
                    json.dumps(r["llm_judge_scores"]),
                    r["llm_judge_pass"], r["overall_pass"],
                    r["input_tokens"], r["output_tokens"], r["judge_tokens"],
                ))

        conn.commit()
        logger.info(f"Inserted canary run {run_id}: {passed}/{total} passed")
        return run_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@task(name="check-and-alert")
def check_and_alert(results: list[dict], run_id: str) -> dict:
    """Check per-persona pass rates and alert Slack if any < 80%."""
    logger = get_run_logger()

    # Group by persona
    persona_stats: dict[str, dict] = {}
    for r in results:
        p = r["persona_module"]
        if p not in persona_stats:
            persona_stats[p] = {"total": 0, "passed": 0}
        persona_stats[p]["total"] += 1
        if r["overall_pass"]:
            persona_stats[p]["passed"] += 1

    # Find personas below 80%
    failing = []
    for persona, stats in sorted(persona_stats.items()):
        rate = stats["passed"] / stats["total"] if stats["total"] else 0
        if rate < 0.8:
            failing.append(f"  - *{persona}*: {rate:.0%} ({stats['passed']}/{stats['total']})")

    total_passed = sum(1 for r in results if r["overall_pass"])
    total = len(results)
    overall_rate = total_passed / total if total else 0

    if failing:
        msg = (
            f":rotating_light: *Canary Heartbeat Alert*\n"
            f"Run `{run_id}` — {total_passed}/{total} passed ({overall_rate:.0%})\n\n"
            f"Personas below 80% threshold:\n" + "\n".join(failing)
        )
        send_slack_alert(msg)
        logger.warning(f"Alert sent: {len(failing)} personas below threshold")
    else:
        logger.info(f"All personas above 80% — {total_passed}/{total} ({overall_rate:.0%})")

    return {
        "alerted": len(failing) > 0,
        "failing_personas": [f.split("*")[1] for f in failing] if failing else [],
        "overall_pass_rate": round(overall_rate, 4),
    }


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------

@flow(name="canary-heartbeat", log_prints=True)
def canary_heartbeat() -> dict:
    """Daily canary heartbeat — 22 API calls, deterministic + judge checks.

    Returns:
        {
            "status": "completed",
            "run_id": str,
            "total_cases": int,
            "passed": int,
            "failed": int,
            "pass_rate": float,
            "alerted": bool,
        }
    """
    logger = get_run_logger()
    logger.info("Starting daily canary heartbeat")

    # Load cases
    cases = load_active_cases()
    if not cases:
        logger.warning("No active canary cases found — aborting")
        return {"status": "no_cases", "total_cases": 0}

    # Generate + evaluate each case
    results = []
    for case in cases:
        try:
            gen = generate_email(case)
            result = evaluate_case(case, gen)
            results.append(result)
        except Exception as exc:
            logger.error(f"Failed on {case['case_id']}: {exc}")
            results.append({
                "case_id": case["case_id"],
                "persona_module": case["persona_module"],
                "generated_output": f"ERROR: {exc}",
                "deterministic_pass": False,
                "deterministic_details": {"error": str(exc)},
                "llm_judge_scores": {},
                "llm_judge_pass": False,
                "overall_pass": False,
                "input_tokens": 0,
                "output_tokens": 0,
                "judge_tokens": 0,
            })

    # Persist
    run_id = insert_canary_run(results)

    # Alert if needed
    alert_result = check_and_alert(results, run_id)

    passed = sum(1 for r in results if r["overall_pass"])
    total = len(results)

    return {
        "status": "completed",
        "run_id": run_id,
        "total_cases": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0,
        "alerted": alert_result["alerted"],
    }


if __name__ == "__main__":
    canary_heartbeat()
