"""
Strict JSON schema validation for CertBound AI quality audit pass results (V48).

Validates model responses before ``record_audit_pass_result_v1`` may persist a
pass as ``completed``.  Rules mirror the V48 RPC migration shape checks plus the
worker-side finding constraints required for downstream completion.
"""

from __future__ import annotations

import re
from typing import AbstractSet, Any, Dict, List, Mapping, Optional, Sequence, Set, Union

from workers.finding_policy import ALLOWED_MATERIALITY, CANONICAL_FINDING_CODES, assign_materiality
from workers.llm_audit import ALLOWED_FINDING_TYPES, ALLOWED_SEVERITIES

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

SUPPORTED_FINDING_CODES = CANONICAL_FINDING_CODES | frozenset({"DOMAIN_MISALIGNMENT"})

# V60: codes exclusively owned by the specialized answer-correctness detector.
# The general Pass B judge is instructed not to propose these, and the merge
# boundary (``merge_pass_b_findings``) discards any it emits anyway rather
# than accepting them -- the specialist's deterministic derivation is the
# sole authority for these three codes.
ANSWER_CORRECTNESS_CODES = frozenset({
    "WRONG_ANSWER_KEY",
    "MULTIPLE_DEFENSIBLE_ANSWERS",
    "UNSUPPORTED_ANSWER",
})

ANSWER_CORRECTNESS_VERDICTS = frozenset({
    "SUPPORTED_AS_CORRECT",
    "NOT_SUPPORTED_AS_CORRECT",
    "INSUFFICIENT_EVIDENCE",
})

ALLOWED_RESOLUTION_TYPES = frozenset({
    "NORMAL_DISPUTE",
    "PASS_A_SUBSTITUTION",
    "PASS_B_SUBSTITUTION",
})

ALLOWED_RESOLUTION_STATUS = frozenset({"RESOLVED", "UNRESOLVED"})

_SUBSTITUTED_FOR_PASSES_BY_TYPE: Dict[str, List[str]] = {
    "NORMAL_DISPUTE": [],
    "PASS_A_SUBSTITUTION": ["A", "B"],
    "PASS_B_SUBSTITUTION": ["B"],
}

LabelSet = Union[AbstractSet[str], Sequence[str], Set[str]]


class AiQualityAuditValidationError(ValueError):
    """Raised when an AI quality audit pass result fails schema validation."""


def validate_pass_a_result(
    raw: object,
    *,
    allowed_option_labels: LabelSet,
    required_selection_count: int,
) -> Dict[str, Any]:
    """Validate and normalize a Pass A completed result payload."""
    obj = _require_object(raw, "pass A result")
    labels = _validate_selected_option_labels(
        obj.get("selected_option_labels"),
        allowed_option_labels=allowed_option_labels,
        required_selection_count=required_selection_count,
        prefix="pass A result",
    )
    return {"selected_option_labels": labels}


def validate_pass_b_result(
    raw: object,
    *,
    allowed_option_labels: LabelSet,
    required_selection_count: int,
    frozen_evidence_chunk_ids: LabelSet,
) -> Dict[str, Any]:
    """Validate and normalize a Pass B completed result payload."""
    obj = _require_object(raw, "pass B result")
    labels = _validate_selected_option_labels(
        obj.get("selected_option_labels"),
        allowed_option_labels=allowed_option_labels,
        required_selection_count=required_selection_count,
        prefix="pass B result",
    )
    if "proposed_findings" not in obj:
        raise AiQualityAuditValidationError(
            "pass B result is missing required field 'proposed_findings'"
        )
    proposed = _validate_proposed_findings(
        obj.get("proposed_findings"),
        frozen_evidence_chunk_ids=frozen_evidence_chunk_ids,
        prefix="pass B result.proposed_findings",
    )
    return {
        "selected_option_labels": labels,
        "proposed_findings": proposed,
    }


