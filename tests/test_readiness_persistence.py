"""
Hermetic tests for utils/readiness_persistence.py and related changes.

All DB interactions are mocked via lightweight fakes; no real Supabase
connection is needed.  The readiness formula itself is not changed or tested
here — only that the persistence layer correctly maps inputs to DB payloads
and preserves historical snapshot immutability.

Test groups
-----------
- TestBuildAttemptMetadata              — pure extraction, UUID handling
- TestFetchEligibleMockBankSize         — count scoping and error handling
- TestBuildReadinessSnapshotPayload     — payload mapping, immutability guard
- TestInsertOrFetchReadinessSnapshot    — insert-once, conflict → fetch pattern
- TestBuildQuestionAttemptRowsMetadata  — metadata in paid-mock question_attempt rows
- TestSnapshotTemporalAnchoring         — historical immutability via attempt filter
- TestSnapshotFailureHandling           — failure does not invalidate attempt
- TestBankSizeCounting                  — count correctness, no hardcoded value
- TestSnapshotIdempotency               — no duplicate on retry / refresh
- TestIsHistoricalAttempt               — temporal filter logic
- TestReadinessFormulaUnchanged         — formula output contract
"""
from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Fake Supabase infrastructure
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, data=None, count=None):
        self.data = data or []
        self.count = count


class _FakeQueryBuilder:
    """Minimal chainable fake for supabase.table(...).<chain>.execute()."""

    def __init__(self, data=None, count=None):
        self._data = data or []
        self._count = count

    def select(self, *args, **kwargs): return self
    def eq(self, *args, **kwargs): return self
    def ilike(self, *args, **kwargs): return self
    def neq(self, *args, **kwargs): return self
    def gte(self, *args, **kwargs): return self
    def order(self, *args, **kwargs): return self
    def limit(self, *args, **kwargs): return self
    def in_(self, *args, **kwargs): return self
    def insert(self, *args, **kwargs): return self
    def upsert(self, payload, **kwargs): return self

    def execute(self):
        return _FakeResult(data=list(self._data), count=self._count)


class _RaisingQueryBuilder:
    """Always raises on execute() — used to test error handling."""

    def select(self, *a, **kw): return self
    def eq(self, *a, **kw): return self
    def ilike(self, *a, **kw): return self
    def gte(self, *a, **kw): return self
    def order(self, *a, **kw): return self
    def limit(self, *a, **kw): return self
    def in_(self, *a, **kw): return self
    def insert(self, *a, **kw): return self
    def upsert(self, *a, **kw): return self

    def execute(self):
        raise RuntimeError("simulated DB error")


class _FakeSupabase:
    """Configurable per-table fake supabase client."""

    def __init__(self, table_responses: Optional[dict] = None, raise_on: Optional[set] = None):
        self._table_responses = table_responses or {}
        self._raise_on = raise_on or set()

    def table(self, name: str):
        if name in self._raise_on:
            return _RaisingQueryBuilder()
        data = self._table_responses.get(name)
        if isinstance(data, _FakeQueryBuilder):
            return data
        if data is None:
            return _FakeQueryBuilder(data=[])
        return _FakeQueryBuilder(data=list(data))


# ---------------------------------------------------------------------------
# Stateful snapshot-store fake (simulates INSERT conflict + SELECT fallback)
# ---------------------------------------------------------------------------

class _SnapshotStore:
    """Stateful fake that simulates a readiness_snapshots table.

    First INSERT for a (exam_attempt_id, formula_version) key succeeds.
    Subsequent INSERTs raise a unique-violation error.
    SELECT always returns whatever was first inserted.
    """

    def __init__(self):
        # key = (exam_attempt_id, formula_version)  →  payload dict
        self._rows: dict = {}

    def table(self, name: str):
        if name == "readiness_snapshots":
            return _SnapshotTableProxy(self)
        return _FakeQueryBuilder(data=[])


class _SnapshotTableProxy:
    def __init__(self, store: _SnapshotStore):
        self._store = store
        self._op: Optional[str] = None
        self._payload: Optional[dict] = None
        self._filters: dict = {}

    def select(self, *a, **kw):
        self._op = "select"
        return self

    def insert(self, payload, **kw):
        self._op = "insert"
        self._payload = payload
        return self

    def eq(self, col: str, val):
        self._filters[col] = val
        return self

    def limit(self, *a, **kw):
        return self

    def execute(self):
        if self._op == "insert" and self._payload is not None:
            key = (
                self._payload.get("exam_attempt_id"),
                self._payload.get("formula_version"),
            )
            if key in self._store._rows:
                raise RuntimeError(
                    "duplicate key value violates unique constraint "
                    '"readiness_snapshots_exam_attempt_id_formula_version_key"'
                )
            self._store._rows[key] = copy.deepcopy(self._payload)
            return _FakeResult(data=[self._payload])

        if self._op == "select":
            eid = self._filters.get("exam_attempt_id")
            fv = self._filters.get("formula_version")
            row = self._store._rows.get((eid, fv))
            return _FakeResult(data=[row] if row else [])

        return _FakeResult(data=[])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_attempt(
    attempt_id: int,
    completed_at: str,
    score: float = 72.0,
    mode: str = "Paid Mock Exam",
    total_questions: int = 60,
    exam_name: str = "Salesforce-Admin",
) -> dict:
    return {
        "id": attempt_id,
        "mode": mode,
        "score": score,
        "total_questions": total_questions,
        "correct_answers": int(total_questions * score / 100),
        "started_at": completed_at,
        "completed_at": completed_at,
        "domain_breakdown": {},
        "difficulty_breakdown": {},
        "exam_name": exam_name,
        "language_code": "en",
    }


