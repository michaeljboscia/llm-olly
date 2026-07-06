"""
Shared utilities for LLM Observability Prefect flows.

Provides DB connections, deterministic checks, LLM-as-judge scoring,
Slack alerting, and edit metric computation.
"""

import difflib
import json
import os
import re

import anthropic
import psycopg2
import psycopg2.extras
import requests


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_monitoring_db():
    """Return a psycopg2 connection to the monitoring database.

    Uses the LLM_OLLY_DB_URL environment variable:
        postgresql://langfuse:***@localhost:54332/monitoring
    """
    dsn = os.environ["LLM_OLLY_DB_URL"]
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------

def run_deterministic_checks(output: str, assertions: dict) -> dict:
    """Run deterministic quality checks against generated output.

    Args:
        output: The generated email text.
        assertions: Dict from canary_cases.assertions with keys like
            word_count_range, must_not_include, max_sentences_per_paragraph,
            subject_line_max_words, etc.

    Returns:
        {
            "passed": bool,
            "details": {
                "word_count": int,
                "word_count_ok": bool,
                "forbidden_found": [str],
                "forbidden_ok": bool,
                "structure_ok": bool,
                "structure_issues": [str],
            }
        }
    """
    details = {}
    all_ok = True

    # --- Word count ---
    words = output.split()
    wc = len(words)
    details["word_count"] = wc
    wc_range = assertions.get("word_count_range")
    if wc_range:
        lo, hi = wc_range
        details["word_count_ok"] = lo <= wc <= hi
    else:
        details["word_count_ok"] = True
    if not details["word_count_ok"]:
        all_ok = False

    # --- Forbidden phrases ---
    forbidden = assertions.get("must_not_include", [])
    found = [phrase for phrase in forbidden if phrase.lower() in output.lower()]
    details["forbidden_found"] = found
    details["forbidden_ok"] = len(found) == 0
    if not details["forbidden_ok"]:
        all_ok = False

    # --- Structure checks ---
    structure_issues = []

    max_sent = assertions.get("max_sentences_per_paragraph")
    if max_sent:
        paragraphs = [p.strip() for p in output.split("\n\n") if p.strip()]
        for i, para in enumerate(paragraphs):
            sentences = re.split(r'[.!?]+\s+', para)
            sentences = [s for s in sentences if s.strip()]
            if len(sentences) > max_sent:
                structure_issues.append(
                    f"Paragraph {i+1} has {len(sentences)} sentences (max {max_sent})"
                )

    subj_max = assertions.get("subject_line_max_words")
    if subj_max:
        # Assume first line is the subject
        first_line = output.strip().split("\n")[0]
        subj_words = len(first_line.split())
        if subj_words > subj_max:
            structure_issues.append(
                f"Subject line has {subj_words} words (max {subj_max})"
            )

    details["structure_issues"] = structure_issues
    details["structure_ok"] = len(structure_issues) == 0
    if not details["structure_ok"]:
        all_ok = False

    return {"passed": all_ok, "details": details}


# ---------------------------------------------------------------------------
# LLM-as-judge
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = """You are a strict quality assurance judge for B2B cold emails.
You evaluate whether a generated email faithfully matches its target persona's tone,
framework, personalization requirements, and anti-patterns.

You MUST respond with ONLY a JSON object — no other text.
"""

_JUDGE_USER_TEMPLATE = """## Persona Module
{persona_module}

## Persona Tone Description
{tone}

## Framework
{framework}

## Prospect Input
{prospect_input}

## Generated Email
{output}

## Task
Evaluate the generated email on these dimensions:
1. **tone_fidelity**: Does it match the described tone? (PASS/FAIL)
2. **persona_match**: Does the writing style match the persona module? (PASS/FAIL)
3. **personalization**: Does it incorporate prospect-specific data? (PASS/FAIL)
4. **anti_pattern_free**: Free of generic/spammy patterns? (PASS/FAIL)

Respond with ONLY this JSON (no markdown, no explanation):
{{"tone_fidelity": "PASS or FAIL", "persona_match": "PASS or FAIL", "personalization": "PASS or FAIL", "anti_pattern_free": "PASS or FAIL", "overall": "PASS or FAIL", "reasoning": "one-sentence justification"}}
"""