def validate_pass_b_correctness_result(
    raw: object,
    *,
    allowed_option_labels: LabelSet,
    frozen_evidence_chunk_ids: LabelSet,
) -> Dict[str, Any]:
    """Validate and normalize the specialized answer-correctness detector's
    payload (V60). The detector judges every option independently and never
    chooses a finding code, materiality, severity, or approval state -- this
    only validates its atomic per-option judgments and overall sufficiency
    signal. ``derive_correctness_finding`` is the sole authority translating
    a validated result into a proposed finding.
    """
    obj = _require_object(raw, "pass B correctness result")
    allowed = _label_set(allowed_option_labels) or set()
    if not allowed:
        raise AiQualityAuditValidationError(
            "allowed_option_labels must contain at least one label"
        )
    frozen_raw = _label_set(frozen_evidence_chunk_ids) or set()
    frozen = {chunk_id.lower() for chunk_id in frozen_raw}

    if "option_judgments" not in obj:
        raise AiQualityAuditValidationError(
            "pass B correctness result is missing required field 'option_judgments'"
        )
    raw_judgments = obj.get("option_judgments")
    if not isinstance(raw_judgments, list) or not raw_judgments:
        raise AiQualityAuditValidationError(
            "pass B correctness result.option_judgments must be a non-empty JSON array"
        )

    if "evidence_sufficient_for_decision" not in obj:
        raise AiQualityAuditValidationError(
            "pass B correctness result is missing required field "
            "'evidence_sufficient_for_decision'"
        )
    evidence_sufficient = obj.get("evidence_sufficient_for_decision")
    if not isinstance(evidence_sufficient, bool):
        raise AiQualityAuditValidationError(
            "pass B correctness result.evidence_sufficient_for_decision must be a boolean"
        )

    abstention_reason_raw = obj.get("abstention_reason")
    if abstention_reason_raw is not None and not isinstance(abstention_reason_raw, str):
        raise AiQualityAuditValidationError(
            "pass B correctness result.abstention_reason must be a string or null"
        )
    abstention_reason = (abstention_reason_raw or "").strip() or None

    normalized_judgments: List[Dict[str, Any]] = []
    seen_labels: Set[str] = set()
    any_insufficient = False
    for index, item in enumerate(raw_judgments):
        prefix = f"pass B correctness result.option_judgments[{index}]"
        if not isinstance(item, dict):
            raise AiQualityAuditValidationError(f"{prefix} must be a JSON object")

        label = _require_non_empty_string(item.get("option_label"), f"{prefix}.option_label")
        if label not in allowed:
            raise AiQualityAuditValidationError(
                f"{prefix}.option_label={label!r} is not an allowed option label"
            )
        if label in seen_labels:
            raise AiQualityAuditValidationError(
                f"{prefix}.option_label contains duplicate label: {label!r}"
            )
        seen_labels.add(label)

        verdict = _require_non_empty_string(item.get("verdict"), f"{prefix}.verdict")
        if verdict not in ANSWER_CORRECTNESS_VERDICTS:
            raise AiQualityAuditValidationError(
                f"{prefix}.verdict={verdict!r} is not one of "
                f"{sorted(ANSWER_CORRECTNESS_VERDICTS)}"
            )

        rationale = _require_non_empty_string(
            item.get("evidence_rationale"), f"{prefix}.evidence_rationale"
        )

        citations_raw = item.get("citation_chunk_ids")
        if not isinstance(citations_raw, list):
            raise AiQualityAuditValidationError(
                f"{prefix}.citation_chunk_ids must be a JSON array"
            )
        citations: List[str] = []
        seen_chunks: Set[str] = set()
        for chunk_index, chunk_value in enumerate(citations_raw):
            chunk_id = _validate_uuid_string(
                chunk_value, f"{prefix}.citation_chunk_ids[{chunk_index}]"
            )
            if chunk_id in seen_chunks:
                raise AiQualityAuditValidationError(
                    f"{prefix}.citation_chunk_ids contains duplicate chunk id: {chunk_id!r}"
                )
            if chunk_id not in frozen:
                raise AiQualityAuditValidationError(
                    f"{prefix}.citation_chunk_ids[{chunk_index}]={chunk_id!r} is outside "
                    f"the frozen run evidence set"
                )
            seen_chunks.add(chunk_id)
            citations.append(chunk_id)

        if verdict == "SUPPORTED_AS_CORRECT" and not citations:
            raise AiQualityAuditValidationError(
                f"{prefix} has verdict=SUPPORTED_AS_CORRECT but no citation_chunk_ids"
            )
        if verdict == "INSUFFICIENT_EVIDENCE":
            any_insufficient = True

        normalized_judgments.append({
            "option_label": label,
            "verdict": verdict,
            "citation_chunk_ids": citations,
            "evidence_rationale": rationale,
        })

    missing = allowed - seen_labels
    if missing:
        raise AiQualityAuditValidationError(
            "pass B correctness result.option_judgments is missing judgments "
            f"for options: {sorted(missing)}"
        )

    if evidence_sufficient and any_insufficient:
        raise AiQualityAuditValidationError(
            "pass B correctness result.evidence_sufficient_for_decision cannot be "
            "true while an option_judgments entry has verdict=INSUFFICIENT_EVIDENCE"
        )
    if not frozen and evidence_sufficient:
        raise AiQualityAuditValidationError(
            "pass B correctness result.evidence_sufficient_for_decision cannot be "
            "true when the run has zero frozen evidence chunks"
        )
    if not evidence_sufficient and not abstention_reason:
        raise AiQualityAuditValidationError(
            "pass B correctness result.abstention_reason must be a non-empty string "
            "when evidence_sufficient_for_decision is false"
        )
    if evidence_sufficient and abstention_reason:
        raise AiQualityAuditValidationError(
            "pass B correctness result.abstention_reason must be null when "
            "evidence_sufficient_for_decision is true"
        )

    return {
        "option_judgments": normalized_judgments,
        "evidence_sufficient_for_decision": evidence_sufficient,
        "abstention_reason": abstention_reason,
    }


