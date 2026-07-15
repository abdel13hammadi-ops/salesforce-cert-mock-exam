"""Canonical learner activity-mode identities for exam_attempts.mode.

These string values are persisted in production data and enforced by the
chk_exam_attempts_mode database constraint.  Do not change the literal values
without a migration.
"""

from __future__ import annotations

from typing import FrozenSet, Tuple

PAID_MOCK_EXAM = "Paid Mock Exam"
FREE_MOCK_EXAM = "Free Mock Exam"
PRACTICE_BY_CATEGORY = "Practice by Category"
WEAK_AREAS_PRACTICE = "Weak Areas Practice"
DAILY_SPRINT = "Daily Sprint"

ALL_ACTIVITY_MODES: Tuple[str, ...] = (
    PAID_MOCK_EXAM,
    FREE_MOCK_EXAM,
    PRACTICE_BY_CATEGORY,
    WEAK_AREAS_PRACTICE,
    DAILY_SPRINT,
)

# Full-length paid mocks used for readiness scoring and verified-mock metrics.
READINESS_ELIGIBLE_MODES: FrozenSet[str] = frozenset({PAID_MOCK_EXAM})

# Modes whose question-level child rows may inform weak-area domain evidence.
WEAK_AREA_EVIDENCE_MODES: FrozenSet[str] = frozenset({
    PAID_MOCK_EXAM,
    DAILY_SPRINT,
    PRACTICE_BY_CATEGORY,
    WEAK_AREAS_PRACTICE,
    FREE_MOCK_EXAM,
})


def normalized_mode(mode: object) -> str:
    """Return a case-insensitive comparison key for an activity mode."""
    return str(mode or "").strip().lower()


def is_readiness_eligible_mode(mode: object) -> bool:
    """Return True when mode is a full paid-mock identity (case-insensitive)."""
    return normalized_mode(mode) == normalized_mode(PAID_MOCK_EXAM)
