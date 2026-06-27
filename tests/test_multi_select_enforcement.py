"""Focused tests for shared multi-select UI enforcement."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.question_answer_key import (
    apply_multi_select_answer_ui,
    build_multi_select_checkbox_plan,
    is_answer_correct,
    normalize_option_entries,
    reconcile_multi_select_selection,
    resolve_required_select_count,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _multiple_question(*, select_count: int, correct_ids: list[str]) -> dict:
    options = [
        {"id": "1", "text": "Alpha"},
        {"id": "2", "text": "Beta"},
        {"id": "3", "text": "Gamma"},
        {"id": "4", "text": "Delta"},
    ]
    for opt in options:
        opt["is_correct"] = opt["id"] in correct_ids
    return {
        "type": "multiple",
        "question_type": "multiple",
        "select_count": select_count,
        "options": options,
        "correct_ids": correct_ids,
    }


class _FakeSessionState(dict):
    pass


class _GuardedSessionState(_FakeSessionState):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.checkbox_instantiated = False
        self.post_checkbox_widget_writes: List[str] = []

    def __setitem__(self, key, value):
        if self.checkbox_instantiated and isinstance(key, str):
            self.post_checkbox_widget_writes.append(key)
        super().__setitem__(key, value)


class _RecordingCheckbox:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, label, *, value=False, disabled=False, key=None):
        self.calls.append(
            {"label": label, "value": value, "disabled": disabled, "key": key}
        )
        if hasattr(self.session_state, "checkbox_instantiated"):
            self.session_state.checkbox_instantiated = True
        if key is not None:
            return bool(self.session_state.get(key, value))
        return bool(value)

    def bind(self, session_state: _FakeSessionState):
        self.session_state = session_state
        return self


class TestMultiSelectEnforcement(unittest.TestCase):
    def test_select_two_cannot_retain_three_selections(self):
        reconciled = reconcile_multi_select_selection(["1", "2", "3"], ["1", "2"], 2)
        self.assertEqual(reconciled, ["1", "2"])

    def test_select_three_cannot_retain_four_selections(self):
        reconciled = reconcile_multi_select_selection(["1", "2", "3", "4"], ["1", "2", "3"], 3)
        self.assertEqual(reconciled, ["1", "2", "3"])

    def test_unchecked_options_become_unavailable_at_limit(self):
        question = _multiple_question(select_count=2, correct_ids=["1", "3"])
        plan = build_multi_select_checkbox_plan(question["options"], ["1", "2"], 2)

        by_id = {item["id"]: item for item in plan}
        self.assertFalse(by_id["1"]["disabled"])
        self.assertFalse(by_id["2"]["disabled"])
        self.assertTrue(by_id["3"]["disabled"])
        self.assertTrue(by_id["4"]["disabled"])

    def test_selected_options_remain_deselectable_at_limit(self):
        plan = build_multi_select_checkbox_plan(
            [{"id": "1", "text": "A"}, {"id": "2", "text": "B"}, {"id": "3", "text": "C"}],
            ["1", "2"],
            2,
        )
        for item in plan:
            if item["checked"]:
                self.assertFalse(item["disabled"])

    def test_deselection_re_enables_unchecked_alternatives(self):
        plan = build_multi_select_checkbox_plan(
            [{"id": "1", "text": "A"}, {"id": "2", "text": "B"}, {"id": "3", "text": "C"}],
            ["1"],
            2,
        )
        by_id = {item["id"]: item for item in plan}
        self.assertFalse(by_id["1"]["disabled"])
        self.assertFalse(by_id["2"]["disabled"])
        self.assertFalse(by_id["3"]["disabled"])

    def test_navigation_rerun_preserves_valid_prior_selection(self):
        session_state = _FakeSessionState(
            {
                "practice_0_1": True,
                "practice_0_2": True,
                "practice_0_3": True,
            }
        )
        checkbox = _RecordingCheckbox().bind(session_state)
        question = _multiple_question(select_count=2, correct_ids=["1", "3"])

        selected = apply_multi_select_answer_ui(
            question,
            previous_selection=["1", "2"],
            key_prefix="practice_0",
            session_state=session_state,
            checkbox_fn=checkbox,
        )

        self.assertEqual(selected, ["1", "2"])
        self.assertFalse(session_state["practice_0_3"])
        self.assertTrue(session_state["practice_0_1"])
        self.assertTrue(session_state["practice_0_2"])

    def test_apply_helper_rejects_extra_widget_selection_without_dropping_arbitrary_prior(self):
        session_state = _FakeSessionState(
            {
                "exam_0_Alpha": True,
                "exam_0_Beta": True,
                "exam_0_Gamma": True,
            }
        )
        checkbox = _RecordingCheckbox().bind(session_state)
        question = {
            "type": "multiple",
            "select_count": 2,
            "options": ["Alpha", "Beta", "Gamma", "Delta"],
        }

        selected = apply_multi_select_answer_ui(
            question,
            previous_selection=["Alpha", "Beta"],
            key_prefix="exam_0",
            session_state=session_state,
            checkbox_fn=checkbox,
        )

        self.assertEqual(selected, ["Alpha", "Beta"])
        self.assertFalse(session_state["exam_0_Gamma"])

    def test_all_ui_entry_points_use_shared_helper(self):
        sources = {
            "app.py": (REPO_ROOT / "app.py").read_text(encoding="utf-8"),
            "Practice_By_Category.py": (REPO_ROOT / "pages" / "Practice_By_Category.py").read_text(encoding="utf-8"),
            "Weak_Areas_Practice.py": (REPO_ROOT / "pages" / "Weak_Areas_Practice.py").read_text(encoding="utf-8"),
        }
        for name, source in sources.items():
            with self.subTest(source=name):
                self.assertIn("apply_multi_select_answer_ui", source)
                self.assertNotIn("cap_multi_select_selection", source)

    def test_scoring_still_requires_exact_set_and_count(self):
        question = _multiple_question(select_count=2, correct_ids=["1", "3"])
        self.assertTrue(is_answer_correct(["1", "3"], question))
        self.assertFalse(is_answer_correct(["1", "2"], question))
        self.assertFalse(is_answer_correct(["1", "2", "3"], question))
        self.assertFalse(is_answer_correct(["1"], question))

    def test_exam_text_options_normalize_to_shared_entries(self):
        entries = normalize_option_entries(["Alpha", "Beta"])
        self.assertEqual(entries, [{"id": "Alpha", "label": "Alpha"}, {"id": "Beta", "label": "Beta"}])

    def test_required_count_comes_from_select_count(self):
        question = _multiple_question(select_count=3, correct_ids=["1", "2", "4"])
        self.assertEqual(resolve_required_select_count(question), 3)

    def test_apply_helper_does_not_sync_widgets_after_checkbox_creation(self):
        source = (REPO_ROOT / "utils" / "question_answer_key.py").read_text(encoding="utf-8")
        fn_body = source.split("def apply_multi_select_answer_ui", 1)[1].split("\ndef ", 1)[0]
        loop_marker = "for item in plan:"
        loop_start = fn_body.index(loop_marker)
        before_loop = fn_body[:loop_start]
        after_loop = fn_body[loop_start:]
        self.assertIn("sync_multi_select_widget_selection", before_loop)
        self.assertNotIn(
            "sync_multi_select_widget_selection",
            after_loop.split("return reconciled", 1)[0],
        )

    def test_select_two_render_does_not_mutate_widget_keys_after_instantiation(self):
        session_state = _GuardedSessionState(
            {
                "practice_0_1": True,
                "practice_0_2": True,
                "practice_0_3": True,
            }
        )
        checkbox = _RecordingCheckbox().bind(session_state)
        question = _multiple_question(select_count=2, correct_ids=["1", "3"])

        selected = apply_multi_select_answer_ui(
            question,
            previous_selection=["1", "2"],
            key_prefix="practice_0",
            session_state=session_state,
            checkbox_fn=checkbox,
        )

        self.assertEqual(selected, ["1", "2"])
        self.assertEqual(session_state.post_checkbox_widget_writes, [])

    def test_restored_practice_state_with_valid_multi_select_answers_renders_safely(self):
        session_state = _GuardedSessionState()
        checkbox = _RecordingCheckbox().bind(session_state)
        question = _multiple_question(select_count=2, correct_ids=["1", "3"])

        selected = apply_multi_select_answer_ui(
            question,
            previous_selection=["1", "3"],
            key_prefix="practice_2",
            session_state=session_state,
            checkbox_fn=checkbox,
        )

        self.assertEqual(selected, ["1", "3"])
        self.assertEqual(session_state.post_checkbox_widget_writes, [])
        self.assertTrue(session_state["practice_2_1"])
        self.assertTrue(session_state["practice_2_3"])

    def test_restored_stale_overflow_state_is_normalized_before_render(self):
        session_state = _GuardedSessionState(
            {
                "weak_4_1": True,
                "weak_4_2": True,
                "weak_4_3": True,
                "weak_4_4": True,
            }
        )
        checkbox = _RecordingCheckbox().bind(session_state)
        question = _multiple_question(select_count=3, correct_ids=["1", "2", "4"])

        selected = apply_multi_select_answer_ui(
            question,
            previous_selection=["1", "2", "3"],
            key_prefix="weak_4",
            session_state=session_state,
            checkbox_fn=checkbox,
        )

        self.assertEqual(selected, ["1", "2", "3"])
        self.assertFalse(session_state["weak_4_4"])
        self.assertEqual(session_state.post_checkbox_widget_writes, [])


if __name__ == "__main__":
    unittest.main()
