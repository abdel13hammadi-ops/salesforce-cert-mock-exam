"""Focused tests for V46 Phase 1 free-mock curation workflow."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Dict, List
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.free_mock_curation import (
    ADM_EXAM_NAME,
    BA_EXAM_NAME,
    FREE_MOCK_MIN_MULTI_SELECT,
    FREE_MOCK_SLOT_COUNT,
    FREE_MOCK_CURATION_SETUP_MESSAGE,
    FreeMockCurationSetupError,
    blueprint_total,
    compare_domain_counts,
    count_multi_select,
    create_draft,
    get_blueprint,
    get_curation_state,
    is_missing_curation_backend_error,
    normalize_draft_items,
    publish_draft,
    replace_draft_items,
    validate_draft,
    validate_draft_items_local,
    validate_question_eligibility,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT / "supabase" / "migrations" / "20260629120000_v46_free_mock_curation_foundation.sql"
)
PERMISSION_FIX_MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260629123000_v46_restrict_free_mock_rpc_permissions.sql"
)

FREE_MOCK_RPC_SIGNATURES = (
    "free_mock_blueprint_v1(text)",
    "validate_free_mock_question_eligibility_v1(integer, text, text)",
    "collect_free_mock_draft_failures_v1(uuid)",
    "create_free_mock_draft_v1(text, text, text)",
    "replace_free_mock_draft_items_v1(uuid, jsonb, text)",
    "validate_free_mock_draft_v1(uuid)",
    "publish_free_mock_draft_v1(uuid, text, text)",
    "get_free_mock_curation_state_v1(text, text)",
)
APP_PATH = REPO_ROOT / "app.py"
ACCESS_CONTROL_PATH = REPO_ROOT / "utils" / "access_control.py"
ADMIN_PAGE_PATH = REPO_ROOT / "pages" / "Admin_Free_Mock_Curation.py"


def _make_question(
    qid: int,
    *,
    exam_name: str,
    category: str,
    question_type: str = "single",
    select_count=1,
    explanation: str = "Because this is correct.",
    mock_eligible: bool = True,
) -> dict:
    return {
        "id": qid,
        "exam_name": exam_name,
        "language_code": "en",
        "category": category,
        "question_type": question_type,
        "select_count": select_count,
        "explanation": explanation,
        "is_active": True,
        "is_exam_eligible": True,
        "mock_eligible": mock_eligible,
        "quality_status": "approved",
    }


def _make_options(qid: int, *, question_type: str = "single", select_count: int = 1) -> List[dict]:
    if question_type == "single":
        return [
            {"question_id": qid, "option_label": "A", "option_text": f"A-{qid}", "is_correct": True},
            {"question_id": qid, "option_label": "B", "option_text": f"B-{qid}", "is_correct": False},
            {"question_id": qid, "option_label": "C", "option_text": f"C-{qid}", "is_correct": False},
        ]
    labels = ["A", "B", "C", "D", "E"]
    opts = []
    for idx, label in enumerate(labels):
        opts.append(
            {
                "question_id": qid,
                "option_label": label,
                "option_text": f"{label}-{qid}",
                "is_correct": idx < select_count,
            }
        )
    return opts


def _build_valid_adm_items() -> tuple:
    blueprint = get_blueprint(ADM_EXAM_NAME)
    questions_by_id: Dict[int, dict] = {}
    options_by_question: Dict[int, List[dict]] = {}
    items = []
    slot = 1
    next_id = 1000
    multi_assigned = 0

    for domain, count in blueprint.items():
        for _ in range(count):
            qtype = "multiple" if multi_assigned < FREE_MOCK_MIN_MULTI_SELECT else "single"
            sc = 2 if qtype == "multiple" else 1
            q = _make_question(next_id, exam_name=ADM_EXAM_NAME, category=domain, question_type=qtype, select_count=sc)
            questions_by_id[next_id] = q
            options_by_question[next_id] = _make_options(next_id, question_type=qtype, select_count=sc)
            items.append({"slot_order": slot, "question_id": next_id, "domain_name": domain})
            if qtype == "multiple":
                multi_assigned += 1
            slot += 1
            next_id += 1

    return items, questions_by_id, options_by_question


def _build_valid_ba_items() -> tuple:
    blueprint = get_blueprint(BA_EXAM_NAME)
    questions_by_id: Dict[int, dict] = {}
    options_by_question: Dict[int, List[dict]] = {}
    items = []
    slot = 1
    next_id = 2000
    multi_assigned = 0

    for domain, count in blueprint.items():
        for _ in range(count):
            qtype = "multiple" if multi_assigned < FREE_MOCK_MIN_MULTI_SELECT else "single"
            sc = 2 if qtype == "multiple" else 1
            q = _make_question(next_id, exam_name=BA_EXAM_NAME, category=domain, question_type=qtype, select_count=sc)
            questions_by_id[next_id] = q
            options_by_question[next_id] = _make_options(next_id, question_type=qtype, select_count=sc)
            items.append({"slot_order": slot, "question_id": next_id, "domain_name": domain})
            if qtype == "multiple":
                multi_assigned += 1
            slot += 1
            next_id += 1

    return items, questions_by_id, options_by_question


class TestBlueprints(unittest.TestCase):
    def test_adm_blueprint_totals_15(self):
        self.assertEqual(blueprint_total(ADM_EXAM_NAME), FREE_MOCK_SLOT_COUNT)

    def test_ba_blueprint_totals_15(self):
        self.assertEqual(blueprint_total(BA_EXAM_NAME), FREE_MOCK_SLOT_COUNT)


class TestLocalValidation(unittest.TestCase):
    def test_valid_adm_draft_passes(self):
        items, qmap, omap = _build_valid_adm_items()
        valid, failures = validate_draft_items_local(items, qmap, omap, exam_name=ADM_EXAM_NAME)
        self.assertTrue(valid, failures)
        self.assertEqual(failures, [])

    def test_valid_ba_draft_passes(self):
        items, qmap, omap = _build_valid_ba_items()
        valid, failures = validate_draft_items_local(items, qmap, omap, exam_name=BA_EXAM_NAME)
        self.assertTrue(valid, failures)

    def test_exactly_15_items_required(self):
        items, qmap, omap = _build_valid_adm_items()
        short = items[:14]
        valid, failures = validate_draft_items_local(short, qmap, omap, exam_name=ADM_EXAM_NAME)
        self.assertFalse(valid)
        self.assertTrue(any(f["code"] == "ITEM_COUNT" for f in failures))

    def test_unique_question_ids_required(self):
        items, qmap, omap = _build_valid_adm_items()
        dup = list(items)
        dup[-1] = dict(dup[-2])
        valid, failures = validate_draft_items_local(dup, qmap, omap, exam_name=ADM_EXAM_NAME)
        self.assertFalse(valid)
        self.assertTrue(any(f["code"] == "DUPLICATE_QUESTION" for f in failures))

    def test_slot_order_one_through_fifteen_required(self):
        items, qmap, omap = _build_valid_adm_items()
        broken = list(items)
        broken[0] = dict(broken[0], slot_order=16)
        valid, failures = validate_draft_items_local(broken, qmap, omap, exam_name=ADM_EXAM_NAME)
        self.assertFalse(valid)
        codes = {f["code"] for f in failures}
        self.assertTrue("MISSING_SLOT" in codes or "SLOT_OUT_OF_RANGE" in codes or "DUPLICATE_SLOT" in codes)

    def test_adm_domain_blueprint_enforced(self):
        items, qmap, omap = _build_valid_adm_items()
        wrong = list(items)
        wrong[0] = dict(wrong[0], domain_name="Automation")
        qmap[wrong[0]["question_id"]] = _make_question(
            wrong[0]["question_id"],
            exam_name=ADM_EXAM_NAME,
            category="Automation",
        )
        valid, failures = validate_draft_items_local(wrong, qmap, omap, exam_name=ADM_EXAM_NAME)
        self.assertFalse(valid)
        self.assertTrue(any(f["code"] == "DOMAIN_COUNT" for f in failures))

    def test_ba_domain_blueprint_enforced(self):
        items, qmap, omap = _build_valid_ba_items()
        qid = items[0]["question_id"]
        qmap[qid] = _make_question(qid, exam_name=BA_EXAM_NAME, category="Requirements")
        items[0] = dict(items[0], domain_name="Requirements")
        valid, failures = validate_draft_items_local(items, qmap, omap, exam_name=BA_EXAM_NAME)
        self.assertFalse(valid)
        self.assertTrue(any(f["code"] == "DOMAIN_COUNT" for f in failures))

    def test_minimum_two_multiselect(self):
        items, qmap, omap = _build_valid_adm_items()
        for item in items:
            qid = item["question_id"]
            qmap[qid] = _make_question(
                qid,
                exam_name=ADM_EXAM_NAME,
                category=item["domain_name"],
                question_type="single",
                select_count=1,
            )
            omap[qid] = _make_options(qid, question_type="single", select_count=1)
        valid, failures = validate_draft_items_local(items, qmap, omap, exam_name=ADM_EXAM_NAME)
        self.assertFalse(valid)
        self.assertTrue(any(f["code"] == "MIN_MULTI_SELECT" for f in failures))

    def test_explanation_required(self):
        q = _make_question(42, exam_name=ADM_EXAM_NAME, category="Automation", explanation="")
        failures = validate_question_eligibility(q, _make_options(42), exam_name=ADM_EXAM_NAME)
        self.assertTrue(any(f["code"] == "MISSING_EXPLANATION" for f in failures))

    def test_eligibility_flags_required(self):
        q = _make_question(43, exam_name=ADM_EXAM_NAME, category="Automation")
        q["quality_status"] = "needs_edit"
        failures = validate_question_eligibility(q, _make_options(43), exam_name=ADM_EXAM_NAME)
        self.assertTrue(any(f["code"] == "NOT_APPROVED" for f in failures))

        q2 = _make_question(44, exam_name=ADM_EXAM_NAME, category="Automation", mock_eligible=False)
        failures2 = validate_question_eligibility(q2, _make_options(44), exam_name=ADM_EXAM_NAME)
        self.assertTrue(any(f["code"] == "NOT_MOCK_ELIGIBLE" for f in failures2))


class TestDraftHelpers(unittest.TestCase):
    def test_normalize_draft_items_sorts_by_slot(self):
        items = normalize_draft_items(
            [{"slot_order": 3, "question_id": 1}, {"slot_order": 1, "question_id": 2}]
        )
        self.assertEqual([row["slot_order"] for row in items], [1, 3])

    def test_compare_domain_counts_reports_delta(self):
        items, qmap, _ = _build_valid_adm_items()
        rows = compare_domain_counts(items, qmap, ADM_EXAM_NAME)
        self.assertTrue(all(row["delta"] == 0 for row in rows))

    def test_count_multi_select(self):
        items, qmap, _ = _build_valid_adm_items()
        self.assertGreaterEqual(count_multi_select(items, qmap), FREE_MOCK_MIN_MULTI_SELECT)


class _FakeRpcResult:
    def __init__(self, data):
        self.data = data
        self.error = None


class _FakeRpcBuilder:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return _FakeRpcResult(self._data)


class FakeSupabase:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._responses: dict[str, list] = {}

    def set_response(self, name: str, data: list):
        self._responses[name] = data

    def rpc(self, name: str, params: dict):
        self.calls.append((name, params))
        return _FakeRpcBuilder(self._responses.get(name, []))


class TestRpcWrappers(unittest.TestCase):
    def test_create_draft_calls_rpc(self):
        client = FakeSupabase()
        client.set_response(
            "create_free_mock_draft_v1",
            [{"free_mock_set_id": "set-1", "created": True}],
        )
        row = create_draft(
            client,
            exam_name=ADM_EXAM_NAME,
            language_code="en",
            actor_email="admin@certbound.test",
        )
        self.assertEqual(row["free_mock_set_id"], "set-1")
        self.assertEqual(client.calls[0][0], "create_free_mock_draft_v1")

    def test_replace_draft_items_payload(self):
        client = FakeSupabase()
        client.set_response("replace_free_mock_draft_items_v1", [{"item_count": 2}])
        count = replace_draft_items(
            client,
            set_id="set-1",
            items=[{"slot_order": 1, "question_id": 10}, {"slot_order": 2, "question_id": 11}],
            actor_email="admin@certbound.test",
        )
        self.assertEqual(count, 2)
        self.assertEqual(client.calls[0][1]["p_items"][0]["question_id"], 10)

    def test_validate_draft_returns_failures(self):
        client = FakeSupabase()
        client.set_response(
            "validate_free_mock_draft_v1",
            [{"valid": False, "failures": [{"code": "ITEM_COUNT", "message": "expected 15"}]}],
        )
        result = validate_draft(client, set_id="set-1")
        self.assertFalse(result["valid"])
        self.assertEqual(result["failures"][0]["code"], "ITEM_COUNT")

    def test_publish_draft_returns_version(self):
        client = FakeSupabase()
        client.set_response(
            "publish_free_mock_draft_v1",
            [{"version_number": 2, "retired_set_id": "old-set"}],
        )
        result = publish_draft(
            client,
            set_id="set-1",
            actor_email="admin@certbound.test",
            reason="Initial curated set",
        )
        self.assertEqual(result["version_number"], 2)

    def test_get_curation_state_splits_draft_and_published(self):
        client = FakeSupabase()
        client.set_response(
            "get_free_mock_curation_state_v1",
            [
                {"status": "published", "set_id": "pub", "version_number": 1, "items": []},
                {"status": "draft", "set_id": "draft", "items": []},
            ],
        )
        state = get_curation_state(client, exam_name=ADM_EXAM_NAME)
        self.assertEqual(state["published"]["set_id"], "pub")
        self.assertEqual(state["draft"]["set_id"], "draft")


class TestMigrationContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.is_file())

    def test_schema_objects_present(self):
        for token in (
            "CREATE TABLE IF NOT EXISTS public.free_mock_sets",
            "CREATE TABLE IF NOT EXISTS public.free_mock_set_items",
            "idx_free_mock_sets_one_published",
            "idx_free_mock_sets_one_draft",
            "guard_free_mock_set_mutation_v1",
            "guard_free_mock_set_item_mutation_v1",
        ):
            self.assertIn(token, self.sql)

    def test_rpcs_present(self):
        for rpc in (
            "create_free_mock_draft_v1",
            "replace_free_mock_draft_items_v1",
            "validate_free_mock_draft_v1",
            "publish_free_mock_draft_v1",
            "get_free_mock_curation_state_v1",
            "collect_free_mock_draft_failures_v1",
        ):
            self.assertIn(rpc, self.sql)

    def test_atomic_publish_retires_prior_set(self):
        self.assertIn("status = 'retired'", self.sql)
        self.assertIn("publish blocked", self.sql)

    def test_immutable_published_sets(self):
        self.assertIn("published free_mock_set", self.sql)
        self.assertIn("cannot modify items on", self.sql)
        self.assertIn("OLD.retired_at IS NULL", self.sql)
        self.assertIn("NEW.updated_at = OLD.updated_at", self.sql)

    def test_replace_validates_before_delete(self):
        self.assertIn("Validate the full payload before deleting existing draft items", self.sql)
        self.assertIn("duplicate slot_order", self.sql)
        self.assertIn("duplicate question_id", self.sql)
        self.assertIn("belongs to", self.sql)

    def test_publish_uses_advisory_lock(self):
        self.assertIn("pg_advisory_xact_lock", self.sql)
        self.assertIn("free_mock_publish", self.sql)

    def test_foundation_helper_rpcs_lack_explicit_anon_authenticated_revokes(self):
        """Additive permission fix must stay separate; foundation keeps the original gap."""
        helper_signatures = (
            "free_mock_blueprint_v1(text)",
            "validate_free_mock_question_eligibility_v1(integer, text, text)",
            "collect_free_mock_draft_failures_v1(uuid)",
        )
        for signature in helper_signatures:
            self.assertIn(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC;", self.sql)
            self.assertNotIn(
                f"REVOKE EXECUTE ON FUNCTION public.{signature} FROM anon;",
                self.sql,
            )
            self.assertNotIn(
                f"REVOKE EXECUTE ON FUNCTION public.{signature} FROM authenticated;",
                self.sql,
            )


class TestFreeMockRpcPermissions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = PERMISSION_FIX_MIGRATION_PATH.read_text(encoding="utf-8")

    def test_permission_fix_migration_file_exists(self):
        self.assertTrue(PERMISSION_FIX_MIGRATION_PATH.is_file())

    def test_all_eight_functions_revoke_public_anon_authenticated(self):
        for signature in FREE_MOCK_RPC_SIGNATURES:
            with self.subTest(signature=signature):
                self.assertIn(
                    f"REVOKE EXECUTE ON FUNCTION public.{signature} FROM PUBLIC;",
                    self.sql,
                )
                self.assertIn(
                    f"REVOKE EXECUTE ON FUNCTION public.{signature} FROM anon;",
                    self.sql,
                )
                self.assertIn(
                    f"REVOKE EXECUTE ON FUNCTION public.{signature} FROM authenticated;",
                    self.sql,
                )

    def test_all_eight_functions_grant_execute_to_service_role(self):
        for signature in FREE_MOCK_RPC_SIGNATURES:
            with self.subTest(signature=signature):
                self.assertIn(
                    f"GRANT EXECUTE ON FUNCTION public.{signature} TO service_role;",
                    self.sql,
                )


class TestSetupSafety(unittest.TestCase):
    def test_missing_backend_error_detection(self):
        self.assertTrue(is_missing_curation_backend_error(Exception("function create_free_mock_draft_v1 does not exist")))
        self.assertFalse(is_missing_curation_backend_error(Exception("publish blocked")))

    def test_setup_message_constant(self):
        self.assertIn("20260629120000_v46_free_mock_curation_foundation.sql", FREE_MOCK_CURATION_SETUP_MESSAGE)


class TestPhase1Isolation(unittest.TestCase):
    def test_admin_page_exists_and_read_only(self):
        source = ADMIN_PAGE_PATH.read_text(encoding="utf-8")
        self.assertIn("Admin Free Mock Curation", source)
        self.assertIn("Question content is not editable", source)
        self.assertIn("FREE_MOCK_CURATION_SETUP_MESSAGE", source)
        self.assertIn("FreeMockCurationSetupError", source)
        self.assertNotIn("update_question", source)
        self.assertNotIn("free_mock_exam", source)

    def test_admin_page_uses_service_role_client(self):
        source = ADMIN_PAGE_PATH.read_text(encoding="utf-8")
        self.assertIn("get_supabase_admin_client", source)
        self.assertIn("require_admin()", source)
        self.assertNotIn("get_supabase_user_client", source)

    def test_admin_sidebar_link(self):
        source = ACCESS_CONTROL_PATH.read_text(encoding="utf-8")
        self.assertIn("pages/Admin_Free_Mock_Curation.py", source)
        self.assertIn("Free Mock Curation", source)
        self.assertIn("is_admin_unlocked()", source)

    def test_learner_runtime_still_expects_ten_questions(self):
        source = APP_PATH.read_text(encoding="utf-8")
        self.assertIn("len(selected) != 10", source)
        self.assertIn("generate_free_mock_questions", source)
        self.assertNotIn("free_mock_sets", source)
        self.assertNotIn("Admin_Free_Mock_Curation", source)


if __name__ == "__main__":
    unittest.main()
