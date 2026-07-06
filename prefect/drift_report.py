"""
Weekly drift report flow.

Cron: 0 8 * * 1 (Monday 8 AM ET)

Refreshes materialized views (persona_health_7d, canary_health_7d), queries
both for alert conditions, and sends a single Slack summary — either "all
clear" or a list of personas needing attention.
"""

import psycopg2.extras
from prefect import flow, get_run_logger, task

from common import get_monitoring_db, send_slack_alert

# Alert thresholds
THRESH_EDITING_EFFORT = 0.4
THRESH_SUBJECT_CHANGE = 0.7
THRESH_CTA_CHANGE = 0.5
THRESH_CANARY_PASS = 0.8


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@task(name="refresh-materialized-views", retries=2, retry_delay_seconds=10)
def refresh_views() -> None:
    """Refresh persona_health_7d and canary_health_7d materialized views."""
    logger = get_run_logger()
    conn = get_monitoring_db()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            logger.info("Refreshing persona_health_7d...")
            cur.execute("REFRESH MATERIALIZED VIEW persona_health_7d")
            logger.info("Refreshing canary_health_7d...")
            cur.execute("REFRESH MATERIALIZED VIEW canary_health_7d")
        logger.info("Materialized views refreshed")
    finally:
        conn.close()


@task(name="query-persona-health")
def query_persona_health() -> list[dict]:
    """Query persona_health_7d for all rows."""
    conn = get_monitoring_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT persona_module, emails_with_edits,
                       avg_editing_effort, median_editing_effort,
                       subject_change_rate, cta_change_rate,
                       avg_body_diff, reply_count, sent_count, reply_rate
                FROM persona_health_7d
                ORDER BY persona_module
            """)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@task(name="query-canary-health")
def query_canary_health() -> list[dict]:
    """Query canary_health_7d for all rows."""
    conn = get_monitoring_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT persona_module, total_tests, passed,
                       pass_rate, avg_embedding_sim
                FROM canary_health_7d
                ORDER BY persona_module
            """)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@task(name="compute-alerts")
def compute_alerts(
    persona_health: list[dict],
    canary_health: list[dict],
) -> dict:
    """Check thresholds and build alert list.

    Returns:
        {
            "alerts": [{"persona": str, "issues": [str]}, ...],
            "persona_count": int,
            "canary_count": int,
        }
    """
    logger = get_run_logger()

    # Index canary health by persona
    canary_by_persona = {r["persona_module"]: r for r in canary_health}

    alerts: list[dict] = []

    # All persona modules — union of both views
    all_personas = sorted(
        set(r["persona_module"] for r in persona_health)
        | set(r["persona_module"] for r in canary_health)
    )

    for persona in all_personas:
        issues = []

        # Check persona health thresholds
        ph = next((r for r in persona_health if r["persona_module"] == persona), None)
        if ph:
            if ph["avg_editing_effort"] is not None and ph["avg_editing_effort"] > THRESH_EDITING_EFFORT:
                issues.append(
                    f"avg_editing_effort={ph['avg_editing_effort']:.2f} (>{THRESH_EDITING_EFFORT})"
                )
            if ph["subject_change_rate"] is not None and ph["subject_change_rate"] > THRESH_SUBJECT_CHANGE:
                issues.append(
                    f"subject_change_rate={ph['subject_change_rate']:.2f} (>{THRESH_SUBJECT_CHANGE})"
                )
            if ph["cta_change_rate"] is not None and ph["cta_change_rate"] > THRESH_CTA_CHANGE:
                issues.append(
                    f"cta_change_rate={ph['cta_change_rate']:.2f} (>{THRESH_CTA_CHANGE})"
                )

        # Check canary health thresholds
        ch = canary_by_persona.get(persona)
        if ch:
            if ch["pass_rate"] is not None and ch["pass_rate"] < THRESH_CANARY_PASS:
                issues.append(
                    f"canary_pass_rate={ch['pass_rate']:.2f} (<{THRESH_CANARY_PASS})"
                )

        if issues:
            alerts.append({"persona": persona, "issues": issues})

    logger.info(
        f"Alert check complete: {len(alerts)} personas flagged out of {len(all_personas)}"
    )

    return {
        "alerts": alerts,
        "persona_count": len(persona_health),
        "canary_count": len(canary_health),
    }


@task(name="send-drift-report")
def send_drift_report(alert_data: dict) -> bool:
    """Send a single Slack notification with the drift report."""
    logger = get_run_logger()
    alerts = alert_data["alerts"]

    if not alerts:
        msg = (
            ":white_check_mark: *Weekly Drift Report — All Clear*\n"
            f"Checked {alert_data['persona_count']} personas (edit health) "
            f"and {alert_data['canary_count']} personas (canary health).\n"
            "All metrics within acceptable thresholds."
        )
    else:
        lines = [
            f":warning: *Weekly Drift Report — {len(alerts)} Persona(s) Need Attention*\n"
        ]
        for a in alerts:
            issue_list = ", ".join(a["issues"])
            lines.append(f"  - *{a['persona']}*: {issue_list}")

        lines.append(
            f"\nChecked {alert_data['persona_count']} edit-health rows, "
            f"{alert_data['canary_count']} canary-health rows."
        )
        msg = "\n".join(lines)

    sent = send_slack_alert(msg)
    if sent:
        logger.info("Drift report sent to Slack")
    else:
        logger.warning("Failed to send Slack notification (webhook not set or request failed)")

    return sent


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------

@flow(name="drift-report", log_prints=True)
def drift_report() -> dict:
    """Weekly drift report — refresh views, check thresholds, alert Slack.

    Returns:
        {
            "status": "completed",
            "persona_count": int,
            "canary_count": int,
            "alerts_fired": int,
            "flagged_personas": [str],
            "slack_sent": bool,
        }
    """
    logger = get_run_logger()
    logger.info("Starting weekly drift report")

    # Refresh materialized views first
    refresh_views()

    # Query both views
    persona_health = query_persona_health()
    canary_health = query_canary_health()

    # Compute alerts
    alert_data = compute_alerts(persona_health, canary_health)

    # Send Slack notification
    slack_sent = send_drift_report(alert_data)

    flagged = [a["persona"] for a in alert_data["alerts"]]

    return {
        "status": "completed",
        "persona_count": alert_data["persona_count"],
        "canary_count": alert_data["canary_count"],
        "alerts_fired": len(flagged),
        "flagged_personas": flagged,
        "slack_sent": slack_sent,
    }


if __name__ == "__main__":
    drift_report()
