"""
Precommitted benchmark acceptance policy (V58-QUALITY-04E, corrected by
V58-QUALITY-04E-R1 and V58-QUALITY-04E-R2).

Encodes the acceptance policy from V58-QUALITY-04D (as corrected in
V58-QUALITY-04E, fail-closed-hardened in V58-QUALITY-04E-R1, and bound to
prediction-artifact identity in V58-QUALITY-04E-R2) into deterministic
classification logic. Given a scorecard produced by
``workers.quality_benchmark_execution.score_predictions`` and a
``fixture_metadata`` description of the benchmark's ground-truth identity
and review process, ``classify_scorecard`` returns exactly one of:

    PASS | CONDITIONAL PASS | FAIL | INVALID RUN

This module is intentionally dependency-free (stdlib only): no database,
provider, model, or worker imports. It never runs an engine, never scores a
prediction, and never inspects a fixture file directly - callers (e.g. the
CLI) are responsible for assembling ``fixture_metadata`` from an already
finalized SME-reviewed fixture and, for launch-tier classification, an
explicit review-process attestation. This module never infers review-process
facts (e.g. double-review coverage) from case counts alone; if the caller
does not supply them, launch classification fails closed to INVALID RUN.

V58-QUALITY-04E-R2: configuration identity comes from the scorecard only
--------------------------------------------------------------------------
Configuration identity (``engine_id``/``engine_version``/``provider_id``/
``model_id``/``prompt_version``/``ruleset_version``/``evidence_config_id``/
``source_fixture_sha256``) is read exclusively from
``scorecard["configuration_identity"]`` - the dict
``workers.quality_benchmark_execution.score_predictions`` copies unchanged
from the prediction artifact that was generated. ``fixture_metadata`` no
longer carries (and this module no longer accepts) any engine-configuration
identity field: a caller cannot supply, override, or relabel identity at
classification time, closing the scoring-time relabeling loophole that
existed in V58-QUALITY-04E-R1 (``fixture_metadata["engine_configuration"]``
plus the CLI's ``--engine-configuration-json``, both now removed). The same
completeness rule still applies - every dimension must be an explicit,
non-blank string, with a genuinely not-applicable dimension recorded as an
explicit sentinel (e.g. ``"deterministic-only"``, ``"not-applicable"``) by
the engine adapter at prediction-generation time, never inferred here.

V58-QUALITY-04E-R1 fail-closed corrections (retained)
-------------------------------------------------------
1. Pilot policy v1 requires **exactly** ``PILOT_REQUIRED_CASE_COUNT`` (40)
   finalized cases - not a band. Any other count is INVALID RUN. A future
   pilot of a different size requires a new ``POLICY_VERSION``.
2. Launch's required safety-relevant categories
   (``REQUIRED_LAUNCH_SAFETY_CATEGORIES``) are a fixed module constant.
   ``fixture_metadata["additional_safety_relevant_categories"]`` is
   strictly additive - callers can add extra categories but can never
   remove, replace, or shrink the baseline set (omitting the key, passing
   an empty list, or passing a list missing baseline entries all still
   enforce the full baseline).
3. Classification (both tiers) requires a complete, explicit, non-blank
   configuration identity for the entire run (see above for where it now
   comes from). Any missing or blank dimension is INVALID RUN; nothing is
   ever inferred or defaulted to a passing value.

None of the above changed any existing gate *threshold* (V58-QUALITY-04D's
approved numeric bands are untouched) - these are enforcement corrections to
the already-approved v1 policy intent, not new policy decisions, so
``POLICY_VERSION`` is unchanged (see "Policy versioning" below for why this
is safe here specifically).

Policy versioning
------------------
``POLICY_VERSION`` is stamped on every classification result. Changing any
threshold in this module requires bumping ``POLICY_VERSION``, recording a
written rationale (in docs/V58_BENCHMARK_ACCEPTANCE_POLICY.md), and never
retroactively reclassifying a scorecard that was already classified under a
prior policy version - a stored classification result is a historical
record of "what this policy version said at the time", not a live view.
V58-QUALITY-04E-R1 is an exception recorded explicitly in that doc: it
corrects an enforcement gap in v1 before any real scorecard was ever
classified (no benchmark result has been generated yet), so there is
nothing to retroactively reclassify and no threshold changed - only gaps in
enforcing the already-approved v1 requirements were closed.

Approved classification language
---------------------------------
* Launch PASS language is fixed verbatim (see ``LAUNCH_PASS_LANGUAGE``) and
  must never be paraphrased into a broader accuracy claim. It is scoped to
  "the exact engine, model, prompt, ruleset, evidence configuration, and
  version tested" and must never imply anything universal, guaranteed, or
  statistically conclusive about engine behavior outside that exact
  configuration.
* Pilot PASS language always explicitly states that it is a development
  continuation signal only, not a launch-readiness or production-accuracy
  claim (the 40-case pilot cannot support either claim; see
  docs/V58_BENCHMARK_ACCEPTANCE_POLICY.md).
* ``_PROHIBITED_LAUNCH_CLAIM_PHRASES`` is enforced defensively against every
  piece of human-readable language this module emits (not just the launch
  PASS string), so a future edit cannot silently reintroduce an overclaim.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

POLICY_VERSION = "quality-benchmark-acceptance-policy-v1"

TIER_PILOT = "pilot"
TIER_LAUNCH = "launch"
SUPPORTED_TIERS = frozenset({TIER_PILOT, TIER_LAUNCH})

CLASSIFICATION_PASS = "PASS"
CLASSIFICATION_CONDITIONAL_PASS = "CONDITIONAL PASS"
CLASSIFICATION_FAIL = "FAIL"
CLASSIFICATION_INVALID_RUN = "INVALID RUN"
ALL_CLASSIFICATIONS = (
    CLASSIFICATION_PASS,
    CLASSIFICATION_CONDITIONAL_PASS,
    CLASSIFICATION_FAIL,
    CLASSIFICATION_INVALID_RUN,
)

# ---------------------------------------------------------------------------
# Thresholds (V58-QUALITY-04D, corrected by V58-QUALITY-04E). Any change here
# is a new policy version, not a patch (see module docstring).
# ---------------------------------------------------------------------------

# Pilot policy v1 was designed and approved for exactly one fixture size: the
# 40-case pilot. A pilot-tier classification is refused (INVALID RUN) for any
# other total_case_count so it can never be misread as launch validation and
# so a differently-sized fixture cannot be silently accepted under a policy
# version that was never evaluated against it. A future pilot of a different
# size requires a new POLICY_VERSION, not a change to this constant.
PILOT_REQUIRED_CASE_COUNT = 40

LAUNCH_MIN_TOTAL_CASE_COUNT = 100
LAUNCH_MIN_KNOWN_GOOD_CASE_COUNT = 25
LAUNCH_MIN_BLOCKING_CASE_COUNT = 30
LAUNCH_MIN_CATEGORY_SAMPLE_FLOOR = 15
LAUNCH_MIN_NON_BLOCKING_DOUBLE_REVIEW_FRACTION = 0.20

PILOT_MAX_UNSCORED_CASES_FAIL_THRESHOLD = 2  # more than this -> FAIL
PILOT_KNOWN_GOOD_FALSE_REJECTIONS_FAIL_THRESHOLD = 2  # >= this many -> FAIL
PILOT_WARNING_RECALL_CONDITIONAL_THRESHOLD = 0.50  # strictly below -> CONDITIONAL

LAUNCH_KNOWN_GOOD_FALSE_REJECTION_RATE_FAIL_THRESHOLD = 0.05  # > this -> FAIL
LAUNCH_KNOWN_GOOD_FALSE_REJECTION_RATE_CONDITIONAL_THRESHOLD = 0.02  # > this (and <= FAIL) -> CONDITIONAL
LAUNCH_DEFECTIVE_RECALL_FAIL_THRESHOLD = 0.80  # < this -> FAIL
LAUNCH_DEFECTIVE_RECALL_CONDITIONAL_THRESHOLD = 0.90  # < this (and >= FAIL) -> CONDITIONAL
LAUNCH_CATEGORY_RECALL_CONDITIONAL_THRESHOLD = 0.85  # < this, for eligible categories -> CONDITIONAL
LAUNCH_PRECISION_CONDITIONAL_THRESHOLD = 0.70  # < this -> CONDITIONAL (never FAIL)

# Required launch safety-relevant defect categories: categories where a miss
# can let an actually-wrong or actually-indefensible answer reach a
# candidate. answer_quality/explanation_quality are pedagogical-quality
# categories, not correctness-safety categories, and are deliberately
# excluded from this baseline. This is a fixed policy floor, not a caller
# option: fixture_metadata["additional_safety_relevant_categories"] may only
# ADD categories on top of this baseline (see _normalize_fixture_metadata).
# Removing or replacing a baseline category requires a new POLICY_VERSION.
REQUIRED_LAUNCH_SAFETY_CATEGORIES = frozenset({"correctness", "ambiguity", "source_support"})

# The complete set of dimensions that must be present as explicit,
# non-blank strings in scorecard["configuration_identity"] (copied verbatim
# by score_predictions from the prediction artifact - see
# workers.quality_benchmark_execution) before classification may proceed.
# V58-QUALITY-04E-R2: this module reads identity exclusively from the
# scorecard - fixture_metadata carries no engine-configuration field at all,
# so there is nothing here for a caller to override or relabel. A genuinely
# not-applicable dimension (e.g. no LLM call made) must already be an
# explicit sentinel recorded at prediction-generation time (e.g.
# "deterministic-only", "not-applicable") - this module never infers one.
REQUIRED_SCORECARD_CONFIGURATION_IDENTITY_FIELDS = (
    "engine_id",
    "engine_version",
    "provider_id",
    "model_id",
    "prompt_version",
    "ruleset_version",
    "evidence_config_id",
    "source_fixture_sha256",
)

LAUNCH_PASS_LANGUAGE = (
    "Passed CertBound's launch benchmark for the exact engine, model, prompt, "
    "ruleset, evidence configuration, and version tested."
)

_PROHIBITED_LAUNCH_CLAIM_PHRASES = (
    "production-accurate",
    "production accurate",
    "universally accurate",
    "proven safe",
    "guaranteed",
    "statistically conclusive",
)


class PolicyInputError(ValueError):
    """Raised when ``scorecard``/``fixture_metadata`` are structurally invalid.

    Reserved for programmer/integration errors (wrong types, missing
    structurally-required keys) - never for domain-level policy outcomes,
    which are always returned as an INVALID RUN/FAIL/CONDITIONAL PASS
    classification instead of an exception.
    """


class _PolicyInternalError(RuntimeError):
    """Raised if this module would emit prohibited claim language.

    Indicates a bug in this module's own text templates, not a caller error;
    should never be reachable in normal operation (covered by tests).
    """


def _assert_no_prohibited_language(*texts: str) -> None:
    for text in texts:
        lowered = text.lower()
        for phrase in _PROHIBITED_LAUNCH_CLAIM_PHRASES:
            if phrase in lowered:
                raise _PolicyInternalError(
                    f"policy language contains a prohibited claim phrase {phrase!r}: {text!r}"
                )


# ---------------------------------------------------------------------------
# fixture_metadata validation / normalization
# ---------------------------------------------------------------------------


def _get_bool(mapping: Mapping[str, Any], key: str, *, default: Optional[bool] = False) -> Optional[bool]:
    if key not in mapping or mapping[key] is None:
        return default
    value = mapping[key]
    if not isinstance(value, bool):
        raise PolicyInputError(f"fixture_metadata[{key!r}] must be a boolean, got {type(value).__name__}")
    return value


def _get_str_or_none(mapping: Mapping[str, Any], key: str) -> Optional[str]:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PolicyInputError(f"fixture_metadata[{key!r}] must be a string or null, got {type(value).__name__}")
    return value


def _get_int(mapping: Mapping[str, Any], key: str, *, default: int = 0) -> int:
    value = mapping.get(key, default)
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyInputError(f"fixture_metadata[{key!r}] must be an integer, got {type(value).__name__}")
    return value


def _get_str_list(mapping: Mapping[str, Any], key: str) -> List[str]:
    value = mapping.get(key) or []
    if not isinstance(value, (list, tuple)):
        raise PolicyInputError(f"fixture_metadata[{key!r}] must be a list, got {type(value).__name__}")
    normalized: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise PolicyInputError(f"fixture_metadata[{key!r}] entries must be strings")
        normalized.append(item)
    return normalized


def _get_category_counts(mapping: Mapping[str, Any], key: str) -> Dict[str, int]:
    value = mapping.get(key) or {}
    if not isinstance(value, dict):
        raise PolicyInputError(f"fixture_metadata[{key!r}] must be a JSON object")
    normalized: Dict[str, int] = {}
    for category, count in value.items():
        if not isinstance(category, str):
            raise PolicyInputError(f"fixture_metadata[{key!r}] keys must be strings")
        if isinstance(count, bool) or not isinstance(count, int):
            raise PolicyInputError(f"fixture_metadata[{key!r}][{category!r}] must be an integer")
        normalized[category] = count
    return normalized


def _normalize_review_process(mapping: Mapping[str, Any], key: str) -> Optional[Dict[str, Any]]:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PolicyInputError(f"fixture_metadata[{key!r}] must be a JSON object or null")
    return {
        "blocking_cases_double_reviewed_count": _get_int(value, "blocking_cases_double_reviewed_count"),
        "blocking_cases_total_count": _get_int(value, "blocking_cases_total_count"),
        "non_blocking_cases_double_reviewed_count": _get_int(value, "non_blocking_cases_double_reviewed_count"),
        "non_blocking_cases_total_count": _get_int(value, "non_blocking_cases_total_count"),
        "disagreements_adjudicated_or_excluded": _get_bool(
            value, "disagreements_adjudicated_or_excluded", default=False
        ),
    }


def _normalize_fixture_metadata(fixture_metadata: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(fixture_metadata, Mapping):
        raise PolicyInputError("fixture_metadata must be a mapping")
    return {
        "ground_truth_finalized": _get_bool(fixture_metadata, "ground_truth_finalized"),
        "is_ai_drafted": _get_bool(fixture_metadata, "is_ai_drafted", default=True),
        "sme_review_status": _get_str_or_none(fixture_metadata, "sme_review_status"),
        "rejected_case_ids": _get_str_list(fixture_metadata, "rejected_case_ids"),
        "unresolved_second_review_case_ids": _get_str_list(
            fixture_metadata, "unresolved_second_review_case_ids"
        ),
        "source_fixture_sha256": _get_str_or_none(fixture_metadata, "source_fixture_sha256"),
        "configuration_mixed": _get_bool(fixture_metadata, "configuration_mixed", default=False),
        "total_case_count": _get_int(fixture_metadata, "total_case_count"),
        "known_good_case_count": _get_int(fixture_metadata, "known_good_case_count"),
        "defective_case_count": _get_int(fixture_metadata, "defective_case_count"),
        "blocking_case_count": _get_int(fixture_metadata, "blocking_case_count"),
        "warning_case_count": _get_int(fixture_metadata, "warning_case_count"),
        "category_case_counts": _get_category_counts(fixture_metadata, "category_case_counts"),
        # Additive-only: the required baseline is always unioned in, so a
        # caller can never remove/replace it by omitting the key, passing an
        # empty list, or passing a list that lacks some baseline entries.
        "safety_relevant_categories": sorted(
            REQUIRED_LAUNCH_SAFETY_CATEGORIES
            | set(_get_str_list(fixture_metadata, "additional_safety_relevant_categories"))
        ),
        "review_process": _normalize_review_process(fixture_metadata, "review_process"),
    }


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


# ---------------------------------------------------------------------------
# Shared (tier-independent) ground-truth / configuration gates
# ---------------------------------------------------------------------------


def _evaluate_shared_gates(
    scorecard: Mapping[str, Any], meta: Mapping[str, Any]
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Ground-truth and configuration gates shared by both tiers.

    Returns (gate_results, invalid_reasons). Any non-empty invalid_reasons
    means the run is INVALID RUN regardless of any other metric.
    """
    gates: Dict[str, Dict[str, Any]] = {}
    reasons: List[str] = []

    is_ai_drafted = meta["is_ai_drafted"]
    gates["fixture_is_not_ai_drafted"] = {"passed": not is_ai_drafted, "is_ai_drafted": is_ai_drafted}
    if is_ai_drafted:
        reasons.append("fixture is AI-drafted; results are invalid until SME review is finalized")

    ground_truth_finalized = meta["ground_truth_finalized"]
    gates["ground_truth_finalized"] = {"passed": bool(ground_truth_finalized)}
    if not ground_truth_finalized:
        reasons.append("ground truth is not finalized")

    review_status = meta["sme_review_status"]
    status_ok = review_status == "complete"
    gates["sme_review_status_complete"] = {"passed": status_ok, "sme_review_status": review_status}
    if not status_ok:
        reasons.append(f"fixture is partially reviewed: sme_review_status={review_status!r} (expected 'complete')")

    rejected_ids = meta["rejected_case_ids"]
    gates["no_rejected_cases"] = {"passed": not rejected_ids, "rejected_case_ids": sorted(rejected_ids)}
    if rejected_ids:
        reasons.append(f"fixture has {len(rejected_ids)} rejected case(s) still present: {sorted(rejected_ids)}")

    unresolved_ids = meta["unresolved_second_review_case_ids"]
    gates["no_unresolved_second_review_cases"] = {
        "passed": not unresolved_ids,
        "unresolved_second_review_case_ids": sorted(unresolved_ids),
    }
    if unresolved_ids:
        reasons.append(
            f"fixture has {len(unresolved_ids)} unresolved needs_second_review case(s): {sorted(unresolved_ids)}"
        )

    # Complete configuration identity (V58-QUALITY-04E-R1 correction 4,
    # bound to the prediction artifact in V58-QUALITY-04E-R2): every
    # dimension must be an explicit, non-blank string read exclusively from
    # scorecard["configuration_identity"] - the dict score_predictions
    # copies unchanged from the prediction artifact generated for this run.
    # fixture_metadata carries no identity field at all, so there is
    # nothing here for a caller to override or relabel at classification
    # time. Nothing is ever inferred or defaulted to a passing value - a
    # missing dimension is always INVALID RUN.
    def _nonblank(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    scorecard_identity = scorecard.get("configuration_identity")
    if not isinstance(scorecard_identity, Mapping):
        identity_fields: Dict[str, Any] = {field: None for field in REQUIRED_SCORECARD_CONFIGURATION_IDENTITY_FIELDS}
        missing_identity_fields = sorted(REQUIRED_SCORECARD_CONFIGURATION_IDENTITY_FIELDS)
    else:
        identity_fields = {
            field: scorecard_identity.get(field) for field in REQUIRED_SCORECARD_CONFIGURATION_IDENTITY_FIELDS
        }
        missing_identity_fields = sorted(
            field for field, value in identity_fields.items() if not _nonblank(value)
        )
    config_mixed = meta["configuration_mixed"]
    config_identity_ok = not missing_identity_fields and not config_mixed
    gates["engine_configuration_identity"] = {
        "passed": config_identity_ok,
        "identity_fields": identity_fields,
        "missing_or_blank_fields": missing_identity_fields,
        "configuration_mixed": config_mixed,
    }
    if config_mixed:
        reasons.append("engine configuration is mixed across the prediction artifact (configuration_mixed=True)")
    if missing_identity_fields:
        reasons.append(
            "scorecard configuration identity is incomplete; missing or blank field(s): "
            f"{missing_identity_fields} (a not-applicable dimension must be an explicit sentinel recorded "
            "at prediction-generation time, e.g. 'deterministic-only' or 'not-applicable' - it is never "
            "inferred, and it can never be supplied or overridden at classification time)"
        )

    ground_truth_hash = meta["source_fixture_sha256"]
    scorecard_ground_truth_hash = scorecard.get("ground_truth_source_fixture_sha256")
    prediction_source_hash = scorecard.get("prediction_source_fixture_sha256")
    identity_source_hash = identity_fields.get("source_fixture_sha256")
    provenance_matches = scorecard.get("prediction_source_fixture_matches_ground_truth_source")
    hashes_present = bool(ground_truth_hash) and bool(scorecard_ground_truth_hash) and bool(prediction_source_hash)
    hashes_agree = hashes_present and ground_truth_hash == scorecard_ground_truth_hash
    # V58-QUALITY-04E-R2 tamper check: configuration_identity's own copy of
    # the source-fixture hash must agree with the scorecard's top-level
    # prediction_source_fixture_sha256 field - both are supposed to be the
    # exact same fact recorded two different ways; disagreement means the
    # scorecard was edited inconsistently after scoring.
    identity_hash_consistent = (
        not _nonblank(prediction_source_hash) or identity_source_hash == prediction_source_hash
    )
    provenance_ok = provenance_matches is True and hashes_agree and identity_hash_consistent
    gates["provenance_hashes_match"] = {
        "passed": provenance_ok,
        "fixture_metadata_source_fixture_sha256": ground_truth_hash,
        "scorecard_ground_truth_source_fixture_sha256": scorecard_ground_truth_hash,
        "scorecard_prediction_source_fixture_sha256": prediction_source_hash,
        "scorecard_configuration_identity_source_fixture_sha256": identity_source_hash,
        "scorecard_prediction_source_fixture_matches_ground_truth_source": provenance_matches,
    }
    if not provenance_ok:
        reasons.append(
            "scorecard/fixture provenance does not match (source-fixture hashes disagree, unset, the "
            "prediction source-fixture hash is blank, or the scorecard's configuration_identity source-fixture "
            "hash is inconsistent with its own prediction_source_fixture_sha256 field)"
        )

    case_count = scorecard.get("case_count")
    scored_case_count = scorecard.get("scored_case_count")
    unscored_case_count = scorecard.get("unscored_case_count")
    coverage_internally_consistent = (
        isinstance(case_count, int)
        and isinstance(scored_case_count, int)
        and isinstance(unscored_case_count, int)
        and case_count == scored_case_count + unscored_case_count
    )
    gates["scorecard_case_coverage_internally_consistent"] = {
        "passed": coverage_internally_consistent,
        "case_count": case_count,
        "scored_case_count": scored_case_count,
        "unscored_case_count": unscored_case_count,
    }
    if not coverage_internally_consistent:
        reasons.append("scorecard case coverage is internally inconsistent (case_count != scored + unscored)")

    total_case_count = meta["total_case_count"]
    all_expected_cases_represented = isinstance(case_count, int) and case_count == total_case_count
    gates["all_expected_cases_represented"] = {
        "passed": all_expected_cases_represented,
        "scorecard_case_count": case_count,
        "fixture_metadata_total_case_count": total_case_count,
    }
    if not all_expected_cases_represented:
        reasons.append(
            "not all expected cases are represented: scorecard case_count "
            f"({case_count!r}) does not match the finalized benchmark's total case count ({total_case_count!r})"
        )

    return gates, reasons


# ---------------------------------------------------------------------------
# Category diagnostics (shared helper)
# ---------------------------------------------------------------------------


def _category_diagnostics(
    scorecard: Mapping[str, Any], meta: Mapping[str, Any], *, sample_floor: int
) -> Dict[str, Dict[str, Any]]:
    recall_by_category = ((scorecard.get("metrics") or {}).get("recall_by_defect_category")) or {}
    ground_truth_counts = meta["category_case_counts"]
    all_categories = sorted(set(recall_by_category) | set(ground_truth_counts))
    diagnostics: Dict[str, Dict[str, Any]] = {}
    for category in all_categories:
        bucket = recall_by_category.get(category) or {}
        n = ground_truth_counts.get(category, bucket.get("n", bucket.get("total", 0)))
        diagnostics[category] = {
            "n": n,
            "detected": bucket.get("detected"),
            "recall": bucket.get("recall"),
            "false_negatives": bucket.get("false_negatives"),
            "diagnostic_only": n < sample_floor,
        }
    return diagnostics


# ---------------------------------------------------------------------------
# Pilot-tier classification
# ---------------------------------------------------------------------------


def _classify_pilot(
    scorecard: Mapping[str, Any], meta: Mapping[str, Any]
) -> Tuple[str, Dict[str, Dict[str, Any]], List[str]]:
    gates: Dict[str, Dict[str, Any]] = {}
    reasons: List[str] = []

    total_case_count = meta["total_case_count"]
    eligible = total_case_count == PILOT_REQUIRED_CASE_COUNT
    gates["pilot_required_case_count"] = {
        "passed": eligible,
        "total_case_count": total_case_count,
        "required": PILOT_REQUIRED_CASE_COUNT,
    }
    if not eligible:
        reasons.append(
            f"total_case_count={total_case_count} does not equal the pilot policy v1 required case count "
            f"({PILOT_REQUIRED_CASE_COUNT}); a differently-sized pilot was never evaluated against this "
            "policy version and must not be classified under it - a new pilot size requires a new "
            "POLICY_VERSION"
        )
        return CLASSIFICATION_INVALID_RUN, gates, reasons

    blocking_false_approval_ids = list(scorecard.get("blocking_false_approval_case_ids") or [])
    unscored_blocking_ids = list(scorecard.get("unscored_blocking_case_ids") or [])
    unscored_case_count = int(scorecard.get("unscored_case_count") or 0)
    false_rejection_ids = list(scorecard.get("false_rejection_case_ids") or [])
    warning_recall_detected = scorecard.get("warning_recall_detected")
    warning_recall_total = scorecard.get("warning_recall_total")
    warning_recall = scorecard.get("warning_recall")

    fail_triggers: List[str] = []

    gates["blocking_false_approvals"] = {
        "passed": not blocking_false_approval_ids,
        "case_ids": blocking_false_approval_ids,
    }
    if blocking_false_approval_ids:
        fail_triggers.append(
            f"at least one blocking-defect case was falsely approved: {blocking_false_approval_ids}"
        )

    gates["unscored_blocking_cases"] = {"passed": not unscored_blocking_ids, "case_ids": unscored_blocking_ids}
    if unscored_blocking_ids:
        fail_triggers.append(f"at least one blocking case is unscored: {unscored_blocking_ids}")

    unscored_ceiling_ok = unscored_case_count <= PILOT_MAX_UNSCORED_CASES_FAIL_THRESHOLD
    gates["unscored_case_ceiling"] = {
        "passed": unscored_ceiling_ok,
        "unscored_case_count": unscored_case_count,
        "max_allowed": PILOT_MAX_UNSCORED_CASES_FAIL_THRESHOLD,
    }
    if not unscored_ceiling_ok:
        fail_triggers.append(
            f"more than {PILOT_MAX_UNSCORED_CASES_FAIL_THRESHOLD} cases are unscored ({unscored_case_count})"
        )

    known_good_false_rejection_fail = len(false_rejection_ids) >= PILOT_KNOWN_GOOD_FALSE_REJECTIONS_FAIL_THRESHOLD
    gates["known_good_false_rejections_ceiling"] = {
        "passed": not known_good_false_rejection_fail,
        "count": len(false_rejection_ids),
        "case_ids": false_rejection_ids,
        "fail_threshold": PILOT_KNOWN_GOOD_FALSE_REJECTIONS_FAIL_THRESHOLD,
    }
    if known_good_false_rejection_fail:
        fail_triggers.append(
            f"at least {PILOT_KNOWN_GOOD_FALSE_REJECTIONS_FAIL_THRESHOLD} known-good cases received a false "
            f"rejection: {false_rejection_ids}"
        )

    if fail_triggers:
        reasons.extend(fail_triggers)
        return CLASSIFICATION_FAIL, gates, reasons

    conditional_triggers: List[str] = []

    unscored_nonblocking_count = unscored_case_count - len(unscored_blocking_ids)
    unscored_nonblocking_conditional = 1 <= unscored_nonblocking_count <= 2
    gates["unscored_nonblocking_conditional_band"] = {
        "triggered": unscored_nonblocking_conditional,
        "unscored_nonblocking_count": unscored_nonblocking_count,
    }
    if unscored_nonblocking_conditional:
        conditional_triggers.append(f"{unscored_nonblocking_count} nonblocking case(s) are unscored")

    exactly_one_false_rejection = len(false_rejection_ids) == 1
    gates["known_good_false_rejection_conditional_band"] = {
        "triggered": exactly_one_false_rejection,
        "case_ids": false_rejection_ids,
    }
    if exactly_one_false_rejection:
        conditional_triggers.append(f"exactly 1 known-good case received a false rejection: {false_rejection_ids}")

    warning_recall_below_threshold = (
        warning_recall_total not in (None, 0) and warning_recall is not None
        and warning_recall < PILOT_WARNING_RECALL_CONDITIONAL_THRESHOLD
    )
    gates["warning_level_recall"] = {
        "triggered": bool(warning_recall_below_threshold),
        "numerator": warning_recall_detected,
        "denominator": warning_recall_total,
        "recall": warning_recall,
        "conditional_threshold": PILOT_WARNING_RECALL_CONDITIONAL_THRESHOLD,
    }
    if warning_recall_below_threshold:
        conditional_triggers.append(
            f"warning-level defective-case recall {warning_recall} "
            f"({warning_recall_detected}/{warning_recall_total}) is below "
            f"{PILOT_WARNING_RECALL_CONDITIONAL_THRESHOLD}"
        )

    if conditional_triggers:
        reasons.extend(conditional_triggers)
        return CLASSIFICATION_CONDITIONAL_PASS, gates, reasons

    return CLASSIFICATION_PASS, gates, reasons


def _pilot_language(classification: str) -> str:
    scope_caveat = (
        "This reflects the ~40-case pilot benchmark only. It is NOT a launch-readiness claim and "
        "NOT a production-accuracy claim."
    )
    if classification == CLASSIFICATION_PASS:
        return (
            "PASS (pilot) - development continuation signal only: proceed to building the launch-scale "
            f"benchmark with this engine configuration. {scope_caveat}"
        )
    if classification == CLASSIFICATION_CONDITIONAL_PASS:
        return f"CONDITIONAL PASS (pilot) - fix the named defects and rerun the full pilot. {scope_caveat}"
    if classification == CLASSIFICATION_FAIL:
        return (
            "FAIL (pilot) - reject this engine configuration as tested; root-cause the failing gate(s) "
            f"and rerun the full pilot. {scope_caveat}"
        )
    return (
        "INVALID RUN (pilot) - no pass/fail decision can be drawn; ground-truth, configuration, or "
        f"provenance requirements were not met. Correct the issue and re-run. {scope_caveat}"
    )


_PILOT_LIMITATIONS = (
    "A ~40-case pilot with defect-category buckets of roughly 4-10 cases cannot support a statistical "
    "confidence interval; a 100% observed rate on a small bucket is not a reliable estimate.",
    "Ground truth here reflects a single primary SME reviewer per case (second review is opt-in/escalation "
    "only); the label set itself carries unquantified label-noise risk at pilot scale.",
    "The pilot fixture is hand-curated to deliberately contain defects; it is not a random sample of real "
    "exam content, so observed rates are not generalizable prevalence estimates.",
    "A pilot PASS or CONDITIONAL PASS is a development-iteration signal only and must never be presented "
    "as evidence of production accuracy or launch readiness.",
)


# ---------------------------------------------------------------------------
# Launch-tier classification
# ---------------------------------------------------------------------------


def _classify_launch(
    scorecard: Mapping[str, Any], meta: Mapping[str, Any]
) -> Tuple[str, Dict[str, Dict[str, Any]], List[str]]:
    gates: Dict[str, Dict[str, Any]] = {}
    invalid_reasons: List[str] = []

    total_ok = meta["total_case_count"] >= LAUNCH_MIN_TOTAL_CASE_COUNT
    gates["minimum_total_case_count"] = {
        "passed": total_ok,
        "total_case_count": meta["total_case_count"],
        "minimum_required": LAUNCH_MIN_TOTAL_CASE_COUNT,
    }
    if not total_ok:
        invalid_reasons.append(
            f"fewer than {LAUNCH_MIN_TOTAL_CASE_COUNT} finalized reviewed cases "
            f"({meta['total_case_count']})"
        )

    known_good_ok = meta["known_good_case_count"] >= LAUNCH_MIN_KNOWN_GOOD_CASE_COUNT
    gates["minimum_known_good_case_count"] = {
        "passed": known_good_ok,
        "known_good_case_count": meta["known_good_case_count"],
        "minimum_required": LAUNCH_MIN_KNOWN_GOOD_CASE_COUNT,
    }
    if not known_good_ok:
        invalid_reasons.append(
            f"fewer than {LAUNCH_MIN_KNOWN_GOOD_CASE_COUNT} known-good cases ({meta['known_good_case_count']})"
        )

    blocking_ok = meta["blocking_case_count"] >= LAUNCH_MIN_BLOCKING_CASE_COUNT
    gates["minimum_blocking_case_count"] = {
        "passed": blocking_ok,
        "blocking_case_count": meta["blocking_case_count"],
        "minimum_required": LAUNCH_MIN_BLOCKING_CASE_COUNT,
    }
    if not blocking_ok:
        invalid_reasons.append(
            f"fewer than {LAUNCH_MIN_BLOCKING_CASE_COUNT} blocking-defect cases ({meta['blocking_case_count']})"
        )

    review_process = meta["review_process"]
    if review_process is None:
        gates["review_process_attestation_present"] = {"passed": False}
        invalid_reasons.append(
            "launch review-process attestation is absent; double-review coverage cannot be inferred from "
            "case count alone"
        )
    else:
        blocking_total = review_process["blocking_cases_total_count"]
        blocking_reviewed = review_process["blocking_cases_double_reviewed_count"]
        blocking_double_review_ok = blocking_total > 0 and blocking_reviewed >= blocking_total
        gates["blocking_cases_fully_double_reviewed"] = {
            "passed": blocking_double_review_ok,
            "blocking_cases_double_reviewed_count": blocking_reviewed,
            "blocking_cases_total_count": blocking_total,
        }
        if not blocking_double_review_ok:
            invalid_reasons.append(
                "100% of blocking cases were not attested as independently double-reviewed "
                f"({blocking_reviewed}/{blocking_total})"
            )

        non_blocking_total = review_process["non_blocking_cases_total_count"]
        non_blocking_reviewed = review_process["non_blocking_cases_double_reviewed_count"]
        non_blocking_fraction = _rate(non_blocking_reviewed, non_blocking_total)
        non_blocking_double_review_ok = (
            non_blocking_fraction is not None
            and non_blocking_fraction >= LAUNCH_MIN_NON_BLOCKING_DOUBLE_REVIEW_FRACTION
        )
        gates["non_blocking_cases_double_review_coverage"] = {
            "passed": non_blocking_double_review_ok,
            "non_blocking_cases_double_reviewed_count": non_blocking_reviewed,
            "non_blocking_cases_total_count": non_blocking_total,
            "fraction": non_blocking_fraction,
            "minimum_required_fraction": LAUNCH_MIN_NON_BLOCKING_DOUBLE_REVIEW_FRACTION,
        }
        if not non_blocking_double_review_ok:
            invalid_reasons.append(
                f"fewer than {LAUNCH_MIN_NON_BLOCKING_DOUBLE_REVIEW_FRACTION:.0%} of remaining cases were "
                f"attested as independently double-reviewed ({non_blocking_reviewed}/{non_blocking_total})"
            )

        disagreements_ok = bool(review_process["disagreements_adjudicated_or_excluded"])
        gates["review_disagreements_adjudicated_or_excluded"] = {"passed": disagreements_ok}
        if not disagreements_ok:
            invalid_reasons.append(
                "material reviewer disagreements were not attested as adjudicated or excluded"
            )

    unscored_case_count = int(scorecard.get("unscored_case_count") or 0)
    unscored_ok = unscored_case_count == 0
    gates["zero_unscored_cases"] = {"passed": unscored_ok, "unscored_case_count": unscored_case_count}
    if not unscored_ok:
        invalid_reasons.append(
            f"{unscored_case_count} case(s) are unscored/execution-errored; launch classification requires "
            "zero unscored cases"
        )

    if invalid_reasons:
        return CLASSIFICATION_INVALID_RUN, gates, invalid_reasons

    fail_reasons: List[str] = []

    blocking_false_approval_ids = list(scorecard.get("blocking_false_approval_case_ids") or [])
    gates["blocking_false_approvals"] = {
        "passed": not blocking_false_approval_ids,
        "case_ids": blocking_false_approval_ids,
    }
    if blocking_false_approval_ids:
        fail_reasons.append(f"at least one blocking-defect case was falsely approved: {blocking_false_approval_ids}")

    known_good_cases = ((scorecard.get("metrics") or {}).get("known_good_cases")) or 0
    false_rejections = ((scorecard.get("metrics") or {}).get("false_rejections")) or 0
    false_rejection_rate = _rate(false_rejections, known_good_cases)
    fr_fail = false_rejection_rate is not None and false_rejection_rate > LAUNCH_KNOWN_GOOD_FALSE_REJECTION_RATE_FAIL_THRESHOLD
    gates["known_good_false_rejection_rate"] = {
        "passed": not fr_fail,
        "rate": false_rejection_rate,
        "false_rejections": false_rejections,
        "known_good_cases": known_good_cases,
        "fail_threshold": LAUNCH_KNOWN_GOOD_FALSE_REJECTION_RATE_FAIL_THRESHOLD,
    }
    if fr_fail:
        fail_reasons.append(
            f"known-good false-rejection rate {false_rejection_rate} exceeds "
            f"{LAUNCH_KNOWN_GOOD_FALSE_REJECTION_RATE_FAIL_THRESHOLD}"
        )

    overall_recall = (scorecard.get("metrics") or {}).get("overall_recall")
    recall_fail = overall_recall is not None and overall_recall < LAUNCH_DEFECTIVE_RECALL_FAIL_THRESHOLD
    gates["overall_defective_case_recall"] = {
        "passed": not recall_fail,
        "overall_recall": overall_recall,
        "fail_threshold": LAUNCH_DEFECTIVE_RECALL_FAIL_THRESHOLD,
    }
    if recall_fail:
        fail_reasons.append(
            f"overall defective-case recall {overall_recall} is below {LAUNCH_DEFECTIVE_RECALL_FAIL_THRESHOLD}"
        )

    if fail_reasons:
        return CLASSIFICATION_FAIL, gates, fail_reasons

    conditional_reasons: List[str] = []

    fr_conditional = (
        false_rejection_rate is not None
        and LAUNCH_KNOWN_GOOD_FALSE_REJECTION_RATE_CONDITIONAL_THRESHOLD < false_rejection_rate
        <= LAUNCH_KNOWN_GOOD_FALSE_REJECTION_RATE_FAIL_THRESHOLD
    )
    gates["known_good_false_rejection_rate"]["conditional_triggered"] = fr_conditional
    if fr_conditional:
        conditional_reasons.append(
            f"known-good false-rejection rate {false_rejection_rate} is above "
            f"{LAUNCH_KNOWN_GOOD_FALSE_REJECTION_RATE_CONDITIONAL_THRESHOLD} and at most "
            f"{LAUNCH_KNOWN_GOOD_FALSE_REJECTION_RATE_FAIL_THRESHOLD}"
        )

    recall_conditional = (
        overall_recall is not None
        and LAUNCH_DEFECTIVE_RECALL_FAIL_THRESHOLD <= overall_recall < LAUNCH_DEFECTIVE_RECALL_CONDITIONAL_THRESHOLD
    )
    gates["overall_defective_case_recall"]["conditional_triggered"] = recall_conditional
    if recall_conditional:
        conditional_reasons.append(
            f"overall defective-case recall {overall_recall} is at least {LAUNCH_DEFECTIVE_RECALL_FAIL_THRESHOLD} "
            f"but below {LAUNCH_DEFECTIVE_RECALL_CONDITIONAL_THRESHOLD}"
        )

    category_diagnostics = _category_diagnostics(
        scorecard, meta, sample_floor=LAUNCH_MIN_CATEGORY_SAMPLE_FLOOR
    )
    low_recall_eligible_categories = []
    for category, info in category_diagnostics.items():
        if info["diagnostic_only"]:
            continue
        recall = info["recall"]
        if recall is not None and recall < LAUNCH_CATEGORY_RECALL_CONDITIONAL_THRESHOLD:
            low_recall_eligible_categories.append({"category": category, "recall": recall, "n": info["n"]})
    gates["eligible_category_recall"] = {
        "triggered": bool(low_recall_eligible_categories),
        "categories": low_recall_eligible_categories,
        "conditional_threshold": LAUNCH_CATEGORY_RECALL_CONDITIONAL_THRESHOLD,
        "sample_floor": LAUNCH_MIN_CATEGORY_SAMPLE_FLOOR,
    }
    if low_recall_eligible_categories:
        conditional_reasons.append(
            f"eligible categor(ies) with n >= {LAUNCH_MIN_CATEGORY_SAMPLE_FLOOR} below "
            f"{LAUNCH_CATEGORY_RECALL_CONDITIONAL_THRESHOLD} recall: {low_recall_eligible_categories}"
        )

    safety_categories = meta["safety_relevant_categories"]
    undercovered_safety_categories = [
        {"category": category, "n": meta["category_case_counts"].get(category, 0)}
        for category in safety_categories
        if meta["category_case_counts"].get(category, 0) < LAUNCH_MIN_CATEGORY_SAMPLE_FLOOR
    ]
    gates["safety_relevant_category_coverage"] = {
        "triggered": bool(undercovered_safety_categories),
        "undercovered_categories": undercovered_safety_categories,
        "sample_floor": LAUNCH_MIN_CATEGORY_SAMPLE_FLOOR,
    }
    if undercovered_safety_categories:
        conditional_reasons.append(
            f"safety-relevant categor(ies) lack required n >= {LAUNCH_MIN_CATEGORY_SAMPLE_FLOOR} coverage: "
            f"{undercovered_safety_categories}"
        )

    precision_num = scorecard.get("overall_precision_numerator")
    precision_den = scorecard.get("overall_precision_denominator")
    overall_precision = (scorecard.get("metrics") or {}).get("finding_precision")
    precision_conditional = overall_precision is not None and overall_precision < LAUNCH_PRECISION_CONDITIONAL_THRESHOLD
    gates["overall_finding_precision"] = {
        "triggered": bool(precision_conditional),
        "precision": overall_precision,
        "numerator": precision_num,
        "denominator": precision_den,
        "conditional_threshold": LAUNCH_PRECISION_CONDITIONAL_THRESHOLD,
    }
    if precision_conditional:
        conditional_reasons.append(
            f"overall finding precision {overall_precision} ({precision_num}/{precision_den}) is below "
            f"{LAUNCH_PRECISION_CONDITIONAL_THRESHOLD} (diagnostic - does not FAIL the run on its own)"
        )

    if conditional_reasons:
        return CLASSIFICATION_CONDITIONAL_PASS, gates, conditional_reasons

    return CLASSIFICATION_PASS, gates, []


def _launch_language(classification: str) -> str:
    if classification == CLASSIFICATION_PASS:
        return LAUNCH_PASS_LANGUAGE
    if classification == CLASSIFICATION_CONDITIONAL_PASS:
        return (
            "CONDITIONAL PASS on CertBound's launch benchmark for the exact engine, model, prompt, ruleset, "
            "evidence configuration, and version tested; named follow-up items must be remediated and tracked "
            "before this configuration may be described as having passed the launch benchmark."
        )
    if classification == CLASSIFICATION_FAIL:
        return (
            "FAILED CertBound's launch benchmark for the exact engine, model, prompt, ruleset, evidence "
            "configuration, and version tested; this configuration is rejected for launch until the failing "
            "gate(s) are corrected and the full launch benchmark is re-run."
        )
    return (
        "INVALID RUN against CertBound's launch benchmark; ground-truth, review-process, composition, or "
        "provenance requirements were not met, so no pass/fail decision can be drawn. Correct the underlying "
        "issue and re-run in full."
    )


_LAUNCH_LIMITATIONS = (
    "A launch PASS is scoped exclusively to the exact engine, model, prompt, ruleset, evidence configuration, "
    "and version tested; any change to any of those invalidates the PASS and requires a fresh full launch run.",
    "Categories below the per-category sample floor remain diagnostic-only even at launch scale and can never "
    "independently gate PASS/FAIL.",
    "A launch PASS is not a claim of production accuracy, universal accuracy, an assurance of safety, a "
    "guarantee, or statistical conclusiveness beyond the exact tested configuration.",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_scorecard(
    scorecard: Mapping[str, Any],
    benchmark_tier: str,
    fixture_metadata: Mapping[str, Any],
) -> Dict[str, Any]:
    """Deterministically classify a benchmark scorecard under this policy.

    Parameters
    ----------
    scorecard:
        The dict returned by ``workers.quality_benchmark_execution.score_predictions``
        (or an equivalently-shaped structure, e.g. reloaded from a written
        scorecard JSON file).
    benchmark_tier:
        ``"pilot"`` or ``"launch"``.
    fixture_metadata:
        Ground-truth identity, composition, and (for launch) review-process
        attestation describing the finalized benchmark fixture the
        scorecard was scored against. See module docstring - never inferred
        from case counts alone; callers must supply ``review_process``
        explicitly for launch-tier classification.
        ``additional_safety_relevant_categories`` may only add to the fixed
        ``REQUIRED_LAUNCH_SAFETY_CATEGORIES`` baseline, never replace it.
        Deliberately does NOT accept any engine-configuration identity
        field (V58-QUALITY-04E-R2) - identity is read exclusively from
        ``scorecard["configuration_identity"]``, which must already contain
        ``provider_id``/``model_id``/``prompt_version``/``ruleset_version``/
        ``evidence_config_id`` (each an explicit non-blank string or an
        explicit not-applicable sentinel recorded at prediction-generation
        time).

    Returns a JSON-serializable dict with ``policy_version``,
    ``benchmark_tier``, ``classification``, ``gate_results``, ``reasons``,
    ``limitations``, ``sample_counts``, ``configuration_identity``, and
    ``classification_language``.

    Raises ``PolicyInputError`` for structurally malformed input (never for
    a legitimate domain outcome, which is always returned as a
    classification, not an exception).
    """
    if not isinstance(scorecard, Mapping):
        raise PolicyInputError("scorecard must be a mapping")
    if benchmark_tier not in SUPPORTED_TIERS:
        raise PolicyInputError(
            f"benchmark_tier must be one of {sorted(SUPPORTED_TIERS)}, got {benchmark_tier!r}"
        )

    meta = _normalize_fixture_metadata(fixture_metadata)

    shared_gates, shared_invalid_reasons = _evaluate_shared_gates(scorecard, meta)

    if shared_invalid_reasons:
        classification = CLASSIFICATION_INVALID_RUN
        gate_results = shared_gates
        reasons = shared_invalid_reasons
    elif benchmark_tier == TIER_PILOT:
        classification, tier_gates, reasons = _classify_pilot(scorecard, meta)
        gate_results = {**shared_gates, **tier_gates}
    else:
        classification, tier_gates, reasons = _classify_launch(scorecard, meta)
        gate_results = {**shared_gates, **tier_gates}

    if benchmark_tier == TIER_PILOT:
        classification_language = _pilot_language(classification)
        limitations = list(_PILOT_LIMITATIONS)
    else:
        classification_language = _launch_language(classification)
        limitations = list(_LAUNCH_LIMITATIONS)

    _assert_no_prohibited_language(classification_language, *reasons, *limitations)

    # The category sample-eligibility floor (LAUNCH_MIN_CATEGORY_SAMPLE_FLOOR)
    # is the only defined "eligible category" threshold in this policy —
    # pilot never individually gates on category-level metrics (see
    # _classify_pilot), so reusing the same floor here simply means every
    # pilot-scale category (n=4-10 in the current fixture) is correctly
    # reported as diagnostic-only rather than inventing a second, unused
    # pilot-specific floor constant.
    category_diagnostics = _category_diagnostics(
        scorecard, meta, sample_floor=LAUNCH_MIN_CATEGORY_SAMPLE_FLOOR
    )

    sample_counts = {
        "total_case_count": meta["total_case_count"],
        "known_good_case_count": meta["known_good_case_count"],
        "defective_case_count": meta["defective_case_count"],
        "blocking_case_count": meta["blocking_case_count"],
        "warning_case_count": meta["warning_case_count"],
        "scored_case_count": scorecard.get("scored_case_count"),
        "unscored_case_count": scorecard.get("unscored_case_count"),
        "category_case_counts": dict(meta["category_case_counts"]),
        "category_diagnostics": category_diagnostics,
    }

    scorecard_identity = scorecard.get("configuration_identity")
    if not isinstance(scorecard_identity, Mapping):
        scorecard_identity = {}
    configuration_identity = {
        "engine_id": scorecard_identity.get("engine_id", scorecard.get("engine_id")),
        "engine_version": scorecard_identity.get("engine_version", scorecard.get("engine_version")),
        "provider_id": scorecard_identity.get("provider_id"),
        "model_id": scorecard_identity.get("model_id"),
        "prompt_version": scorecard_identity.get("prompt_version"),
        "ruleset_version": scorecard_identity.get("ruleset_version"),
        "evidence_config_id": scorecard_identity.get("evidence_config_id"),
        "source_fixture_sha256": scorecard_identity.get("source_fixture_sha256"),
        "sme_reviewer_id": scorecard.get("sme_reviewer_id"),
        "ground_truth_source_fixture_sha256": scorecard.get("ground_truth_source_fixture_sha256"),
        "prediction_source_fixture_sha256": scorecard.get("prediction_source_fixture_sha256"),
    }

    return {
        "policy_version": POLICY_VERSION,
        "benchmark_tier": benchmark_tier,
        "classification": classification,
        "gate_results": gate_results,
        "reasons": reasons,
        "limitations": limitations,
        "sample_counts": sample_counts,
        "configuration_identity": configuration_identity,
        "classification_language": classification_language,
    }