def derive_correctness_finding(
    *,
    correctness_result: Mapping[str, Any],
    stored_correct_option_labels: LabelSet,
    required_selection_count: int,
    finding_ref: str = "FC1",
) -> Optional[Dict[str, Any]]:
    """Deterministically derive at most one correctness finding from a
    validated specialist result (V60, bounded-abstention revision /
    V60-DERIVE-01). The specialist never chooses a finding code,
    materiality, or severity -- this function is the sole authority
    translating its atomic per-option judgments into a proposed finding (or
    ``None`` when the stored answer is fully confirmed).

    Let ``S`` = stored correct-label set, ``R`` = required selection count,
    ``E`` = labels judged ``SUPPORTED_AS_CORRECT``, ``C`` = labels judged
    ``NOT_SUPPORTED_AS_CORRECT`` (directly contradicted), ``U`` = labels
    judged ``INSUFFICIENT_EVIDENCE`` (unresolved):

      1. ``E == S``: the stored answer is fully confirmed and nothing
         outside it is also supported -- no correctness finding (``None``),
         *regardless* of any remaining ``U`` members. A distractor the
         specialist could not decide is never, by itself, a reason to
         abstain (this is the V60-RULE-REVIEW-01 fix: previously *any*
         ``U`` member forced abstention here, even when the stored answer
         itself was already decisively confirmed).
      2. ``len(E) > R``: more options are independently supported than the
         question allows.
         2a. If ``S`` is a subset of ``E`` *and* ``U`` is non-empty: this is
             the validated "trap/meta-option" pattern (qbv1-037; distinct
             from the fully-resolved qbv1-036 tie by the presence of a
             still-unresolved option) -- an unresolved option could still
             turn out to be the question's real intended distinguishing
             answer, so this abstains (``OTHER_REVIEW_NEEDED``) rather than
             auto-resolving. This is a conservative, deliberate departure
             from an unconditional "S subset of E always abstains" reading:
             qbv1-036 also has ``S`` a subset of ``E`` but is *fully*
             resolved (``U`` empty, ``evidence_sufficient_for_decision``
             true) and is correctly ``MULTIPLE_DEFENSIBLE_ANSWERS`` --
             the two captured-telemetry examples are only distinguishable
             by ``U``, not by the ``S ⊆ E`` relationship alone.
         2b. Otherwise: ``MULTIPLE_DEFENSIBLE_ANSWERS``.
      3. ``len(E) == R`` and ``E != S`` (guaranteed once rule 1 does not
         apply and ``|S| == R``): an alternative, exact-size answer set
         replaces the stored one.
         3a. If any stored label is directly contradicted (``S`` intersects
             ``C``): ``WRONG_ANSWER_KEY``.
         3b. Otherwise (every stored label is merely ``INSUFFICIENT_EVIDENCE``,
             never itself contradicted): ``UNSUPPORTED_ANSWER``. This is the
             count-based-vs-contradiction-based distinction validated in
             V60-RULE-REVIEW-01 (qbv1-006/007 vs qbv1-034) -- the prior
             implementation selected the code purely by comparing set sizes
             and could never reach this branch at all (it aborted on any
             ``U`` member first).
      4. ``len(E) < R``: too few options are supported to match the
         required count.
         4a. If every stored label has a decisive verdict (``S`` is a
             subset of ``E`` union ``C`` -- i.e. no stored label is itself
             still ``INSUFFICIENT_EVIDENCE``): the required count is
             already structurally unavailable from the stored set no
             matter how any *other* still-open ``U`` member resolves --
             ``UNSUPPORTED_ANSWER``.
         4b. Otherwise (a stored label is itself unresolved, e.g.
             qbv1-020/qbv1-030 where every option including the stored one
             is ``INSUFFICIENT_EVIDENCE``): fall through to rule 5.
      5. Genuine unresolved state (catch-all): none of the above
         deterministic branches apply, so resolving the remaining ``U``
         member(s) could still change whether a correctness finding exists
         at all -- ``OTHER_REVIEW_NEEDED``.
    """
    stored = _label_set(stored_correct_option_labels) or set()
    judgments = list(correctness_result.get("option_judgments") or [])

    verdict_by_label: Dict[str, str] = {}
    for judgment in judgments:
        label = judgment.get("option_label")
        verdict = judgment.get("verdict")
        if label is not None and verdict is not None:
            verdict_by_label[str(label)] = str(verdict)

    supported = {label for label, v in verdict_by_label.items() if v == "SUPPORTED_AS_CORRECT"}
    contradicted = {label for label, v in verdict_by_label.items() if v == "NOT_SUPPORTED_AS_CORRECT"}
    insufficient = {label for label, v in verdict_by_label.items() if v == "INSUFFICIENT_EVIDENCE"}

    cited_chunks: List[str] = []
    seen_chunks: Set[str] = set()
    for judgment in judgments:
        for chunk_id in judgment.get("citation_chunk_ids") or []:
            if chunk_id not in seen_chunks:
                seen_chunks.add(chunk_id)
                cited_chunks.append(chunk_id)

    def _abstain(reason: Optional[str] = None) -> Dict[str, Any]:
        resolved_reason = (
            reason
            or correctness_result.get("abstention_reason")
            or "The answer-correctness detector could not reach a decisive "
            "evidence-based verdict for one or more options."
        )
        return {
            "finding_ref": finding_ref,
            "finding_code": "OTHER_REVIEW_NEEDED",
            "finding_type": "correctness",
            "severity": "high",
            "materiality": "blocking",
            "title": "Answer correctness could not be confirmed from evidence",
            "description": resolved_reason,
            "evidence_chunk_ids": cited_chunks,
            "metadata": {
                "correctness_detector_abstained": True,
                "abstention_reason": resolved_reason,
            },
        }

    # Rule 1 -- stored answer confirmed, nothing else supported. A
    # non-stored distractor being INSUFFICIENT_EVIDENCE is never, by
    # itself, a reason to abstain.
    if supported == stored:
        return None

    # Rule 2 -- more options supported than required.
    if len(supported) > required_selection_count:
        if stored <= supported and insufficient:
            return _abstain()
        code = "MULTIPLE_DEFENSIBLE_ANSWERS"
        title = "Multiple options are independently supported by evidence"
        description = (
            f"The answer-correctness detector found {sorted(supported)} "
            "independently evidence-supported, exceeding the required "
            f"selection count of {required_selection_count}."
        )
        return {
            "finding_ref": finding_ref,
            "finding_code": code,
            "finding_type": "correctness",
            "severity": "high",
            "materiality": "blocking",
            "title": title,
            "description": description,
            "evidence_chunk_ids": cited_chunks,
            "metadata": {
                "correctness_detector_supported_labels": sorted(supported),
                "correctness_detector_stored_labels": sorted(stored),
            },
        }

    # Rule 3 -- an exact-size alternative answer set replaces the stored
    # one (supported != stored is guaranteed here; rule 1 already handled
    # the equality case).
    if len(supported) == required_selection_count:
        if stored & contradicted:
            code = "WRONG_ANSWER_KEY"
            title = "Stored answer key is not the evidence-supported option set"
            description = (
                f"The answer-correctness detector found {sorted(supported)} "
                f"evidence-supported instead of the stored correct set {sorted(stored)}."
            )
        else:
            code = "UNSUPPORTED_ANSWER"
            title = "Stored answer key is not confirmed by evidence"
            description = (
                f"The answer-correctness detector found {sorted(supported)} "
                f"evidence-supported instead of the stored correct set "
                f"{sorted(stored)}, which was not directly contradicted but "
                "was never itself confirmed."
            )
        return {
            "finding_ref": finding_ref,
            "finding_code": code,
            "finding_type": "correctness",
            "severity": "high",
            "materiality": "blocking",
            "title": title,
            "description": description,
            "evidence_chunk_ids": cited_chunks,
            "metadata": {
                "correctness_detector_supported_labels": sorted(supported),
                "correctness_detector_stored_labels": sorted(stored),
            },
        }

    # Rule 4 -- too few options supported. If every stored label already
    # has a decisive verdict (supported or contradicted, never itself
    # insufficient), the required count is structurally unavailable no
    # matter how any other still-open option resolves.
    decisive_labels = supported | contradicted
    if stored <= decisive_labels:
        description = (
            f"The answer-correctness detector found only {sorted(supported)} "
            f"evidence-supported option(s), fewer than the required "
            f"{required_selection_count}; the stored answer key {sorted(stored)} "
            "is not confirmed."
        )
        return {
            "finding_ref": finding_ref,
            "finding_code": "UNSUPPORTED_ANSWER",
            "finding_type": "correctness",
            "severity": "high",
            "materiality": "blocking",
            "title": "Stored answer key is not confirmed by evidence",
            "description": description,
            "evidence_chunk_ids": cited_chunks,
            "metadata": {
                "correctness_detector_supported_labels": sorted(supported),
                "correctness_detector_stored_labels": sorted(stored),
            },
        }

    # Rule 5 -- genuine unresolved state: a stored label (rule 4) or an
    # option relevant to a potential trap/meta-option pattern (rule 2)
    # remains INSUFFICIENT_EVIDENCE, and resolving it could still change
    # whether a correctness finding exists, its code, or the
    # required-answer-count outcome.
    return _abstain()


