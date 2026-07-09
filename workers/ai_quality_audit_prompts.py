"""
Deterministic prompt builders for CertBound V48 AI quality audit passes.

Each builder returns ``(system_prompt, user_prompt)`` with stable formatting
for identical inputs.  Companion JSON Schema dicts are exported for providers
that support structured output.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from workers.ai_quality_audit_schemas import (
    ALLOWED_RESOLUTION_STATUS,
    ALLOWED_RESOLUTION_TYPES,
    ANSWER_COMPLETENESS_VALUES,
    ANSWER_CORRECTNESS_VERDICTS,
    SUPPORTED_FINDING_CODES,
)
from workers.finding_policy import ALLOWED_MATERIALITY
from workers.llm_audit import ALLOWED_FINDING_TYPES, ALLOWED_SEVERITIES

_PASS_A_SYSTEM = (
    "You are a Salesforce certification exam candidate taking a blind practice "
    "question. Select the option label(s) you believe are correct. Respond "
    "only with JSON matching the required schema."
)

_PASS_B_SYSTEM = (
    "You are an independent technical reviewer for Salesforce certification "
    "question quality. Compare the candidate's blind answer, the stored "
    "answer key, the explanation, and frozen official resource evidence. "
    "Propose findings only when warranted; preserve technically valid "
    "questions. Respond only with JSON matching the required schema."
)

_PASS_C_SYSTEM = (
    "You are an independent dispute reviewer for a Salesforce certification "
    "question quality audit. Resolve the triggered dispute or substitution "
    "exactly as specified. Respond only with JSON matching the required schema."
)

_PASS_B_CORRECTNESS_SYSTEM = (
    "You are an independent answer-correctness verifier for a Salesforce "
    "certification question audit. Judge every option strictly and "
    "independently against only the frozen official evidence provided. "
    "You do not select a finding code, materiality, severity, or approval "
    "state -- only report per-option evidence verdicts and an overall "
    "evidence-sufficiency judgment. Respond only with JSON matching the "
    "required schema."
)

_WARNING_ONLY_CODES = frozenset({"SOURCE_SUPPORT_WEAK", "DOMAIN_MISALIGNMENT"})

_FINDING_CODE_DEFINITIONS: Dict[str, str] = {
    "WRONG_ANSWER_KEY": (
        "The stored answer key conflicts with the best-supported answer."
    ),
    "UNSUPPORTED_ANSWER": (
        "The stored answer may be plausible, but the provided evidence does not "
        "sufficiently support it."
    ),
    "MULTIPLE_DEFENSIBLE_ANSWERS": (
        "More than the required number of options are reasonably defensible from "
        "the question and evidence."
    ),
    "AMBIGUOUS_QUESTION": (
        "The stem is unclear, underspecified, contradictory, or admits materially "
        "different interpretations."
    ),
    "EXPLANATION_MISSING": (
        "No meaningful explanation is provided."
    ),
    "EXPLANATION_INCOMPLETE": (
        "An explanation exists but does not adequately justify the answer or "
        "reject key distractors."
    ),
    "WEAK_DISTRACTORS": (
        "One or more distractors are obviously implausible, irrelevant, malformed, "
        "or noncompetitive."
    ),
    "SOURCE_SUPPORT_WEAK": (
        "The available evidence is too weak, indirect, incomplete, or outdated to "
        "substantiate the answer."
    ),
    "DOMAIN_MISALIGNMENT": (
        "The question or answer content falls outside the stated certification "
        "domain or exam scope."
    ),
    "LOW_COGNITIVE_LEVEL": (
        "The question tests only recall or recognition without requiring "
        "comprehension, application, or analysis appropriate to the exam level."
    ),
    "DIFFICULTY_MISMATCH": (
        "The question's difficulty is substantially misaligned with the stated or "
        "expected exam difficulty tier."
    ),
    "OUTDATED_CONTENT": (
        "The question relies on content, terminology, or platform behavior that is "
        "no longer current or accurate."
    ),
    "OTHER_REVIEW_NEEDED": (
        "A substantive quality concern exists that does not fit a more specific "
        "finding code."
    ),
    "EMPTY_QUESTION_TEXT": (
        "The question stem is blank, placeholder-only, or lacks substantive content."
    ),
    "INVALID_SELECT_COUNT": (
        "The number of correct options does not match the required selection count "
        "for the question type."
    ),
    "TOO_FEW_OPTIONS": (
        "The question provides fewer answer options than the minimum required for "
        "a valid item of this type."
    ),
    "EMPTY_OPTION_TEXT": (
        "One or more answer options are blank, placeholder-only, or lack substantive "
        "content."
    ),
    "DUPLICATE_OPTION_LABELS": (
        "Two or more options share the same option label identifier."
    ),
    "DUPLICATE_OPTION_TEXT": (
        "Two or more options contain identical or near-identical answer text."
    ),
    "DUPLICATE_CORRECT_OPTIONS": (
        "More than one option is marked correct when only one distinct correct "
        "answer should exist."
    ),
    "CORRECT_COUNT_MISMATCH": (
        "The number of options marked correct does not match the required selection "
        "count stated by the question type."
    ),
    "SINGLE_SELECT_COUNT_MISMATCH": (
        "A single-select question has more than one option marked correct."
    ),
    "STEM_COUNT_MISMATCH": (
        "The question stem's stated selection count conflicts with the question "
        "type or the number of correct options."
    ),
    "OPTION_DISPLAY_ORDER_ISSUES": (
        "Answer options are ordered in a way that reveals the correct answer or "
        "undermines item validity."
    ),
    "DUPLICATE_QUESTION_STEM_EXACT": (
        "The question stem is an exact duplicate of another item in the corpus."
    ),
    "DUPLICATE_QUESTION_STEM_NEAR_EXACT": (
        "The question stem is a near-exact duplicate of another item, differing "
        "only in trivial wording."
    ),
    "SEMANTIC_CONCEPT_CLUSTER_OVERSIZE": (
        "The question bundles too many distinct concepts for a single item of this "
        "scope and difficulty."
    ),
}

_SUBSTITUTION_BY_REASON = {
    "PASS_A_SCHEMA_INVALID": "PASS_A_SUBSTITUTION",
    "PASS_B_SCHEMA_INVALID": "PASS_B_SUBSTITUTION",
}

_SUBSTITUTED_FOR_BY_RESOLUTION = {
    "NORMAL_DISPUTE": [],
    "PASS_A_SUBSTITUTION": ["A", "B"],
    "PASS_B_SUBSTITUTION": ["B"],
}


def build_pass_a_prompt(blind_context: dict) -> Tuple[str, str]:
    """Build blind Pass A prompts with no answer/evidence/explanation leakage."""
    required_count = int(blind_context["required_selection_count"])
    options = _sorted_options(blind_context.get("options") or [])

    lines = [
        "Task: blind answer selection (Pass A).",
        "You have NOT seen the official answer key, explanation, or resource evidence.",
        f"Certification: {blind_context['certification_exam_name']}",
        f"Domain: {blind_context['domain_name']}",
        f"Question type: {blind_context['question_type']}",
        f"Required selection count: {required_count}",
        "",
        "Question:",
        blind_context["question_text"],
        "",
        "Options:",
    ]
    for option in options:
        lines.append(
            f"- [{option['option_label']}] {option['option_text']}"
        )
    lines.extend(
        [
            "",
            f"Select exactly {required_count} distinct option label(s).",
            'Return JSON: {"selected_option_labels": ["<label>", ...]}',
        ]
    )
    return _PASS_A_SYSTEM, "\n".join(lines)


def build_pass_b_prompt(
    comparison_context: dict,
    *,
    retry_schema_errors: Sequence[str] | None = None,
    exclude_finding_codes: frozenset | None = None,
) -> Tuple[str, str]:
    """Build Pass B prompts with frozen evidence in retrieval rank order.

    ``exclude_finding_codes`` (V60) narrows the general-quality judge's scope
    when a specialized detector exclusively owns those codes for this run
    (see ``build_pass_b_correctness_prompt``). Defaults to the full,
    unrestricted code set for backward compatibility.
    """
    excluded = frozenset(exclude_finding_codes or ())
    required_count = int(comparison_context["required_selection_count"])
    options = _sorted_options(comparison_context.get("options") or [])
    pass_a_labels = list(comparison_context.get("pass_a_selected_option_labels") or [])
    stored_correct = list(comparison_context.get("stored_correct_option_labels") or [])
    frozen_evidence = list(comparison_context.get("frozen_evidence") or [])
    frozen_evidence.sort(key=lambda item: item["rank"])

    codes_block = _format_finding_codes_block(exclude=excluded)

    lines = [
        "Task: evidence-backed quality review (Pass B).",
        f"Certification: {comparison_context['certification_exam_name']}",
        f"Domain: {comparison_context['domain_name']}",
        f"Question type: {comparison_context['question_type']}",
        f"Required selection count: {required_count}",
        "",
        "Question:",
        comparison_context["question_text"],
        "",
        "Options:",
    ]
    for option in options:
        correct_flag = "yes" if option.get("is_correct") else "no"
        lines.append(
            f"- [{option['option_label']}] {option['option_text']} "
            f"(stored_correct={correct_flag})"
        )

    lines.extend(
        [
            "",
            f"Pass A selected labels: {json.dumps(pass_a_labels, separators=(',', ':'))}",
            f"Stored correct labels: {json.dumps(stored_correct, separators=(',', ':'))}",
            "",
            "Explanation:",
            comparison_context.get("explanation") or "",
            "",
            "Frozen evidence (retrieval rank order; cite only these chunk_id values):",
        ]
    )
    if frozen_evidence:
        for item in frozen_evidence:
            header = (
                f"[rank={item['rank']} chunk_id={item['chunk_id']}]"
            )
            if item.get("title"):
                header += f" title={item['title']!r}"
            if item.get("source_label"):
                header += f" source={item['source_label']!r}"
            lines.append(header)
            lines.append(item["chunk_text"])
            lines.append("")
    else:
        lines.append("(no frozen evidence chunks for this run)")

    zero_evidence_example = json.dumps(
        {
            "finding_ref": "F2",
            "finding_code": "SOURCE_SUPPORT_WEAK",
            "finding_type": "source_support",
            "severity": "medium",
            "materiality": "warning",
            "title": "Weak source support",
            "description": "Official evidence does not substantiate the explanation.",
            "evidence_chunk_ids": [],
            "metadata": {
                "source_support_context": {
                    "attempted_retrieval": len(frozen_evidence),
                    "evidence_limitation": (
                        "Frozen evidence did not substantiate the stored explanation."
                    ),
                    "proposed_technical_claim": (
                        "The stored explanation overstates what the official sources support."
                    ),
                    "insufficiency_reason": (
                        "No frozen chunk directly supports the explanation claim."
                    ),
                }
            },
        },
        indent=2,
        sort_keys=True,
    )

    if excluded:
        selection_procedure = [
            "Finding code selection procedure:",
            "1. Choose the single most specific finding code that fully explains the defect.",
            "2. Do not add multiple codes when one code fully explains the defect.",
            "3. Use materiality=blocking only when the defect can invalidate correctness "
            "or make the question unsafe to publish.",
            "4. Use materiality=warning for quality defects that do not invalidate the "
            "stored answer.",
        ]
    else:
        selection_procedure = [
            "Finding code selection procedure:",
            "1. Solve the question independently from the stem, options, and frozen evidence.",
            "2. Compare your independent answer with the stored answer key.",
            "3. Choose the single most specific finding code that fully explains the defect.",
            "4. Do not emit WRONG_ANSWER_KEY merely because evidence is weak; use "
            "UNSUPPORTED_ANSWER when the stored answer is plausible but insufficiently "
            "supported.",
            "5. Do not emit MULTIPLE_DEFENSIBLE_ANSWERS unless multiple options "
            "genuinely satisfy the stem and exceed the required selection count.",
            "6. Do not add multiple codes when one code fully explains the defect.",
            "7. Use materiality=blocking only when the defect can invalidate correctness "
            "or make the question unsafe to publish.",
            "8. Use materiality=warning for quality defects that do not invalidate the "
            "stored answer.",
        ]

    lines.extend(
        [
            "",
            "Supported finding codes:",
            codes_block,
            "",
            *selection_procedure,
            "",
            "Materiality assignment:",
            "- blocking: correctness is wrong, the stem cannot be answered reliably, "
            "or the item is unsafe to publish as-is.",
            "- warning: quality concerns (distractors, explanation depth, source support, "
            "cognitive level) that do not invalidate the stored answer.",
            "- informational: minor polish issues with no material impact on learning.",
            "- SOURCE_SUPPORT_WEAK and DOMAIN_MISALIGNMENT are warning-only; never "
            "assign materiality=blocking to them.",
        ]
    )

    if excluded:
        lines.extend(
            [
                "",
                "Scope restriction for this run:",
                f"- Do NOT propose these finding codes: {', '.join(sorted(excluded))}.",
                "- A separate specialized detector exclusively owns answer-correctness "
                "verification for this run and its output is authoritative for those "
                "codes; anything you propose in that scope anyway will be discarded, "
                "not merged. Focus on explanation quality, distractor quality, "
                "cognitive level, source support, and other non-correctness concerns.",
            ]
        )

    lines.extend(
        [
            "",
            "Rules:",
            "- Use only supported finding_code values.",
            "- Do not invent unsupported blocking findings.",
            "- evidence_chunk_ids must reference frozen chunk_id values only.",
            "- Use unique finding_ref values (for example F1, F2).",
            "- metadata must be a JSON object; metadata: {} is invalid when "
            "SOURCE_SUPPORT_WEAK uses zero evidence_chunk_ids.",
            "",
            "Zero-evidence SOURCE_SUPPORT_WEAK contract (required when "
            "evidence_chunk_ids is []):",
            "- metadata.source_support_context must be a JSON object with:",
            '  "attempted_retrieval": <nonnegative integer>,',
            '  "evidence_limitation": "<non-empty string>",',
            '  "proposed_technical_claim": "<non-empty string>",',
            '  "insufficiency_reason": "<non-empty string>"',
            "- SOURCE_SUPPORT_WEAK cannot use materiality=blocking.",
            "- DOMAIN_MISALIGNMENT remains warning-only.",
            "",
            "Minimal valid zero-evidence SOURCE_SUPPORT_WEAK example:",
            zero_evidence_example,
        ]
    )

    if retry_schema_errors:
        lines.extend(
            [
                "",
                "Prior Pass B response failed deterministic schema validation:",
                *[f"- {error}" for error in retry_schema_errors],
                "Correct only the invalid JSON shape from the prior attempt.",
                "Preserve the same question context, selected_option_labels intent, "
                "and finding intent where valid.",
                "Do not omit required metadata.source_support_context for zero-evidence "
                "SOURCE_SUPPORT_WEAK findings.",
            ]
        )

    lines.extend(
        [
            "",
            f"Select exactly {required_count} option label(s) for your own answer.",
            "Return JSON with selected_option_labels and proposed_findings "
            "(proposed_findings may be an empty array).",
        ]
    )
    return _PASS_B_SYSTEM, "\n".join(lines)


def build_pass_b_correctness_prompt(
    comparison_context: dict,
    *,
    retry_schema_errors: Sequence[str] | None = None,
) -> Tuple[str, str]:
    """Build the specialized answer-correctness detector prompt (V60).

    Judges every option independently against only the frozen evidence.
    Deliberately excludes Pass A's independently-selected labels -- anchoring
    to another model's answer would undermine this detector's independence
    and increase correlated errors. Never asks the model to choose a finding
    code, materiality, severity, or approval state; those are derived
    deterministically in ``workers.ai_quality_audit_schemas.
    derive_correctness_finding``.
    """
    required_count = int(comparison_context["required_selection_count"])
    options = _sorted_options(comparison_context.get("options") or [])
    stored_correct = list(comparison_context.get("stored_correct_option_labels") or [])
    frozen_evidence = list(comparison_context.get("frozen_evidence") or [])
    frozen_evidence.sort(key=lambda item: item["rank"])

    lines = [
        "Task: independent answer-correctness verification (Pass B specialist).",
        f"Certification: {comparison_context['certification_exam_name']}",
        f"Domain: {comparison_context['domain_name']}",
        f"Question type: {comparison_context['question_type']}",
        f"Required selection count: {required_count}",
        "",
        "Question:",
        comparison_context["question_text"],
        "",
        "Options:",
    ]
    for option in options:
        lines.append(f"- [{option['option_label']}] {option['option_text']}")

    lines.extend(
        [
            "",
            f"Stored correct labels: {json.dumps(stored_correct, separators=(',', ':'))}",
            "",
            "Frozen evidence (retrieval rank order; cite only these chunk_id values):",
        ]
    )
    if frozen_evidence:
        for item in frozen_evidence:
            header = f"[rank={item['rank']} chunk_id={item['chunk_id']}]"
            if item.get("title"):
                header += f" title={item['title']!r}"
            if item.get("source_label"):
                header += f" source={item['source_label']!r}"
            lines.append(header)
            lines.append(item["chunk_text"])
            lines.append("")
    else:
        lines.append("(no frozen evidence chunks for this run)")

    lines.extend(
        [
            "",
            "Your task:",
            "1. Judge EVERY option independently and only against the frozen evidence "
            "above.",
            "2. Do not use general Salesforce knowledge, training data, or "
            "assumptions beyond what the frozen evidence states. If the evidence "
            "does not address an option, do not guess.",
            "3. Do not consider or infer any other assessment, blind answer attempt, "
            "or review of this question; judge strictly from the artifacts provided "
            "here.",
            "4. For each option, choose exactly one verdict:",
            "   - SUPPORTED_AS_CORRECT: the frozen evidence affirmatively "
            "establishes this option is correct.",
            "   - NOT_SUPPORTED_AS_CORRECT: the frozen evidence is sufficient to "
            "judge this option, and it affirmatively establishes the option is NOT "
            "correct.",
            "   - INSUFFICIENT_EVIDENCE: the frozen evidence does not say enough to "
            "judge this option either way. Use this instead of guessing.",
            "5. Every SUPPORTED_AS_CORRECT verdict must cite at least one frozen "
            "chunk_id that directly supports it.",
            "6. Set evidence_sufficient_for_decision=true only if every option "
            "received a decisive verdict (SUPPORTED_AS_CORRECT or "
            "NOT_SUPPORTED_AS_CORRECT) and the evidence is adequate to distinguish "
            "the correct option(s) from the rest. If any option is "
            "INSUFFICIENT_EVIDENCE, this must be false.",
            "7. When evidence_sufficient_for_decision is false, provide a concise, "
            "specific abstention_reason explaining what the evidence fails to "
            "establish. When it is true, leave abstention_reason null.",
            "8. You are not selecting a finding code, materiality, severity, or "
            "approval state. Only report per-option verdicts and the overall "
            "sufficiency judgment; any finding is derived deterministically from "
            "your verdicts.",
            "9. For every option marked SUPPORTED_AS_CORRECT, also classify "
            "answer_completeness:",
            "   - FULLY_RESPONSIVE: complete enough, by itself, to satisfy the "
            "exact stem and required selection count.",
            "   - PARTIAL_COMPONENT: factually true and evidence-supported, but "
            "incomplete relative to a fully responsive answer.",
            "   For every option not marked SUPPORTED_AS_CORRECT, use "
            "NOT_APPLICABLE. Do not infer completeness merely from words such "
            "as both, either, all, or neither -- use only the frozen evidence "
            "and the exact stem. Do not alter the verdict meanings above.",
            "",
            "Rules:",
            "- Provide exactly one judgment per listed option label, no more, no "
            "fewer.",
            "- citation_chunk_ids must reference frozen chunk_id values only.",
            "- evidence_rationale must be concise (1-2 sentences) and specific to "
            "the cited evidence, not a restatement of the option text.",
        ]
    )

    if not frozen_evidence:
        lines.extend(
            [
                "- This run has zero frozen evidence chunks: every option must be "
                "judged INSUFFICIENT_EVIDENCE and evidence_sufficient_for_decision "
                "must be false.",
            ]
        )

    if retry_schema_errors:
        lines.extend(
            [
                "",
                "Prior answer-correctness response failed deterministic schema "
                "validation:",
                *[f"- {error}" for error in retry_schema_errors],
                "Correct only the invalid JSON shape from the prior attempt. "
                "Preserve your per-option evidence judgments where they were valid.",
            ]
        )

    lines.extend(
        [
            "",
            "Return JSON with option_judgments (one entry per option label, each "
            "including answer_completeness), evidence_sufficient_for_decision, "
            "and abstention_reason (string or null).",
        ]
    )
    return _PASS_B_CORRECTNESS_SYSTEM, "\n".join(lines)


def build_pass_c_prompt(
    comparison_context: dict,
    pass_b_proposed_findings: Sequence[Mapping[str, Any]],
    dispute_context: dict,
) -> Tuple[str, str]:
    """Build Pass C prompts with an explicit resolution discriminator."""
    reason_code = str(dispute_context.get("reason_code") or "").strip()
    finding_refs = list(dispute_context.get("finding_refs") or [])
    hints = dispute_context.get("resolution_hints") or {}

    expected_resolution_type = str(
        hints.get("expected_resolution_type")
        or _SUBSTITUTION_BY_REASON.get(reason_code, "NORMAL_DISPUTE")
    )
    expected_substituted = list(
        hints.get("expected_substituted_for_passes")
        or _SUBSTITUTED_FOR_BY_RESOLUTION.get(expected_resolution_type, [])
    )
    allowed_refs = list(
        hints.get("allowed_confirmed_finding_refs")
        or finding_refs
        or [item.get("finding_ref") for item in pass_b_proposed_findings]
    )
    allowed_refs = [str(ref).strip() for ref in allowed_refs if str(ref).strip()]

    _, comparison_block = build_pass_b_prompt(comparison_context)

    proposed_block = json.dumps(
        list(pass_b_proposed_findings),
        sort_keys=True,
        separators=(",", ":"),
    )

    lines = [
        "Task: dispute or substitution resolution (Pass C).",
        f"Trigger reason_code: {reason_code}",
        f"Trigger finding_refs: {json.dumps(finding_refs, separators=(',', ':'))}",
        f"Trigger summary: {dispute_context.get('trigger_reason') or hints.get('trigger_reason') or ''}",
        "",
        "Resolution discriminator (required):",
        f"- resolution_type must be {expected_resolution_type!r}",
        f"- substituted_for_passes must be exactly "
        f"{json.dumps(expected_substituted, separators=(',', ':'))}",
        "- resolution_status must be RESOLVED or UNRESOLVED",
        "- UNRESOLVED requires confirmed_finding_refs=[]",
        "- RESOLVED requires confirmed_finding_refs to reference only allowed refs:",
        json.dumps(allowed_refs, separators=(",", ":")),
        "",
    ]

    if expected_resolution_type == "NORMAL_DISPUTE":
        lines.extend(
            [
                "Review Pass B proposed findings below. Confirm only substantiated "
                "refs from the allowed set; omit unconfirmed blocking claims.",
                "",
                "Pass B proposed_findings:",
                proposed_block,
            ]
        )
    else:
        lines.extend(
            [
                "Perform the substituted review pass(es) yourself and emit fresh "
                "proposed_findings plus confirmed_finding_refs drawn only from "
                "your Pass C proposed_findings.",
                "",
                "Comparison context for your substituted review:",
                comparison_block,
            ]
        )

    lines.extend(
        [
            "",
            "Return JSON matching the Pass C schema.",
        ]
    )
    return _PASS_C_SYSTEM, "\n".join(lines)


def _sorted_options(options: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        [dict(option) for option in options],
        key=lambda item: (item.get("display_order", 0), item.get("option_label", "")),
    )


def _format_finding_codes_block(*, exclude: frozenset = frozenset()) -> str:
    lines = []
    for code in sorted(SUPPORTED_FINDING_CODES):
        if code in exclude:
            continue
        definition = _FINDING_CODE_DEFINITIONS.get(code)
        if not definition:
            raise ValueError(
                f"Missing intrinsic definition for supported finding code: {code}"
            )
        suffix = " (warning-only materiality)" if code in _WARNING_ONLY_CODES else ""
        lines.append(f"- {code}{suffix}: {definition}")
    return "\n".join(lines)


def _proposed_finding_schema(*, include_evidence: bool = True) -> dict:
    properties: Dict[str, Any] = {
        "finding_ref": {"type": "string"},
        "finding_code": {
            "type": "string",
            "enum": sorted(SUPPORTED_FINDING_CODES),
        },
        "finding_type": {
            "type": "string",
            "enum": sorted(ALLOWED_FINDING_TYPES),
        },
        "severity": {
            "type": "string",
            "enum": sorted(ALLOWED_SEVERITIES),
        },
        "materiality": {
            "type": "string",
            "enum": sorted(ALLOWED_MATERIALITY),
        },
        "title": {"type": "string"},
        "description": {"type": "string"},
        "metadata": {"type": "object"},
    }
    required = [
        "finding_ref",
        "finding_code",
        "finding_type",
        "severity",
        "materiality",
        "title",
        "description",
        "metadata",
    ]
    if include_evidence:
        properties["evidence_chunk_ids"] = {
            "type": "array",
            "items": {"type": "string"},
        }
        required.append("evidence_chunk_ids")
    return {
        "type": "object",
        "required": required,
        "additionalProperties": False,
        "properties": properties,
    }


PASS_A_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "required": ["selected_option_labels"],
    "additionalProperties": False,
    "properties": {
        "selected_option_labels": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        },
    },
}

PASS_B_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "required": ["selected_option_labels", "proposed_findings"],
    "additionalProperties": False,
    "properties": {
        "selected_option_labels": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        },
        "proposed_findings": {
            "type": "array",
            "items": _proposed_finding_schema(),
        },
    },
}

def _option_judgment_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "option_label",
            "verdict",
            "citation_chunk_ids",
            "evidence_rationale",
            "answer_completeness",
        ],
        "additionalProperties": False,
        "properties": {
            "option_label": {"type": "string"},
            "verdict": {
                "type": "string",
                "enum": sorted(ANSWER_CORRECTNESS_VERDICTS),
            },
            "citation_chunk_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "evidence_rationale": {"type": "string"},
            "answer_completeness": {
                "type": "string",
                "enum": sorted(ANSWER_COMPLETENESS_VALUES),
            },
        },
    }


PASS_B_CORRECTNESS_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "required": ["option_judgments", "evidence_sufficient_for_decision"],
    "additionalProperties": False,
    "properties": {
        "option_judgments": {
            "type": "array",
            "items": _option_judgment_schema(),
            "minItems": 1,
        },
        "evidence_sufficient_for_decision": {"type": "boolean"},
        "abstention_reason": {"type": ["string", "null"]},
    },
}

PASS_C_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "required": [
        "resolution_type",
        "resolution_status",
        "substituted_for_passes",
        "confirmed_finding_refs",
    ],
    "additionalProperties": False,
    "properties": {
        "resolution_type": {
            "type": "string",
            "enum": sorted(ALLOWED_RESOLUTION_TYPES),
        },
        "resolution_status": {
            "type": "string",
            "enum": sorted(ALLOWED_RESOLUTION_STATUS),
        },
        "substituted_for_passes": {
            "type": "array",
            "items": {"type": "string"},
        },
        "confirmed_finding_refs": {
            "type": "array",
            "items": {"type": "string"},
        },
        "proposed_findings": {
            "type": "array",
            "items": _proposed_finding_schema(),
        },
    },
}
