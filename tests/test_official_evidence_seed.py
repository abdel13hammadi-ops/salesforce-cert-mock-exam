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
from workers.certification_registry import (
    BA_EXAM_NAME,
    PAB_EXAM_NAME,
    SCC_EXAM_NAME,
    get_certification_definition,
    get_platform_app_builder_definition,
    get_sales_cloud_consultant_definition,
)
from workers.official_evidence_seed import (
    ADM_EXAM_NAME,
    BA_EVIDENCE_CONFIG_ID,
    BA_FIXTURE_VERSION,
    FIXTURE_VERSION,
    MAX_EXCERPT_CHARS,
    PAB_EVIDENCE_CONFIG_ID,
    PAB_FIXTURE_VERSION,
    SCC_EVIDENCE_CONFIG_ID,
    SCC_FIXTURE_VERSION,
    OfficialEvidenceSeedError,
    OfficialEvidenceSeedOutputError,
    _dedupe_items,
    _is_placeholder_chunk_id,
    assert_output_safe_for_write,
    bound_excerpt,
    build_export_item,
    build_fixture_payload,
    collect_eligible_export_items,
    evidence_fixture_path_for_certification,
    export_official_evidence_seed,
    filter_fixture_items_by_certification,
    fixture_item_to_candidate_row,
    fixture_item_to_resource_row,
    inventory_report_dict,
    load_evidence_fixture_for_certification,
    resolve_evidence_identity_for_certification,
    retrieve_official_evidence_from_fixture,
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

    def test_v1_fixture_is_byte_identical_to_pre_pab_checkpoint(self):
        """PAB-EXP-04A Correction 1: official_evidence_seed_v1.json must be
        restored to its exact contents as of commit
        6fb472527bfcca3eff38ace835886c7ae5bce083 (the last commit before
        PAB-EXP-04 appended PAB records to this file). This SHA-256 was
        computed directly from that commit's blob during PAB-EXP-04A.
        """
        fixture_path = Path(__file__).resolve().parents[1] / "workers" / "fixtures" / "official_evidence_seed_v1.json"
        if not fixture_path.exists():
            self.skipTest("official_evidence_seed_v1.json not generated yet")
        import hashlib

        digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        self.assertEqual(
            digest,
            "75f472ceff12b704ff25d3c968fe6d7d644761508fa0d7e7c2dfbb95c8d91e51",
            "official_evidence_seed_v1.json no longer matches the pre-PAB-EXP-04 "
            "checkpoint (commit 6fb472527bfcca3eff38ace835886c7ae5bce083) -- its "
            "historical evidence identity must never be mutated.",
        )
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        certs = {item["certification"] for item in payload["evidence_items"]}
        self.assertNotIn(
            PAB_EXAM_NAME,
            certs,
            "official-evidence-seed-v1 must remain ADM/BA only; Platform App "
            "Builder evidence belongs exclusively in official-evidence-pab-v1.",
        )
        self.assertNotIn(
            SCC_EXAM_NAME,
            certs,
            "official-evidence-seed-v1 must remain ADM/BA only; Sales Cloud "
            "Consultant evidence belongs exclusively in official-evidence-scc-v1.",
        )


class TestEvidenceIdentityRouting(unittest.TestCase):
    """Certification -> evidence-package identity routing."""

    def test_administrator_resolves_to_frozen_shared_identity(self):
        self.assertEqual(
            resolve_evidence_identity_for_certification(ADM_EXAM_NAME),
            FIXTURE_VERSION,
        )

    def test_business_analyst_resolves_to_isolated_identity(self):
        self.assertEqual(
            resolve_evidence_identity_for_certification(BA_EXAM_NAME),
            BA_FIXTURE_VERSION,
        )
        self.assertNotEqual(BA_FIXTURE_VERSION, FIXTURE_VERSION)

    def test_pab_resolves_to_isolated_identity(self):
        self.assertEqual(
            resolve_evidence_identity_for_certification(PAB_EXAM_NAME),
            PAB_FIXTURE_VERSION,
        )
        self.assertNotEqual(PAB_FIXTURE_VERSION, FIXTURE_VERSION)

    def test_scc_resolves_to_isolated_identity(self):
        self.assertEqual(
            resolve_evidence_identity_for_certification(SCC_EXAM_NAME),
            SCC_FIXTURE_VERSION,
        )
        self.assertNotEqual(SCC_FIXTURE_VERSION, FIXTURE_VERSION)
        self.assertNotEqual(SCC_FIXTURE_VERSION, BA_FIXTURE_VERSION)
        self.assertNotEqual(SCC_FIXTURE_VERSION, PAB_FIXTURE_VERSION)

    def test_unknown_certification_raises_with_no_fallback(self):
        with self.assertRaises(OfficialEvidenceSeedError):
            resolve_evidence_identity_for_certification("Salesforce Certified Nonexistent Thing")
        with self.assertRaises(OfficialEvidenceSeedError):
            evidence_fixture_path_for_certification("")

    def test_fixture_paths_are_distinct_per_certification(self):
        adm_path = evidence_fixture_path_for_certification(ADM_EXAM_NAME)
        ba_path = evidence_fixture_path_for_certification(BA_EXAM_NAME)
        pab_path = evidence_fixture_path_for_certification(PAB_EXAM_NAME)
        scc_path = evidence_fixture_path_for_certification(SCC_EXAM_NAME)
        self.assertEqual(adm_path.name, "official_evidence_seed_v1.json")
        self.assertEqual(ba_path.name, "official_evidence_ba_v1.json")
        self.assertEqual(pab_path.name, "official_evidence_pab_v1.json")
        self.assertEqual(scc_path.name, "official_evidence_scc_v1.json")
        all_paths = [adm_path, ba_path, pab_path, scc_path]
        self.assertEqual(len(all_paths), len(set(all_paths)))


class TestPlatformAppBuilderFixtureContent(unittest.TestCase):
    """PAB-EXP-04A Correction 2/3: the new, isolated PAB evidence package.
    Verifies structural validity, source provenance, domain coverage, and
    isolation from the historical ADM/BA package.
    """

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = (
            Path(__file__).resolve().parents[1] / "workers" / "fixtures" / "official_evidence_pab_v1.json"
        )
        if not cls.fixture_path.exists():
            raise unittest.SkipTest("official_evidence_pab_v1.json not generated yet")
        cls.payload = json.loads(cls.fixture_path.read_text(encoding="utf-8"))

    def test_fixture_identity_and_isolation_metadata(self):
        self.assertEqual(self.payload["fixture_version"], PAB_FIXTURE_VERSION)
        self.assertEqual(self.payload.get("evidence_config_id"), PAB_EVIDENCE_CONFIG_ID)
        self.assertTrue(self.payload["no_synthetic_evidence"])
        self.assertIn(FIXTURE_VERSION, self.payload.get("isolated_from", []))

    def test_passes_shared_structural_validation(self):
        validate_fixture_payload(self.payload)

    def test_contains_only_platform_app_builder_records(self):
        certs = {item["certification"] for item in self.payload["evidence_items"]}
        self.assertEqual(certs, {PAB_EXAM_NAME})

    def test_all_five_domains_have_at_least_one_record(self):
        pab = get_platform_app_builder_definition()
        expected_domains = {domain.domain_name for domain in pab.domains}
        covered = {item["domain"] for item in self.payload["evidence_items"]}
        self.assertEqual(expected_domains, expected_domains & covered)

    def test_every_record_is_compact_and_source_verified(self):
        self.assertLess(len(self.payload["evidence_items"]), 18)
        for item in self.payload["evidence_items"]:
            self.assertEqual(item["provenance_status"], "verified_official_resource_library")
            self.assertTrue(item["canonical_url"].startswith("https://trailhead.salesforce.com/"))
            self.assertEqual(item["publisher"], "Salesforce, Inc.")
            excerpt = item["chunk_text_excerpt"]
            self.assertEqual(
                item["content_hash"],
                sha256_hex(excerpt) if item["excerpt_mode"] == "full_chunk" else item["content_hash"],
            )
            self.assertNotIn("Exam Practice Questions", excerpt)
            self.assertNotIn("Flashcard", excerpt)

    def test_legacy_automation_record_supports_flow_preference_without_overclaiming(self):
        matches = [
            item
            for item in self.payload["evidence_items"]
            if "Workflow Rules" in item["chunk_text_excerpt"]
            and "Flow" in item["chunk_text_excerpt"]
        ]
        self.assertTrue(matches, "expected a retrieved record distinguishing legacy tools from Flow")
        for item in matches:
            excerpt = item["chunk_text_excerpt"]
            self.assertIn("main automation tool", excerpt)
            self.assertIn("Migrate to Flow", excerpt)


class TestBusinessAnalystFixtureContent(unittest.TestCase):
    """BA-EXP-02: isolated Business Analyst evidence package."""

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = (
            Path(__file__).resolve().parents[1] / "workers" / "fixtures" / "official_evidence_ba_v1.json"
        )
        if not cls.fixture_path.exists():
            raise unittest.SkipTest("official_evidence_ba_v1.json not generated yet")
        cls.payload = json.loads(cls.fixture_path.read_text(encoding="utf-8"))

    def test_fixture_identity_and_isolation_metadata(self):
        self.assertEqual(self.payload["fixture_version"], BA_FIXTURE_VERSION)
        self.assertEqual(self.payload.get("evidence_config_id"), BA_EVIDENCE_CONFIG_ID)
        self.assertTrue(self.payload["no_synthetic_evidence"])
        self.assertIn(FIXTURE_VERSION, self.payload.get("isolated_from", []))
        self.assertNotIn("intended_use", self.payload)

    def test_passes_shared_structural_validation(self):
        validate_fixture_payload(self.payload)

    def test_contains_only_business_analyst_records(self):
        certs = {item["certification"] for item in self.payload["evidence_items"]}
        self.assertEqual(certs, {BA_EXAM_NAME})
        self.assertNotIn(ADM_EXAM_NAME, certs)
        self.assertNotIn(PAB_EXAM_NAME, certs)

    def test_all_six_official_domains_have_at_least_one_record(self):
        ba = get_certification_definition(BA_EXAM_NAME)
        expected_domains = {domain.domain_name for domain in ba.domains}
        covered = {item["domain"] for item in self.payload["evidence_items"]}
        self.assertEqual(expected_domains, covered)

    def test_every_record_uses_official_salesforce_trailhead_source(self):
        for item in self.payload["evidence_items"]:
            self.assertTrue(item["canonical_url"].startswith("https://trailhead.salesforce.com/"))
            self.assertEqual(item["source_url"], item["canonical_url"])
            self.assertEqual(item["publisher"], "Salesforce, Inc.")
            self.assertEqual(item["provenance_status"], "verified_official_resource_library")
            self.assertEqual(item["certification_code"], "BA-201")

    def test_every_record_is_compact_and_hash_aligned(self):
        seen_hashes = set()
        for item in self.payload["evidence_items"]:
            excerpt = item["chunk_text_excerpt"]
            self.assertEqual(item["content_hash"], sha256_hex(excerpt))
            self.assertNotIn(excerpt, seen_hashes)
            seen_hashes.add(excerpt)
            self.assertNotIn("Exam Practice Questions", excerpt)
            self.assertNotIn("flashcard", excerpt.lower())

    def test_duplicate_chunk_ids_are_rejected_by_validator(self):
        tampered = json.loads(json.dumps(self.payload))
        tampered["evidence_items"][1]["resource_chunk_id"] = tampered["evidence_items"][0][
            "resource_chunk_id"
        ]
        with self.assertRaises(OfficialEvidenceSeedError):
            validate_fixture_payload(tampered)

    def test_unknown_domain_yields_no_qualifying_match(self):
        result = retrieve_official_evidence_from_fixture(
            certification_exam_name=BA_EXAM_NAME,
            domain_name="Nonexistent Domain",
            query_text="completely unrelated gibberish text xyzzy plugh",
            fixture_payload=self.payload,
        )
        self.assertEqual(result["evidence_identity"], BA_FIXTURE_VERSION)
        self.assertEqual(result["ranked_chunks"], [])

    def test_ba_retrieval_never_returns_administrator_chunk_ids(self):
        adm_payload = load_evidence_fixture_for_certification(ADM_EXAM_NAME)
        ba_chunk_ids = {item["resource_chunk_id"] for item in self.payload["evidence_items"]}
        adm_chunk_ids = {item["resource_chunk_id"] for item in adm_payload["evidence_items"]}
        self.assertEqual(ba_chunk_ids.intersection(adm_chunk_ids), set())

        result = retrieve_official_evidence_from_fixture(
            certification_exam_name=BA_EXAM_NAME,
            domain_name="Requirements",
            query_text="requirements lifecycle pain points business needs dependencies",
            fixture_payload=self.payload,
        )
        returned_ids = {chunk["resource_chunk_id"] for chunk in result["ranked_chunks"]}
        self.assertTrue(returned_ids.issubset(ba_chunk_ids))
        self.assertEqual(returned_ids.intersection(adm_chunk_ids), set())


class TestSalesCloudConsultantFixtureContent(unittest.TestCase):
    """SCC-EXP-04: isolated Sales Cloud Consultant evidence package."""

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = (
            Path(__file__).resolve().parents[1] / "workers" / "fixtures" / "official_evidence_scc_v1.json"
        )
        if not cls.fixture_path.exists():
            raise unittest.SkipTest("official_evidence_scc_v1.json not generated yet")
        cls.payload = json.loads(cls.fixture_path.read_text(encoding="utf-8"))

    def test_fixture_identity_and_isolation_metadata(self):
        self.assertEqual(self.payload["fixture_version"], SCC_FIXTURE_VERSION)
        self.assertEqual(self.payload.get("evidence_config_id"), SCC_EVIDENCE_CONFIG_ID)
        self.assertTrue(self.payload["no_synthetic_evidence"])
        self.assertIn(FIXTURE_VERSION, self.payload.get("isolated_from", []))
        self.assertIn(BA_FIXTURE_VERSION, self.payload.get("isolated_from", []))
        self.assertIn(PAB_FIXTURE_VERSION, self.payload.get("isolated_from", []))
        self.assertNotIn("intended_use", self.payload)

    def test_passes_shared_structural_validation(self):
        validate_fixture_payload(self.payload)

    def test_contains_only_sales_cloud_consultant_records(self):
        certs = {item["certification"] for item in self.payload["evidence_items"]}
        self.assertEqual(certs, {SCC_EXAM_NAME})
        self.assertNotIn(ADM_EXAM_NAME, certs)
        self.assertNotIn(BA_EXAM_NAME, certs)
        self.assertNotIn(PAB_EXAM_NAME, certs)

    def test_all_five_official_domains_have_at_least_one_record(self):
        scc = get_sales_cloud_consultant_definition()
        expected_domains = {domain.domain_name for domain in scc.domains}
        self.assertEqual(
            expected_domains,
            {
                "Practical Application of Sales Cloud Expertise",
                "Sales Lifecycle",
                "Consulting & Implementation Strategies",
                "Data Management",
                "Predictive and Generative AI",
            },
        )
        covered = {item["domain"] for item in self.payload["evidence_items"]}
        self.assertEqual(expected_domains, covered)

    def test_every_record_uses_official_salesforce_trailhead_source(self):
        for item in self.payload["evidence_items"]:
            self.assertTrue(item["canonical_url"].startswith("https://trailhead.salesforce.com/"))
            self.assertEqual(item["source_url"], item["canonical_url"])
            self.assertEqual(item["publisher"], "Salesforce, Inc.")
            self.assertEqual(item["provenance_status"], "verified_official_resource_library")
            self.assertEqual(item["certification_code"], "Sales-Con-201")

    def test_every_record_is_compact_and_hash_aligned(self):
        self.assertLessEqual(len(self.payload["evidence_items"]), 10)
        seen_hashes = set()
        seen_ids = set()
        for item in self.payload["evidence_items"]:
            excerpt = item["chunk_text_excerpt"]
            self.assertEqual(item["content_hash"], sha256_hex(excerpt))
            self.assertNotIn(excerpt, seen_hashes)
            seen_hashes.add(excerpt)
            self.assertNotIn(item["resource_chunk_id"], seen_ids)
            seen_ids.add(item["resource_chunk_id"])
            self.assertNotIn("Exam Practice Questions", excerpt)
            self.assertNotIn("flashcard", excerpt.lower())

    def test_each_domain_maps_to_exactly_one_official_domain(self):
        valid_domains = {
            "Practical Application of Sales Cloud Expertise",
            "Sales Lifecycle",
            "Consulting & Implementation Strategies",
            "Data Management",
            "Predictive and Generative AI",
        }
        for item in self.payload["evidence_items"]:
            self.assertIn(item["domain"], valid_domains)
            self.assertEqual(item["domain_tags"], [item["domain"]])

    def test_unknown_or_misspelled_domain_is_rejected_by_registry(self):
        from workers.certification_registry import (
            CertificationRegistryError,
            validate_certification_domain,
        )

        with self.assertRaises(CertificationRegistryError):
            validate_certification_domain(SCC_EXAM_NAME, "Practical Applications of Sales Cloud Expertise")
        with self.assertRaises(CertificationRegistryError):
            validate_certification_domain(SCC_EXAM_NAME, "Nonexistent SCC Domain")
        self.assertEqual(
            validate_certification_domain(SCC_EXAM_NAME, "Sales Lifecycle"),
            SCC_EXAM_NAME,
        )

    def test_duplicate_chunk_ids_are_rejected_by_validator(self):
        tampered = json.loads(json.dumps(self.payload))
        tampered["evidence_items"][1]["resource_chunk_id"] = tampered["evidence_items"][0][
            "resource_chunk_id"
        ]
        with self.assertRaises(OfficialEvidenceSeedError):
            validate_fixture_payload(tampered)

    def test_unknown_domain_yields_no_qualifying_match(self):
        result = retrieve_official_evidence_from_fixture(
            certification_exam_name=SCC_EXAM_NAME,
            domain_name="Nonexistent Domain",
            query_text="completely unrelated gibberish text xyzzy plugh",
            fixture_payload=self.payload,
        )
        self.assertEqual(result["evidence_identity"], SCC_FIXTURE_VERSION)
        self.assertEqual(result["ranked_chunks"], [])

    def test_scc_retrieval_never_returns_other_certifications_chunk_ids(self):
        adm_payload = load_evidence_fixture_for_certification(ADM_EXAM_NAME)
        ba_payload = load_evidence_fixture_for_certification(BA_EXAM_NAME)
        pab_payload = load_evidence_fixture_for_certification(PAB_EXAM_NAME)
        scc_chunk_ids = {item["resource_chunk_id"] for item in self.payload["evidence_items"]}
        other_chunk_ids = (
            {item["resource_chunk_id"] for item in adm_payload["evidence_items"]}
            | {item["resource_chunk_id"] for item in ba_payload["evidence_items"]}
            | {item["resource_chunk_id"] for item in pab_payload["evidence_items"]}
        )
        self.assertEqual(scc_chunk_ids.intersection(other_chunk_ids), set())

        result = retrieve_official_evidence_from_fixture(
            certification_exam_name=SCC_EXAM_NAME,
            domain_name="Data Management",
            query_text="data quality dimensions assess accuracy completeness consistency",
            fixture_payload=self.payload,
        )
        returned_ids = {chunk["resource_chunk_id"] for chunk in result["ranked_chunks"]}
        self.assertTrue(returned_ids.issubset(scc_chunk_ids))
        self.assertEqual(returned_ids.intersection(other_chunk_ids), set())

    def test_retrieval_is_deterministic(self):
        first = retrieve_official_evidence_from_fixture(
            certification_exam_name=SCC_EXAM_NAME,
            domain_name="Predictive and Generative AI",
            query_text="predictive AI generative AI new content trends",
            fixture_payload=self.payload,
        )
        second = retrieve_official_evidence_from_fixture(
            certification_exam_name=SCC_EXAM_NAME,
            domain_name="Predictive and Generative AI",
            query_text="predictive AI generative AI new content trends",
            fixture_payload=self.payload,
        )
        self.assertEqual(
            [c["resource_chunk_id"] for c in first["ranked_chunks"]],
            [c["resource_chunk_id"] for c in second["ranked_chunks"]],
        )


class TestPlatformAppBuilderFixtureRetrievalSmoke(unittest.TestCase):
    """No-model, local smoke retrieval against the isolated PAB fixture only."""

    def setUp(self):
        self.pab_payload = load_evidence_fixture_for_certification(PAB_EXAM_NAME)
        self.adm_payload = load_evidence_fixture_for_certification(ADM_EXAM_NAME)

    def test_each_pab_domain_returns_official_evidence(self):
        queries = {
            "Salesforce Fundamentals": "sharing solutions object record field access reports dashboards",
            "User Interface": "user interface customization Lightning components record types",
            "Data Modeling and Management": "data model relationship types schema builder field types",
            "Business Logic and Process Automation": "formula fields validation rules approval processes automation",
            "App Deployment": "application lifecycle sandbox changesets packaging deployment",
        }
        for domain_name, query in queries.items():
            with self.subTest(domain=domain_name):
                result = retrieve_official_evidence_from_fixture(
                    certification_exam_name=PAB_EXAM_NAME,
                    domain_name=domain_name,
                    query_text=query,
                    fixture_payload=self.pab_payload,
                )
                self.assertEqual(result["evidence_identity"], PAB_FIXTURE_VERSION)
                self.assertGreaterEqual(len(result["ranked_chunks"]), 1)
                for chunk in result["ranked_chunks"]:
                    self.assertEqual(chunk["retrieval_rank"] >= 1, True)

    def test_pab_retrieval_never_returns_adm_or_ba_chunk_ids(self):
        pab_chunk_ids = {
            item["resource_chunk_id"] for item in self.pab_payload["evidence_items"]
        }
        adm_ba_chunk_ids = {
            item["resource_chunk_id"] for item in self.adm_payload["evidence_items"]
        }
        self.assertEqual(pab_chunk_ids.intersection(adm_ba_chunk_ids), set())

        result = retrieve_official_evidence_from_fixture(
            certification_exam_name=PAB_EXAM_NAME,
            domain_name="Business Logic and Process Automation",
            query_text="approval processes validation rules formula fields automation",
            fixture_payload=self.pab_payload,
        )
        returned_ids = {chunk["resource_chunk_id"] for chunk in result["ranked_chunks"]}
        self.assertTrue(returned_ids.issubset(pab_chunk_ids))
        self.assertEqual(returned_ids.intersection(adm_ba_chunk_ids), set())

    def test_administrator_retrieval_is_unaffected_by_pab_fixture(self):
        result = retrieve_official_evidence_from_fixture(
            certification_exam_name=ADM_EXAM_NAME,
            domain_name="Configuration and Setup",
            query_text="lookup relationship deleted record recycle bin",
            fixture_payload=self.adm_payload,
        )
        self.assertEqual(result["evidence_identity"], FIXTURE_VERSION)
        pab_chunk_ids = {
            item["resource_chunk_id"] for item in self.pab_payload["evidence_items"]
        }
        returned_ids = {chunk["resource_chunk_id"] for chunk in result["ranked_chunks"]}
        self.assertEqual(returned_ids.intersection(pab_chunk_ids), set())

    def test_unknown_pab_domain_yields_no_qualifying_match(self):
        result = retrieve_official_evidence_from_fixture(
            certification_exam_name=PAB_EXAM_NAME,
            domain_name="Nonexistent Domain",
            query_text="completely unrelated gibberish text xyzzy plugh",
            fixture_payload=self.pab_payload,
        )
        self.assertEqual(result["ranked_chunks"], [])

    def test_retrieval_is_deterministic(self):
        first = retrieve_official_evidence_from_fixture(
            certification_exam_name=PAB_EXAM_NAME,
            domain_name="App Deployment",
            query_text="sandbox changesets packaging deployment lifecycle",
            fixture_payload=self.pab_payload,
        )
        second = retrieve_official_evidence_from_fixture(
            certification_exam_name=PAB_EXAM_NAME,
            domain_name="App Deployment",
            query_text="sandbox changesets packaging deployment lifecycle",
            fixture_payload=self.pab_payload,
        )
        self.assertEqual(
            [c["resource_chunk_id"] for c in first["ranked_chunks"]],
            [c["resource_chunk_id"] for c in second["ranked_chunks"]],
        )


class _PabEvidenceFakeClient:
    """Minimal Supabase-shaped fake client, mirroring
    tests/test_ai_quality_audit_evidence.py::EvidenceFakeClient, used to
    prove the *real* operational evidence-loading path
    (workers.ai_quality_audit_evidence.prepare_smoke_evidence_set) can
    retrieve Platform App Builder evidence once official_resources /
    resource_chunks rows exist for it -- not just the fixture-specific
    retrieve_official_evidence_from_fixture() test helper above.
    """

    def __init__(self):
        self._tables: dict[str, list[dict]] = {}
        self._candidate_rows: list[dict] = []
        self.rpc_calls: list[tuple[str, dict]] = []

    def set_table(self, name, rows):
        self._tables[name] = list(rows)

    def table(self, name):
        return _PabFakeTableQuery(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        if name == "get_question_version_blind_context_v1":
            return _PabFakeRpcBuilder(
                [
                    {
                        "question_version_id": params["p_question_version_id"],
                        "question_id": 1,
                        "certification_exam_name": PAB_EXAM_NAME,
                        "domain_name": "Business Logic and Process Automation",
                        "question_text": (
                            "Which approach should a Platform App Builder recommend for a "
                            "newly designed approval process with validation rules?"
                        ),
                        "question_type": "single",
                        "select_count": 1,
                        "options": [
                            {"option_label": "A", "option_text": "Salesforce Flow", "display_order": 1},
                        ],
                    }
                ]
            )
        if name == "list_audit_candidate_resource_chunks_v1":
            return _PabFakeRpcBuilder(list(self._candidate_rows))
        raise AssertionError(f"unexpected rpc {name!r}")


class _PabFakeRpcResult:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error


class _PabFakeRpcBuilder:
    def __init__(self, data, error=None):
        self._data = data
        self._error = error

    def execute(self):
        return _PabFakeRpcResult(self._data, self._error)


class _PabFakeTableQuery:
    def __init__(self, client, table_name):
        self._client = client
        self._table_name = table_name
        self._filters: list[tuple[str, str, object]] = []

    def select(self, _fields):
        return self

    def eq(self, field, value):
        self._filters.append(("eq", field, value))
        return self

    def execute(self):
        rows = list(self._client._tables.get(self._table_name, []))
        for _, field, value in self._filters:
            rows = [row for row in rows if row.get(field) == value]
        return _PabFakeRpcResult(rows)


class TestPlatformAppBuilderOperationalRetrievalPath(unittest.TestCase):
    """PAB-EXP-04A Correction 4: proves the real generation/audit runtime
    path (prepare_smoke_evidence_set) retrieves PAB evidence, seeded from the
    verified official_evidence_pab_v1.json fixture converted into
    official_resources/resource_chunks-shaped rows. This is Option A/B in
    combination: certification-based routing already works correctly at the
    live-database layer purely via the certification_exam_name column filter
    (there is no evidence_config_id concept at that layer); this test proves
    that once PAB rows exist there, retrieval genuinely works end to end.
    """

    def setUp(self):
        from workers.ai_quality_audit_evidence import prepare_smoke_evidence_set

        self.prepare_smoke_evidence_set = prepare_smoke_evidence_set
        payload = load_evidence_fixture_for_certification(PAB_EXAM_NAME)
        items = filter_fixture_items_by_certification(payload, PAB_EXAM_NAME)
        self.assertTrue(items)

        self.client = _PabEvidenceFakeClient()
        resource_rows = []
        seen_resource_ids: set[str] = set()
        for item in items:
            row = fixture_item_to_resource_row(item)
            row = {
                **row,
                "certification_exam_name": PAB_EXAM_NAME,
                "is_active": True,
            }
            if row["id"] not in seen_resource_ids:
                seen_resource_ids.add(row["id"])
                resource_rows.append(row)
        self.client.set_table("official_resources", resource_rows)
        self.client._candidate_rows = [
            fixture_item_to_candidate_row(item) for item in items
        ]
        self.qvid = "cccccccc-0000-0000-0000-0000000000aa"

    def test_prepare_smoke_evidence_set_retrieves_pab_evidence(self):
        prepared = self.prepare_smoke_evidence_set(self.client, self.qvid)
        self.assertGreaterEqual(len(prepared.evidence_chunks), 1)
        self.assertEqual(prepared.certification_exam_name, PAB_EXAM_NAME)

        pab_payload = load_evidence_fixture_for_certification(PAB_EXAM_NAME)
        pab_chunk_ids = {
            item["resource_chunk_id"] for item in pab_payload["evidence_items"]
        }
        for chunk in prepared.evidence_chunks:
            self.assertIn(chunk["resource_chunk_id"], pab_chunk_ids)


if __name__ == "__main__":
    unittest.main()