def merge_pass_b_findings(
    *,
    correctness_finding: Optional[Mapping[str, Any]],
    general_proposed_findings: Sequence[Mapping[str, Any]],
    frozen_evidence_chunk_ids: LabelSet,
) -> Dict[str, Any]:
    """The single deterministic Pass B merge boundary (V60).

    The specialist's derived correctness finding (if any) is authoritative
    for ``ANSWER_CORRECTNESS_CODES``. The general judge's non-correctness
    findings are kept unchanged (already canonicalized by
    ``validate_pass_b_result``). If the general judge nonetheless proposes a
    correctness-family code, it is never accepted -- it is dropped and
    recorded in ``dropped_general_findings`` for drift diagnostics rather
    than silently discarded or allowed to override the specialist. This is
    the only place finding lists are combined; no second materiality
    mapping is introduced here -- the specialist's finding is run through
    the exact same ``_validate_proposed_finding``/``assign_materiality``
    path as any other proposed finding.
    """
    validated_correctness: List[Dict[str, Any]] = []
    if correctness_finding is not None:
        validated_correctness = _validate_proposed_findings(
            [dict(correctness_finding)],
            frozen_evidence_chunk_ids=frozen_evidence_chunk_ids,
            prefix="pass B correctness result.derived_finding",
        )

    dropped: List[Dict[str, Any]] = []
    kept_general: List[Dict[str, Any]] = []
    for item in general_proposed_findings:
        code = item.get("finding_code")
        if code in ANSWER_CORRECTNESS_CODES:
            dropped.append({
                "finding_ref": item.get("finding_ref"),
                "finding_code": code,
                "reason": "general_judge_emitted_specialist_owned_code",
            })
            continue
        kept_general.append(dict(item))

    merged = list(validated_correctness) + kept_general

    seen_refs: Set[str] = set()
    for item in merged:
        ref = item.get("finding_ref")
        if ref in seen_refs:
            raise AiQualityAuditValidationError(
                f"pass B merged proposed_findings contains duplicate finding_ref: {ref!r}"
            )
        seen_refs.add(ref)

    return {
        "proposed_findings": merged,
        "dropped_general_findings": dropped,
    }


