"""
llm-olly bridge — Promptfoo/Langfuse integration and edit distance tracking.

Key exports:
    - bridge_results: Push Promptfoo eval output into Langfuse traces + scores
    - compute_edit_metrics: Full edit distance between AI draft and human-sent email
    - compute_section_diffs: Per-section (subject, greeting, body, cta, signoff) diffs
    - compute_editing_effort: Composite 0-1 editing effort score
    - LangfuseNarrativeTracer: Drop-in instrumentation wrapper for the Narrative Generator
"""

from bridge.edit_distance import (
    compute_edit_metrics,
    compute_editing_effort,
    compute_section_diffs,
)
from bridge.langfuse_wrapper import LangfuseNarrativeTracer
from bridge.promptfoo_to_langfuse import bridge_results

__all__ = [
    "bridge_results",
    "compute_edit_metrics",
    "compute_editing_effort",
    "compute_section_diffs",
    "LangfuseNarrativeTracer",
]
