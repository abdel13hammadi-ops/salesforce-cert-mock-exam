"""
Tests for V58 verified official evidence seed export helpers.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.v58_export_official_evidence_seed import main as export_main
from workers.official_evidence_seed import (
    ADM_EXAM_NAME,
    BA_EXAM_NAME,
    MAX_EXCERPT_CHARS,
    OfficialEvidenceSeedError,
    OfficialEvidenceSeedOutputError,
    _dedupe_items,
    _is_placeholder_chunk_id,
    assert_output_safe_for_write,
    bound_excerpt,
    build_export_item,
    build_fixture_payload,
    collect_eligible_export_items,
    export_official_evidence_seed,
    inventory_report_dict,
    select_export_items,
    validate_chunk_provenance,
    validate_fixture_payload,
    write_fixture_file,
)
from workers.resource_chunking import sha256_hex

_ADM_RESOURCE_ID = "a1000001-0001-4001-8001-000000000001"
_BA_RESOURCE_ID = "b1000001-0001-4001-8001-000000000001"
_ADM_VERSION_ID = "d1000001-0001-4001-8001-000000000001"
_BA_VERSION_ID = "d1000001-0001-4001-8001-000000000002"
_ADM_CHUNK_1 = "e1000001-0001-4001-8001-000000000001"
_ADM_CHUNK_2 = "e1000001-0001-4001-8001-000000000002"
_BA_CHUNK_1 = "e1000001-0001-4001-8001-000000000003"
_BA_CHUNK_2 = "e1000001-0001-4001-8001-000000000004"

_ADM_TEXT_1 = (
    "Lookup Relationships\n"
    "If the lookup field is optional, you can specify one of three behaviors to "
    "occur if the lookup record is deleted."
)
_ADM_TEXT_2 = (
    "Deleted records remain in the Recycle Bin for 15 days before permanent deletion."
)
_BA_TEXT_1 = (
    "Project scope management defines and controls what is and is not included "
    "in the project."
)
_BA_TEXT_2 = (
    "A user story captures a user need and acceptance criteria for delivery teams."
)


def _resource(
    *,
    resource_id: str,
    certification: str,
    title: str,
    metadata: dict | None = None,
) -> dict:
    return {
        "id": resource_id,
        "certification_exam_name": certification,
        "title": title,
        "publisher": "Salesforce, Inc.",
        "canonical_url": f"https://help.salesforce.com/s/articleView?id={resource_id}",
        "resource_type": "help_article",
        "is_active": True,
        "metadata": metadata or {},
    }


def _version(*, version_id: str, resource_id: str, content_text: str) -> dict:
    return {
        "id": version_id,
        "resource_id": resource_id,
        "version_number": 1,
        "content_hash": sha256_hex(content_text),
        "effective_at": "2026-01-01T00:00:00+00:00",
        "source_external_version": "2026.01",
        "retrieved_at": "2026-01-02T00:00:00+00:00",
        "source_url": f"https://help.salesforce.com/s/articleView?id={resource_id}",
    }


def _chunk(*, chunk_id: str, version_id: str, chunk_index: int, chunk_text: str, metadata: dict | None = None) -> dict:
    return {
        "id": chunk_id,
        "resource_version_id": version_id,
        "chunk_index": chunk_index,
        "chunk_text": chunk_text,
        "content_hash": sha256_hex(chunk_text),
        "metadata": metadata or {},
    }


def _sample_rows() -> tuple[list[dict], list[dict], list[dict]]:
    resources = [
        _resource(
            resource_id=_ADM_RESOURCE_ID,
            certification=ADM_EXAM_NAME,
            title="Considerations for Object Relationships",
            metadata={"domain": "lookup relationship delete behavior"},
        ),
        _resource(
            resource_id=_BA_RESOURCE_ID,
            certification=BA_EXAM_NAME,
            title="Project Scope Management",
            metadata={"domain": "project scope statement"},
        ),
    ]
    versions = [
        _version(version_id=_ADM_VERSION_ID, resource_id=_ADM_RESOURCE_ID, content_text=_ADM_TEXT_1 + "\n\n" + _ADM_TEXT_2),
        _version(version_id=_BA_VERSION_ID, resource_id=_BA_RESOURCE_ID, content_text=_BA_TEXT_1 + "\n\n" + _BA_TEXT_2),
    ]
    chunks = [
        _chunk(chunk_id=_ADM_CHUNK_1, version_id=_ADM_VERSION_ID, chunk_index=0, chunk_text=_ADM_TEXT_1),
        _chunk(
            chunk_id=_ADM_CHUNK_2,
            version_id=_ADM_VERSION_ID,
            chunk_index=1,
            chunk_text=_ADM_TEXT_2,
            metadata={"domain": "Recycle Bin retention restoration permissions and permanent deletion"},
        ),
        _chunk(chunk_id=_BA_CHUNK_1, version_id=_BA_VERSION_ID, chunk_index=0, chunk_text=_BA_TEXT_1),
        _chunk(
            chunk_id=_BA_CHUNK_2,
            version_id=_BA_VERSION_ID,
            chunk_index=1,
            chunk_text=_BA_TEXT_2,
            metadata={"domain": "user-story package components"},
        ),
    ]
    return resources, versions, chunks


class FakeQuery:
    def __init__(self, parent: "FakeSupabase", table_name: str):
        self.parent = parent
        self.table_name = table_name
        self._select = "*"
        self._offset = 0
        self._limit = 1000

    def select(self, fields: str, *, count: str | None = None):
        self._select = fields
        return self

    def range(self, start: int, end: int):
        self._offset = start
        self._limit = end - start + 1
        return self

    def execute(self):
        self.parent.read_calls.append(("select", self.table_name, self._select))
        rows = list(self.parent.tables.get(self.table_name, []))
        sliced = rows[self._offset:self._offset + self._limit]
        return FakeResult(data=sliced)


class FakeResult:
    def __init__(self, data=None, error=None):
        self.data = data if data is not None else []
        self.error = error


class FakeSupabase:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables
        self.read_calls: list[tuple[str, str, str]] = []
        self.mutation_calls: list[tuple[str, ...]] = []

    def table(self, name: str):
        return FakeQuery(self, name)

    def rpc(self, *_args, **_kwargs):
        self.mutation_calls.append(("rpc",))
        raise AssertionError("mutation RPC must not be called")

    def insert(self, *_args, **_kwargs):
        self.mutation_calls.append(("insert",))
        raise AssertionError("insert must not be called")

    def update(self, *_args, **_kwargs):
        self.mutation_calls.append(("update",))
        raise AssertionError("update must not be called")

    def delete(self, *_args, **_kwargs):
        self.mutation_calls.append(("delete",))
        raise AssertionError("delete must not be called")


class TestProvenanceValidation(unittest.TestCase):

    def setUp(self):
        self.resources, self.versions, self.chunks = _sample_rows()

    def test_valid_provenance_builds_export_item(self):
        item = build_export_item(
            resource=self.resources[0],
            version=self.versions[0],
            chunk=self.chunks[0],
            exported_at="2026-07-05T20:00:00+00:00",
        )
        self.assertEqual(item["resource_chunk_id"], _ADM_CHUNK_1)
        self.assertEqual(item["provenance_status"], "verified_official_resource_library")
        self.assertLessEqual(len(item["chunk_text_excerpt"]), MAX_EXCERPT_CHARS)

    def test_placeholder_chunk_id_rejected(self):
        chunk = dict(self.chunks[0], id="11111111-1111-1111-1111-111111111111")
        with self.assertRaises(OfficialEvidenceSeedError):
            validate_chunk_provenance(
                resource=self.resources[0],
                version=self.versions[0],
                chunk=chunk,
            )

    def test_missing_publisher_rejected(self):
        resource = dict(self.resources[0], publisher="")
        with self.assertRaises(OfficialEvidenceSeedError):
            validate_chunk_provenance(resource=resource, version=self.versions[0], chunk=self.chunks[0])

    def test_hash_mismatch_rejected(self):
        chunk = dict(self.chunks[0], content_hash="deadbeef")
        with self.assertRaises(OfficialEvidenceSeedError):
            validate_chunk_provenance(resource=self.resources[0], version=self.versions[0], chunk=chunk)


class TestSelectionAndDedup(unittest.TestCase):

    def test_dedupe_is_deterministic(self):
        resources, versions, chunks = _sample_rows()
        client = FakeSupabase({
            "official_resources": resources,
            "resource_versions": versions,
            "resource_chunks": chunks + [dict(chunks[0])],
        })
        first = collect_eligible_export_items(client)
        second = collect_eligible_export_items(client)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 4)

    def test_select_export_items_covers_both_certifications(self):
        resources, versions, chunks = _sample_rows()
        client = FakeSupabase({
            "official_resources": resources,
            "resource_versions": versions,
            "resource_chunks": chunks,
        })
        eligible = collect_eligible_export_items(client)
        selected = select_export_items(eligible, target_min=4, target_max=4)
        certs = {item["certification"] for item in selected}
        self.assertIn(ADM_EXAM_NAME, certs)
        self.assertIn(BA_EXAM_NAME, certs)


class TestFixtureSerialization(unittest.TestCase):

    def test_fixture_is_json_serializable_and_valid(self):
        resources, versions, chunks = _sample_rows()
        client = FakeSupabase({
            "official_resources": resources,
            "resource_versions": versions,
            "resource_chunks": chunks,
        })
        selection = export_official_evidence_seed(client, target_min=4, target_max=4, generated_at="2026-07-05T20:00:00+00:00")
        payload = build_fixture_payload(selection.items, inventory=selection.inventory, generated_at="2026-07-05T20:00:00+00:00")
        validate_fixture_payload(payload)
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("SUPABASE_", serialized)
        self.assertNotIn("service_role", serialized.lower())
        self.assertTrue(payload["no_synthetic_evidence"])

    def test_write_fixture_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seed.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(OfficialEvidenceSeedOutputError):
                write_fixture_file(path, {"fixture_version": "x", "evidence_items": []})


class TestExporterSafety(unittest.TestCase):

    def test_collect_eligible_performs_select_only(self):
        resources, versions, chunks = _sample_rows()
        client = FakeSupabase({
            "official_resources": resources,
            "resource_versions": versions,
            "resource_chunks": chunks,
        })
        collect_eligible_export_items(client)
        self.assertTrue(client.read_calls)
        self.assertEqual(client.mutation_calls, [])

    def test_bound_excerpt_enforces_max_size(self):
        long_text = "word " * 400
        excerpt = bound_excerpt(long_text, max_chars=100)
        self.assertLessEqual(len(excerpt), 100)
        self.assertTrue(excerpt)

    def test_assert_output_safe_for_write_blocks_credentials(self):
        with self.assertRaises(OfficialEvidenceSeedOutputError):
            assert_output_safe_for_write('{"key":"SUPABASE_SERVICE_ROLE_KEY"}')

    def test_inventory_report_dict_shape(self):
        resources, versions, chunks = _sample_rows()
        client = FakeSupabase({
            "official_resources": resources,
            "resource_versions": versions,
            "resource_chunks": chunks,
        })
        selection = export_official_evidence_seed(client, target_min=4, target_max=4)
        report = inventory_report_dict(selection.inventory)
        self.assertIn("eligible_chunk_count", report)
        self.assertEqual(report["eligible_chunk_count"], 4)


class TestExporterCli(unittest.TestCase):

    def test_inventory_mode_uses_fake_client(self):
        resources, versions, chunks = _sample_rows()
        fake = FakeSupabase({
            "official_resources": resources,
            "resource_versions": versions,
            "resource_chunks": chunks,
        })
        with patch(
            "scripts.v58_export_official_evidence_seed.load_supabase_client",
            return_value=fake,
        ):
            with patch(
                "scripts.v58_export_official_evidence_seed._running_under_pytest",
                return_value=False,
            ):
                code = export_main(["--inventory"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.mutation_calls, [])


class TestPlaceholderDetection(unittest.TestCase):

    def test_known_placeholder_patterns(self):
        self.assertTrue(_is_placeholder_chunk_id("11111111-1111-1111-1111-111111111111"))
        self.assertFalse(_is_placeholder_chunk_id(_ADM_CHUNK_1))


class TestExportedFixtureFile(unittest.TestCase):

    def test_committed_fixture_passes_validation_when_present(self):
        fixture_path = Path(__file__).resolve().parents[1] / "workers" / "fixtures" / "official_evidence_seed_v1.json"
        if not fixture_path.exists():
            self.skipTest("official_evidence_seed_v1.json not generated yet")
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        validate_fixture_payload(payload)
        self.assertTrue(payload["no_synthetic_evidence"])
        certs = {item["certification"] for item in payload["evidence_items"]}
        self.assertIn(ADM_EXAM_NAME, certs)
        self.assertIn(BA_EXAM_NAME, certs)
        self.assertGreaterEqual(len(payload["evidence_items"]), 24)


if __name__ == "__main__":
    unittest.main()