def run_llm_judge(
    output: str,
    persona_module: str,
    prospect_input: dict,
    assertions: dict | None = None,
    model: str = "claude-sonnet-4-20250514",
) -> dict:
    """Run Claude-as-judge binary scoring on a generated email.

    Args:
        output: Generated email text.
        persona_module: Name of the persona module (e.g., "executive").
        prospect_input: The prospect data dict from canary_cases.
        assertions: Optional assertions dict for tone/framework info.
        model: Claude model to use for judging.

    Returns:
        {
            "scores": {"tone_fidelity": "PASS", ...},
            "overall_pass": bool,
            "input_tokens": int,
            "output_tokens": int,
        }
    """
    assertions = assertions or {}
    tone = assertions.get("tone", "professional")
    framework = assertions.get("framework", "not specified")

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=300,
        system=_JUDGE_SYSTEM,
        messages=[{
            "role": "user",
            "content": _JUDGE_USER_TEMPLATE.format(
                persona_module=persona_module,
                tone=tone,
                framework=framework,
                prospect_input=json.dumps(prospect_input, indent=2),
                output=output,
            ),
        }],
    )

    raw = message.content[0].text.strip()
    # Strip markdown fences if model wraps them
    if raw.startswith("```"):
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    scores = json.loads(raw)

    overall = scores.get("overall", "FAIL").upper() == "PASS"

    return {
        "scores": scores,
        "overall_pass": overall,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }


# ---------------------------------------------------------------------------
# Slack alerting
# ---------------------------------------------------------------------------

def send_slack_alert(message: str, webhook_url: str | None = None) -> bool:
    """POST a message to a Slack incoming webhook.

    Args:
        message: The message body (supports Slack mrkdwn).
        webhook_url: Override for the webhook URL. Falls back to
            SLACK_WEBHOOK_URL env var.

    Returns:
        True on success, False on failure.
    """
    url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return False

    try:
        resp = requests.post(url, json={"text": message}, timeout=10)
        return resp.status_code == 200
    except requests.RequestException:
        return False


# ---------------------------------------------------------------------------
# Edit metrics
# ---------------------------------------------------------------------------

def compute_edit_metrics(original: str, edited: str) -> dict:
    """Compute word-level diff ratio and section-level diffs.

    Args:
        original: The AI-generated draft.
        edited: The human-edited version.

    Returns:
        {
            "editing_effort": float,  # 0.0 = no edits, 1.0 = complete rewrite
            "word_diff_ratio": float,
            "subject_changed": bool,
            "body_diff_ratio": float,
            "added_words": int,
            "removed_words": int,
        }
    """
    orig_words = original.split()
    edit_words = edited.split()

    sm = difflib.SequenceMatcher(None, orig_words, edit_words)
    word_diff_ratio = 1.0 - sm.ratio()

    # Subject = first line
    orig_lines = original.strip().split("\n")
    edit_lines = edited.strip().split("\n")
    orig_subject = orig_lines[0] if orig_lines else ""
    edit_subject = edit_lines[0] if edit_lines else ""
    subject_changed = orig_subject.strip() != edit_subject.strip()

    # Body = everything after first line
    orig_body = "\n".join(orig_lines[1:]).strip()
    edit_body = "\n".join(edit_lines[1:]).strip()
    body_sm = difflib.SequenceMatcher(
        None, orig_body.split(), edit_body.split()
    )
    body_diff_ratio = 1.0 - body_sm.ratio()

    # Count added/removed
    opcodes = sm.get_opcodes()
    added = sum(
        j2 - j1 for tag, i1, i2, j1, j2 in opcodes if tag in ("insert", "replace")
    )
    removed = sum(
        i2 - i1 for tag, i1, i2, j1, j2 in opcodes if tag in ("delete", "replace")
    )

    # Composite editing effort: weighted average
    editing_effort = (
        0.3 * (1.0 if subject_changed else 0.0)
        + 0.7 * body_diff_ratio
    )

    return {
        "editing_effort": round(editing_effort, 4),
        "word_diff_ratio": round(word_diff_ratio, 4),
        "subject_changed": subject_changed,
        "body_diff_ratio": round(body_diff_ratio, 4),
        "added_words": added,
        "removed_words": removed,
    }
