"""Tests for V48 local relevance label sidecar management (offline only)."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from typing import Any, List, Mapping

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.v48_build_relevance_review_packet import (
    ALLOWED_RELEVANCE_LABELS,
    LOCAL_REVIEW_ROOT,
    PACKET_SCHEMA_VERSION,
    REFERENCE_REPLAY_CONTENT_SET_HASH,
    compute_packet_hash,
    compute_pair_id,
)
from scripts.v48_manage_relevance_labels import (
    EXPECTED_PAIR_COUNT,
    LABEL_SIDECAR_SCHEMA_VERSION,
    RelevanceLabelSidecarIntegrityError,
    RelevanceLabelSidecarPathError,
    RelevanceLabelSidecarStateError,
    analyze_sidecar_labels,
    atomic_write_json,
    build_sidecar_payload,
    compute_label_set_hash,
    default_sidecar_path,
    finalize_label_sidecar,
    initialize_label_sidecar,
    load_verified_source_packet,
    main,
    validate_label_sidecar,
    validate_local_path,
    verify_source_packet_integrity,
)
from scripts.v48_real_hybrid_replay import load_frozen_replay_fixture, FROZEN_REPLAY_FIXTURE_PATH
from workers.ai_quality_audit_shadow import (
    CONFIDENCE_CLASS_SEMANTIC_REVIEW,
    classify_question_shadow_from_replay_record,
)
from workers.v48_hybrid_replay_authoritative_text import _selected_semantic_review_bindings

_SENSITIVE_QUERY = "SECRET QUERY TEXT MUST NOT LEAK"
_SENSITIVE_CHUNK = "SECRET CHUNK TEXT MUST NOT LEAK"
_REFERENCE_PACKET_HASH = (
    "a2106b8a4719349392ec19682196145506206ab8ef2138e988469041a8686942"
)


def _ensure_local_review_root() -> str:
    os.makedirs(LOCAL_REVIEW_ROOT, exist_ok=True)
    return LOCAL_REVIEW_ROOT


def _build_source_packet(*, pair_count: int = EXPECTED_PAIR_COUNT) -> dict[str, Any]:
    fixture = load_frozen_replay_fixture(fixture_path=FROZEN_REPLAY_FIXTURE_PATH)
    pairs: List[dict[str, Any]] = []
    index = 0
    for record in fixture["questions"]:
        shadow = classify_question_shadow_from_replay_record(record)
        if shadow["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
            continue
        question_version_id = str(record["question_version_id"])
        for binding in _selected_semantic_review_bindings(record, candidate_limit=2):
            pairs.append(
                {
                    "pair_id": compute_pair_id(
                        question_version_id=question_version_id,
                        candidate_identity=binding.candidate_identity,
                    ),
                    "question_version_id": question_version_id,
                    "candidate_identity": binding.candidate_identity,
                    "resource_id": f"{index:032x}",
                    "resource_chunk_id": f"{index+1:032x}",
                    "resource_type": binding.resource_type,
                    "resource_title": binding.title,
                    "authoritative_query_text": f"{_SENSITIVE_QUERY} {question_version_id}",
                    "authoritative_candidate_chunk_text": (
                        f"{_SENSITIVE_CHUNK} {binding.candidate_identity}"
                    ),
                    "relevance_score": binding.expected_relevance_score,
                    "semantic_similarity": round(0.75 + index * 0.01, 9),
                    "confidence_class": CONFIDENCE_CLASS_SEMANTIC_REVIEW,
                    "qualified_count_v1": 1,
                    "structural_candidate_count": len(shadow["candidates"]),
                    "l1_structural_guards_pass": True,
                    "qualified_v1": True,
                    "relevance_label": None,
                    "reviewer_notes": "",
                }
            )
            index += 1
            if len(pairs) >= pair_count:
                break
        if len(pairs) >= pair_count:
            break

    body = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "generated_at": "2026-07-03T00:00:00+00:00",
        "replay_content_set_hash": REFERENCE_REPLAY_CONTENT_SET_HASH,
        "model_name": "text-embedding-3-small",
        "model_version": "openai-text-embedding-3-small-2026-07-03",
        "dimensions": 1536,
        "question_count": 10,
        "semantic_review_question_count": 7,
        "pair_count": len(pairs),
        "candidate_limit": 2,
        "provider_request_count": 0,
        "cache_only": True,
        "cache_hit_count": 21,
        "cache_miss_count": 0,
        "allowed_relevance_labels": list(ALLOWED_RELEVANCE_LABELS),
        "pairs": pairs,
    }
    hash_input = {
        key: value for key, value in body.items() if key not in {"generated_at", "packet_hash"}
    }
    body["packet_hash"] = compute_packet_hash(hash_input)
    return body


class TestRelevanceLabelSidecarHelpers(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(dir=_ensure_local_review_root())

    def _write_source_packet(self, packet: Mapping[str, Any] | None = None) -> str:
        payload = dict(packet or _build_source_packet())
        path = os.path.join(self.temp_dir, "v48_relevance_review_test.json")
        atomic_write_json(path, payload)
        return path

    def test_dry_run_help_makes_zero_external_calls(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        self.assertIn("help", stdout.getvalue())

    def test_initialize_creates_fourteen_unlabeled_entries(self):
        packet_path = self._write_source_packet()
        sidecar_path = os.path.join(self.temp_dir, "labels.json")
        result = initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=sidecar_path,
            overwrite=False,
            execute=True,
        )
        self.assertEqual(result["final_status"], "success")
        with open(sidecar_path, encoding="utf-8") as handle:
            sidecar = json.load(handle)
        self.assertEqual(len(sidecar["labels"]), EXPECTED_PAIR_COUNT)
        self.assertIsNone(sidecar["label_set_hash"])
        self.assertIsNone(sidecar["finalized_at"])
        for entry in sidecar["labels"]:
            self.assertIsNone(entry["relevance_label"])
            self.assertEqual(entry["reviewer_notes"], "")

    def test_source_packet_is_never_modified(self):
        packet_path = self._write_source_packet()
        before = open(packet_path, "rb").read()
        initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=os.path.join(self.temp_dir, "labels.json"),
            overwrite=False,
            execute=True,
        )
        after = open(packet_path, "rb").read()
        self.assertEqual(before, after)

    def test_sidecar_contains_no_authoritative_text(self):
        packet_path = self._write_source_packet()
        sidecar_path = os.path.join(self.temp_dir, "labels.json")
        initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=sidecar_path,
            overwrite=False,
            execute=True,
        )
        serialized = open(sidecar_path, encoding="utf-8").read()
        self.assertNotIn("authoritative_query_text", serialized)
        self.assertNotIn("authoritative_candidate_chunk_text", serialized)
        self.assertNotIn(_SENSITIVE_QUERY, serialized)
        self.assertNotIn(_SENSITIVE_CHUNK, serialized)

    def test_output_must_be_under_gitignored_local_directory(self):
        with self.assertRaises(RelevanceLabelSidecarPathError):
            validate_local_path("/tmp/outside.json")

    def test_initialize_requires_overwrite(self):
        packet_path = self._write_source_packet()
        sidecar_path = os.path.join(self.temp_dir, "labels.json")
        initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=sidecar_path,
            overwrite=False,
            execute=True,
        )
        with self.assertRaises(RelevanceLabelSidecarStateError):
            initialize_label_sidecar(
                packet_path=packet_path,
                sidecar_path=sidecar_path,
                overwrite=False,
                execute=True,
            )

    def test_validate_reports_safe_aggregates_only(self):
        packet_path = self._write_source_packet()
        sidecar_path = os.path.join(self.temp_dir, "labels.json")
        initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=sidecar_path,
            overwrite=False,
            execute=True,
        )
        report = validate_label_sidecar(labels_path=sidecar_path)
        self.assertEqual(report["final_status"], "validated")
        self.assertEqual(report["total_pairs"], EXPECTED_PAIR_COUNT)
        self.assertEqual(report["unlabeled_count"], EXPECTED_PAIR_COUNT)
        self.assertTrue(report["source_packet_hash_matches"])
        self.assertTrue(report["all_expected_pairs_present"])
        serialized = json.dumps(report)
        self.assertNotIn(_SENSITIVE_QUERY, serialized)
        self.assertNotIn(_SENSITIVE_CHUNK, serialized)

    def test_invalid_label_value_is_counted(self):
        packet_path = self._write_source_packet()
        sidecar_path = os.path.join(self.temp_dir, "labels.json")
        initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=sidecar_path,
            overwrite=False,
            execute=True,
        )
        sidecar = json.load(open(sidecar_path, encoding="utf-8"))
        sidecar["labels"][0]["relevance_label"] = "not_a_real_label"
        atomic_write_json(sidecar_path, sidecar)
        report = validate_label_sidecar(labels_path=sidecar_path)
        self.assertEqual(report["invalid_label_count"], 1)

    def test_null_labels_prevent_finalization(self):
        packet_path = self._write_source_packet()
        sidecar_path = os.path.join(self.temp_dir, "labels.json")
        initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=sidecar_path,
            overwrite=False,
            execute=True,
        )
        with self.assertRaises(RelevanceLabelSidecarIntegrityError):
            finalize_label_sidecar(labels_path=sidecar_path, execute=True)

    def test_duplicate_pair_ids_fail_finalization(self):
        packet_path = self._write_source_packet()
        sidecar_path = os.path.join(self.temp_dir, "labels.json")
        initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=sidecar_path,
            overwrite=False,
            execute=True,
        )
        sidecar = json.load(open(sidecar_path, encoding="utf-8"))
        sidecar["labels"].append(dict(sidecar["labels"][0]))
        for entry in sidecar["labels"]:
            entry["relevance_label"] = "relevant"
        atomic_write_json(sidecar_path, sidecar)
        with self.assertRaises(RelevanceLabelSidecarIntegrityError):
            finalize_label_sidecar(labels_path=sidecar_path, execute=True)

    def test_missing_pair_ids_fail_finalization(self):
        packet_path = self._write_source_packet()
        sidecar_path = os.path.join(self.temp_dir, "labels.json")
        initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=sidecar_path,
            overwrite=False,
            execute=True,
        )
        sidecar = json.load(open(sidecar_path, encoding="utf-8"))
        sidecar["labels"] = sidecar["labels"][:-1]
        for entry in sidecar["labels"]:
            entry["relevance_label"] = "relevant"
        atomic_write_json(sidecar_path, sidecar)
        with self.assertRaises(RelevanceLabelSidecarIntegrityError):
            finalize_label_sidecar(labels_path=sidecar_path, execute=True)

    def test_unknown_pair_ids_fail_finalization(self):
        packet_path = self._write_source_packet()
        sidecar_path = os.path.join(self.temp_dir, "labels.json")
        initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=sidecar_path,
            overwrite=False,
            execute=True,
        )
        sidecar = json.load(open(sidecar_path, encoding="utf-8"))
        sidecar["labels"][-1]["pair_id"] = "f" * 64
        for entry in sidecar["labels"]:
            entry["relevance_label"] = "relevant"
        atomic_write_json(sidecar_path, sidecar)
        with self.assertRaises(RelevanceLabelSidecarIntegrityError):
            finalize_label_sidecar(labels_path=sidecar_path, execute=True)

    def test_source_packet_hash_mismatch_fails_closed(self):
        packet_path = self._write_source_packet()
        sidecar_path = os.path.join(self.temp_dir, "labels.json")
        initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=sidecar_path,
            overwrite=False,
            execute=True,
        )
        sidecar = json.load(open(sidecar_path, encoding="utf-8"))
        sidecar["source_packet_hash"] = "0" * 64
        atomic_write_json(sidecar_path, sidecar)
        report = validate_label_sidecar(labels_path=sidecar_path)
        self.assertFalse(report["source_packet_hash_matches"])

    def test_recomputed_source_packet_hash_is_verified(self):
        packet = _build_source_packet()
        tampered = dict(packet)
        tampered["packet_hash"] = "0" * 64
        path = os.path.join(self.temp_dir, "tampered.json")
        atomic_write_json(path, tampered)
        with self.assertRaises(RelevanceLabelSidecarIntegrityError):
            load_verified_source_packet(path)

    def test_label_set_hash_is_order_independent(self):
        labels_a = [
            {"pair_id": "a", "relevance_label": "relevant", "reviewer_notes": ""},
            {"pair_id": "b", "relevance_label": "irrelevant", "reviewer_notes": "x"},
        ]
        labels_b = list(reversed(labels_a))
        self.assertEqual(compute_label_set_hash(labels_a), compute_label_set_hash(labels_b))

    def test_changing_label_or_notes_changes_label_set_hash(self):
        base = [
            {"pair_id": "a", "relevance_label": "relevant", "reviewer_notes": ""},
            {"pair_id": "b", "relevance_label": "irrelevant", "reviewer_notes": ""},
        ]
        changed_label = [
            {"pair_id": "a", "relevance_label": "uncertain", "reviewer_notes": ""},
            base[1],
        ]
        changed_notes = [
            base[0],
            {"pair_id": "b", "relevance_label": "irrelevant", "reviewer_notes": "note"},
        ]
        first = compute_label_set_hash(base)
        self.assertNotEqual(first, compute_label_set_hash(changed_label))
        self.assertNotEqual(first, compute_label_set_hash(changed_notes))

    def test_successful_finalization_sets_finalized_at_and_label_set_hash(self):
        packet_path = self._write_source_packet()
        sidecar_path = os.path.join(self.temp_dir, "labels.json")
        initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=sidecar_path,
            overwrite=False,
            execute=True,
        )
        sidecar = json.load(open(sidecar_path, encoding="utf-8"))
        for entry in sidecar["labels"]:
            entry["relevance_label"] = "relevant"
        atomic_write_json(sidecar_path, sidecar)
        result = finalize_label_sidecar(labels_path=sidecar_path, execute=True)
        self.assertEqual(result["final_status"], "success")
        finalized = json.load(open(sidecar_path, encoding="utf-8"))
        self.assertIsNotNone(finalized["finalized_at"])
        self.assertEqual(finalized["label_set_hash"], result["label_set_hash"])

    def test_already_finalized_sidecar_cannot_be_finalized_again(self):
        packet_path = self._write_source_packet()
        sidecar_path = os.path.join(self.temp_dir, "labels.json")
        initialize_label_sidecar(
            packet_path=packet_path,
            sidecar_path=sidecar_path,
            overwrite=False,
            execute=True,
        )
        sidecar = json.load(open(sidecar_path, encoding="utf-8"))
        for entry in sidecar["labels"]:
            entry["relevance_label"] = "relevant"
        atomic_write_json(sidecar_path, sidecar)
        finalize_label_sidecar(labels_path=sidecar_path, execute=True)
        with self.assertRaises(RelevanceLabelSidecarStateError):
            finalize_label_sidecar(labels_path=sidecar_path, execute=True)

    def test_default_sidecar_filename_uses_source_packet_hash_prefix(self):
        path = default_sidecar_path(source_packet_hash=_REFERENCE_PACKET_HASH)
        self.assertTrue(path.endswith("v48_relevance_labels_a2106b8a47193493.json"))


class TestRelevanceLabelSidecarIsolation(unittest.TestCase):
    def test_no_live_worker_imports_label_manager(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        workers_dir = os.path.join(repo_root, "workers")
        offenders = []
        for name in os.listdir(workers_dir):
            if not name.endswith(".py") or name == "__init__.py":
                continue
            path = os.path.join(workers_dir, name)
            with open(path, encoding="utf-8") as handle:
                contents = handle.read()
            if "v48_manage_relevance_labels" in contents:
                offenders.append(name)
        self.assertEqual(offenders, [])

    def test_sidecar_schema_fields(self):
        packet = _build_source_packet()
        sidecar = build_sidecar_payload(
            source_packet=packet,
            source_packet_path="/tmp/example/v48_relevance_review_test.json",
            source_packet_hash=packet["packet_hash"],
        )
        self.assertEqual(sidecar["schema_version"], LABEL_SIDECAR_SCHEMA_VERSION)
        self.assertEqual(sidecar["allowed_relevance_labels"], list(ALLOWED_RELEVANCE_LABELS))


if __name__ == "__main__":
    unittest.main()
