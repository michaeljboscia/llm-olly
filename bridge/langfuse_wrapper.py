"""
Langfuse SDK Wrapper for the Narrative Generator

Drop-in instrumentation module for the cold email generation pipeline.
Uses manual Langfuse Python SDK v4 instrumentation with the @observe pattern
to avoid the OTel double-counting bug (Langfuse GitHub issue #12306).

Handles Anthropic cache token tracking (cache_read_input_tokens,
cache_creation_input_tokens) as first-class usage fields.

Usage:
    from bridge.langfuse_wrapper import LangfuseNarrativeTracer

    tracer = LangfuseNarrativeTracer()
    trace_id = tracer.trace_generation(
        persona_id="cfo_strategic",
        prospect_data={"domain": "acme.com", ...},
        model_version="claude-sonnet-4-20250514",
        base_prompt_version=3,
        persona_prompt_version=2,
    )
    tracer.log_generation(
        trace_id=trace_id,
        input_messages=[{"role": "user", "content": "..."}],
        output_text="Generated email...",
        usage_details={"input": 500, "output": 200, "cache_read_input_tokens": 450},
    )
    tracer.score_generation(trace_id, "quality", 0.85, "Good personalization")
    tracer.flush()

Environment variables:
    LANGFUSE_PUBLIC_KEY  — Langfuse project public key
    LANGFUSE_SECRET_KEY  — Langfuse project secret key
    LANGFUSE_HOST        — Langfuse instance URL (default: http://localhost:3300)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from langfuse import Langfuse

from bridge.edit_distance import compute_edit_metrics

logger = logging.getLogger(__name__)


class LangfuseNarrativeTracer:
    """Langfuse instrumentation wrapper for the Narrative Generator pipeline.

    Provides a clean API for tracing email generations, logging LLM calls
    with full Anthropic usage details (including cache tokens), attaching
    quality scores, and recording edit distance metrics.

    Thread-safe: the underlying Langfuse client handles batching and flushing.
    """

    def __init__(self, host: str | None = None, debug: bool = False) -> None:
        """Initialize the Langfuse client.

        Args:
            host: Override for LANGFUSE_HOST env var. If None, reads from env.
            debug: Enable Langfuse SDK debug logging.
        """
        kwargs: dict[str, Any] = {}
        if host:
            kwargs["host"] = host
        if debug:
            kwargs["debug"] = True

        self._client = Langfuse(**kwargs)
        # Cache of active traces keyed by trace_id for span attachment
        self._traces: dict[str, Any] = {}
        logger.info("LangfuseNarrativeTracer initialized (host=%s)", host or "env")

    @property
    def client(self) -> Langfuse:
        """Direct access to the Langfuse client for advanced usage."""
        return self._client

    def trace_generation(
        self,
        persona_id: str,
        prospect_data: dict,
        model_version: str,
        base_prompt_version: int,
        persona_prompt_version: int,
        experiment_id: str | None = None,
        variant: str | None = None,
        session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Start a new trace for an email generation.

        Creates a Langfuse trace with the full generation context as metadata.
        Returns a trace_id that must be passed to subsequent log/score calls.

        Args:
            persona_id: Persona module identifier (e.g., 'cfo_strategic').
            prospect_data: Prospect context dict (domain, company, signals, etc.).
            model_version: Claude model version string.
            base_prompt_version: Base prompt version number.
            persona_prompt_version: Persona module version number.
            experiment_id: Optional A/B experiment UUID.
            variant: Optional experiment variant ('a' or 'b').
            session_id: Optional session ID for grouping related traces.
            tags: Optional list of tags.

        Returns:
            trace_id string for use in subsequent calls.
        """
        trace_id = str(uuid.uuid4())
        prospect_domain = prospect_data.get("domain", "unknown")

        metadata: dict[str, Any] = {
            "persona_id": persona_id,
            "model_version": model_version,
            "base_prompt_version": base_prompt_version,
            "persona_prompt_version": persona_prompt_version,
            "prospect_domain": prospect_domain,
        }
        if experiment_id:
            metadata["experiment_id"] = experiment_id
            metadata["variant"] = variant

        trace_tags = tags or []
        trace_tags.extend(["narrative-generator", f"persona:{persona_id}"])

        trace_kwargs: dict[str, Any] = {
            "id": trace_id,
            "name": f"email-gen-{persona_id}-{prospect_domain}",
            "input": prospect_data,
            "metadata": metadata,
            "tags": trace_tags,
        }
        if session_id:
            trace_kwargs["session_id"] = session_id

        trace = self._client.trace(**trace_kwargs)
        self._traces[trace_id] = trace

        logger.debug(
            "Trace started: %s (persona=%s, domain=%s)",
            trace_id[:8],
            persona_id,
            prospect_domain,
        )
        return trace_id

    def log_generation(
        self,
        trace_id: str,
        input_messages: list[dict],
        output_text: str,
        usage_details: dict[str, int] | None = None,
        cache_metrics: dict[str, Any] | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> None:
        """Log an LLM generation call within a trace.

        Creates a generation span with full Anthropic usage details including
        cache_read_input_tokens and cache_creation_input_tokens.

        Args:
            trace_id: Trace ID from trace_generation().
            input_messages: List of message dicts sent to the LLM.
            output_text: Generated text output.
            usage_details: Token usage dict with keys:
                - input: total input tokens
                - output: output tokens
                - cache_read_input_tokens: tokens read from cache
                - cache_creation_input_tokens: tokens written to cache
            cache_metrics: Optional additional cache info (hit_rate, etc.).
            model: Model name override (uses trace metadata if not provided).
            system_prompt: System prompt text for logging.
        """
        trace = self._traces.get(trace_id)
        if not trace:
            logger.error("Trace %s not found — call trace_generation() first", trace_id)
            return

        gen_input: dict[str, Any] = {"messages": input_messages}
        if system_prompt:
            gen_input["system"] = system_prompt

        gen_kwargs: dict[str, Any] = {
            "name": "claude-generation",
            "input": gen_input,
            "output": output_text,
        }

        # Model name from explicit param or trace metadata
        if model:
            gen_kwargs["model"] = model
        else:
            trace_meta = trace.metadata if hasattr(trace, "metadata") else {}
            if isinstance(trace_meta, dict):
                gen_kwargs["model"] = trace_meta.get("model_version", "claude-sonnet-4-20250514")

        # Usage details — Langfuse v3 supports arbitrary usage keys
        if usage_details:
            langfuse_usage: dict[str, int] = {}
            # Map to Langfuse's expected keys
            if "input" in usage_details:
                langfuse_usage["input"] = usage_details["input"]
            if "output" in usage_details:
                langfuse_usage["output"] = usage_details["output"]
            # Cache tokens as first-class usage fields (Langfuse v3 feature)
            if "cache_read_input_tokens" in usage_details:
                langfuse_usage["cache_read_input_tokens"] = usage_details["cache_read_input_tokens"]
            if "cache_creation_input_tokens" in usage_details:
                langfuse_usage["cache_creation_input_tokens"] = usage_details["cache_creation_input_tokens"]
            gen_kwargs["usage_details"] = langfuse_usage

        # Cache metrics as metadata
        if cache_metrics:
            gen_kwargs["metadata"] = {"cache_metrics": cache_metrics}

        trace.generation(**gen_kwargs)

        logger.debug(
            "Generation logged on trace %s: %d input tokens, %d output tokens",
            trace_id[:8],
            usage_details.get("input", 0) if usage_details else 0,
            usage_details.get("output", 0) if usage_details else 0,
        )

    def score_generation(
        self,
        trace_id: str,
        score_name: str,
        value: float,
        comment: str = "",
    ) -> None:
        """Attach a score to a trace.

        Common score names:
            - 'quality': LLM-as-judge quality score (0-1)
            - 'human_approval': binary approval from operator (0 or 1)
            - 'reply_received': whether the email received a reply (0 or 1)
            - 'persona_fidelity': persona match score from eval (0-1)
            - 'editing_effort': composite edit distance score (0-1)

        Args:
            trace_id: Trace ID from trace_generation().
            score_name: Name of the score dimension.
            value: Numeric score value (typically 0-1).
            comment: Optional explanation or context.
        """
        trace = self._traces.get(trace_id)
        if not trace:
            logger.error("Trace %s not found — call trace_generation() first", trace_id)
            return

        trace.score(
            name=score_name,
            value=value,
            comment=comment[:500] if comment else "",
        )

        logger.debug(
            "Score '%s'=%.3f on trace %s",
            score_name,
            value,
            trace_id[:8],
        )

    def log_edit_metrics(
        self,
        trace_id: str,
        ai_draft: str,
        human_sent: str,
        edit_metrics: dict | None = None,
    ) -> dict:
        """Compute and log edit distance metrics for a generation.

        If edit_metrics is not provided, computes them from the ai_draft
        and human_sent texts using the edit_distance module.

        Attaches the metrics as trace metadata and also creates an
        'editing_effort' score for easy filtering in Langfuse.

        Args:
            trace_id: Trace ID from trace_generation().
            ai_draft: The AI-generated email text.
            human_sent: The human-edited email text as actually sent.
            edit_metrics: Pre-computed metrics dict (optional).

        Returns:
            The edit metrics dict (computed or passed through).
        """
        trace = self._traces.get(trace_id)
        if not trace:
            logger.error("Trace %s not found — call trace_generation() first", trace_id)
            return edit_metrics or {}

        # Compute if not provided
        if edit_metrics is None:
            edit_metrics = compute_edit_metrics(ai_draft, human_sent)

        # Update trace with edit info
        trace.update(
            output=human_sent,
            metadata={
                "edit_metrics": edit_metrics,
                "human_sent": True,
            },
        )

        # Attach editing_effort as a score for Langfuse dashboard filtering
        effort = edit_metrics.get("editing_effort", 0.0)
        interpretation = edit_metrics.get("interpretation", "unknown")
        self.score_generation(
            trace_id=trace_id,
            score_name="editing_effort",
            value=effort,
            comment=f"{interpretation} (word_diff={edit_metrics.get('word_diff_ratio', 0):.3f})",
        )

        # Log section-level changes as individual scores for drill-down
        section_diffs = edit_metrics.get("section_diffs", {})
        for section_name, diff_data in section_diffs.items():
            if diff_data.get("changed"):
                self.score_generation(
                    trace_id=trace_id,
                    score_name=f"edit_{section_name}",
                    value=diff_data.get("word_diff_ratio", 0.0),
                    comment=f"{section_name} section was edited",
                )

        logger.info(
            "Edit metrics logged on trace %s: effort=%.3f (%s)",
            trace_id[:8],
            effort,
            interpretation,
        )

        return edit_metrics

    def get_prompt(
        self,
        prompt_name: str,
        label: str = "production",
        cache_ttl_seconds: int = 300,
    ) -> Any:
        """Fetch a prompt from Langfuse prompt management.

        Uses client-side caching with TTL to minimize API calls.
        Prompts are versioned and label-based (production, staging, etc.).

        Args:
            prompt_name: Name of the prompt in Langfuse (e.g., 'base-cold-email',
                        'persona-cfo_strategic').
            label: Deployment label to fetch (default: 'production').
            cache_ttl_seconds: Client-side cache TTL in seconds (default: 300).

        Returns:
            Langfuse prompt object with .prompt attribute and .compile() method.

        Raises:
            Exception: If prompt not found or Langfuse is unreachable.
        """
        try:
            prompt = self._client.get_prompt(
                prompt_name,
                label=label,
                cache_ttl_seconds=cache_ttl_seconds,
            )
            logger.debug(
                "Fetched prompt '%s' (label=%s, version=%s)",
                prompt_name,
                label,
                getattr(prompt, "version", "unknown"),
            )
            return prompt
        except Exception:
            logger.exception("Failed to fetch prompt '%s' (label=%s)", prompt_name, label)
            raise

    def flush(self) -> None:
        """Flush all pending events to Langfuse.

        Call this before process exit or at the end of a batch to ensure
        all traces, generations, and scores are sent.
        """
        self._client.flush()
        logger.debug("Langfuse events flushed (%d active traces)", len(self._traces))

    def shutdown(self) -> None:
        """Flush and shut down the Langfuse client.

        Call this on application shutdown for clean resource cleanup.
        """
        self.flush()
        self._client.shutdown()
        self._traces.clear()
        logger.info("LangfuseNarrativeTracer shut down")

    def __enter__(self) -> LangfuseNarrativeTracer:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.shutdown()
