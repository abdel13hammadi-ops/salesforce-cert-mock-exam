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

_WARNING_ONLY_CODES = frozenset({"SOURCE_SUPPORT_WEAK", "DOMAIN_MISALIGNMENT"})

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
) -> Tuple[str, str]:
    """Build Pass B prompts with frozen evidence in retrieval rank order."""
    required_count = int(comparison_context["required_selection_count"])
    options = _sorted_options(comparison_context.get("options") or [])
    pass_a_labels = list(comparison_context.get("pass_a_selected_option_labels") or [])
    stored_correct = list(comparison_context.get("stored_correct_option_labels") or [])
    frozen_evidence = list(comparison_context.get("frozen_evidence") or [])
    frozen_evidence.sort(key=lambda item: item["rank"])

    codes_block = _format_finding_codes_block()

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

    lines.extend(
        [
            "",
            "Supported finding codes:",
            codes_block,
            "",
            "Rules:",
            "- Use only supported finding_code values.",
            "- Assign materiality=blocking only for substantive defects that "
            "would mislead candidates.",
            "- SOURCE_SUPPORT_WEAK and DOMAIN_MISALIGNMENT are warning-only; "
            "never assign materiality=blocking to them.",
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


def _format_finding_codes_block() -> str:
    lines = []
    for code in sorted(SUPPORTED_FINDING_CODES):
        suffix = " (warning-only materiality)" if code in _WARNING_ONLY_CODES else ""
        lines.append(f"- {code}{suffix}")
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
