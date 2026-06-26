"""Focused tests for Daily Sprint auto-start helpers in Practice_By_Category."""

from __future__ import annotations

import ast
import os
import random
import sys
import time
from collections import defaultdict
from copy import deepcopy

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

EXAM_NAME = "Salesforce Certified Platform Administrator"
DOMAIN = "Security and Access"
PRACTICE_PAGE = os.path.join(ROOT, "pages", "Practice_By_Category.py")

HELPER_NAMES = {
    "DAILY_SPRINT_QUESTION_COUNT",
    "DAILY_SPRINT_AUTO_START_GUARD",
    "build_available_categories",
    "select_practice_questions",
    "initialize_practice_session",
    "maybe_auto_start_daily_sprint",
}


class FakeSessionState(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _load_practice_helpers():
    with open(PRACTICE_PAGE, encoding="utf-8") as handle:
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
        "defaultdict": defaultdict,
        "random": random,
        "time": time,
    }
    exec(compile(module_source, PRACTICE_PAGE, "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def helpers():
    return _load_practice_helpers()


def _make_question(qid, category=DOMAIN, difficulty="medium"):
    opts = [
        {"id": f"{qid}-a", "text": "A", "is_correct": True},
        {"id": f"{qid}-b", "text": "B", "is_correct": False},
    ]
    return {
        "id": qid,
        "exam_name": EXAM_NAME,
        "language_code": "en",
        "category": category,
        "difficulty": difficulty,
        "question": f"Q{qid}",
        "type": "single",
        "select_count": 1,
        "explanation": "Because.",
        "options": deepcopy(opts),
        "correct_ids": [f"{qid}-a"],
    }


def _build_bank(count=12, category=DOMAIN):
    difficulties = (["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4)[:count]
    return [_make_question(i, category=category, difficulty=d) for i, d in enumerate(difficulties)]


@pytest.fixture
def session_state():
    return FakeSessionState()


def test_valid_daily_sprint_deep_link_auto_starts(helpers, session_state):
    bank = _build_bank(12)
    domains = [DOMAIN]
    reruns = []

    started = helpers["maybe_auto_start_daily_sprint"](
        is_daily_sprint=True,
        daily_sprint_exam_name=EXAM_NAME,
        daily_sprint_category=DOMAIN,
        premium=True,
        exam_names=[EXAM_NAME],
        question_bank=bank,
        domains=domains,
        language_code="en",
        session_state=session_state,
        rerun_fn=lambda: reruns.append(True),
    )

    assert started is True
    assert reruns == [True]
    assert session_state["practice_started"] is True
    assert session_state["practice_mode_label"] == "Daily Sprint"
    assert session_state["practice_count"] == helpers["DAILY_SPRINT_QUESTION_COUNT"]
    assert len(session_state["practice_questions"]) == 10
    assert session_state[helpers["DAILY_SPRINT_AUTO_START_GUARD"]] is True


def test_rerun_does_not_start_second_session(helpers, session_state):
    bank = _build_bank(12)
    reruns = []

    helpers["maybe_auto_start_daily_sprint"](
        is_daily_sprint=True,
        daily_sprint_exam_name=EXAM_NAME,
        daily_sprint_category=DOMAIN,
        premium=True,
        exam_names=[EXAM_NAME],
        question_bank=bank,
        domains=[DOMAIN],
        language_code="en",
        session_state=session_state,
        rerun_fn=lambda: reruns.append(True),
    )
    first_questions = list(session_state["practice_questions"])

    started_again = helpers["maybe_auto_start_daily_sprint"](
        is_daily_sprint=True,
        daily_sprint_exam_name=EXAM_NAME,
        daily_sprint_category=DOMAIN,
        premium=True,
        exam_names=[EXAM_NAME],
        question_bank=bank,
        domains=[DOMAIN],
        language_code="en",
        session_state=session_state,
        rerun_fn=lambda: reruns.append(True),
    )

    assert started_again is False
    assert reruns == [True]
    assert session_state["practice_questions"] == first_questions


def test_invalid_category_falls_back_safely(helpers, session_state):
    bank = _build_bank(12)
    reruns = []

    started = helpers["maybe_auto_start_daily_sprint"](
        is_daily_sprint=True,
        daily_sprint_exam_name=EXAM_NAME,
        daily_sprint_category="Nonexistent Domain",
        premium=True,
        exam_names=[EXAM_NAME],
        question_bank=bank,
        domains=[DOMAIN],
        language_code="en",
        session_state=session_state,
        rerun_fn=lambda: reruns.append(True),
    )

    assert started is False
    assert reruns == []
    assert session_state.get("practice_started") is None
    assert helpers["DAILY_SPRINT_AUTO_START_GUARD"] not in session_state


def test_non_daily_sprint_flow_unchanged(helpers, session_state):
    bank = _build_bank(12)
    reruns = []

    started = helpers["maybe_auto_start_daily_sprint"](
        is_daily_sprint=False,
        daily_sprint_exam_name=EXAM_NAME,
        daily_sprint_category=DOMAIN,
        premium=True,
        exam_names=[EXAM_NAME],
        question_bank=bank,
        domains=[DOMAIN],
        language_code="en",
        session_state=session_state,
        rerun_fn=lambda: reruns.append(True),
    )

    assert started is False
    assert reruns == []
    assert session_state.get("practice_started") is None

    random.seed(7)
    selected = helpers["select_practice_questions"](bank, DOMAIN, 10)
    helpers["initialize_practice_session"](
        selected,
        DOMAIN,
        10,
        EXAM_NAME,
        "en",
        "Practice by Category",
        session_state,
    )

    assert session_state["practice_mode_label"] == "Practice by Category"
    assert session_state["practice_count"] == 10
    assert len(session_state["practice_questions"]) == 10
