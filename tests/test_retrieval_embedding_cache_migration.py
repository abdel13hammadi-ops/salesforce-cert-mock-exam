"""Migration-contract tests for retrieval_embedding_cache (V48 hybrid Stage 2
prerequisite: durable embedding-cache persistence foundation).

These are static, text-based checks against the migration SQL (no live
database connection is available in this environment), following the same
convention as tests/test_retrieval_shadow_evaluations_migration.py and
tests/test_retrieval_shadow_evaluations_privilege_correction_migration.py.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260703210000_v48_retrieval_embedding_cache_foundation.sql"
)

_SCOPE_EXCLUDED_TABLES = (
    "retrieval_shadow_evaluations",
    "audit_runs",
    "audit_run_dedup_keys",
    "audit_run_evidence_set",
)

_FORBIDDEN_REGRANT_PRIVILEGES = (
    "UPDATE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
    "ALL",
)


def _grant_statements(sql: str) -> list[str]:
    return [
        line.strip().rstrip(";")
        for line in sql.splitlines()
        if line.strip().startswith("GRANT")
    ]


def _revoke_statements(sql: str) -> list[str]:
    return [
        line.strip().rstrip(";")
        for line in sql.splitlines()
        if line.strip().startswith("REVOKE")
    ]


class TestRetrievalEmbeddingCacheMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")
        cls.table_block = cls.sql.split(
            "CREATE TABLE IF NOT EXISTS public.retrieval_embedding_cache (",
            1,
        )[1].split("\nCREATE INDEX", 1)[0]

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.is_file())

    def test_table_is_created_additively(self):
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS public.retrieval_embedding_cache (",
            self.sql,
        )
        self.assertNotIn("DROP TABLE", self.sql)
        self.assertNotIn("TRUNCATE TABLE", self.sql)

    def test_no_pgvector_extension_introduced(self):
        # Prose comments are allowed to explain *why* pgvector was
        # deliberately not used; only actual extension/type usage is
        # forbidden.
        self.assertNotIn("CREATE EXTENSION", self.sql)
        self.assertNotRegex(self.table_block.lower(), r"\bvector\(")

    def test_required_fields_present(self):
        for column in (
            "id",
            "content_scope",
            "content_hash",
            "embedding_provider_name",
            "embedding_model_name",
            "embedding_model_version",
            "embedding_dimensions",
            "embedding_vector",
            "provider_response_hash",
            "created_at",
        ):
            with self.subTest(column=column):
                self.assertRegex(self.sql, rf"\b{column}\b\s+\S")

    def test_embedding_vector_is_double_precision_array(self):
        self.assertRegex(
            self.sql,
            r"embedding_vector\s+double precision\[\]\s+NOT NULL",
        )

    def test_unique_identity_constraint_exact_columns(self):
        self.assertIn(
            "CONSTRAINT retrieval_embedding_cache_unique_identity",
            self.sql,
        )
        unique_block = self.sql.split(
            "CONSTRAINT retrieval_embedding_cache_unique_identity", 1
        )[1].split(")", 1)[0]
        expected_columns = (
            "content_scope",
            "content_hash",
            "embedding_provider_name",
            "embedding_model_name",
            "embedding_model_version",
            "embedding_dimensions",
        )
        for column in expected_columns:
            with self.subTest(column=column):
                self.assertIn(column, unique_block)
        # Exactness: no unexpected extra column sneaks into the tuple.
        column_count = len([c for c in unique_block.split(",") if c.strip()])
        self.assertEqual(column_count, len(expected_columns))

    def test_content_scope_allowlist(self):
        self.assertIn(
            "CONSTRAINT retrieval_embedding_cache_content_scope_valid",
            self.sql,
        )
        for value in ("query", "chunk"):
            with self.subTest(value=value):
                self.assertIn(f"'{value}'", self.sql)

    def test_content_scope_allowlist_excludes_other_values(self):
        scope_block = self.sql.split(
            "CONSTRAINT retrieval_embedding_cache_content_scope_valid", 1
        )[1].split(")", 1)[0]
        self.assertNotIn("resource", scope_block)
        self.assertNotIn("document", scope_block)

    def test_names_and_versions_require_nonempty_trim(self):
        for constraint, column in (
            ("retrieval_embedding_cache_provider_name_nonempty", "embedding_provider_name"),
            ("retrieval_embedding_cache_model_name_nonempty", "embedding_model_name"),
            ("retrieval_embedding_cache_model_version_nonempty", "embedding_model_version"),
        ):
            with self.subTest(constraint=constraint):
                self.assertIn(f"CONSTRAINT {constraint}", self.sql)
                self.assertIn(f"CHECK (TRIM({column}) <> '')", self.sql)

    def test_content_hash_format_is_sha256_hex(self):
        self.assertIn(
            "CONSTRAINT retrieval_embedding_cache_content_hash_format",
            self.sql,
        )
        self.assertIn("CHECK (content_hash ~ '^[0-9a-f]{64}$')", self.sql)

    def test_provider_response_hash_format_is_sha256_hex(self):
        self.assertIn(
            "CONSTRAINT retrieval_embedding_cache_provider_response_hash_format",
            self.sql,
        )
        self.assertIn(
            "CHECK (provider_response_hash ~ '^[0-9a-f]{64}$')",
            self.sql,
        )

    def test_dimensions_must_be_positive(self):
        self.assertIn(
            "CONSTRAINT retrieval_embedding_cache_dimensions_positive",
            self.sql,
        )
        self.assertIn("CHECK (embedding_dimensions > 0)", self.sql)

    def test_vector_must_be_one_dimensional(self):
        self.assertIn(
            "CONSTRAINT retrieval_embedding_cache_vector_is_one_dimensional",
            self.sql,
        )
        self.assertIn(
            "CHECK (array_ndims(embedding_vector) = 1)",
            self.sql,
        )

    def test_vector_cardinality_must_match_dimensions(self):
        self.assertIn(
            "CONSTRAINT retrieval_embedding_cache_vector_cardinality_matches_dimensions",
            self.sql,
        )
        self.assertIn(
            "CHECK (COALESCE(array_length(embedding_vector, 1), 0) = embedding_dimensions)",
            self.sql,
        )

    def test_vector_must_have_no_null_elements(self):
        self.assertIn(
            "CONSTRAINT retrieval_embedding_cache_vector_has_no_null_elements",
            self.sql,
        )
        self.assertIn(
            "CHECK (array_position(embedding_vector, NULL::double precision) IS NULL)",
            self.sql,
        )

    def test_rls_is_enabled(self):
        self.assertIn(
            "ALTER TABLE public.retrieval_embedding_cache ENABLE ROW LEVEL SECURITY;",
            self.sql,
        )

    def test_public_anon_authenticated_are_explicitly_revoked(self):
        for role in ("PUBLIC", "anon", "authenticated"):
            with self.subTest(role=role):
                self.assertIn(
                    f"REVOKE ALL ON TABLE public.retrieval_embedding_cache FROM {role};",
                    self.sql,
                )

    def test_service_role_is_revoked_before_being_regranted(self):
        revoke_idx = self.sql.index(
            "REVOKE ALL ON TABLE public.retrieval_embedding_cache FROM service_role;"
        )
        grant_idx = self.sql.index(
            "GRANT SELECT, INSERT, DELETE ON TABLE public.retrieval_embedding_cache TO service_role;"
        )
        self.assertLess(revoke_idx, grant_idx)

    def test_only_select_insert_delete_are_regranted_to_service_role(self):
        grant_lines = [
            line
            for line in _grant_statements(self.sql)
            if "public.retrieval_embedding_cache" in line
        ]
        self.assertTrue(grant_lines, "expected at least one GRANT on the table")
        for line in grant_lines:
            with self.subTest(line=line):
                self.assertIn("service_role", line)
                privileges = line.split("ON TABLE", 1)[0].replace("GRANT", "", 1)
                granted = {token.strip() for token in privileges.split(",")}
                self.assertEqual(granted, {"SELECT", "INSERT", "DELETE"})

    def test_forbidden_privileges_are_never_granted(self):
        grant_lines = [
            line
            for line in _grant_statements(self.sql)
            if "public.retrieval_embedding_cache" in line
        ]
        for line in grant_lines:
            privileges_clause = line.split("ON TABLE", 1)[0]
            for forbidden in _FORBIDDEN_REGRANT_PRIVILEGES:
                with self.subTest(line=line, forbidden=forbidden):
                    self.assertNotIn(forbidden, privileges_clause)

    def test_no_anon_or_authenticated_grants_or_policies(self):
        self.assertNotIn("TO anon", self.sql)
        self.assertNotIn("TO authenticated", self.sql)
        self.assertNotIn("CREATE POLICY", self.sql)

    def test_no_rpc_or_function_added(self):
        self.assertNotIn("CREATE OR REPLACE FUNCTION", self.sql)
        self.assertNotIn("CREATE FUNCTION", self.sql)
        self.assertNotIn("DROP FUNCTION", self.sql)

    def test_no_foreign_key_or_reference_to_excluded_tables(self):
        for table in _SCOPE_EXCLUDED_TABLES:
            with self.subTest(table=table):
                self.assertNotIn(f"REFERENCES public.{table}", self.sql)
                self.assertNotIn(f"ALTER TABLE public.{table}", self.sql)

    def test_excluded_tables_are_not_modified_in_working_tree(self):
        # Scoped to each excluded table's own migration file(s), not a bare
        # substring match against every changed path: test filenames for
        # those tables legitimately contain the table name without
        # modifying the table's schema.
        excluded_migration_globs = {
            "retrieval_shadow_evaluations": (
                "20260702230000_v48_retrieval_shadow_evaluations_foundation.sql",
                "20260703200000_v48_retrieval_shadow_evaluations_privilege_correction.sql",
            ),
        }
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        changed = set(result.stdout.splitlines())
        for table in _SCOPE_EXCLUDED_TABLES:
            for filename in excluded_migration_globs.get(table, ()):
                with self.subTest(table=table, filename=filename):
                    matching = [path for path in changed if path.endswith(filename)]
                    self.assertEqual(matching, [])

    def test_no_runtime_or_worker_files_changed(self):
        result = subprocess.run(
            ["git", "status", "--porcelain", "workers"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.stdout.strip(), "")

    def test_no_update_privilege_comment_documents_immutability(self):
        # The design intent (no UPDATE-based mutation) must be explicit, not
        # just an absence -- guards against a future edit silently adding
        # UPDATE without updating the documented contract.
        self.assertIn("write-once", self.sql)
        self.assertIn("immutable", self.sql)


if __name__ == "__main__":
    unittest.main()
