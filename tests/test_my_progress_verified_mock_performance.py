"""Focused tests for verified mock performance metrics in My Progress."""

from __future__ import annotations

import ast
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MY_PROGRESS_PAGE = os.path.join(ROOT, "pages", "My_Progress.py")

HELPER_NAMES = {
    "VERIFIED_MOCK_PERFORMANCE_HEADER",
    "VERIFIED_MOCK_PERFORMANCE_EMPTY_MESSAGE",
    "_safe_float",
    "_safe_int",
    "_parse_dt",
    "get_correct_count",
    "format_user_datetime",
    "filter_readiness_attempts",
    "build_verified_mock_performance_metrics",
    "build_attempt_history_rows",
}


def _load_my_progress_helpers():
    with open(MY_PROGRESS_PAGE, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    selected_nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {getattr(t, "id", None) for t in node.targets}
            if targets & HELPER_NAMES:
                selected_nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in HELPER_NAMES:
            selected_nodes.append(node)
    module_source = ast.unparse(ast.Module(body=selected_nodes, type_ignores=[]))
    namespace = {
        "Any": __import__("typing").Any,
        "Dict": __import__("typing").Dict,
        "List": __import__("typing").List,
        "datetime": datetime,
        "timezone": timezone,
        "ZoneInfo": ZoneInfo,
    }
    exec(compile(module_source, MY_PROGRESS_PAGE, "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def helpers():
    return _load_my_progress_helpers()


def _attempt(
    attempt_id: int,
    mode: str,
    score: float,
    *,
    total_questions: int = 60,
    completed_at: str = "2026-06-24T12:00:00+00:00",
    category: str = "Security and Access",
):
    return {
        "id": attempt_id,
        "mode": mode,
        "score": score,
        "total_questions": total_questions,
        "correct_answers": int(score * total_questions / 100),
        "completed_at": completed_at,
        "started_at": completed_at,
        "category": category,
        "language_code": "en",
    }


def test_daily_sprint_does_not_affect_mock_performance_metrics(helpers):
    attempts = [
        _attempt(1, "Daily Sprint", 100.0, total_questions=10, completed_at="2026-06-25T12:00:00+00:00"),
        _attempt(2, "Paid Mock Exam", 75.0, completed_at="2026-06-24T12:00:00+00:00"),
    ]
    metrics = helpers["build_verified_mock_performance_metrics"](attempts, 60)

    assert metrics["has_verified_mocks"] is True
    assert metrics["latest_score"] == 75.0
    assert metrics["average_score"] == 75.0
    assert metrics["best_score"] == 75.0
    assert metrics["verified_mock_count"] == 1


def test_practice_modes_do_not_affect_mock_performance_metrics(helpers):
    attempts = [
        _attempt(1, "Practice by Category", 90.0, total_questions=10),
        _attempt(2, "Weak Areas Practice", 85.0, total_questions=10),
        _attempt(3, "Free Mock Exam", 80.0, total_questions=10),
        _attempt(4, "Paid Mock Exam", 70.0, total_questions=60),
    ]
    metrics = helpers["build_verified_mock_performance_metrics"](attempts, 60)

    assert metrics["verified_mock_count"] == 1
    assert metrics["latest_score"] == 70.0
    assert metrics["average_score"] == 70.0
    assert metrics["best_score"] == 70.0


def test_verified_paid_mocks_populate_four_metrics(helpers):
    attempts = [
        _attempt(1, "Paid Mock Exam", 82.0, completed_at="2026-06-25T12:00:00+00:00"),
        _attempt(2, "Paid Mock Exam", 76.0, completed_at="2026-06-24T12:00:00+00:00"),
        _attempt(3, "Paid Mock Exam", 88.0, completed_at="2026-06-23T12:00:00+00:00"),
    ]
    metrics = helpers["build_verified_mock_performance_metrics"](attempts, 60)

    assert metrics["has_verified_mocks"] is True
    assert metrics["latest_score"] == 82.0
    assert metrics["average_score"] == round((82.0 + 76.0 + 88.0) / 3, 2)
    assert metrics["best_score"] == 88.0
    assert metrics["verified_mock_count"] == 3


def test_invalid_non_qualifying_paid_mocks_are_excluded(helpers):
    attempts = [
        _attempt(1, "Paid Mock Exam", 95.0, total_questions=30),
        _attempt(2, "Paid Mock Exam", 72.0, total_questions=60),
    ]
    metrics = helpers["build_verified_mock_performance_metrics"](attempts, 60)

    assert metrics["verified_mock_count"] == 1
    assert metrics["latest_score"] == 72.0


def test_attempt_history_remains_unfiltered(helpers):
    attempts = [
        _attempt(1, "Daily Sprint", 100.0, total_questions=10),
        _attempt(2, "Practice by Category", 80.0, total_questions=10),
        _attempt(3, "Paid Mock Exam", 75.0, total_questions=60),
    ]
    rows = helpers["build_attempt_history_rows"](attempts, "UTC")

    assert len(rows) == 3
    assert {row["Mode"] for row in rows} == {
        "Daily Sprint",
        "Practice by Category",
        "Paid Mock Exam",
    }


def test_empty_state_when_no_verified_paid_mocks(helpers):
    attempts = [
        _attempt(1, "Daily Sprint", 100.0, total_questions=10),
        _attempt(2, "Practice by Category", 80.0, total_questions=10),
    ]
    metrics = helpers["build_verified_mock_performance_metrics"](attempts, 60)

    assert metrics["has_verified_mocks"] is False
    assert metrics["latest_score"] is None
    assert metrics["average_score"] is None
    assert metrics["best_score"] is None
    assert metrics["verified_mock_count"] == 0
    assert metrics["trend_attempts"] == []
    assert helpers["VERIFIED_MOCK_PERFORMANCE_HEADER"] == "Verified Mock Performance"
    assert "Attempt History" in helpers["VERIFIED_MOCK_PERFORMANCE_EMPTY_MESSAGE"]