def validate_pass_c_result(
    raw: object,
    *,
    pass_b_proposed_finding_refs: Optional[LabelSet] = None,
    frozen_evidence_chunk_ids: Optional[LabelSet] = None,
) -> Dict[str, Any]:
    """Validate and normalize a Pass C completed result payload."""
    obj = _require_object(raw, "pass C result")

    resolution_type = _require_non_empty_string(
        obj.get("resolution_type"),
        "pass C result.resolution_type",
    )
    if resolution_type not in ALLOWED_RESOLUTION_TYPES:
        raise AiQualityAuditValidationError(
            f"pass C result.resolution_type must be one of "
            f"{sorted(ALLOWED_RESOLUTION_TYPES)}, got {resolution_type!r}"
        )

    resolution_status = _require_non_empty_string(
        obj.get("resolution_status"),
        "pass C result.resolution_status",
    )
    if resolution_status not in ALLOWED_RESOLUTION_STATUS:
        raise AiQualityAuditValidationError(
            f"pass C result.resolution_status must be one of "
            f"{sorted(ALLOWED_RESOLUTION_STATUS)}, got {resolution_status!r}"
        )

    substituted = _validate_string_list(
        obj.get("substituted_for_passes"),
        "pass C result.substituted_for_passes",
        element_type=str,
        allow_empty=True,
    )
    expected_substituted = _SUBSTITUTED_FOR_PASSES_BY_TYPE[resolution_type]
    if substituted != expected_substituted:
        raise AiQualityAuditValidationError(
            f"pass C result.resolution_type={resolution_type!r} requires "
            f"substituted_for_passes={expected_substituted!r}, got {substituted!r}"
        )

    confirmed = _validate_confirmed_finding_refs(
        obj.get("confirmed_finding_refs"),
        prefix="pass C result.confirmed_finding_refs",
    )

    if resolution_status == "UNRESOLVED":
        if confirmed:
            raise AiQualityAuditValidationError(
                "pass C result.confirmed_finding_refs must be empty when "
                "resolution_status is UNRESOLVED"
            )
        normalized: Dict[str, Any] = {
            "resolution_type": resolution_type,
            "resolution_status": resolution_status,
            "substituted_for_passes": substituted,
            "confirmed_finding_refs": [],
        }
        return normalized

    if resolution_type == "NORMAL_DISPUTE":
        upstream = _label_set(pass_b_proposed_finding_refs)
        if upstream is None:
            raise AiQualityAuditValidationError(
                "pass_b_proposed_finding_refs is required for NORMAL_DISPUTE validation"
            )
        for ref in confirmed:
            if ref not in upstream:
                raise AiQualityAuditValidationError(
                    f"pass C result.confirmed_finding_refs[{ref!r}] is not present "
                    f"in Pass B proposed_findings"
                )
        normalized = {
            "resolution_type": resolution_type,
            "resolution_status": resolution_status,
            "substituted_for_passes": substituted,
            "confirmed_finding_refs": confirmed,
        }
        return normalized

    # Substitution modes: validate Pass C proposed_findings when present.
    frozen = _label_set(frozen_evidence_chunk_ids)
    if frozen is None:
        raise AiQualityAuditValidationError(
            "frozen_evidence_chunk_ids is required for substitution Pass C validation"
        )
    if "proposed_findings" not in obj:
        raise AiQualityAuditValidationError(
            "pass C result is missing required field 'proposed_findings' "
            f"for resolution_type={resolution_type!r}"
        )
    proposed = _validate_proposed_findings(
        obj.get("proposed_findings"),
        frozen_evidence_chunk_ids=frozen,
        prefix="pass C result.proposed_findings",
    )
    proposed_refs = {item["finding_ref"] for item in proposed}
    for ref in confirmed:
        if ref not in proposed_refs:
            raise AiQualityAuditValidationError(
                f"pass C result.confirmed_finding_refs[{ref!r}] is not present "
                f"in Pass C proposed_findings"
            )

    return {
        "resolution_type": resolution_type,
        "resolution_status": resolution_status,
        "substituted_for_passes": substituted,
        "confirmed_finding_refs": confirmed,
        "proposed_findings": proposed,
    }


