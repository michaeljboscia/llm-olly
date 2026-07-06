"""
Promptfoo → Langfuse Bridge

Reads Promptfoo JSON eval output and creates Langfuse traces with scores.
No native export exists between Promptfoo and Langfuse (GitHub Discussion #3375),
so this bridge script fills the gap.

Usage:
    python promptfoo_to_langfuse.py results.json
    python promptfoo_to_langfuse.py results.json --tag model-change --session eval-2026-03-19

Environment variables:
    LANGFUSE_PUBLIC_KEY  — Langfuse project public key
    LANGFUSE_SECRET_KEY  — Langfuse project secret key
    LANGFUSE_HOST        — Langfuse instance URL (default: http://localhost:3300)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langfuse import Langfuse

logger = logging.getLogger(__name__)


def _init_langfuse(host: str | None = None) -> Langfuse:
    """Initialize Langfuse client from environment variables.

    LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY are read from env automatically
    by the SDK. We only override host if provided.
    """
    kwargs: dict[str, Any] = {}
    if host:
        kwargs["host"] = host
    return Langfuse(**kwargs)


def _extract_provider_info(result: dict) -> dict:
    """Extract provider metadata from a Promptfoo result entry."""
    provider_raw = result.get("provider", {})
    if isinstance(provider_raw, str):
        return {"provider_id": provider_raw}
    return {
        "provider_id": provider_raw.get("id", "unknown"),
        "provider_label": provider_raw.get("label", ""),
        "model": provider_raw.get("id", "").split(":")[-1] if ":" in provider_raw.get("id", "") else "",
    }


def _extract_cost_and_latency(result: dict) -> dict:
    """Extract cost and latency from a Promptfoo result entry."""
    metrics: dict[str, Any] = {}
    cost = result.get("cost")
    if cost is not None:
        metrics["cost_usd"] = cost
    latency = result.get("latencyMs")
    if latency is not None:
        metrics["latency_ms"] = latency
    # Token usage if present
    token_usage = result.get("tokenUsage", {})
    if token_usage:
        metrics["input_tokens"] = token_usage.get("total", 0)
        metrics["output_tokens"] = token_usage.get("completion", 0)
        metrics["prompt_tokens"] = token_usage.get("prompt", 0)
        # Cache tokens if the provider reports them
        if "cached" in token_usage:
            metrics["cache_read_input_tokens"] = token_usage["cached"]
    return metrics


def _build_trace_metadata(result: dict, provider_info: dict, cost_latency: dict) -> dict:
    """Assemble metadata dict for a Langfuse trace."""
    meta = {**provider_info, **cost_latency}
    # Persona and other vars from the test case
    vars_data = result.get("vars", {})
    if vars_data:
        meta["vars"] = vars_data
        # Promote persona to top-level for easy filtering
        if "persona" in vars_data:
            meta["persona"] = vars_data["persona"]
    # Test index for traceability
    if "testIdx" in result:
        meta["test_index"] = result["testIdx"]
    return meta


def _score_name_from_assertion(assertion: dict, index: int) -> str:
    """Derive a human-readable score name from a Promptfoo assertion."""
    # Prefer the metric name if set
    metric = assertion.get("metric")
    if metric:
        return metric
    # Fall back to assertion type + index
    atype = assertion.get("type", f"assertion-{index}")
    # Clean up type names for Langfuse display
    return atype.replace("-", "_")


def bridge_results(
    results_path: str,
    tags: list[str] | None = None,
    session_id: str | None = None,
    langfuse_host: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Read Promptfoo results JSON and create Langfuse traces + scores.

    Args:
        results_path: Path to Promptfoo output JSON file.
        tags: Optional list of tags to attach to every trace.
        session_id: Optional session ID to group all traces under.
        langfuse_host: Override for LANGFUSE_HOST env var.
        dry_run: If True, parse and validate but don't send to Langfuse.

    Returns:
        Summary dict with counts of traces and scores created.
    """
    path = Path(results_path)
    if not path.exists():
        raise FileNotFoundError(f"Results file not found: {results_path}")

    with open(path) as f:
        data = json.load(f)

    # Promptfoo output structure: { results: { results: [...], stats: {...} }, ... }
    results_container = data.get("results", {})
    outputs = results_container.get("results", [])
    eval_timestamp = data.get("createdAt") or data.get("timestamp", "")
    eval_id = data.get("evalId", str(uuid.uuid4())[:8])

    if not outputs:
        logger.warning("No test results found in %s", results_path)
        return {"traces": 0, "scores": 0, "errors": 0}

    logger.info(
        "Found %d test results in %s (eval: %s)",
        len(outputs),
        results_path,
        eval_id,
    )

    if dry_run:
        logger.info("Dry run — skipping Langfuse upload")
        return {"traces": len(outputs), "scores": 0, "errors": 0, "dry_run": True}

    langfuse = _init_langfuse(host=langfuse_host)
    trace_count = 0
    score_count = 0
    error_count = 0
    trace_tags = tags or []
    trace_tags.append("promptfoo-eval")

    for idx, result in enumerate(outputs):
        try:
            provider_info = _extract_provider_info(result)
            cost_latency = _extract_cost_and_latency(result)
            metadata = _build_trace_metadata(result, provider_info, cost_latency)

            # Build input from the prompt
            prompt_data = result.get("prompt", {})
            trace_input = prompt_data.get("raw", prompt_data.get("display", ""))
            trace_output = result.get("text", result.get("response", {}).get("output", ""))

            # Determine trace name from persona or test index
            persona = metadata.get("persona", f"test-{idx}")
            trace_name = f"promptfoo-{eval_id}-{persona}"

            trace_kwargs: dict[str, Any] = {
                "name": trace_name,
                "input": trace_input,
                "output": trace_output,
                "metadata": metadata,
                "tags": trace_tags,
            }
            if session_id:
                trace_kwargs["session_id"] = session_id

            # Add usage details as a generation span if we have token data
            trace = langfuse.trace(**trace_kwargs)

            # Log the generation with usage details for cost tracking
            model_name = provider_info.get("model") or provider_info.get("provider_id", "unknown")
            usage_details: dict[str, int] = {}
            if "input_tokens" in cost_latency or "output_tokens" in cost_latency:
                usage_details["input"] = cost_latency.get("prompt_tokens", 0)
                usage_details["output"] = cost_latency.get("output_tokens", 0)
                if "cache_read_input_tokens" in cost_latency:
                    usage_details["cache_read_input_tokens"] = cost_latency["cache_read_input_tokens"]

            generation_kwargs: dict[str, Any] = {
                "name": "llm-generation",
                "model": model_name,
                "input": trace_input,
                "output": trace_output,
                "metadata": {"eval_id": eval_id, "eval_timestamp": eval_timestamp},
            }
            if usage_details:
                generation_kwargs["usage_details"] = usage_details

            trace.generation(**generation_kwargs)
            trace_count += 1

            # Attach assertion scores
            grading_results = result.get("gradingResult", {}).get("componentResults", [])
            # Also check top-level gradingResults (varies by Promptfoo version)
            if not grading_results:
                grading_results = result.get("gradingResults", [])

            for score_idx, grading in enumerate(grading_results):
                assertion = grading.get("assertion", {})
                score_name = _score_name_from_assertion(assertion, score_idx)
                # Promptfoo uses pass (bool) and score (float 0-1)
                score_value = grading.get("score", 1.0 if grading.get("pass") else 0.0)
                score_comment = grading.get("reason", "")
                pass_status = grading.get("pass")
                if pass_status is not None:
                    score_comment = f"[{'PASS' if pass_status else 'FAIL'}] {score_comment}"

                trace.score(
                    name=score_name,
                    value=score_value,
                    comment=score_comment[:500],  # Langfuse has comment length limits
                )
                score_count += 1

            # Also attach overall pass/fail as a score
            overall_pass = result.get("success", result.get("pass"))
            if overall_pass is not None:
                trace.score(
                    name="overall_pass",
                    value=1.0 if overall_pass else 0.0,
                    comment=f"Overall eval result: {'PASS' if overall_pass else 'FAIL'}",
                )
                score_count += 1

        except Exception:
            logger.exception("Error processing result %d", idx)
            error_count += 1

    # Flush all pending events
    langfuse.flush()
    logger.info(
        "Bridge complete: %d traces, %d scores, %d errors",
        trace_count,
        score_count,
        error_count,
    )

    return {
        "traces": trace_count,
        "scores": score_count,
        "errors": error_count,
        "eval_id": eval_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bridge Promptfoo eval results to Langfuse traces and scores."
    )
    parser.add_argument(
        "results_file",
        help="Path to Promptfoo JSON output file (e.g., results.json)",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Tag to attach to all traces (repeatable)",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Session ID to group traces under",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Langfuse host URL (overrides LANGFUSE_HOST env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate without sending to Langfuse",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        summary = bridge_results(
            results_path=args.results_file,
            tags=args.tag,
            session_id=args.session,
            langfuse_host=args.host,
            dry_run=args.dry_run,
        )
        print(json.dumps(summary, indent=2))
        sys.exit(0 if summary["errors"] == 0 else 1)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(2)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in results file: %s", e)
        sys.exit(2)


if __name__ == "__main__":
    main()
