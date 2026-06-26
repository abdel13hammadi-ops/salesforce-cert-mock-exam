"""Focused tests for Daily Sprint completion UX in Practice_By_Category."""

from __future__ import annotations

import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PRACTICE_PAGE = os.path.join(ROOT, "pages", "Practice_By_Category.py")

HELPER_NAMES = {
    "DAILY_SPRINT_MODE_LABEL",
    "DAILY_SPRINT_DASHBOARD_PAGE",
    "is_daily_sprint_session",
    "practice_results_heading",
    "format_practice_score_metric",
    "format_practice_correct_metric",
    "build_practice_completion_view",
}


class FakeSessionState(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _load_practice_completion_helpers():
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
    namespace: dict = {}
    exec(compile(module_source, PRACTICE_PAGE, "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def helpers():
    return _load_practice_completion_helpers()


def test_sprint_specific_completion_heading(helpers):
    session_state = FakeSessionState({"practice_mode_label": helpers["DAILY_SPRINT_MODE_LABEL"]})
    view = helpers["build_practice_completion_view"](80.0, 8, 10, session_state)

    assert view["heading"] == "Daily Sprint Complete"


def test_score_and_correct_count_display(helpers):
    session_state = FakeSessionState({"practice_mode_label": helpers["DAILY_SPRINT_MODE_LABEL"]})
    view = helpers["build_practice_completion_view"](70.0, 7, 10, session_state)

    assert view["score_metric"] == "70.0%"
    assert view["correct_metric"] == "7 / 10"


def test_dashboard_return_action(helpers):
    session_state = FakeSessionState({"practice_mode_label": helpers["DAILY_SPRINT_MODE_LABEL"]})
    view = helpers["build_practice_completion_view"](90.0, 9, 10, session_state)

    assert view["show_dashboard_return"] is True
    assert view["dashboard_path"] == helpers["DAILY_SPRINT_DASHBOARD_PAGE"]
    assert view["dashboard_label"] == "Back to Dashboard"


def test_preserved_review_flow(helpers):
    session_state = FakeSessionState({"practice_mode_label": helpers["DAILY_SPRINT_MODE_LABEL"]})
    view = helpers["build_practice_completion_view"](60.0, 6, 10, session_state)

    assert view["review_heading"] == "Answer Review"


def test_non_sprint_results_behavior_unchanged(helpers):
    session_state = FakeSessionState({"practice_mode_label": "Practice by Category"})
    view = helpers["build_practice_completion_view"](85.0, 17, 20, session_state)

    assert view["heading"] == "Practice Results"
    assert view["score_metric"] == "85.0%"
    assert view["correct_metric"] == "17 / 20"
    assert view["review_heading"] == "Answer Review"
    assert view["show_dashboard_return"] is False
    assert view["show_primary_start_new_practice"] is True