def _require_object(raw: object, prefix: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise AiQualityAuditValidationError(
            f"{prefix} must be a JSON object, got {type(raw).__name__}"
        )
    return raw


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise AiQualityAuditValidationError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    text = value.strip()
    if not text:
        raise AiQualityAuditValidationError(f"{field_name} must not be empty")
    return text


def _label_set(values: Optional[LabelSet]) -> Optional[Set[str]]:
    if values is None:
        return None
    return {str(item).strip() for item in values if str(item).strip()}


def _validate_selected_option_labels(
    raw: object,
    *,
    allowed_option_labels: LabelSet,
    required_selection_count: int,
    prefix: str,
) -> List[str]:
    if required_selection_count < 1:
        raise AiQualityAuditValidationError(
            "required_selection_count must be a positive integer"
        )
    allowed = _label_set(allowed_option_labels) or set()
    if not allowed:
        raise AiQualityAuditValidationError(
            "allowed_option_labels must contain at least one label"
        )

    if not isinstance(raw, list):
        raise AiQualityAuditValidationError(
            f"{prefix}.selected_option_labels must be a JSON array, got "
            f"{type(raw).__name__ if raw is not None else 'null'}"
        )
    if not raw:
        raise AiQualityAuditValidationError(
            f"{prefix}.selected_option_labels must not be empty"
        )

    normalized: List[str] = []
    seen: Set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise AiQualityAuditValidationError(
                f"{prefix}.selected_option_labels[{index}] must be a string, "
                f"got {type(item).__name__}"
            )
        label = item.strip()
        if not label:
            raise AiQualityAuditValidationError(
                f"{prefix}.selected_option_labels[{index}] must not be empty"
            )
        if label in seen:
            raise AiQualityAuditValidationError(
                f"{prefix}.selected_option_labels contains duplicate label: {label!r}"
            )
        if label not in allowed:
            raise AiQualityAuditValidationError(
                f"{prefix}.selected_option_labels[{index}]={label!r} is not an allowed option label"
            )
        seen.add(label)
        normalized.append(label)

    if len(normalized) != required_selection_count:
        raise AiQualityAuditValidationError(
            f"{prefix}.selected_option_labels must contain exactly "
            f"{required_selection_count} label(s), got {len(normalized)}"
        )
    return normalized


def _validate_string_list(
    raw: object,
    field_name: str,
    *,
    element_type: type = str,
    allow_empty: bool = True,
    unique: bool = False,
    non_empty_elements: bool = False,
) -> List[Any]:
    if not isinstance(raw, list):
        raise AiQualityAuditValidationError(
            f"{field_name} must be a JSON array, got "
            f"{type(raw).__name__ if raw is not None else 'null'}"
        )
    if not allow_empty and not raw:
        raise AiQualityAuditValidationError(f"{field_name} must not be empty")

    normalized: List[Any] = []
    seen: Set[Any] = set()
    for index, item in enumerate(raw):
        if element_type is str:
            if not isinstance(item, str):
                raise AiQualityAuditValidationError(
                    f"{field_name}[{index}] must be a string, got {type(item).__name__}"
                )
            value: Any = item.strip()
            if non_empty_elements and not value:
                raise AiQualityAuditValidationError(
                    f"{field_name}[{index}] must not be empty"
                )
        else:
            value = item
        if unique:
            if value in seen:
                raise AiQualityAuditValidationError(
                    f"{field_name} contains duplicate value: {value!r}"
                )
            seen.add(value)
        normalized.append(value)
    return normalized


def _validate_confirmed_finding_refs(raw: object, *, prefix: str) -> List[str]:
    return _validate_string_list(
        raw,
        prefix,
        element_type=str,
        allow_empty=True,
        unique=True,
        non_empty_elements=True,
    )


def _validate_uuid_string(value: object, field_name: str) -> str:
    text = _require_non_empty_string(value, field_name)
    if not _UUID_RE.match(text):
        raise AiQualityAuditValidationError(
            f"{field_name} must be a UUID string, got {text!r}"
        )
    return text.lower()


def _validate_metadata(raw: object, field_name: str) -> Dict[str, Any]:
    if raw is None:
        raise AiQualityAuditValidationError(
            f"{field_name} must be a JSON object, got null"
        )
    if not isinstance(raw, dict):
        raise AiQualityAuditValidationError(
            f"{field_name} must be a JSON object, got {type(raw).__name__}"
        )
    return dict(raw)


def _validate_source_support_context(
    metadata: Mapping[str, Any],
    *,
    prefix: str,
) -> None:
    ctx = metadata.get("source_support_context")
    if not isinstance(ctx, dict):
        raise AiQualityAuditValidationError(
            f"{prefix}.metadata.source_support_context must be a JSON object"
        )

    attempted = ctx.get("attempted_retrieval")
    if isinstance(attempted, bool) or not isinstance(attempted, (int, float)):
        raise AiQualityAuditValidationError(
            f"{prefix}.metadata.source_support_context.attempted_retrieval "
            "must be a nonnegative integer"
        )
    if attempted < 0 or attempted != int(attempted):
        raise AiQualityAuditValidationError(
            f"{prefix}.metadata.source_support_context.attempted_retrieval "
            "must be a nonnegative integer"
        )

    for field in (
        "evidence_limitation",
        "proposed_technical_claim",
        "insufficiency_reason",
    ):
        value = ctx.get(field)
        if not isinstance(value, str) or not value.strip():
            raise AiQualityAuditValidationError(
                f"{prefix}.metadata.source_support_context.{field} must be non-empty"
            )


def _validate_proposed_finding(
    index: int,
    raw: object,
    *,
    frozen_evidence_chunk_ids: Set[str],
    list_prefix: str,
) -> Dict[str, Any]:
    prefix = f"{list_prefix}[{index}]"
    if not isinstance(raw, dict):
        raise AiQualityAuditValidationError(
            f"{prefix} must be a JSON object, got {type(raw).__name__}"
        )

    finding_ref = _require_non_empty_string(raw.get("finding_ref"), f"{prefix}.finding_ref")
    finding_code = _require_non_empty_string(raw.get("finding_code"), f"{prefix}.finding_code")
    if finding_code not in SUPPORTED_FINDING_CODES:
        raise AiQualityAuditValidationError(
            f"{prefix}.finding_code={finding_code!r} is not a supported finding code"
        )

    finding_type = _require_non_empty_string(raw.get("finding_type"), f"{prefix}.finding_type")
    if finding_type not in ALLOWED_FINDING_TYPES:
        raise AiQualityAuditValidationError(
            f"{prefix}.finding_type={finding_type!r} is not an allowed finding type"
        )

    severity = _require_non_empty_string(raw.get("severity"), f"{prefix}.severity")
    if severity not in ALLOWED_SEVERITIES:
        raise AiQualityAuditValidationError(
            f"{prefix}.severity={severity!r} is not an allowed severity"
        )

    materiality = _require_non_empty_string(raw.get("materiality"), f"{prefix}.materiality")
    if materiality not in ALLOWED_MATERIALITY:
        raise AiQualityAuditValidationError(
            f"{prefix}.materiality={materiality!r} is not an allowed materiality"
        )

    title = _require_non_empty_string(raw.get("title"), f"{prefix}.title")
    description = _require_non_empty_string(raw.get("description"), f"{prefix}.description")
    if "metadata" not in raw:
        raise AiQualityAuditValidationError(
            f"{prefix} is missing required field 'metadata'"
        )
    metadata = _validate_metadata(raw.get("metadata"), f"{prefix}.metadata")

    if finding_code == "SOURCE_SUPPORT_WEAK":
        if finding_type != "source_support":
            raise AiQualityAuditValidationError(
                f"{prefix}.finding_type must be 'source_support' when "
                f"finding_code is SOURCE_SUPPORT_WEAK"
            )
        if materiality == "blocking":
            raise AiQualityAuditValidationError(
                f"{prefix}.materiality cannot be 'blocking' when "
                f"finding_code is SOURCE_SUPPORT_WEAK"
            )

    if finding_code == "DOMAIN_MISALIGNMENT":
        if finding_type != "coverage":
            raise AiQualityAuditValidationError(
                f"{prefix}.finding_type must be 'coverage' when "
                f"finding_code is DOMAIN_MISALIGNMENT"
            )
        if materiality == "blocking":
            raise AiQualityAuditValidationError(
                f"{prefix}.materiality cannot be 'blocking' when "
                f"finding_code is DOMAIN_MISALIGNMENT"
            )

    if "evidence_chunk_ids" not in raw:
        raise AiQualityAuditValidationError(
            f"{prefix} is missing required field 'evidence_chunk_ids'"
        )
    chunk_ids_raw = raw.get("evidence_chunk_ids")
    if not isinstance(chunk_ids_raw, list):
        raise AiQualityAuditValidationError(
            f"{prefix}.evidence_chunk_ids must be a JSON array"
        )

    normalized_chunk_ids: List[str] = []
    seen_chunks: Set[str] = set()
    frozen = {chunk_id.lower() for chunk_id in frozen_evidence_chunk_ids}
    for chunk_index, chunk_value in enumerate(chunk_ids_raw):
        chunk_id = _validate_uuid_string(
            chunk_value,
            f"{prefix}.evidence_chunk_ids[{chunk_index}]",
        )
        if chunk_id in seen_chunks:
            raise AiQualityAuditValidationError(
                f"{prefix}.evidence_chunk_ids contains duplicate chunk id: {chunk_id!r}"
            )
        if chunk_id not in frozen:
            raise AiQualityAuditValidationError(
                f"{prefix}.evidence_chunk_ids[{chunk_index}]={chunk_id!r} is outside "
                f"the frozen run evidence set"
            )
        seen_chunks.add(chunk_id)
        normalized_chunk_ids.append(chunk_id)

    if finding_code == "SOURCE_SUPPORT_WEAK" and not normalized_chunk_ids:
        _validate_source_support_context(metadata, prefix=prefix)

    # V59-FINDING-01: the provider-emitted `materiality` above is only
    # validated for shape (member of ALLOWED_MATERIALITY) and for the two
    # code-specific "must not be blocking" guards; it is never trusted as
    # the value that reaches persistence. `workers.finding_policy.
    # assign_materiality` — this repository's sole materiality authority —
    # is applied here, at parse time, before either the Pass B/C in-memory
    # proposal or the eventual `audit_findings` row can exist. This is the
    # narrowest boundary shared by every downstream consumer (primary Pass
    # B proposals, Pass C substitution proposals, and — transitively, since
    # confirmed findings are built from these same validated proposals —
    # persistence and `_summarize_findings`), so a provider cannot smuggle
    # a noncanonical materiality (e.g. `EXPLANATION_MISSING@warning`) past
    # this point. Idempotent: re-validating an already-canonical finding
    # recomputes the same value. The provider's original value is not
    # silently discarded — it is preserved in `metadata.provider_materiality`
    # whenever it disagrees with canonical policy, in addition to already
    # being preserved verbatim in the pass result's `raw_response_text`.
    canonical_materiality = assign_materiality({
        "finding_code": finding_code,
        "finding_type": finding_type,
        "severity": severity,
        "title": title,
        "description": description,
    })
    if canonical_materiality != materiality:
        metadata = dict(metadata)
        metadata["provider_materiality"] = materiality

    return {
        "finding_ref": finding_ref,
        "finding_code": finding_code,
        "finding_type": finding_type,
        "severity": severity,
        "materiality": canonical_materiality,
        "title": title,
        "description": description,
        "evidence_chunk_ids": normalized_chunk_ids,
        "metadata": metadata,
    }


def _validate_proposed_findings(
    raw: object,
    *,
    frozen_evidence_chunk_ids: LabelSet,
    prefix: str,
) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        raise AiQualityAuditValidationError(
            f"{prefix} must be a JSON array, got "
            f"{type(raw).__name__ if raw is not None else 'null'}"
        )

    frozen = _label_set(frozen_evidence_chunk_ids) or set()
    normalized: List[Dict[str, Any]] = []
    seen_refs: Set[str] = set()
    for index, item in enumerate(raw):
        finding = _validate_proposed_finding(
            index,
            item,
            frozen_evidence_chunk_ids=frozen,
            list_prefix=prefix,
        )
        ref = finding["finding_ref"]
        if ref in seen_refs:
            raise AiQualityAuditValidationError(
                f"{prefix} contains duplicate finding_ref: {ref!r}"
            )
        seen_refs.add(ref)
        normalized.append(finding)
    return normalized
