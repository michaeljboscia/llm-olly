"""
Edit Distance Computation Module

Computes editing effort metrics between AI-generated drafts and human-sent emails.
Designed for the GTM Machine Narrative Generator's cold email pipeline.

The editing_effort score (0-1) indicates how much a human changed the AI output:
    0.00 - 0.15  →  accepted as-is
    0.15 - 0.40  →  normal light editing
    0.40 - 0.60  →  investigate — persona may need tuning
    0.60+        →  persona broken — output being heavily rewritten

Uses:
    - difflib.SequenceMatcher for word-level diff ratio
    - rapidfuzz.fuzz for character-level fuzzy matching
    - Regex-based section parsing for per-section diffs

References:
    - Devatine & Abraham (2024): compression-based edit distance correlates 0.87 with human edit time
    - EditLens: cosine distance 0.03 = undetectable, 0.15 = essentially rewritten
    - DR-04 drift detection research
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

# --- Editing effort interpretation thresholds ---
THRESHOLD_ACCEPTED = 0.15
THRESHOLD_LIGHT_EDIT = 0.40
THRESHOLD_INVESTIGATE = 0.60

# --- Weights for composite editing_effort score ---
WEIGHT_WORD_DIFF = 0.40
WEIGHT_SEMANTIC = 0.30     # Placeholder until sentence-transformer integration
WEIGHT_CHAR_DISTANCE = 0.30

# --- Section parsing patterns ---
# Greeting: lines starting with Hi/Hey/Hello/Dear/Morning/Afternoon + name
_GREETING_PATTERN = re.compile(
    r"^(Hi|Hey|Hello|Dear|Good\s+(?:morning|afternoon|evening)|Morning|Afternoon)\b[^\n]*",
    re.IGNORECASE | re.MULTILINE,
)

# CTA: lines containing question marks, "would you", "interested in", "open to",
# "worth a", "make sense", "thoughts?", or similar soft-ask patterns
_CTA_PATTERN = re.compile(
    r"^.*(?:would you|interested in|open to|worth a|make sense|thoughts\??|"
    r"happy to|love to|want to|like to|curious if|mind if|how about|"
    r"can we|could we|shall we|let me know)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)

# Signoff: common email closings — Best, Cheers, Thanks, Regards, etc.
_SIGNOFF_PATTERN = re.compile(
    r"^(Best|Cheers|Thanks|Thank you|Regards|Warm regards|Kind regards|"
    r"All the best|Talk soon|Looking forward|Sincerely|Respectfully|"
    r"Take care|Sent from)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class SectionDiff:
    """Diff metrics for a single email section."""
    section: str
    ai_text: str
    human_text: str
    word_diff_ratio: float      # 0 = identical, 1 = completely different
    char_similarity: float       # rapidfuzz ratio (0-100 scale, normalized to 0-1)
    changed: bool               # True if any meaningful edit detected

    def to_dict(self) -> dict:
        return {
            "section": self.section,
            "ai_text": self.ai_text,
            "human_text": self.human_text,
            "word_diff_ratio": round(self.word_diff_ratio, 4),
            "char_similarity": round(self.char_similarity, 4),
            "changed": self.changed,
        }


@dataclass
class EditMetrics:
    """Full edit distance metrics between an AI draft and human-sent email."""
    word_diff_ratio: float          # 0 = identical, 1 = completely different
    char_similarity: float          # 0-1, from rapidfuzz
    semantic_distance: float        # 0-1, placeholder until embedding model integrated
    editing_effort: float           # composite 0-1 score
    interpretation: str             # human-readable threshold label
    word_count_ai: int
    word_count_human: int
    word_count_delta: int
    section_diffs: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "word_diff_ratio": round(self.word_diff_ratio, 4),
            "char_similarity": round(self.char_similarity, 4),
            "semantic_distance": round(self.semantic_distance, 4),
            "editing_effort": round(self.editing_effort, 4),
            "interpretation": self.interpretation,
            "word_count_ai": self.word_count_ai,
            "word_count_human": self.word_count_human,
            "word_count_delta": self.word_count_delta,
            "section_diffs": self.section_diffs,
        }


def _normalize_text(text: str) -> str:
    """Normalize whitespace and strip for consistent comparison."""
    return re.sub(r"\s+", " ", text.strip())


def _word_diff_ratio(text_a: str, text_b: str) -> float:
    """Compute word-level diff ratio using SequenceMatcher.

    Returns 0.0 for identical texts, 1.0 for completely different.
    """
    if not text_a and not text_b:
        return 0.0
    if not text_a or not text_b:
        return 1.0

    words_a = _normalize_text(text_a).split()
    words_b = _normalize_text(text_b).split()
    similarity = SequenceMatcher(None, words_a, words_b).ratio()
    return 1.0 - similarity


def _char_similarity(text_a: str, text_b: str) -> float:
    """Compute character-level fuzzy similarity using rapidfuzz.

    Returns 0.0 for completely different, 1.0 for identical.
    """
    if not text_a and not text_b:
        return 1.0
    if not text_a or not text_b:
        return 0.0

    norm_a = _normalize_text(text_a)
    norm_b = _normalize_text(text_b)
    # rapidfuzz.fuzz.ratio returns 0-100
    return fuzz.ratio(norm_a, norm_b) / 100.0


def _parse_subject(full_email: str) -> tuple[str, str]:
    """Extract subject line from email text.

    Looks for 'Subject: ...' prefix or assumes first line is subject.
    Returns (subject, remaining_body).
    """
    lines = full_email.strip().split("\n")
    if not lines:
        return "", ""

    first_line = lines[0].strip()
    if first_line.lower().startswith("subject:"):
        subject = first_line[len("subject:"):].strip()
        body = "\n".join(lines[1:]).strip()
    elif first_line.lower().startswith("re:") or first_line.lower().startswith("fwd:"):
        subject = first_line.strip()
        body = "\n".join(lines[1:]).strip()
    else:
        # No explicit subject marker — treat entire text as body
        subject = ""
        body = full_email.strip()

    return subject, body


def _extract_section(text: str, pattern: re.Pattern) -> tuple[str, str]:
    """Extract the first match of a pattern from text.

    Returns (matched_text, remaining_text_with_match_removed).
    """
    match = pattern.search(text)
    if not match:
        return "", text
    matched = match.group(0).strip()
    remaining = (text[:match.start()] + text[match.end():]).strip()
    return matched, remaining


def _parse_email_sections(text: str) -> dict[str, str]:
    """Parse email into sections: subject, greeting, body, cta, signoff.

    Uses regex-based heuristics. The 'body' section is whatever remains
    after extracting the other sections.
    """
    subject, body_text = _parse_subject(text)

    greeting, body_text = _extract_section(body_text, _GREETING_PATTERN)
    signoff, body_text = _extract_section(body_text, _SIGNOFF_PATTERN)
    cta, body_text = _extract_section(body_text, _CTA_PATTERN)

    return {
        "subject": subject,
        "greeting": greeting,
        "body": body_text.strip(),
        "cta": cta,
        "signoff": signoff,
    }


def compute_section_diffs(ai_draft: str, human_sent: str) -> dict[str, dict]:
    """Compute per-section diffs between AI draft and human-sent email.

    Sections: subject, greeting, body, cta, signoff.

    Args:
        ai_draft: The AI-generated email text.
        human_sent: The human-edited/sent email text.

    Returns:
        Dict mapping section names to SectionDiff.to_dict() results.
    """
    ai_sections = _parse_email_sections(ai_draft)
    human_sections = _parse_email_sections(human_sent)

    diffs: dict[str, dict] = {}
    for section_name in ("subject", "greeting", "body", "cta", "signoff"):
        ai_text = ai_sections.get(section_name, "")
        human_text = human_sections.get(section_name, "")

        word_diff = _word_diff_ratio(ai_text, human_text)
        char_sim = _char_similarity(ai_text, human_text)
        # A section is "changed" if word diff exceeds a small threshold
        # (accounts for trivial whitespace normalization diffs)
        changed = word_diff > 0.05

        diff = SectionDiff(
            section=section_name,
            ai_text=ai_text,
            human_text=human_text,
            word_diff_ratio=word_diff,
            char_similarity=char_sim,
            changed=changed,
        )
        diffs[section_name] = diff.to_dict()

    return diffs


def compute_editing_effort(metrics: dict[str, Any]) -> float:
    """Compute composite editing effort score (0-1).

    Weights:
        40% word-level diff ratio
        30% semantic distance (placeholder — uses 1 - char_similarity as proxy)
        30% character distance (1 - char_similarity)

    When a real sentence-transformer embedding model is integrated,
    the semantic_distance field will use cosine distance instead of
    the char-level proxy.

    Args:
        metrics: Dict with keys 'word_diff_ratio', 'char_similarity',
                 and optionally 'semantic_distance'.

    Returns:
        Float between 0 and 1.
    """
    word_diff = metrics.get("word_diff_ratio", 0.0)
    char_sim = metrics.get("char_similarity", 1.0)
    semantic = metrics.get("semantic_distance", 1.0 - char_sim)

    char_distance = 1.0 - char_sim
    effort = (
        WEIGHT_WORD_DIFF * word_diff
        + WEIGHT_SEMANTIC * semantic
        + WEIGHT_CHAR_DISTANCE * char_distance
    )
    return max(0.0, min(1.0, effort))


def _interpret_effort(effort: float) -> str:
    """Map editing effort score to human-readable interpretation."""
    if effort < THRESHOLD_ACCEPTED:
        return "accepted_as_is"
    elif effort < THRESHOLD_LIGHT_EDIT:
        return "light_editing"
    elif effort < THRESHOLD_INVESTIGATE:
        return "investigate"
    else:
        return "persona_broken"


def compute_edit_metrics(ai_draft: str, human_sent: str) -> dict[str, Any]:
    """Compute full edit distance metrics between an AI draft and human-sent email.

    This is the primary entry point. Returns a complete metrics dict suitable
    for storing in the email_generations.edit_metrics JSONB column.

    Args:
        ai_draft: The AI-generated email text (subject + body as single string).
        human_sent: The human-edited/sent email text.

    Returns:
        Dict with all edit metrics including per-section diffs.
    """
    if not ai_draft or not human_sent:
        logger.warning("Empty input to compute_edit_metrics (ai=%d, human=%d chars)",
                       len(ai_draft or ""), len(human_sent or ""))
        return EditMetrics(
            word_diff_ratio=1.0 if (ai_draft or human_sent) else 0.0,
            char_similarity=0.0 if (ai_draft or human_sent) else 1.0,
            semantic_distance=1.0 if (ai_draft or human_sent) else 0.0,
            editing_effort=1.0 if (ai_draft or human_sent) else 0.0,
            interpretation="persona_broken" if (ai_draft or human_sent) else "accepted_as_is",
            word_count_ai=len(ai_draft.split()) if ai_draft else 0,
            word_count_human=len(human_sent.split()) if human_sent else 0,
            word_count_delta=0,
            section_diffs={},
        ).to_dict()

    # Core metrics
    word_diff = _word_diff_ratio(ai_draft, human_sent)
    char_sim = _char_similarity(ai_draft, human_sent)

    # Semantic distance placeholder — mirrors char distance until
    # sentence-transformer embedding model is wired in
    semantic_distance = 1.0 - char_sim

    # Composite score
    raw_metrics = {
        "word_diff_ratio": word_diff,
        "char_similarity": char_sim,
        "semantic_distance": semantic_distance,
    }
    effort = compute_editing_effort(raw_metrics)
    interpretation = _interpret_effort(effort)

    # Word counts
    ai_words = len(ai_draft.split())
    human_words = len(human_sent.split())

    # Per-section diffs
    section_diffs = compute_section_diffs(ai_draft, human_sent)

    result = EditMetrics(
        word_diff_ratio=word_diff,
        char_similarity=char_sim,
        semantic_distance=semantic_distance,
        editing_effort=effort,
        interpretation=interpretation,
        word_count_ai=ai_words,
        word_count_human=human_words,
        word_count_delta=human_words - ai_words,
        section_diffs=section_diffs,
    )

    logger.debug(
        "Edit metrics: effort=%.3f (%s), word_diff=%.3f, char_sim=%.3f",
        result.editing_effort,
        result.interpretation,
        result.word_diff_ratio,
        result.char_similarity,
    )

    return result.to_dict()
