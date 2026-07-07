"""
SME review packet export/import workflow for the quality benchmark pilot
(V58-QUALITY-03D).

This module prepares the human-review process for the 40-case AI-drafted
pilot benchmark (workers/fixtures/quality_benchmark_v1.json). It does not
perform or simulate SME review, and it never invents reviewer decisions.

Two directions are supported:

1. Export: read the AI-drafted fixture (read-only) and produce a flat,
   reviewer-friendly CSV with one row per case. All SME-editable columns are
   left blank so a qualified Salesforce SME can fill them in offline.

2. Import/validate: read a completed (or partially completed) SME CSV,
   validate it strictly (unknown/duplicate case IDs, invalid answer labels,
   invalid finding codes, invalid decision/confidence values, missing
   confidence, missing corrections, missing notes on unresolved/rejected
   cases, any AI-derived/immutable context field that no longer matches
   what the source fixture would produce, and a source-fixture SHA-256 hash
   that no longer matches the current source fixture file), report
   AI-vs-SME disagreements and an AI-vs-SME agreement rate (NOT a human
   inter-rater reliability metric — there is only one human reviewer's
   input here), and — only when every case in the source fixture has a
   valid, fully-adjudicated SME decision (no validation errors, no missing
   confidence/corrections/notes, no unresolved needs_second_review cases,
   no reject_case cases, and an explicit non-blank reviewer identifier is
   supplied) — build a *new*, separate reviewed-fixture payload marked
   sme_reviewed=true, stamped with sme_reviewer_id, source_fixture_sha256,
   and a UTC review_imported_at_utc timestamp. The original AI-drafted
   fixture file is never opened for writing by this module.

   "Review completed" and "benchmark finalizable as trusted ground truth"
   are deliberately distinct concepts: a review that adjudicates every case
   (including cases the SME rejects) is complete, but a reviewed fixture is
   only marked sme_reviewed=true / sme_review_status="complete" when, in
   addition, no case remains rejected and no case remains stuck needing a
   second review. Rejected cases are never silently dropped — doing so
   would change the intended benchmark size — so they must be corrected or
   replaced by a human before the benchmark can be finalized.

   When a reviewed fixture is finalized, each case's *effective* label —
   the exact fields workers.quality_benchmark's loader and scoring consume
   (expected_correct_option_labels, expected_finding_codes,
   expected_materiality, known_good, reviewer_label) — is resolved from the
   SME's decision: unchanged for "approve", or replaced by the SME's
   correction(s) for "correct_label" (with known_good/materiality always
   *recalculated* from the resolved finding codes via
   workers.finding_policy, never trusted from the CSV). The original
   AI-drafted label is preserved separately as ai_drafted_reviewer_label so
   no provenance is lost.    "approve" rows must carry no correction fields,
    and "correct_label" rows must materially change at least one of the
    answer label(s) or finding code(s) — a no-op "correction" is rejected.

   sme_finding_codes supports three distinct raw-field values:
     blank / ""     → inherit the AI-drafted finding codes unchanged
     "CLEAR"        → explicitly replace the AI-drafted findings with []
                      (making the case effectively known-good); valid only
                      for correct_label, must not be combined with any
                      canonical code, and must still constitute a material
                      correction (rejected if AI-drafted findings are
                      already empty and no other field changes)
     "CODE1|CODE2"  → replace the AI-drafted findings with the listed codes
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from workers.finding_policy import (
    BLOCKING_CODES,
    CANONICAL_FINDING_CODES,
    INFORMATIONAL_CODES,
    MATERIALITY_RANK,
    WARNING_CODES,
)
from workers.quality_benchmark import BenchmarkFixtureError, load_benchmark_fixture

DEFAULT_SOURCE_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "quality_benchmark_v1.json"
)
DEFAULT_REVIEWED_OUTPUT_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "quality_benchmark_v1_sme_reviewed.json"
)

MULTI_VALUE_SEPARATOR = "|"

SME_DECISIONS = frozenset({"approve", "correct_label", "reject_case", "needs_second_review"})
CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})
BOOLEAN_STRINGS = frozenset({"true", "false"})

# Control token for sme_finding_codes: instructs the importer to replace the
# AI-drafted finding-code list with an explicit empty list, making the case
# effectively known-good.  Semantics of the three possible raw field values:
#
#   blank / ""     → inherit the AI-drafted finding codes unchanged
#   "CLEAR"        → explicitly replace the AI-drafted findings with []
#   "CODE1|CODE2"  → replace the AI-drafted findings with the listed codes
#
# CLEAR is a reserved control token, not a canonical finding code.  It is
# valid only when sme_decision=correct_label and must appear alone (never
# combined with a canonical code such as "CLEAR|WRONG_ANSWER_KEY").  A
# correct_label row that uses CLEAR must still constitute a material
# correction: if the AI-drafted findings are already empty and no other
# field changes, CLEAR is a no-op and will be rejected.
CLEAR_TOKEN = "CLEAR"

_OPTION_LABELS = ("A", "B", "C", "D")

CSV_COLUMNS: Tuple[str, ...] = (
    "case_id",
    "certification",
    "domain",
    "question_text",
    "option_a_text",
    "option_b_text",
    "option_c_text",
    "option_d_text",
    "stored_correct_answer",
    "expected_evidence_supported_answer",
    "known_good",
    "expected_finding_codes",
    "expected_materiality",
    "reviewer_rationale",
    "official_source_title",
    "canonical_url",
    "evidence_excerpt",
    "ai_drafted_label",
    "source_fixture_sha256",
    "sme_decision",
    "sme_correct_answer",
    "sme_finding_codes",
    "sme_notes",
    "confidence",
    "needs_second_review",
)

SME_EDITABLE_COLUMNS: Tuple[str, ...] = (
    "sme_decision",
    "sme_correct_answer",
    "sme_finding_codes",
    "sme_notes",
    "confidence",
    "needs_second_review",
)

_AI_DERIVED_COLUMNS: Tuple[str, ...] = tuple(
    col for col in CSV_COLUMNS if col not in SME_EDITABLE_COLUMNS
)

# Every AI-derived column except case_id (which is the lookup key itself).
# These must exactly match what build_export_row() would (re)generate from
# the *current* source fixture; any mismatch means the CSV was altered,
# reordered/mismatched against the wrong case, or exported from a stale or
# tampered source fixture (source_fixture_sha256 is included here, so a
# fixture-drift/tamper scenario is caught by the same generic mechanism).
IMMUTABLE_CONTEXT_COLUMNS: Tuple[str, ...] = tuple(
    col for col in _AI_DERIVED_COLUMNS if col != "case_id"
)


class BenchmarkSmeReviewError(ValueError):
    """Base error for the SME review export/import workflow."""


class SmeReviewExportError(BenchmarkSmeReviewError):
    """Raised when the reviewer-facing export cannot be produced safely."""


class SmeReviewImportError(BenchmarkSmeReviewError):
    """Raised when a completed SME CSV cannot be validated or finalized."""


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def load_source_fixture(path: Path | str = DEFAULT_SOURCE_FIXTURE_PATH) -> dict:
    """Load and schema-validate the AI-drafted benchmark fixture (read-only)."""
    try:
        return load_benchmark_fixture(path)
    except BenchmarkFixtureError as exc:
        raise SmeReviewExportError(f"source fixture failed schema validation: {exc}") from exc


def compute_source_fixture_sha256(path: Path | str) -> str:
    """Compute a SHA-256 hash of the exact source fixture bytes (read-only).

    Hashing the raw file bytes (rather than the parsed JSON) ensures any
    byte-for-byte change to the source fixture — including whitespace or key
    ordering changes that json-level comparison might miss — is detected.
    """
    fixture_path = Path(path)
    if not fixture_path.exists():
        raise SmeReviewExportError(f"source fixture not found for hashing: {fixture_path}")
    return hashlib.sha256(fixture_path.read_bytes()).hexdigest()


def _utc_now_iso8601() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _option_text_by_label(question: Mapping[str, Any]) -> Dict[str, str]:
    texts: Dict[str, str] = {}
    for option in question.get("options", []):
        label = str(option.get("option_label", "")).strip()
        if label:
            texts[label] = str(option.get("option_text", ""))
    return texts


def _stored_correct_labels(question: Mapping[str, Any]) -> List[str]:
    return [
        str(option.get("option_label", "")).strip()
        for option in question.get("options", [])
        if option.get("is_correct") is True
    ]


def _evidence_chunks(case: Mapping[str, Any]) -> List[Dict[str, Any]]:
    snapshot = case.get("resource_snapshot") or {}
    chunks = snapshot.get("chunks")
    if not isinstance(chunks, list):
        return []
    return [chunk for chunk in chunks if isinstance(chunk, dict)]


def _unique_preserve_order(values: Sequence[str]) -> List[str]:
    seen: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def _ai_drafted_label(case: Mapping[str, Any]) -> str:
    if case.get("known_good"):
        return "known_good"
    codes = MULTI_VALUE_SEPARATOR.join(case.get("expected_finding_codes", []))
    materiality = case.get("expected_materiality") or ""
    return f"defective {MULTI_VALUE_SEPARATOR} {codes} {MULTI_VALUE_SEPARATOR} {materiality}"


def build_export_row(case: Mapping[str, Any], *, source_fixture_sha256: str) -> Dict[str, str]:
    """Build one CSV row (as a dict) for a single benchmark case.

    All SME-editable columns are returned blank. ``source_fixture_sha256``
    is embedded in every row (there is no natural place for file-level
    metadata in a flat, one-row-per-case CSV) so the importer can both
    detect per-row tampering of that value and confirm the packet was
    exported from the exact source fixture currently on disk.
    """
    question = case.get("question", {})
    option_texts = _option_text_by_label(question)
    chunks = _evidence_chunks(case)

    row: Dict[str, str] = {col: "" for col in CSV_COLUMNS}
    row["case_id"] = str(case.get("case_id", ""))
    row["certification"] = str(case.get("certification", ""))
    row["domain"] = str(case.get("domain", ""))
    row["question_text"] = str(question.get("question_text", ""))
    for label in _OPTION_LABELS:
        row[f"option_{label.lower()}_text"] = option_texts.get(label, "")
    row["stored_correct_answer"] = MULTI_VALUE_SEPARATOR.join(_stored_correct_labels(question))
    row["expected_evidence_supported_answer"] = MULTI_VALUE_SEPARATOR.join(
        str(label) for label in case.get("expected_correct_option_labels", [])
    )
    row["known_good"] = "true" if case.get("known_good") else "false"
    row["expected_finding_codes"] = MULTI_VALUE_SEPARATOR.join(
        str(code) for code in case.get("expected_finding_codes", [])
    )
    row["expected_materiality"] = str(case.get("expected_materiality") or "")
    row["reviewer_rationale"] = str(case.get("reviewer_rationale", ""))
    row["official_source_title"] = "; ".join(
        _unique_preserve_order([str(chunk.get("resource_title", "")) for chunk in chunks])
    )
    row["canonical_url"] = " | ".join(
        _unique_preserve_order([str(chunk.get("canonical_url", "")) for chunk in chunks])
    )
    row["evidence_excerpt"] = "\n---\n".join(
        str(chunk.get("chunk_text", "")) for chunk in chunks
    )
    row["ai_drafted_label"] = _ai_drafted_label(case)
    row["source_fixture_sha256"] = source_fixture_sha256
    # SME-editable columns are intentionally left blank here; never populate
    # them from AI-derived values.
    for col in SME_EDITABLE_COLUMNS:
        row[col] = ""
    return row


def build_export_rows(
    fixture: Mapping[str, Any], *, source_fixture_sha256: str
) -> List[Dict[str, str]]:
    """Build all export rows for every case in a loaded benchmark fixture."""
    return [
        build_export_row(case, source_fixture_sha256=source_fixture_sha256)
        for case in fixture["cases"]
    ]


def write_export_csv(
    rows: Sequence[Mapping[str, str]],
    output_path: Path | str,
    *,
    allow_overwrite: bool = False,
) -> None:
    """Write reviewer-export rows to a CSV file."""
    path = Path(output_path)
    if path.exists() and not allow_overwrite:
        raise SmeReviewExportError(f"refusing to overwrite existing export file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})


def export_sme_review_csv(
    source_fixture_path: Path | str = DEFAULT_SOURCE_FIXTURE_PATH,
    output_csv_path: Path | str = None,
    *,
    allow_overwrite: bool = False,
) -> Dict[str, Any]:
    """High-level export: load fixture, build rows, write CSV.

    Returns a small JSON-serializable summary dict (never the row contents,
    to keep CLI output compact).
    """
    if output_csv_path is None:
        raise SmeReviewExportError("output_csv_path is required")
    fixture = load_source_fixture(source_fixture_path)
    source_hash = compute_source_fixture_sha256(source_fixture_path)
    rows = build_export_rows(fixture, source_fixture_sha256=source_hash)
    write_export_csv(rows, output_csv_path, allow_overwrite=allow_overwrite)
    return {
        "source_fixture": str(source_fixture_path),
        "source_fixture_sha256": source_hash,
        "output_csv": str(output_csv_path),
        "case_count": len(rows),
        "columns": list(CSV_COLUMNS),
        "sme_editable_columns": list(SME_EDITABLE_COLUMNS),
    }


# ---------------------------------------------------------------------------
# Import / validation
# ---------------------------------------------------------------------------


@dataclass
class SmeReviewValidationReport:
    errors: List[str] = field(default_factory=list)
    missing_case_ids: List[str] = field(default_factory=list)
    missing_decision_case_ids: List[str] = field(default_factory=list)
    completed_case_ids: List[str] = field(default_factory=list)
    unresolved_second_review_case_ids: List[str] = field(default_factory=list)
    rejected_case_ids: List[str] = field(default_factory=list)
    decision_counts: Dict[str, int] = field(default_factory=dict)
    disagreements: List[Dict[str, Any]] = field(default_factory=list)
    ai_sme_agreement_rate: Optional[float] = None
    ai_sme_agreement_note: str = ""
    source_fixture_sha256: Optional[str] = None
    is_valid: bool = True
    is_complete: bool = False
    is_finalizable: bool = False


def read_sme_review_csv(path: Path | str) -> List[Dict[str, str]]:
    """Read a completed (or partially completed) SME review CSV.

    Raises SmeReviewImportError if the header does not exactly match the
    expected export columns (order-independent).
    """
    csv_path = Path(path)
    if not csv_path.exists():
        raise SmeReviewImportError(f"SME review CSV not found: {csv_path}")
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = tuple(reader.fieldnames or ())
        if set(header) != set(CSV_COLUMNS):
            missing = sorted(set(CSV_COLUMNS) - set(header))
            unexpected = sorted(set(header) - set(CSV_COLUMNS))
            raise SmeReviewImportError(
                "SME review CSV header does not match expected columns "
                f"(missing={missing!r}, unexpected={unexpected!r})"
            )
        rows = [dict(row) for row in reader]
    return rows


def _normalize(value: Optional[str]) -> str:
    return (value or "").strip()


def _split_multi(value: str) -> List[str]:
    normalized = _normalize(value)
    if not normalized:
        return []
    return [part.strip() for part in normalized.split(MULTI_VALUE_SEPARATOR) if part.strip()]


def _materiality_for_code(code: str) -> str:
    """Canonical-policy materiality for a single finding code.

    Uses the repository's own code-level materiality buckets
    (workers.finding_policy) rather than any materiality value a reviewer
    might type into the CSV. Falls back to "warning" for any canonical code
    not explicitly bucketed (matching assign_materiality()'s own fallback).
    """
    if code in BLOCKING_CODES:
        return "blocking"
    if code in INFORMATIONAL_CODES:
        return "informational"
    if code in WARNING_CODES:
        return "warning"
    return "warning"


def _resolved_materiality(finding_codes: Sequence[str]) -> Optional[str]:
    """Case-level materiality derived from a set of resolved finding codes.

    None when there are no codes (i.e. the case is effectively known-good).
    Otherwise the highest-severity materiality among the resolved codes.
    """
    if not finding_codes:
        return None
    levels = [_materiality_for_code(code) for code in finding_codes]
    return max(levels, key=lambda level: MATERIALITY_RANK[level])


def validate_sme_review_rows(
    rows: Sequence[Mapping[str, str]],
    fixture: Mapping[str, Any],
    *,
    source_fixture_path: Path | str = DEFAULT_SOURCE_FIXTURE_PATH,
) -> SmeReviewValidationReport:
    """Validate SME review rows against the source fixture (read-only).

    Hard errors mark the report invalid:
      - unknown/duplicate case_id
      - invalid sme_decision, confidence, needs_second_review, answer-label,
        or finding-code values
      - a non-blank sme_decision with a blank confidence
      - sme_decision == "correct_label" without at least one of
        sme_correct_answer / sme_finding_codes, or without sme_notes
      - sme_decision == "reject_case", sme_decision == "needs_second_review",
        or needs_second_review == "true", without sme_notes
      - any IMMUTABLE_CONTEXT_COLUMNS value (certification, domain,
        question_text, option texts, stored/expected answers, known_good,
        expected finding codes/materiality, reviewer_rationale, source
        title/URL, evidence_excerpt, ai_drafted_label, or
        source_fixture_sha256) that no longer matches exactly what
        build_export_row() would (re)generate for that case_id from the
        *current* source fixture on disk (this also catches row content
        that was swapped between case IDs, and a source fixture that has
        drifted or been altered since the CSV was exported)

    Blank SME decisions are not errors by themselves — they mark a case as
    not-yet-reviewed and block completion, but do not block validating the
    rest of the file. A case whose decision is the valid enum value
    "needs_second_review" (or whose needs_second_review flag is "true") is
    not an error either, but it blocks finalization until resolved — see
    unresolved_second_review_case_ids and is_complete. A case decided
    "reject_case" is also not an error — the review can be complete with
    such a case — but it blocks the benchmark from being marked finalized
    ground truth; see rejected_case_ids and is_finalizable.
    """
    report = SmeReviewValidationReport()
    report.source_fixture_sha256 = compute_source_fixture_sha256(source_fixture_path)

    cases_by_id = {str(case["case_id"]): case for case in fixture["cases"]}
    valid_case_ids = set(cases_by_id.keys())

    seen_case_ids: set = set()
    rows_by_case_id: Dict[str, Mapping[str, str]] = {}

    for index, row in enumerate(rows):
        case_id = _normalize(row.get("case_id"))
        row_label = f"row {index}" + (f" (case_id={case_id!r})" if case_id else "")

        if not case_id:
            report.errors.append(f"{row_label}: case_id is required")
            continue
        if case_id not in valid_case_ids:
            report.errors.append(f"{row_label}: unknown case_id {case_id!r}")
            continue
        if case_id in seen_case_ids:
            report.errors.append(f"{row_label}: duplicate case_id {case_id!r}")
            continue
        seen_case_ids.add(case_id)
        rows_by_case_id[case_id] = row

        case = cases_by_id[case_id]
        option_labels = {
            str(opt.get("option_label", "")).strip()
            for opt in case.get("question", {}).get("options", [])
        }

        # Do not trust CSV context fields merely because case_id is valid:
        # recompute what this case's row should look like from the current
        # source fixture and compare every immutable column exactly.
        expected_row = build_export_row(
            case, source_fixture_sha256=report.source_fixture_sha256
        )
        for column in IMMUTABLE_CONTEXT_COLUMNS:
            actual_value = row.get(column, "")
            expected_value = expected_row[column]
            if actual_value != expected_value:
                if column == "source_fixture_sha256":
                    report.errors.append(
                        f"{row_label}: source_fixture_sha256 mismatch — this review packet "
                        "was exported from a different or altered source fixture "
                        f"(expected {expected_value!r}, got {actual_value!r})"
                    )
                else:
                    report.errors.append(
                        f"{row_label}: immutable field {column!r} does not match the source "
                        "fixture; it may have been edited, or this row's content may belong "
                        "to a different case"
                    )

        decision = _normalize(row.get("sme_decision"))
        confidence = _normalize(row.get("confidence"))
        sme_answer = _normalize(row.get("sme_correct_answer"))
        sme_codes_raw = _normalize(row.get("sme_finding_codes"))
        notes = _normalize(row.get("sme_notes"))
        needs_second_review_raw = _normalize(row.get("needs_second_review")).lower()

        if decision and decision not in SME_DECISIONS:
            report.errors.append(
                f"{row_label}: invalid sme_decision {decision!r}; "
                f"expected one of {sorted(SME_DECISIONS)}"
            )
        if confidence and confidence not in CONFIDENCE_LEVELS:
            report.errors.append(
                f"{row_label}: invalid confidence {confidence!r}; "
                f"expected one of {sorted(CONFIDENCE_LEVELS)}"
            )
        if needs_second_review_raw and needs_second_review_raw not in BOOLEAN_STRINGS:
            report.errors.append(
                f"{row_label}: invalid needs_second_review {needs_second_review_raw!r}; "
                f"expected 'true' or 'false' (or blank)"
            )
        if sme_answer:
            for label in _split_multi(sme_answer):
                if label not in option_labels:
                    report.errors.append(
                        f"{row_label}: invalid sme_correct_answer label {label!r}; "
                        f"case options are {sorted(option_labels)}"
                    )
        if sme_codes_raw:
            _code_parts = _split_multi(sme_codes_raw)
            _has_clear = any(p == CLEAR_TOKEN for p in _code_parts)
            if _has_clear:
                # CLEAR is a control token, not a canonical finding code.
                if len(_code_parts) > 1:
                    report.errors.append(
                        f"{row_label}: CLEAR cannot be combined with other finding codes "
                        f"(e.g. 'CLEAR{MULTI_VALUE_SEPARATOR}WRONG_ANSWER_KEY' is invalid)"
                    )
                elif decision and decision != "correct_label":
                    report.errors.append(
                        f"{row_label}: CLEAR in sme_finding_codes is only valid for "
                        f"sme_decision=correct_label (got sme_decision={decision!r})"
                    )
            else:
                for code in _code_parts:
                    if code not in CANONICAL_FINDING_CODES:
                        report.errors.append(
                            f"{row_label}: invalid sme_finding_codes value {code!r}; "
                            "not a canonical finding code (workers.finding_policy)"
                        )

        # A completed decision (whether or not it is itself valid) must carry
        # a confidence level. This is checked independently of the decision's
        # own validity so a row can never sneak past finalization simply by
        # omitting confidence.
        if decision and not confidence:
            report.errors.append(
                f"{row_label}: confidence is required when sme_decision is set "
                f"(decision={decision!r})"
            )

        if decision == "approve" and (sme_answer or sme_codes_raw):
            report.errors.append(
                f"{row_label}: sme_decision=approve must not include sme_correct_answer or "
                "sme_finding_codes corrections; leave them blank, or use correct_label instead"
            )

        if decision == "correct_label":
            if not sme_answer and not sme_codes_raw:
                report.errors.append(
                    f"{row_label}: correct_label requires at least one of "
                    "sme_correct_answer or sme_finding_codes"
                )
            if not notes:
                report.errors.append(
                    f"{row_label}: correct_label requires sme_notes explaining the correction"
                )
            if sme_answer or sme_codes_raw:
                ai_labels_set = {
                    str(label).strip() for label in case.get("expected_correct_option_labels", [])
                }
                ai_codes_set = {
                    str(code).strip() for code in case.get("expected_finding_codes", [])
                }
                sme_labels_set = set(_split_multi(sme_answer)) if sme_answer else None
                # CLEAR resolves to an explicit empty set; blank inherits (None).
                if sme_codes_raw == CLEAR_TOKEN:
                    sme_codes_set: Optional[set] = set()
                elif sme_codes_raw:
                    sme_codes_set = set(_split_multi(sme_codes_raw))
                else:
                    sme_codes_set = None
                resolved_labels_set = (
                    sme_labels_set if sme_labels_set is not None else ai_labels_set
                )
                resolved_codes_set = (
                    sme_codes_set if sme_codes_set is not None else ai_codes_set
                )
                if resolved_labels_set == ai_labels_set and resolved_codes_set == ai_codes_set:
                    report.errors.append(
                        f"{row_label}: correct_label must materially differ from the "
                        "AI-drafted answer label(s) and/or finding code(s) — a no-op "
                        "correction is not accepted"
                    )

        notes_required_reasons: List[str] = []
        if decision == "reject_case":
            notes_required_reasons.append("sme_decision=reject_case")
        if decision == "needs_second_review":
            notes_required_reasons.append("sme_decision=needs_second_review")
        if needs_second_review_raw == "true":
            notes_required_reasons.append("needs_second_review=true")
        if notes_required_reasons and not notes:
            report.errors.append(
                f"{row_label}: sme_notes is required when "
                f"{' or '.join(notes_required_reasons)}"
            )

    report.missing_case_ids = sorted(valid_case_ids - seen_case_ids)

    decision_counts: Dict[str, int] = {}
    for case_id, row in rows_by_case_id.items():
        decision = _normalize(row.get("sme_decision"))
        if not decision:
            report.missing_decision_case_ids.append(case_id)
            continue
        if decision not in SME_DECISIONS:
            # Already recorded as an error above; do not double-count toward
            # completion or agreement statistics.
            continue
        report.completed_case_ids.append(case_id)
        decision_counts[decision] = decision_counts.get(decision, 0) + 1

        needs_second_review_flag = _normalize(row.get("needs_second_review")).lower() == "true"
        if decision == "needs_second_review" or needs_second_review_flag:
            report.unresolved_second_review_case_ids.append(case_id)
        if decision == "reject_case":
            report.rejected_case_ids.append(case_id)

    report.missing_case_ids.sort()
    report.missing_decision_case_ids.sort()
    report.completed_case_ids.sort()
    report.unresolved_second_review_case_ids.sort()
    report.rejected_case_ids.sort()
    report.decision_counts = decision_counts

    for case_id in report.completed_case_ids:
        row = rows_by_case_id[case_id]
        decision = _normalize(row.get("sme_decision"))
        if decision != "approve":
            case = cases_by_id[case_id]
            report.disagreements.append(
                {
                    "case_id": case_id,
                    "sme_decision": decision,
                    "ai_expected_answer": MULTI_VALUE_SEPARATOR.join(
                        str(label) for label in case.get("expected_correct_option_labels", [])
                    ),
                    "sme_correct_answer": _normalize(row.get("sme_correct_answer")),
                    "ai_expected_finding_codes": MULTI_VALUE_SEPARATOR.join(
                        str(code) for code in case.get("expected_finding_codes", [])
                    ),
                    "sme_finding_codes": _normalize(row.get("sme_finding_codes")),
                    "sme_notes": _normalize(row.get("sme_notes")),
                }
            )

    approve_count = decision_counts.get("approve", 0)
    total_decided = len(report.completed_case_ids)
    if total_decided == 0:
        report.ai_sme_agreement_rate = None
        report.ai_sme_agreement_note = (
            "0/0 SME decisions recorded; AI-SME agreement not computable"
        )
    else:
        report.ai_sme_agreement_rate = round(approve_count / total_decided, 6)
        report.ai_sme_agreement_note = (
            f"AI-SME agreement: {approve_count}/{total_decided} decided cases "
            "marked 'approve' (this compares the single AI drafter's labels "
            "against the single SME's decisions; it is not a human "
            "inter-rater agreement metric)"
        )

    report.is_valid = len(report.errors) == 0
    report.is_complete = (
        report.is_valid
        and not report.missing_case_ids
        and not report.missing_decision_case_ids
        and not report.unresolved_second_review_case_ids
    )
    # "Complete" (every case adjudicated, nothing left uncertain) is
    # deliberately distinct from "finalizable as trusted ground truth":
    # a complete review may still contain reject_case cases, which must be
    # corrected or replaced by a human before the benchmark can be
    # finalized (rejected cases are never silently dropped).
    report.is_finalizable = report.is_complete and not report.rejected_case_ids
    return report


def _reviewed_case_payload(row: Mapping[str, str]) -> Dict[str, Any]:
    _raw_codes = _normalize(row.get("sme_finding_codes", ""))
    finding_codes: List[str] = [] if _raw_codes == CLEAR_TOKEN else _split_multi(_raw_codes)
    return {
        "decision": _normalize(row.get("sme_decision")),
        "correct_answer_labels": _split_multi(row.get("sme_correct_answer", "")),
        "finding_codes": finding_codes,
        "notes": _normalize(row.get("sme_notes")),
        "confidence": _normalize(row.get("confidence")) or None,
        "needs_second_review": _normalize(row.get("needs_second_review")).lower() == "true",
    }


def _ai_drafted_reviewer_label(case: Mapping[str, Any]) -> Dict[str, Any]:
    """Snapshot of the original AI-drafted effective label, for provenance."""
    return {
        "expected_correct_option_labels": list(case.get("expected_correct_option_labels", [])),
        "expected_finding_codes": list(case.get("expected_finding_codes", [])),
        "expected_materiality": case.get("expected_materiality"),
        "known_good": bool(case.get("known_good")),
        "reviewer_label": copy.deepcopy(case.get("reviewer_label")),
    }


def _resolve_effective_case_label(
    case: Mapping[str, Any], row: Mapping[str, str]
) -> Dict[str, Any]:
    """Compute the SME-resolved effective label that scoring should consume.

    Only "approve" and "correct_label" ever reach this function — "reject_case"
    and unresolved "needs_second_review" cases already prevent
    build_reviewed_fixture() from getting this far.

    For "approve", the effective label is unchanged from the AI draft. For
    "correct_label", SME-submitted answer labels and/or finding codes
    replace the corresponding AI-drafted value; whichever correction field
    was left blank inherits the original AI-drafted value for that
    dimension. known_good and materiality are then *recalculated* from the
    resolved finding codes using workers.finding_policy — never trusted
    from anything a reviewer might type into the CSV (there is no
    materiality column for the SME to fill in at all).
    """
    decision = _normalize(row.get("sme_decision"))
    ai_labels = [str(label).strip() for label in case.get("expected_correct_option_labels", [])]
    ai_codes = [str(code).strip() for code in case.get("expected_finding_codes", [])]

    if decision == "approve":
        resolved_labels = ai_labels
        resolved_codes = ai_codes
    elif decision == "correct_label":
        sme_labels = _split_multi(row.get("sme_correct_answer", ""))
        _sme_codes_raw = _normalize(row.get("sme_finding_codes", ""))
        if _sme_codes_raw == CLEAR_TOKEN:
            # CLEAR: explicitly replace AI-drafted findings with an empty list.
            sme_codes: List[str] = []
            _sme_codes_provided = True
        else:
            sme_codes = _split_multi(_sme_codes_raw)
            _sme_codes_provided = bool(sme_codes)
        resolved_labels = sme_labels if sme_labels else ai_labels
        resolved_codes = sme_codes if _sme_codes_provided else ai_codes
    else:
        # Defense in depth: build_reviewed_fixture()'s gates should make this
        # unreachable (reject_case / needs_second_review always block first).
        raise SmeReviewImportError(
            f"cannot resolve effective label for case {case.get('case_id')!r}: "
            f"unexpected sme_decision {decision!r} reached label resolution"
        )

    resolved_known_good = len(resolved_codes) == 0
    resolved_materiality = _resolved_materiality(resolved_codes)

    return {
        "expected_correct_option_labels": resolved_labels,
        "expected_finding_codes": resolved_codes,
        "expected_materiality": resolved_materiality,
        "known_good": resolved_known_good,
        "reviewer_label": {
            "known_good": resolved_known_good,
            "expected_finding_codes": resolved_codes,
        },
    }


def build_reviewed_fixture(
    fixture: Mapping[str, Any],
    rows: Sequence[Mapping[str, str]],
    report: SmeReviewValidationReport,
    *,
    reviewer_id: str,
    review_imported_at_utc: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a *new* reviewed-fixture payload from a completed, valid review.

    Never mutates or overwrites the source fixture. Raises
    SmeReviewImportError unless the review is valid, has no reject_case
    cases remaining, has no unresolved needs_second_review cases, is fully
    complete, and an explicit non-blank ``reviewer_id`` is supplied.

    ``review_imported_at_utc`` is normally left as ``None`` so a fresh UTC
    ISO-8601 timestamp is generated at call time (i.e. only when the
    finalized output is actually being produced); tests may pass an
    explicit value for determinism.
    """
    if not report.is_valid:
        raise SmeReviewImportError(
            f"cannot build reviewed fixture: {len(report.errors)} validation error(s) present"
        )
    if report.rejected_case_ids:
        raise SmeReviewImportError(
            "cannot build reviewed fixture as trusted ground truth: "
            f"{len(report.rejected_case_ids)} case(s) marked reject_case "
            f"({', '.join(report.rejected_case_ids)}); rejected cases must be corrected or "
            "replaced (not silently dropped) before the benchmark can be finalized"
        )
    if report.unresolved_second_review_case_ids:
        raise SmeReviewImportError(
            "cannot build reviewed fixture: "
            f"{len(report.unresolved_second_review_case_ids)} case(s) still need a second "
            "review (sme_decision='needs_second_review' or needs_second_review=true); "
            "unresolved second-review cases prevent finalization"
        )
    if not report.is_complete:
        pending = len(report.missing_case_ids) + len(report.missing_decision_case_ids)
        raise SmeReviewImportError(
            f"cannot build reviewed fixture: review is incomplete ({pending} case(s) pending "
            "a recorded SME decision); partial reviews cannot produce a finalized fixture"
        )
    if not reviewer_id or not reviewer_id.strip():
        raise SmeReviewImportError(
            "cannot build reviewed fixture: a non-blank reviewer_id is required to finalize"
        )
    if not report.source_fixture_sha256:
        raise SmeReviewImportError(
            "cannot build reviewed fixture: source_fixture_sha256 was not computed by validation"
        )

    rows_by_case_id = {_normalize(row.get("case_id")): row for row in rows}
    reviewed = copy.deepcopy(dict(fixture))
    reviewed_cases = []
    for case in reviewed["cases"]:
        case_id = str(case["case_id"])
        row = rows_by_case_id[case_id]

        new_case = dict(case)
        # Preserve the original AI-drafted label for provenance *before*
        # overwriting the case's effective (loader/scoring-facing) fields.
        new_case["ai_drafted_reviewer_label"] = _ai_drafted_reviewer_label(case)

        resolved = _resolve_effective_case_label(case, row)
        new_case["expected_correct_option_labels"] = resolved["expected_correct_option_labels"]
        new_case["expected_finding_codes"] = resolved["expected_finding_codes"]
        new_case["expected_materiality"] = resolved["expected_materiality"]
        new_case["known_good"] = resolved["known_good"]
        new_case["reviewer_label"] = resolved["reviewer_label"]

        new_case["sme_review"] = _reviewed_case_payload(row)
        reviewed_cases.append(new_case)
    reviewed["cases"] = reviewed_cases

    reviewed["sme_reviewed"] = True
    reviewed["sme_review_status"] = "complete"
    reviewed["sme_reviewer_id"] = reviewer_id.strip()
    reviewed["source_fixture_sha256"] = report.source_fixture_sha256
    reviewed["review_imported_at_utc"] = review_imported_at_utc or _utc_now_iso8601()
    reviewed["sme_review_summary"] = {
        "source_fixture": "quality_benchmark_v1.json",
        "source_fixture_sha256": report.source_fixture_sha256,
        "sme_reviewer_id": reviewed["sme_reviewer_id"],
        "review_imported_at_utc": reviewed["review_imported_at_utc"],
        "reviewed_case_count": len(reviewed_cases),
        "decision_counts": dict(report.decision_counts),
        "ai_sme_agreement_rate": report.ai_sme_agreement_rate,
        "ai_sme_agreement_note": report.ai_sme_agreement_note,
        "disagreement_count": len(report.disagreements),
        "unresolved_second_review_case_ids": list(report.unresolved_second_review_case_ids),
        "rejected_case_ids": list(report.rejected_case_ids),
    }
    return reviewed


def write_reviewed_fixture(
    path: Path | str,
    payload: Mapping[str, Any],
    *,
    source_fixture_path: Path | str = DEFAULT_SOURCE_FIXTURE_PATH,
    allow_overwrite: bool = False,
) -> None:
    """Write the reviewed fixture to a new file, never the source fixture."""
    output_path = Path(path).resolve()
    source_path = Path(source_fixture_path).resolve()
    if output_path == source_path:
        raise SmeReviewImportError(
            "refusing to write the reviewed fixture over the original AI-drafted fixture"
        )
    if output_path.exists() and not allow_overwrite:
        raise SmeReviewImportError(f"refusing to overwrite existing reviewed fixture: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    output_path.write_text(serialized + "\n", encoding="utf-8")


def validation_report_dict(report: SmeReviewValidationReport) -> Dict[str, Any]:
    """JSON-serializable view of a validation report."""
    return {
        "errors": list(report.errors),
        "missing_case_ids": list(report.missing_case_ids),
        "missing_decision_case_ids": list(report.missing_decision_case_ids),
        "completed_case_ids": list(report.completed_case_ids),
        "unresolved_second_review_case_ids": list(report.unresolved_second_review_case_ids),
        "rejected_case_ids": list(report.rejected_case_ids),
        "decision_counts": dict(report.decision_counts),
        "disagreements": list(report.disagreements),
        "ai_sme_agreement_rate": report.ai_sme_agreement_rate,
        "ai_sme_agreement_note": report.ai_sme_agreement_note,
        "source_fixture_sha256": report.source_fixture_sha256,
        "is_valid": report.is_valid,
        "is_complete": report.is_complete,
        "is_finalizable": report.is_finalizable,
    }
