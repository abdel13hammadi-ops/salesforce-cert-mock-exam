"""
Tests for the deterministic duplicate-question stem detector.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.duplicate_question_detector import (
    DETECTION_METHOD_EXACT,
    DETECTION_METHOD_NEAR_EXACT,
    EXACT_NORMALIZED_THRESHOLD,
    FINDING_CODE_EXACT,
    FINDING_CODE_NEAR_EXACT,
    NEAR_EXACT_LEXICAL_THRESHOLD,
    canonical_pair_ids,
    dedupe_duplicate_findings,
    detect_duplicate_question_stems,
    filter_new_duplicate_findings,
    normalize_question_stem,
    orchestrate_certification_duplicate_audit,
    pair_dedupe_key,
    pair_key_from_finding,
)


def _row(qvid: str, cert: str, text: str) -> dict:
    return {
        "question_version_id": qvid,
        "certification_exam_name": cert,
        "question_text": text,
    }


class TestNormalizeQuestionStem(unittest.TestCase):
    def test_case_and_punctuation(self):
        left = normalize_question_stem("What is Salesforce Flow?")
        right = normalize_question_stem("what   is salesforce flow")
        self.assertEqual(left, right)

    def test_unicode_nfkc(self):
        left = normalize_question_stem("caf\u00e9")
        right = normalize_question_stem("caf\u00e9")
        self.assertEqual(left, "café")


class TestExactDuplicateDetection(unittest.TestCase):
    def test_exact_duplicates(self):
        rows = [
            _row("11111111-0000-0000-0000-000000000001", "Platform Admin", "What is Flow?"),
            _row("22222222-0000-0000-0000-000000000002", "Platform Admin", "What is Flow?"),
        ]
        findings = detect_duplicate_question_stems(rows)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["finding_code"], FINDING_CODE_EXACT)
        meta = finding["metadata"]
        self.assertEqual(meta["detection_method"], DETECTION_METHOD_EXACT)
        self.assertEqual(meta["similarity_score"], EXACT_NORMALIZED_THRESHOLD)
        self.assertEqual(meta["similarity_threshold"], EXACT_NORMALIZED_THRESHOLD)
        self.assertEqual(meta["certification_exam_name"], "Platform Admin")
        self.assertEqual(meta["certification_id"], "Platform Admin")

    def test_punctuation_case_only_duplicates(self):
        rows = [
            _row("11111111-0000-0000-0000-000000000001", "Platform Admin", "What is Flow?"),
            _row("22222222-0000-0000-0000-000000000002", "Platform Admin", "what is flow"),
        ]
        findings = detect_duplicate_question_stems(rows)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["finding_code"], FINDING_CODE_EXACT)


class TestNearExactDuplicateDetection(unittest.TestCase):
    def test_near_exact_duplicates(self):
        rows = [
            _row(
                "11111111-0000-0000-0000-000000000001",
                "Platform Admin",
                "Which automation tool should an admin use to build guided screen flows?",
            ),
            _row(
                "22222222-0000-0000-0000-000000000002",
                "Platform Admin",
                "Which automation tool should an administrator use to build guided screen flows?",
            ),
        ]
        findings = detect_duplicate_question_stems(rows)
        near = [f for f in findings if f["finding_code"] == FINDING_CODE_NEAR_EXACT]
        self.assertEqual(len(near), 1)
        meta = near[0]["metadata"]
        self.assertEqual(meta["detection_method"], DETECTION_METHOD_NEAR_EXACT)
        self.assertGreaterEqual(meta["similarity_score"], NEAR_EXACT_LEXICAL_THRESHOLD)
        self.assertEqual(meta["similarity_threshold"], NEAR_EXACT_LEXICAL_THRESHOLD)


class TestUnrelatedQuestions(unittest.TestCase):
    def test_unrelated_questions_produce_no_findings(self):
        rows = [
            _row("11111111-0000-0000-0000-000000000001", "Platform Admin", "What is Flow?"),
            _row("22222222-0000-0000-0000-000000000002", "Platform Admin", "How do profiles differ from permission sets?"),
        ]
        findings = detect_duplicate_question_stems(rows)
        self.assertEqual(findings, [])


class TestPairDeduplication(unittest.TestCase):
    def test_reversed_pair_deduplication(self):
        rows = [
            _row("aaaaaaaa-0000-0000-0000-000000000001", "Platform Admin", "Duplicate stem text"),
            _row("bbbbbbbb-0000-0000-0000-000000000002", "Platform Admin", "Duplicate stem text"),
        ]
        findings = detect_duplicate_question_stems(rows)
        self.assertEqual(len(findings), 1)
        meta = findings[0]["metadata"]
        self.assertEqual(
            meta["question_version_id_a"],
            "aaaaaaaa-0000-0000-0000-000000000001",
        )
        self.assertEqual(
            meta["question_version_id_b"],
            "bbbbbbbb-0000-0000-0000-000000000002",
        )

    def test_dedupe_duplicate_findings_drops_reversed_method_duplicate(self):
        finding = detect_duplicate_question_stems([
            _row("aaaaaaaa-0000-0000-0000-000000000001", "Platform Admin", "Same stem"),
            _row("bbbbbbbb-0000-0000-0000-000000000002", "Platform Admin", "Same stem"),
        ])[0]
        reversed_copy = {
            **finding,
            "metadata": {
                **finding["metadata"],
                "question_version_id_a": finding["metadata"]["question_version_id_b"],
                "question_version_id_b": finding["metadata"]["question_version_id_a"],
            },
        }
        deduped = dedupe_duplicate_findings([finding, reversed_copy])
        self.assertEqual(len(deduped), 1)


class TestRepeatRunIdempotency(unittest.TestCase):
    def test_repeat_run_idempotency_filters_existing_pairs(self):
        rows = [
            _row("aaaaaaaa-0000-0000-0000-000000000001", "Platform Admin", "Same stem"),
            _row("bbbbbbbb-0000-0000-0000-000000000002", "Platform Admin", "Same stem"),
        ]
        first = detect_duplicate_question_stems(rows)
        second = detect_duplicate_question_stems(rows, existing_findings=first)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_filter_new_duplicate_findings(self):
        rows = [
            _row("aaaaaaaa-0000-0000-0000-000000000001", "Platform Admin", "Same stem"),
            _row("bbbbbbbb-0000-0000-0000-000000000002", "Platform Admin", "Same stem"),
            _row("cccccccc-0000-0000-0000-000000000003", "Platform Admin", "Same stem"),
        ]
        existing = detect_duplicate_question_stems(rows[:2])
        fresh = detect_duplicate_question_stems(rows)
        filtered = filter_new_duplicate_findings(fresh, existing)
        keys = {pair_dedupe_key(
            f["metadata"]["question_version_id_a"],
            f["metadata"]["question_version_id_b"],
            f["metadata"]["detection_method"],
        ) for f in filtered}
        self.assertNotIn(
            pair_dedupe_key(
                "aaaaaaaa-0000-0000-0000-000000000001",
                "bbbbbbbb-0000-0000-0000-000000000002",
                DETECTION_METHOD_EXACT,
            ),
            keys,
        )
        self.assertGreaterEqual(len(filtered), 1)


class TestCertificationIsolation(unittest.TestCase):
    def test_same_stem_different_certifications_do_not_match(self):
        rows = [
            _row("11111111-0000-0000-0000-000000000001", "Platform Admin", "What is Flow?"),
            _row("22222222-0000-0000-0000-000000000002", "Sales Cloud", "What is Flow?"),
        ]
        findings = detect_duplicate_question_stems(rows)
        self.assertEqual(findings, [])


class TestSelfCompareGuard(unittest.TestCase):
    def test_canonical_pair_ids_rejects_self_pair(self):
        with self.assertRaises(ValueError):
            canonical_pair_ids("same-id", "same-id")

    def test_single_row_produces_no_findings(self):
        rows = [_row("11111111-0000-0000-0000-000000000001", "Platform Admin", "Solo stem")]
        self.assertEqual(detect_duplicate_question_stems(rows), [])


class TestScaleSmoke(unittest.TestCase):
    def test_large_bank_completes_without_hard_limit(self):
        cert = "Platform Admin"
        rows = [
            _row(f"{idx:08x}-0000-0000-0000-000000000001", cert, f"Stem variant alpha {idx}")
            for idx in range(1200)
        ]
        rows.append(_row("ffffffff-0000-0000-0000-000000000099", cert, "Stem variant alpha duplicate"))
        rows.append(_row("eeeeeeee-0000-0000-0000-000000000098", cert, "Stem variant alpha duplicate"))
        findings = detect_duplicate_question_stems(rows)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["finding_code"], FINDING_CODE_EXACT)


class FakeSupabase:
    def __init__(self):
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return self

    def execute(self):
        name = self.calls[-1][0]
        if name == "create_audit_run_v1":
            return _FakeResult([{"audit_run_id": "audit-run-001"}])
        if name == "list_duplicate_question_pair_keys_v1":
            return _FakeResult([])
        if name == "complete_audit_run_v1":
            findings = self.calls[-1][1]["p_findings"]
            return _FakeResult([{
                "run_status": "completed",
                "finding_count": len(findings),
                "evidence_count": 0,
            }])
        raise AssertionError(f"unexpected rpc {name!r}")


class StatefulFakeSupabase:
    """Simulates durable pair-key storage across independent orchestration runs."""

    DUPLICATE_CODES = frozenset({FINDING_CODE_EXACT, FINDING_CODE_NEAR_EXACT})

    def __init__(self):
        self.calls = []
        self._audit_run_counter = 0
        self._persisted_pair_keys: set[tuple[str, str, str, str, str]] = set()

    def rpc(self, name, params):
        self.calls.append((name, params))
        return self

    def execute(self):
        name, params = self.calls[-1]
        if name == "create_audit_run_v1":
            self._audit_run_counter += 1
            return _FakeResult([{
                "audit_run_id": f"audit-run-{self._audit_run_counter:03d}",
            }])
        if name == "list_duplicate_question_pair_keys_v1":
            cert = params["p_certification_exam_name"]
            ruleset = params.get("p_ruleset_version") or "1.0.0"
            rows = []
            for key in self._persisted_pair_keys:
                key_cert, id_a, id_b, method, key_ruleset = key
                if key_cert != cert or key_ruleset != ruleset:
                    continue
                rows.append({
                    "question_version_id_a": id_a,
                    "question_version_id_b": id_b,
                    "detection_method": method,
                    "ruleset_version": key_ruleset,
                })
            return _FakeResult(rows)
        if name == "complete_audit_run_v1":
            findings = params["p_findings"]
            inserted_count = 0
            for finding in findings:
                if finding.get("finding_code") not in self.DUPLICATE_CODES:
                    inserted_count += 1
                    continue
                meta = finding.get("metadata") or {}
                key = (
                    meta.get("certification_exam_name", ""),
                    meta.get("question_version_id_a", ""),
                    meta.get("question_version_id_b", ""),
                    meta.get("detection_method", ""),
                    meta.get("ruleset_version", "1.0.0"),
                )
                if not all(key):
                    continue
                if key in self._persisted_pair_keys:
                    continue
                self._persisted_pair_keys.add(key)
                inserted_count += 1
            return _FakeResult([{
                "run_status": "completed",
                "finding_count": inserted_count,
                "evidence_count": 0,
            }])
        raise AssertionError(f"unexpected rpc {name!r}")

    @property
    def persisted_pair_count(self) -> int:
        return len(self._persisted_pair_keys)


class _FakeResult:
    def __init__(self, data):
        self.data = data
        self.error = None


class TestOrchestrationPersistence(unittest.TestCase):
    def test_orchestrate_persists_through_audit_lifecycle(self):
        rows = [
            _row("aaaaaaaa-0000-0000-0000-000000000001", "Platform Admin", "Same stem"),
            _row("bbbbbbbb-0000-0000-0000-000000000002", "Platform Admin", "Same stem"),
        ]
        client = FakeSupabase()
        result = orchestrate_certification_duplicate_audit(
            client,
            rows=rows,
            created_by="test-suite",
        )
        self.assertEqual(result["finding_count"], 1)
        rpc_names = [name for name, _ in client.calls]
        self.assertEqual(rpc_names[0], "create_audit_run_v1")
        self.assertEqual(rpc_names[1], "list_duplicate_question_pair_keys_v1")
        self.assertEqual(rpc_names[2], "complete_audit_run_v1")
        complete_params = client.calls[2][1]
        findings = complete_params["p_findings"]
        self.assertEqual(len(findings), 1)
        meta = findings[0]["metadata"]
        self.assertIn("question_version_id_a", meta)
        self.assertIn("question_version_id_b", meta)
        self.assertIn("detection_method", meta)
        self.assertIn("similarity_score", meta)
        self.assertIn("similarity_threshold", meta)


class TestDurableRepeatRunIdempotency(unittest.TestCase):
    def test_independent_orchestration_runs_skip_persisted_pairs(self):
        rows = [
            _row("aaaaaaaa-0000-0000-0000-000000000001", "Platform Admin", "Same stem"),
            _row("bbbbbbbb-0000-0000-0000-000000000002", "Platform Admin", "Same stem"),
        ]
        client = StatefulFakeSupabase()

        first = orchestrate_certification_duplicate_audit(
            client,
            rows=rows,
            created_by="worker-a",
        )
        second = orchestrate_certification_duplicate_audit(
            client,
            rows=rows,
            created_by="worker-b",
        )

        self.assertEqual(first["finding_count"], 1)
        self.assertEqual(second["finding_count"], 0)
        self.assertEqual(client.persisted_pair_count, 1)

        second_complete = [
            call for call in client.calls
            if call[0] == "complete_audit_run_v1"
        ][-1][1]
        self.assertEqual(second_complete["p_findings"], [])

        expected_key = pair_key_from_finding(
            detect_duplicate_question_stems(rows)[0]
        )
        self.assertIsNotNone(expected_key)
        list_calls = [
            call for call in client.calls
            if call[0] == "list_duplicate_question_pair_keys_v1"
        ]
        self.assertEqual(len(list_calls), 2)


REPO_ROOT = Path(__file__).resolve().parents[1]
DUPLICATE_PAIR_DEDUPE_MIGRATION = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260624150000_v45_duplicate_question_pair_dedupe.sql"
)


class TestDuplicatePairOnConflictMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = DUPLICATE_PAIR_DEDUPE_MIGRATION.read_text(encoding="utf-8")

    def test_migration_duplicate_insert_uses_on_conflict_and_completion_tolerates_existing_pair(
        self,
    ):
        self.assertIn("ON CONFLICT (", self.sql)
        self.assertIn("DO NOTHING", self.sql)
        self.assertIn("RETURNING id INTO v_finding_id", self.sql)
        self.assertIn("IF v_finding_id IS NULL THEN", self.sql)
        self.assertNotIn("RAISE EXCEPTION", self.sql.split("ON CONFLICT (", 1)[1].split("DO NOTHING", 1)[0])

        rows = [
            _row("aaaaaaaa-0000-0000-0000-000000000001", "Platform Admin", "Same stem"),
            _row("bbbbbbbb-0000-0000-0000-000000000002", "Platform Admin", "Same stem"),
        ]
        finding = detect_duplicate_question_stems(rows)[0]
        meta = finding["metadata"]
        client = StatefulFakeSupabase()
        client._persisted_pair_keys.add((
            meta["certification_exam_name"],
            meta["question_version_id_a"],
            meta["question_version_id_b"],
            meta["detection_method"],
            meta["ruleset_version"],
        ))

        result = client.rpc(
            "complete_audit_run_v1",
            {
                "p_audit_run_id": "audit-run-concurrent",
                "p_findings": [finding],
            },
        ).execute()

        self.assertIsNone(result.error)
        self.assertEqual(result.data[0]["run_status"], "completed")
        self.assertEqual(result.data[0]["finding_count"], 0)
        self.assertEqual(client.persisted_pair_count, 1)


if __name__ == "__main__":
    unittest.main()
