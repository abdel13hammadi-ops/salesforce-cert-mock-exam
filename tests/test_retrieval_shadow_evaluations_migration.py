"""Migration-contract tests for retrieval_shadow_evaluations (V48 hybrid Stage 1).

These are static, text-based checks against the migration SQL (no live database
connection is available in this environment), following the same convention as
tests/test_repair_ba_multiselect_batch_migration.py and
tests/test_repair_question_1067_migration.py.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260702230000_v48_retrieval_shadow_evaluations_foundation.sql"
)

_FORBIDDEN_AUDIT_TABLES = (
    "audit_runs",
    "audit_run_dedup_keys",
    "audit_run_evidence_set",
)

_FORBIDDEN_STAGE2_FIELDS = (
    "semantic_similarity",
    "embedding",
    "provider_error",
    "qualified_v2",
    "l3_",
    "l4_",
    "audit_run_id",
    "evidence_set_hash",
)


def _extract_check_expression(sql: str, constraint_name: str) -> str:
    """Return the parenthesized body of one named CHECK constraint's CHECK (...).

    Scans forward from ``CHECK (`` tracking paren depth so nested boolean
    groups (AND/OR blocks) are captured whole, then strips the outer parens.
    """
    marker = f"CONSTRAINT {constraint_name}"
    start = sql.index(marker)
    check_idx = sql.index("CHECK (", start)
    paren_start = check_idx + len("CHECK ")
    assert sql[paren_start] == "("
    depth = 0
    for i in range(paren_start, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return sql[paren_start + 1 : i]
    raise AssertionError(f"unbalanced parens in CHECK for {constraint_name}")


def _sql_bool_expr_to_python(expr: str) -> str:
    """Translate a simple SQL boolean CHECK expression to an evaluable Python one.

    Only handles the operators actually used by this migration's CHECK
    constraints (AND, OR, bare '=' equality, '>' ); no LIKE/IN/casts.
    """
    python_expr = re.sub(r"\bAND\b", "and", expr)
    python_expr = re.sub(r"\bOR\b", "or", python_expr)
    python_expr = re.sub(r"(?<![<>=!])=(?!=)", "==", python_expr)
    return python_expr


def _evaluate_confidence_class_coupling(
    sql: str,
    *,
    confidence_class: str,
    qualified_count_v1: int,
    structural_candidate_count: int,
) -> bool:
    expr = _extract_check_expression(
        sql, "retrieval_shadow_evaluations_confidence_class_count_coupling"
    )
    # Collapse to a single line: eval() in expression mode cannot tolerate a
    # bare NEWLINE once bracket depth returns to zero between OR-joined groups.
    python_expr = " ".join(_sql_bool_expr_to_python(expr).split())
    return bool(
        eval(  # noqa: S307 - trusted, repo-local migration SQL, no external input
            python_expr,
            {"__builtins__": {}},
            {
                "confidence_class": confidence_class,
                "qualified_count_v1": qualified_count_v1,
                "structural_candidate_count": structural_candidate_count,
            },
        )
    )


class TestRetrievalShadowEvaluationsMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.is_file())

    def test_table_is_created_additively(self):
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS public.retrieval_shadow_evaluations (",
            self.sql,
        )
        self.assertNotIn("DROP TABLE", self.sql)
        self.assertNotIn("TRUNCATE", self.sql)

    def test_required_identity_columns_present(self):
        for column in (
            "id",
            "evaluation_run_id",
            "question_version_id",
            "certification_exam_name",
            "baseline_retrieval_method",
            "proposed_retrieval_method",
            "schema_version",
            "created_at",
        ):
            with self.subTest(column=column):
                self.assertRegex(self.sql, rf"\b{column}\b\s+\w")

    def test_required_stage1_result_columns_present(self):
        for column in (
            "confidence_class",
            "candidate_count",
            "qualified_count_v1",
            "structural_candidate_count",
            "candidates_json",
        ):
            with self.subTest(column=column):
                self.assertRegex(self.sql, rf"\b{column}\b\s+\w")

    def test_rls_is_enabled(self):
        self.assertIn(
            "ALTER TABLE public.retrieval_shadow_evaluations ENABLE ROW LEVEL SECURITY;",
            self.sql,
        )

    def test_no_anon_or_authenticated_access(self):
        self.assertNotIn("TO anon", self.sql)
        self.assertNotIn("TO authenticated", self.sql)
        self.assertNotIn("CREATE POLICY", self.sql)

    def test_anon_privileges_are_explicitly_revoked(self):
        self.assertIn(
            "REVOKE ALL ON TABLE public.retrieval_shadow_evaluations FROM anon;",
            self.sql,
        )

    def test_authenticated_privileges_are_explicitly_revoked(self):
        self.assertIn(
            "REVOKE ALL ON TABLE public.retrieval_shadow_evaluations FROM authenticated;",
            self.sql,
        )

    def test_public_privileges_are_explicitly_revoked(self):
        self.assertIn(
            "REVOKE ALL ON TABLE public.retrieval_shadow_evaluations FROM PUBLIC;",
            self.sql,
        )

    def test_only_service_role_is_granted_table_access(self):
        grant_lines = [
            line.strip()
            for line in self.sql.splitlines()
            if line.strip().startswith("GRANT")
            and "public.retrieval_shadow_evaluations" in line
        ]
        self.assertTrue(grant_lines, "expected at least one GRANT on the table")
        for line in grant_lines:
            with self.subTest(line=line):
                self.assertIn("TO service_role", line)
                self.assertNotIn("anon", line)
                self.assertNotIn("authenticated", line)
                self.assertNotIn("PUBLIC", line)

    def test_rls_is_not_the_only_access_control(self):
        # The migration must not rely solely on RLS/absence-of-GRANT: an
        # explicit REVOKE...FROM anon/authenticated plus an explicit
        # GRANT...TO service_role must both be present.
        self.assertIn("REVOKE ALL ON TABLE public.retrieval_shadow_evaluations", self.sql)
        self.assertIn("GRANT SELECT, INSERT, DELETE ON TABLE public.retrieval_shadow_evaluations TO service_role;", self.sql)

    def test_unique_evaluation_identity_constraint(self):
        self.assertIn(
            "CONSTRAINT retrieval_shadow_evaluations_unique_identity",
            self.sql,
        )
        unique_block = self.sql.split(
            "CONSTRAINT retrieval_shadow_evaluations_unique_identity", 1
        )[1].split(")", 1)[0]
        for column in (
            "evaluation_run_id",
            "question_version_id",
            "proposed_retrieval_method",
            "schema_version",
        ):
            with self.subTest(column=column):
                self.assertIn(column, unique_block)

    def test_confidence_class_allowlist(self):
        self.assertIn(
            "CONSTRAINT retrieval_shadow_evaluations_confidence_class_valid",
            self.sql,
        )
        for value in (
            "v1_sufficient",
            "semantic_review_candidate",
            "no_structural_candidate",
        ):
            with self.subTest(value=value):
                self.assertIn(f"'{value}'", self.sql)

    def test_counts_are_nonnegative(self):
        for constraint, column in (
            ("retrieval_shadow_evaluations_candidate_count_nonneg", "candidate_count"),
            ("retrieval_shadow_evaluations_qualified_v1_nonneg", "qualified_count_v1"),
            (
                "retrieval_shadow_evaluations_structural_count_nonneg",
                "structural_candidate_count",
            ),
        ):
            with self.subTest(constraint=constraint):
                self.assertIn(f"CONSTRAINT {constraint}", self.sql)
                self.assertIn(f"CHECK ({column} >= 0)", self.sql)

    def test_qualified_and_structural_counts_bounded_by_candidate_count(self):
        self.assertIn(
            "CONSTRAINT retrieval_shadow_evaluations_qualified_v1_le_candidates",
            self.sql,
        )
        self.assertIn(
            "CHECK (qualified_count_v1 <= candidate_count)",
            self.sql,
        )
        self.assertIn(
            "CONSTRAINT retrieval_shadow_evaluations_structural_le_candidates",
            self.sql,
        )
        self.assertIn(
            "CHECK (structural_candidate_count <= candidate_count)",
            self.sql,
        )

    def test_candidates_json_must_be_array(self):
        self.assertIn(
            "CONSTRAINT retrieval_shadow_evaluations_candidates_json_is_array",
            self.sql,
        )
        self.assertIn(
            "CHECK (jsonb_typeof(candidates_json) = 'array')",
            self.sql,
        )

    def test_string_identity_fields_require_nonempty_trim(self):
        for constraint, column in (
            ("retrieval_shadow_evaluations_exam_name_nonempty", "certification_exam_name"),
            (
                "retrieval_shadow_evaluations_baseline_method_nonempty",
                "baseline_retrieval_method",
            ),
            (
                "retrieval_shadow_evaluations_proposed_method_nonempty",
                "proposed_retrieval_method",
            ),
            ("retrieval_shadow_evaluations_schema_version_nonempty", "schema_version"),
        ):
            with self.subTest(constraint=constraint):
                self.assertIn(f"CONSTRAINT {constraint}", self.sql)
                self.assertIn(f"CHECK (TRIM({column}) <> '')", self.sql)

    def test_qualified_v1_bounded_by_structural_candidate_count(self):
        self.assertIn(
            "CONSTRAINT retrieval_shadow_evaluations_qualified_v1_le_structural",
            self.sql,
        )
        self.assertIn(
            "CHECK (qualified_count_v1 <= structural_candidate_count)",
            self.sql,
        )

    def test_candidate_count_matches_json_array_length(self):
        self.assertIn(
            "CONSTRAINT retrieval_shadow_evaluations_candidate_count_matches_json",
            self.sql,
        )
        self.assertIn(
            "CHECK (candidate_count = jsonb_array_length(candidates_json))",
            self.sql,
        )

    def test_confidence_class_count_coupling_constraint_present(self):
        self.assertIn(
            "CONSTRAINT retrieval_shadow_evaluations_confidence_class_count_coupling",
            self.sql,
        )

    def test_confidence_class_coupling_accepts_valid_combinations(self):
        valid_cases = (
            ("v1_sufficient", 1, 1),
            ("v1_sufficient", 3, 10),
            ("v1_sufficient", 1, 25),
            ("semantic_review_candidate", 0, 1),
            ("semantic_review_candidate", 0, 25),
            ("no_structural_candidate", 0, 0),
        )
        for confidence_class, qualified_count_v1, structural_candidate_count in valid_cases:
            with self.subTest(
                confidence_class=confidence_class,
                qualified_count_v1=qualified_count_v1,
                structural_candidate_count=structural_candidate_count,
            ):
                self.assertTrue(
                    _evaluate_confidence_class_coupling(
                        self.sql,
                        confidence_class=confidence_class,
                        qualified_count_v1=qualified_count_v1,
                        structural_candidate_count=structural_candidate_count,
                    )
                )

    def test_confidence_class_coupling_rejects_contradictory_combinations(self):
        invalid_cases = (
            # v1_sufficient requires qualified_count_v1 > 0
            ("v1_sufficient", 0, 5),
            ("v1_sufficient", 0, 0),
            # semantic_review_candidate requires qualified_count_v1 = 0
            ("semantic_review_candidate", 1, 5),
            # semantic_review_candidate requires structural_candidate_count > 0
            ("semantic_review_candidate", 0, 0),
            # no_structural_candidate requires structural_candidate_count = 0
            ("no_structural_candidate", 0, 3),
            # no_structural_candidate requires qualified_count_v1 = 0
            ("no_structural_candidate", 1, 3),
            ("no_structural_candidate", 1, 0),
        )
        for confidence_class, qualified_count_v1, structural_candidate_count in invalid_cases:
            with self.subTest(
                confidence_class=confidence_class,
                qualified_count_v1=qualified_count_v1,
                structural_candidate_count=structural_candidate_count,
            ):
                self.assertFalse(
                    _evaluate_confidence_class_coupling(
                        self.sql,
                        confidence_class=confidence_class,
                        qualified_count_v1=qualified_count_v1,
                        structural_candidate_count=structural_candidate_count,
                    )
                )

    def test_no_foreign_key_to_live_audit_tables(self):
        for table in _FORBIDDEN_AUDIT_TABLES:
            with self.subTest(table=table):
                self.assertNotIn(f"REFERENCES public.{table}", self.sql)

    def test_question_version_reference_has_no_cascade(self):
        self.assertIn(
            "REFERENCES public.question_versions(id)",
            self.sql,
        )
        # question_versions rows are immutable/never deleted; no ON DELETE
        # behavior should be attached to this foreign key.
        fk_line = next(
            line
            for line in self.sql.splitlines()
            if "REFERENCES public.question_versions(id)" in line
        )
        self.assertNotIn("ON DELETE", fk_line)

    def test_evaluation_run_id_is_not_a_foreign_key(self):
        # evaluation_run_id must be a plain identifier column, not linked to
        # audit_runs or any other table by a foreign key.
        self.assertRegex(self.sql, r"evaluation_run_id\s+uuid\s+NOT NULL,")

    def test_no_stage2_or_embedding_fields_introduced(self):
        # Scope to the actual table definition (columns + constraints), not
        # the prose header comments that explicitly document what Stage 2
        # fields are intentionally excluded.
        table_block = self.sql.split(
            "CREATE TABLE IF NOT EXISTS public.retrieval_shadow_evaluations (",
            1,
        )[1].split("\nCREATE INDEX", 1)[0]
        lowered = table_block.lower()
        for token in _FORBIDDEN_STAGE2_FIELDS:
            with self.subTest(token=token):
                self.assertNotIn(token, lowered)

    def test_no_rpcs_added(self):
        self.assertNotIn("CREATE OR REPLACE FUNCTION", self.sql)
        self.assertNotIn("CREATE FUNCTION", self.sql)

    def test_indexes_support_lookup_by_question_and_sweep(self):
        self.assertIn(
            "ON public.retrieval_shadow_evaluations (question_version_id);",
            self.sql,
        )
        self.assertIn(
            "ON public.retrieval_shadow_evaluations (evaluation_run_id);",
            self.sql,
        )


if __name__ == "__main__":
    unittest.main()