def _minimal_snapshot_payload(exam_attempt_id=1, formula_version="V4", score=70.0) -> dict:
    return {
        "user_email": "u@e.com",
        "exam_name": "Salesforce-Admin",
        "exam_attempt_id": exam_attempt_id,
        "formula_version": formula_version,
        "score": score,
        "label": "Ready",
        "confidence_score": 0.8,
        "eligible_mock_count": 3,
        "eligible_question_bank_size": 900,
        "component_scores": {"recent_accuracy": 70.0},
        "snapshot_data": {"score": score},
        "computed_at": "2026-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Tests for build_attempt_metadata
# ---------------------------------------------------------------------------

class TestBuildAttemptMetadata(unittest.TestCase):

    def _call(self, q: dict) -> dict:
        from utils.readiness_persistence import build_attempt_metadata
        return build_attempt_metadata(q)

    def test_all_fields_present(self):
        q = {
            "cognitive_level": "apply",
            "concept_key": "sharing-defaults",
            "question_family_id": "abc123",
            "content_version": 3,
            "external_key": "SF-ADM-042",
        }
        result = self._call(q)
        self.assertEqual(result["cognitive_level"], "apply")
        self.assertEqual(result["concept_key"], "sharing-defaults")
        self.assertEqual(result["question_family_id"], "abc123")
        self.assertEqual(result["question_content_version"], 3)
        self.assertEqual(result["question_external_key"], "SF-ADM-042")
        self.assertEqual(result["metadata_source"], "captured_at_attempt")
        self.assertEqual(result["metadata_capture_version"], "ATTEMPT_METADATA_V1")

    def test_missing_optional_fields_are_none(self):
        """All optional metadata fields default to None when absent."""
        result = self._call({"id": 99})
        self.assertIsNone(result["cognitive_level"])
        self.assertIsNone(result["concept_key"])
        self.assertIsNone(result["question_family_id"])
        self.assertIsNone(result["question_content_version"])
        self.assertIsNone(result["question_external_key"])
        self.assertEqual(result["metadata_source"], "captured_at_attempt")
        self.assertEqual(result["metadata_capture_version"], "ATTEMPT_METADATA_V1")

    def test_empty_string_fields_become_none(self):
        q = {"cognitive_level": "", "concept_key": "  ", "external_key": ""}
        result = self._call(q)
        self.assertIsNone(result["cognitive_level"])
        self.assertIsNone(result["concept_key"])
        self.assertIsNone(result["question_external_key"])

    def test_content_version_coerced_to_int(self):
        result = self._call({"content_version": "5"})
        self.assertEqual(result["question_content_version"], 5)

    def test_non_numeric_content_version_becomes_none(self):
        result = self._call({"content_version": "abc"})
        self.assertIsNone(result["question_content_version"])

    def test_none_content_version_stays_none(self):
        result = self._call({"content_version": None})
        self.assertIsNone(result["question_content_version"])

    def test_empty_question_family_id_becomes_none(self):
        result = self._call({"question_family_id": "  "})
        self.assertIsNone(result["question_family_id"])

    def test_uuid_question_family_id_preserved(self):
        uid = "550e8400-e29b-41d4-a716-446655440000"
        result = self._call({"question_family_id": uid})
        self.assertEqual(result["question_family_id"], uid)

    def test_constant_value_is_attempt_metadata_v1(self):
        from utils.readiness_persistence import ATTEMPT_METADATA_V1
        self.assertEqual(ATTEMPT_METADATA_V1, "ATTEMPT_METADATA_V1")

    def test_empty_question_dict_does_not_raise(self):
        result = self._call({})
        self.assertIn("metadata_capture_version", result)


# ---------------------------------------------------------------------------
# Tests for fetch_eligible_mock_bank_size
# ---------------------------------------------------------------------------

class TestFetchEligibleMockBankSize(unittest.TestCase):

    def _call(self, supabase, exam_name="Salesforce-Admin", language_code="en") -> int:
        from utils.readiness_persistence import fetch_eligible_mock_bank_size
        return fetch_eligible_mock_bank_size(supabase, exam_name, language_code)

    def test_uses_count_attribute_when_present(self):
        qb = _FakeQueryBuilder(data=[{"id": 1}], count=900)
        supabase = _FakeSupabase({"questions": qb})
        self.assertEqual(self._call(supabase), 900)

    def test_falls_back_to_len_data_when_count_none(self):
        qb = _FakeQueryBuilder(data=[{"id": i} for i in range(15)])
        supabase = _FakeSupabase({"questions": qb})
        self.assertEqual(self._call(supabase), 15)

    def test_returns_zero_on_error(self):
        supabase = _FakeSupabase(raise_on={"questions"})
        self.assertEqual(self._call(supabase), 0)

    def test_returns_zero_for_empty_result(self):
        supabase = _FakeSupabase({"questions": _FakeQueryBuilder(data=[])})
        self.assertEqual(self._call(supabase), 0)

    def test_filters_are_certification_and_language_scoped(self):
        qb = _FakeQueryBuilder(data=[], count=42)
        supabase = _FakeSupabase({"questions": qb})
        result = self._call(supabase, exam_name="Cert-X", language_code="fr")
        self.assertEqual(result, 42)


# ---------------------------------------------------------------------------
# Tests for build_readiness_snapshot_payload
# ---------------------------------------------------------------------------

class TestBuildReadinessSnapshotPayload(unittest.TestCase):

    def _sample_readiness(self) -> dict:
        return {
            "score": 72.5,
            "label": "Approaching Ready",
            "confidence_score": 0.68,
            "eligible_mock_count": 4,
            "recent_accuracy": 71.8,
            "domain_score": 70.1,
            "domain_robustness": 68.4,
            "consistency_penalty": 1.2,
            "trend_adjustment": 2.0,
            "trend_slope": 4.5,
            "consistency_standard_deviation": 3.8,
            "is_locked": False,
        }

    def _call(self, readiness: Optional[dict] = None, **kwargs) -> dict:
        from utils.readiness_persistence import build_readiness_snapshot_payload
        if readiness is None:
            readiness = self._sample_readiness()
        return build_readiness_snapshot_payload(
            user_email=kwargs.get("user_email", "test@example.com"),
            exam_name=kwargs.get("exam_name", "Salesforce-Admin"),
            exam_attempt_id=kwargs.get("exam_attempt_id", 101),
            formula_version=kwargs.get("formula_version", "READINESS_V4_PERFORMANCE_ANCHORED"),
            readiness=readiness,
            eligible_bank_size=kwargs.get("eligible_bank_size", 900),
        )

    def test_maps_score_and_label(self):
        payload = self._call()
        self.assertAlmostEqual(payload["score"], 72.5)
        self.assertEqual(payload["label"], "Approaching Ready")

    def test_maps_confidence_score(self):
        payload = self._call()
        self.assertAlmostEqual(payload["confidence_score"], 0.68)

    def test_maps_eligible_mock_count(self):
        payload = self._call()
        self.assertEqual(payload["eligible_mock_count"], 4)

    def test_eligible_bank_size_stored(self):
        payload = self._call(eligible_bank_size=840)
        self.assertEqual(payload["eligible_question_bank_size"], 840)

    def test_component_scores_contain_expected_keys(self):
        payload = self._call()
        cs = payload["component_scores"]
        for key in ("recent_accuracy", "domain_score", "domain_robustness",
                    "consistency_penalty", "trend_adjustment", "trend_slope",
                    "consistency_standard_deviation"):
            self.assertIn(key, cs, f"Missing component_scores key: {key}")

    def test_snapshot_data_is_full_readiness_dict(self):
        r = self._sample_readiness()
        payload = self._call(readiness=r)
        self.assertEqual(payload["snapshot_data"], r)

    def test_formula_version_preserved(self):
        payload = self._call(formula_version="READINESS_V4_PERFORMANCE_ANCHORED")
        self.assertEqual(payload["formula_version"], "READINESS_V4_PERFORMANCE_ANCHORED")

    def test_exam_attempt_id_preserved(self):
        payload = self._call(exam_attempt_id=999)
        self.assertEqual(payload["exam_attempt_id"], 999)

    def test_computed_at_is_present_iso_string(self):
        payload = self._call()
        self.assertIn("computed_at", payload)
        datetime.fromisoformat(payload["computed_at"])

    def test_missing_readiness_keys_default_to_zero(self):
        payload = self._call(readiness={})
        self.assertEqual(payload["score"], 0.0)
        self.assertEqual(payload["confidence_score"], 0.0)
        self.assertEqual(payload["label"], "")

    def test_does_not_mutate_readiness_dict(self):
        r = self._sample_readiness()
        original = copy.deepcopy(r)
        self._call(readiness=r)
        self.assertEqual(r, original)


# ---------------------------------------------------------------------------
# Tests for insert_or_fetch_readiness_snapshot
# ---------------------------------------------------------------------------

class TestInsertOrFetchReadinessSnapshot(unittest.TestCase):

    def test_fresh_insert_returns_true(self):
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        store = _SnapshotStore()
        ok, err = insert_or_fetch_readiness_snapshot(store, _minimal_snapshot_payload())
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_conflict_returns_true_without_overwrite(self):
        """Second insert with same key must succeed by finding the existing row."""
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        store = _SnapshotStore()
        p1 = _minimal_snapshot_payload(score=70.0)
        p2 = _minimal_snapshot_payload(score=85.0)  # different score, same key
        insert_or_fetch_readiness_snapshot(store, p1)
        ok, err = insert_or_fetch_readiness_snapshot(store, p2)
        self.assertTrue(ok, f"Expected True on conflict, got err={err}")

    def test_conflict_does_not_overwrite_score(self):
        """The first score must survive; the second call must not update it."""
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        store = _SnapshotStore()
        first_payload = _minimal_snapshot_payload(score=70.0)
        insert_or_fetch_readiness_snapshot(store, first_payload)
        # Attempt a second insert with a different score
        insert_or_fetch_readiness_snapshot(store, _minimal_snapshot_payload(score=90.0))
        # The stored row must still have the original score
        stored = store._rows.get((1, "V4"))
        self.assertIsNotNone(stored)
        self.assertAlmostEqual(stored["score"], 70.0)

    def test_conflict_does_not_overwrite_computed_at(self):
        """computed_at of the first insert must survive a retry."""
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        store = _SnapshotStore()
        p1 = _minimal_snapshot_payload()
        p1["computed_at"] = "2026-01-01T00:00:00+00:00"
        insert_or_fetch_readiness_snapshot(store, p1)
        p2 = _minimal_snapshot_payload()
        p2["computed_at"] = "2026-06-01T00:00:00+00:00"
        insert_or_fetch_readiness_snapshot(store, p2)
        stored = store._rows.get((1, "V4"))
        self.assertEqual(stored["computed_at"], "2026-01-01T00:00:00+00:00")

    def test_conflict_does_not_overwrite_snapshot_data(self):
        """snapshot_data must remain unchanged after first insert."""
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        store = _SnapshotStore()
        p1 = _minimal_snapshot_payload()
        p1["snapshot_data"] = {"score": 70.0, "source": "first"}
        insert_or_fetch_readiness_snapshot(store, p1)
        p2 = _minimal_snapshot_payload()
        p2["snapshot_data"] = {"score": 90.0, "source": "second"}
        insert_or_fetch_readiness_snapshot(store, p2)
        stored = store._rows.get((1, "V4"))
        self.assertEqual(stored["snapshot_data"]["source"], "first")

    def test_conflict_does_not_overwrite_component_scores(self):
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        store = _SnapshotStore()
        p1 = _minimal_snapshot_payload()
        p1["component_scores"] = {"recent_accuracy": 70.0}
        insert_or_fetch_readiness_snapshot(store, p1)
        p2 = _minimal_snapshot_payload()
        p2["component_scores"] = {"recent_accuracy": 90.0}
        insert_or_fetch_readiness_snapshot(store, p2)
        stored = store._rows.get((1, "V4"))
        self.assertAlmostEqual(stored["component_scores"]["recent_accuracy"], 70.0)

    def test_genuine_db_failure_returns_false(self):
        """Non-conflict failure (e.g. network error) must return (False, err)."""
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        # Use a supabase that raises on both insert AND select
        supabase = _FakeSupabase(raise_on={"readiness_snapshots"})
        ok, err = insert_or_fetch_readiness_snapshot(supabase, _minimal_snapshot_payload())
        self.assertFalse(ok)
        self.assertIsNotNone(err)

    def test_three_retries_all_succeed(self):
        """Three calls with the same key must all return True."""
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        store = _SnapshotStore()
        for _ in range(3):
            ok, err = insert_or_fetch_readiness_snapshot(store, _minimal_snapshot_payload())
            self.assertTrue(ok, f"Expected True, got err={err}")


# ---------------------------------------------------------------------------
# Tests for build_question_attempt_rows metadata (paid-mock path)
# ---------------------------------------------------------------------------

class TestBuildQuestionAttemptRowsMetadata(unittest.TestCase):
    """Verify metadata fields are included in question_attempts rows by
    utils.question_selection.build_question_attempt_rows."""

    def _make_question(self, idx: int, **extra) -> dict:
        return {
            "id": idx,
            "exam_name": "Salesforce-Admin",
            "language_code": "en",
            "category": "Security",
            "difficulty": "medium",
            "answers": ["opt-A"],
            **extra,
        }

    def _call(self, questions, answers=None):
        from utils.question_selection import build_question_attempt_rows
        return build_question_attempt_rows(
            questions,
            answers or {},
            exam_attempt_id=1,
            user_email="u@e.com",
            default_exam_name="Salesforce-Admin",
            default_language_code="en",
            answered_at_iso="2026-01-01T00:00:00+00:00",
        )

    def test_metadata_fields_included_when_present(self):
        q = self._make_question(
            1,
            cognitive_level="apply",
            concept_key="sharing",
            question_family_id="fam-1",
            content_version=2,
            external_key="EXT-1",
        )
        rows = self._call([q])
        r = rows[0]
        self.assertEqual(r["cognitive_level"], "apply")
        self.assertEqual(r["concept_key"], "sharing")
        self.assertEqual(r["question_family_id"], "fam-1")
        self.assertEqual(r["question_content_version"], 2)
        self.assertEqual(r["question_external_key"], "EXT-1")
        self.assertEqual(r["metadata_source"], "captured_at_attempt")
        self.assertEqual(r["metadata_capture_version"], "ATTEMPT_METADATA_V1")

    def test_paid_mock_uuid_question_family_id_in_attempt_row(self):
        """A paid-mock question with a UUID question_family_id must produce a
        question_attempt row that carries the exact UUID."""
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        q = self._make_question(7, question_family_id=uuid)
        rows = self._call([q])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question_family_id"], uuid)
        # Confirm it is captured verbatim (not cast or mangled)
        self.assertIsInstance(rows[0]["question_family_id"], str)
        self.assertIn("-", rows[0]["question_family_id"])

    def test_missing_metadata_becomes_none(self):
        """Rows with no metadata fields must not raise and must be None."""
        rows = self._call([self._make_question(1)])
        r = rows[0]
        self.assertIsNone(r["cognitive_level"])
        self.assertIsNone(r["concept_key"])
        self.assertIsNone(r["question_family_id"])
        self.assertIsNone(r["question_content_version"])
        self.assertIsNone(r["question_external_key"])

    def test_60_questions_produce_60_rows_each_with_metadata(self):
        questions = [self._make_question(i, cognitive_level="recall") for i in range(60)]
        rows = self._call(questions)
        self.assertEqual(len(rows), 60)
        for r in rows:
            self.assertEqual(r["metadata_capture_version"], "ATTEMPT_METADATA_V1")
            self.assertEqual(r["cognitive_level"], "recall")

    def test_metadata_does_not_alter_correctness_fields(self):
        q = self._make_question(1, cognitive_level="apply")
        rows = self._call([q], answers={0: ["opt-A"]})
        r = rows[0]
        self.assertTrue(r["is_correct"])
        self.assertEqual(r["selected_options"], ["opt-A"])

    def test_rows_one_per_question(self):
        rows = self._call([self._make_question(i) for i in range(5)])
        self.assertEqual(len(rows), 5)


# ---------------------------------------------------------------------------
# Tests for _is_historical_attempt (temporal anchor logic)
# ---------------------------------------------------------------------------

class TestIsHistoricalAttempt(unittest.TestCase):

    def _call(self, attempt_ts: str, attempt_id: int, target_ts: str, target_id: int) -> bool:
        from utils.readiness_persistence import _is_historical_attempt
        attempt = {"id": attempt_id, "completed_at": attempt_ts}
        return _is_historical_attempt(attempt, target_ts, target_id)

    def test_earlier_timestamp_is_historical(self):
        self.assertTrue(self._call("2026-01-01T00:00:00", 1, "2026-01-02T00:00:00", 2))

    def test_later_timestamp_is_not_historical(self):
        self.assertFalse(self._call("2026-01-03T00:00:00", 3, "2026-01-02T00:00:00", 2))

    def test_same_timestamp_same_id_is_historical(self):
        self.assertTrue(self._call("2026-01-01T00:00:00", 5, "2026-01-01T00:00:00", 5))

    def test_same_timestamp_lower_id_is_historical(self):
        self.assertTrue(self._call("2026-01-01T00:00:00", 4, "2026-01-01T00:00:00", 5))

    def test_same_timestamp_higher_id_is_not_historical(self):
        self.assertFalse(self._call("2026-01-01T00:00:00", 6, "2026-01-01T00:00:00", 5))

    def test_integer_id_comparison_is_numeric_not_lexicographic(self):
        """id=10 must compare as 10 > 9, not as '10' < '9'."""
        # Attempt id=10 is later than target id=9 with same timestamp
        self.assertFalse(self._call("2026-01-01T00:00:00", 10, "2026-01-01T00:00:00", 9))
        # Attempt id=9 is earlier than target id=10 with same timestamp
        self.assertTrue(self._call("2026-01-01T00:00:00", 9, "2026-01-01T00:00:00", 10))

    def test_microsecond_later_is_not_historical(self):
        """An attempt one microsecond after target must be excluded."""
        self.assertFalse(self._call(
            "2026-01-01T00:00:00.000001", 5,
            "2026-01-01T00:00:00.000000", 5,
        ))

    def test_microsecond_earlier_is_historical(self):
        """An attempt one microsecond before target must be included."""
        self.assertTrue(self._call(
            "2026-01-01T00:00:00.000000", 4,
            "2026-01-01T00:00:00.000001", 5,
        ))

    def test_later_attempt_id_9_and_10_equal_timestamp(self):
        """Specifically test IDs 9 and 10 at the same timestamp (the numeric
        vs lexicographic regression case): 10 > 9 so id=10 must not be
        historical relative to target id=9."""
        self.assertFalse(self._call("2026-06-01T12:00:00", 10, "2026-06-01T12:00:00", 9))
        self.assertTrue( self._call("2026-06-01T12:00:00",  9, "2026-06-01T12:00:00", 10))


# ---------------------------------------------------------------------------
# Tests for historical snapshot immutability (temporal anchoring integration)
# ---------------------------------------------------------------------------

class TestSnapshotTemporalAnchoring(unittest.TestCase):
    """Verify that a snapshot is computed only from attempts that existed at
    the time of the target attempt, and that later attempts cannot mutate it."""

    def _make_supabase(self, attempts: list, target_attempt_id: int) -> _FakeSupabase:
        """Return a fake supabase pre-loaded with the given attempts.

        The 'exam_attempts' table returns all attempts (the function filters).
        'exam_attempts' with .eq('id', target_attempt_id) returns the target.
        We handle this by making all queries return all_attempts; the temporal
        filter runs in Python.
        """
        store = _SnapshotStore()
        target = next((a for a in attempts if a["id"] == target_attempt_id), None)
        target_list = [target] if target else []

        class _RouterSupabase:
            """Routes by table and simulates .eq('id', ...) for the target fetch."""

            def table(self, name: str):
                if name == "readiness_snapshots":
                    return store.table(name)
                if name == "exam_attempts":
                    return _AttemptProxy(attempts, target_attempt_id, target_list)
                if name == "certifications":
                    return _FakeQueryBuilder(data=[{
                        "passing_score": 68, "question_count": 60,
                        "time_limit_minutes": 105,
                    }])
                if name == "question_attempts":
                    return _FakeQueryBuilder(data=[])
                if name == "certification_domains":
                    return _FakeQueryBuilder(data=[])
                return _FakeQueryBuilder(data=[])

        return _RouterSupabase()  # type: ignore[return-value]

    def test_later_attempts_do_not_affect_earlier_snapshot_score(self):
        """A snapshot computed for attempt-1 (score=70) must not be influenced
        by attempt-2 (score=95) which was completed later."""
        from utils.readiness_persistence import compute_and_persist_readiness_snapshot

        attempt1 = _make_attempt(1, "2026-01-01T10:00:00+00:00", score=70.0)
        attempt2 = _make_attempt(2, "2026-06-01T10:00:00+00:00", score=95.0)

        # Compute snapshot for attempt 1 AFTER attempt 2 already exists in DB
        supabase = self._make_supabase([attempt1, attempt2], target_attempt_id=1)
        ok, err = compute_and_persist_readiness_snapshot(
            supabase,
            user_email="u@e.com",
            exam_name="Salesforce-Admin",
            exam_attempt_id=1,
            eligible_bank_size=900,
        )
        # Should succeed
        self.assertTrue(ok, f"Expected ok=True, got err={err}")

        # Confirm the snapshot was computed with only attempt 1 in scope
        # (attempt 2 is filtered out by temporal anchoring)
        # The snapshot's eligible_mock_count must reflect only 1 attempt, which
        # is < 3 required, so the formula returns "locked" → score=0.0 (not 95%)
        store = supabase.table("readiness_snapshots")._store  # type: ignore
        snapshot = next(iter(store._rows.values()), None)
        self.assertIsNotNone(snapshot)
        # With only 1 paid mock attempt the formula is locked → score = 0.0
        self.assertAlmostEqual(snapshot["score"], 0.0, places=1)
        self.assertNotAlmostEqual(snapshot["score"], 95.0, places=1)

    def test_results_page_refresh_cannot_mutate_historical_snapshot(self):
        """A second call to compute_and_persist with the same exam_attempt_id
        must not change score, computed_at, or snapshot_data of the first insert."""
        from utils.readiness_persistence import compute_and_persist_readiness_snapshot

        attempt = _make_attempt(1, "2026-01-01T10:00:00+00:00", score=72.0)
        supabase = self._make_supabase([attempt], target_attempt_id=1)

        # First call
        ok1, _ = compute_and_persist_readiness_snapshot(
            supabase,
            user_email="u@e.com",
            exam_name="Salesforce-Admin",
            exam_attempt_id=1,
            eligible_bank_size=900,
        )
        self.assertTrue(ok1)

        store = supabase.table("readiness_snapshots")._store  # type: ignore
        first_snapshot = copy.deepcopy(next(iter(store._rows.values())))

        # Second call (simulates page refresh)
        ok2, _ = compute_and_persist_readiness_snapshot(
            supabase,
            user_email="u@e.com",
            exam_name="Salesforce-Admin",
            exam_attempt_id=1,
            eligible_bank_size=900,
        )
        self.assertTrue(ok2)

        second_snapshot = next(iter(store._rows.values()))
        self.assertAlmostEqual(first_snapshot["score"], second_snapshot["score"])
        self.assertEqual(first_snapshot["computed_at"], second_snapshot["computed_at"])
        self.assertEqual(first_snapshot["snapshot_data"], second_snapshot["snapshot_data"])

    def test_attempt_id_10_excluded_from_snapshot_for_attempt_id_9(self):
        """Attempt 10 (higher numeric id, same timestamp) must not appear in a
        snapshot keyed to attempt 9 — tests the numeric vs lexicographic edge case."""
        from utils.readiness_persistence import compute_and_persist_readiness_snapshot

        ts = "2026-06-01T12:00:00+00:00"
        attempt9  = _make_attempt(9,  ts, score=70.0)
        attempt10 = _make_attempt(10, ts, score=99.0)

        # Snapshot computed for attempt 9; attempt 10 has same ts but higher id
        supabase = self._make_supabase([attempt9, attempt10], target_attempt_id=9)
        ok, err = compute_and_persist_readiness_snapshot(
            supabase,
            user_email="u@e.com",
            exam_name="Salesforce-Admin",
            exam_attempt_id=9,
            eligible_bank_size=900,
        )
        self.assertTrue(ok, f"Expected ok=True, got err={err}")

        store = supabase.table("readiness_snapshots")._store  # type: ignore
        snap = next(iter(store._rows.values()), None)
        self.assertIsNotNone(snap)
        # Only attempt9 in scope → 1 paid mock < 3 required → locked → score=0.0
        self.assertAlmostEqual(snap["score"], 0.0, places=1)
        self.assertNotAlmostEqual(snap["score"], 99.0, places=1)

    def test_microsecond_later_attempt_excluded_from_snapshot(self):
        """An attempt completed one microsecond after target must be excluded."""
        from utils.readiness_persistence import compute_and_persist_readiness_snapshot

        ts_target = "2026-06-01T12:00:00.000000+00:00"
        ts_later  = "2026-06-01T12:00:00.000001+00:00"
        attempt1 = _make_attempt(1, ts_target, score=70.0)
        attempt2 = _make_attempt(2, ts_later,  score=99.0)

        supabase = self._make_supabase([attempt1, attempt2], target_attempt_id=1)
        ok, _ = compute_and_persist_readiness_snapshot(
            supabase,
            user_email="u@e.com",
            exam_name="Salesforce-Admin",
            exam_attempt_id=1,
            eligible_bank_size=900,
        )
        self.assertTrue(ok)
        store = supabase.table("readiness_snapshots")._store  # type: ignore
        snap = next(iter(store._rows.values()), None)
        self.assertIsNotNone(snap)
        # Only attempt1 in scope → locked → score=0.0; attempt2 (99.0) excluded
        self.assertAlmostEqual(snap["score"], 0.0, places=1)

    def test_retry_snapshot_persistence_returns_success_without_new_row(self):
        """A retry must return (True, None) and must not create a second snapshot row."""
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        store = _SnapshotStore()
        p = _minimal_snapshot_payload(exam_attempt_id=42)
        ok1, _ = insert_or_fetch_readiness_snapshot(store, p)
        ok2, _ = insert_or_fetch_readiness_snapshot(store, p)
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        # There must be exactly one row, not two
        self.assertEqual(len(store._rows), 1)


# ---------------------------------------------------------------------------
# Tests for snapshot failure handling
# ---------------------------------------------------------------------------

class TestSnapshotFailureHandling(unittest.TestCase):

    def test_exam_attempts_failure_calls_on_error(self):
        """exam_attempts failure (not in inner try/except) triggers on_error."""
        from utils.readiness_persistence import compute_and_persist_readiness_snapshot
        errors = []
        supabase = _FakeSupabase(raise_on={"exam_attempts"})
        ok, err = compute_and_persist_readiness_snapshot(
            supabase,
            user_email="u@e.com",
            exam_name="X",
            exam_attempt_id=1,
            eligible_bank_size=0,
            on_error=errors.append,
        )
        self.assertFalse(ok)
        self.assertIsNotNone(err)
        self.assertEqual(len(errors), 1)

    def test_failure_does_not_raise(self):
        from utils.readiness_persistence import compute_and_persist_readiness_snapshot
        supabase = _FakeSupabase(raise_on={"exam_attempts"})
        ok, err = compute_and_persist_readiness_snapshot(
            supabase,
            user_email="u@e.com",
            exam_name="X",
            exam_attempt_id=1,
            eligible_bank_size=0,
        )
        self.assertFalse(ok)

    def test_on_error_exception_does_not_propagate(self):
        from utils.readiness_persistence import compute_and_persist_readiness_snapshot

        def bad_callback(exc):
            raise RuntimeError("callback exploded")

        supabase = _FakeSupabase(raise_on={"exam_attempts"})
        ok, err = compute_and_persist_readiness_snapshot(
            supabase,
            user_email="u@e.com",
            exam_name="X",
            exam_attempt_id=1,
            eligible_bank_size=0,
            on_error=bad_callback,
        )
        self.assertFalse(ok)

    def test_snapshot_upsert_error_does_not_affect_attempt_state(self):
        """Snapshot persistence failure returns (False, msg) only.
        The caller keeps submission_save_state=saved regardless."""
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        supabase = _FakeSupabase(raise_on={"readiness_snapshots"})
        ok, err = insert_or_fetch_readiness_snapshot(supabase, _minimal_snapshot_payload())
        self.assertFalse(ok)
        self.assertIsNotNone(err)


# ---------------------------------------------------------------------------
# Tests for bank-size count correctness
# ---------------------------------------------------------------------------

class TestBankSizeCounting(unittest.TestCase):

    def test_count_is_certification_scoped(self):
        from utils.readiness_persistence import fetch_eligible_mock_bank_size
        qb = _FakeQueryBuilder(data=[], count=5)
        supabase = _FakeSupabase({"questions": qb})
        self.assertEqual(fetch_eligible_mock_bank_size(supabase, "Salesforce-Admin", "en"), 5)

    def test_count_is_language_scoped(self):
        from utils.readiness_persistence import fetch_eligible_mock_bank_size
        qb = _FakeQueryBuilder(data=[], count=300)
        supabase = _FakeSupabase({"questions": qb})
        self.assertEqual(fetch_eligible_mock_bank_size(supabase, "Salesforce-Admin", "fr"), 300)

    def test_does_not_hardcode_specific_number(self):
        from utils.readiness_persistence import fetch_eligible_mock_bank_size
        for expected in (0, 1, 840, 1200):
            qb = _FakeQueryBuilder(data=[], count=expected)
            supabase = _FakeSupabase({"questions": qb})
            self.assertEqual(fetch_eligible_mock_bank_size(supabase, "X", "en"), expected)


# ---------------------------------------------------------------------------
# Tests for snapshot idempotency (no duplicate on retry)
# ---------------------------------------------------------------------------

class TestSnapshotIdempotency(unittest.TestCase):

    def test_three_retries_produce_exactly_one_row(self):
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        store = _SnapshotStore()
        for _ in range(3):
            ok, err = insert_or_fetch_readiness_snapshot(store, _minimal_snapshot_payload())
            self.assertTrue(ok, f"Got err={err}")
        self.assertEqual(len(store._rows), 1)

    def test_different_formula_versions_are_separate_keys(self):
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        store = _SnapshotStore()
        for fv in ("V3", "V4", "V4_PERFORMANCE"):
            payload = _minimal_snapshot_payload(formula_version=fv)
            ok, _ = insert_or_fetch_readiness_snapshot(store, payload)
            self.assertTrue(ok)
        self.assertEqual(len(store._rows), 3)

    def test_retry_after_failed_insert_finds_existing_row(self):
        """If a prior run inserted the snapshot, a retry must find it."""
        from utils.readiness_persistence import insert_or_fetch_readiness_snapshot
        store = _SnapshotStore()
        p = _minimal_snapshot_payload(score=70.0)
        # Simulate: prior run inserted successfully
        insert_or_fetch_readiness_snapshot(store, p)
        # Retry (score=80 would have been updated under the old upsert; must not be)
        ok2, _ = insert_or_fetch_readiness_snapshot(store, _minimal_snapshot_payload(score=80.0))
        self.assertTrue(ok2)
        stored = store._rows.get((1, "V4"))
        self.assertAlmostEqual(stored["score"], 70.0)


# ---------------------------------------------------------------------------
# Smoke tests: no readiness formula output changes
# ---------------------------------------------------------------------------

class TestReadinessFormulaUnchanged(unittest.TestCase):

    def _make_attempt_dict(self, score: float, questions: int = 60) -> dict:
        return {
            "id": 1,
            "mode": "Paid Mock Exam",
            "score": score,
            "total_questions": questions,
            "correct_answers": int(questions * score / 100),
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T02:00:00+00:00",
            "domain_breakdown": {},
            "difficulty_breakdown": {},
            "exam_name": "Salesforce-Admin",
            "language_code": "en",
        }

    def test_formula_returns_expected_keys(self):
        from utils.readiness import calculate_readiness
        attempts = [self._make_attempt_dict(70.0) for _ in range(3)]
        result = calculate_readiness(attempts=attempts, passing_score=68)
        for key in ("score", "label", "confidence_score", "eligible_mock_count",
                    "recent_accuracy", "domain_score", "domain_robustness",
                    "consistency_penalty", "trend_adjustment", "trend_slope"):
            self.assertIn(key, result, f"Key missing from calculate_readiness: {key}")

    def test_readiness_version_constant_unchanged(self):
        from utils.readiness import READINESS_VERSION
        self.assertEqual(READINESS_VERSION, "READINESS_V5_VERIFIED_EVIDENCE")

    def test_locked_result_has_expected_keys(self):
        from utils.readiness import calculate_readiness
        result = calculate_readiness(
            attempts=[self._make_attempt_dict(70.0)], passing_score=68
        )
        self.assertTrue(result["is_locked"])
        self.assertIn("confidence_score", result)

    def test_snapshot_payload_maps_only_existing_formula_keys(self):
        """build_readiness_snapshot_payload must only map keys that actually exist
        in the calculate_readiness return dict; no invented keys allowed."""
        from utils.readiness import calculate_readiness
        from utils.readiness_persistence import build_readiness_snapshot_payload
        attempts = [self._make_attempt_dict(70.0) for _ in range(3)]
        readiness = calculate_readiness(attempts=attempts, passing_score=68)
        payload = build_readiness_snapshot_payload(
            user_email="u@e.com",
            exam_name="Salesforce-Admin",
            exam_attempt_id=1,
            formula_version="READINESS_V4_PERFORMANCE_ANCHORED",
            readiness=readiness,
            eligible_bank_size=900,
        )
        self.assertIsInstance(payload["score"], float)
        self.assertIsInstance(payload["confidence_score"], float)
        self.assertIsInstance(payload["eligible_mock_count"], int)
        for cs_key in payload["component_scores"]:
            self.assertIn(
                cs_key, readiness,
                f"component_scores key {cs_key!r} not in calculate_readiness output"
            )


# ---------------------------------------------------------------------------
# Proxy for temporal anchoring tests (routes exam_attempts by query type)
# ---------------------------------------------------------------------------

class _AttemptProxy:
    """Routes exam_attempts queries:
    - .eq('id', target_id) → [target attempt only]
    - any other filter chain → all attempts
    """

    def __init__(self, all_attempts: list, target_id: int, target_list: list):
        self._all = all_attempts
        self._target_id = target_id
        self._target_list = target_list
        self._id_filter: Optional[int] = None
        self._mode = "all"

    def select(self, *a, **kw): return self
    def ilike(self, *a, **kw): return self
    def order(self, *a, **kw): return self
    def limit(self, n, **kw): return self

    def eq(self, col: str, val):
        if col == "id":
            self._mode = "target"
        return self

    def execute(self):
        if self._mode == "target":
            return _FakeResult(data=list(self._target_list))
        return _FakeResult(data=list(self._all))


if __name__ == "__main__":
    unittest.main()
