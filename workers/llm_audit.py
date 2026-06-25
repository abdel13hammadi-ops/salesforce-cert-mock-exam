"""
Strict LLM audit response schema and validation (Phase 8F).

Validates that an LLM provider's parsed JSON output matches the CertBound
audit finding structure before the findings are forwarded to
``complete_audit_run_v1``.

Expected provider response shape
---------------------------------
  {
    "findings": [          ← required array; may be empty
      {
        "finding_code":     str   ← required, non-empty
        "finding_type":     str   ← required, see ALLOWED_FINDING_TYPES
        "severity":         str   ← required, see ALLOWED_SEVERITIES
        "title":            str   ← required, non-empty
        "description":      str   ← required, non-empty
        "field_path":       str   ← optional
        "confidence":       float ← optional, [0, 1]
        "detector_name":    str   ← optional
        "detector_version": str   ← optional
        "metadata":         dict  ← optional
        "evidence": [
          {
            "resource_chunk_id": str   ← required, non-empty UUID string
            "evidence_role":     str   ← required, see ALLOWED_EVIDENCE_ROLES
            "quote_text":        str   ← optional
            "relevance_score":   float ← optional, [0, 1]
            "metadata":          dict  ← optional
          }
        ]
      }
    ]
  }

No unknown *top-level* response keys are permitted.
"""

from __future__ import annotations

from typing import List

from workers.finding_policy import normalize_llm_finding

# ---------------------------------------------------------------------------
# Allowed enum values — must match complete_audit_run_v1 validation exactly
# ---------------------------------------------------------------------------

ALLOWED_FINDING_TYPES: frozenset = frozenset({
    "correctness", "ambiguity", "duplication", "outdated",
    "formatting", "coverage", "difficulty", "cognitive_level",
    "answer_quality", "explanation_quality", "source_support",
    "policy", "other",
})

ALLOWED_SEVERITIES: frozenset = frozenset({
    "info", "low", "medium", "high", "critical",
})

ALLOWED_EVIDENCE_ROLES: frozenset = frozenset({
    "supporting", "contradicting", "contextual",
})

# Only "findings" is permitted at the root of the response.
_ALLOWED_RESPONSE_KEYS: frozenset = frozenset({"findings"})

# ---------------------------------------------------------------------------
# JSON Schema hint — passed to providers that support structured output
# ---------------------------------------------------------------------------

AUDIT_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "required": ["findings"],
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "finding_code", "finding_type", "severity",
                    "title", "description",
                ],
                "properties": {
                    "finding_code":     {"type": "string"},
                    "finding_type":     {
                        "type": "string",
                        "enum": sorted(ALLOWED_FINDING_TYPES),
                    },
                    "severity":         {
                        "type": "string",
                        "enum": sorted(ALLOWED_SEVERITIES),
                    },
                    "title":            {"type": "string"},
                    "description":      {"type": "string"},
                    "field_path":       {"type": "string"},
                    "confidence":       {
                        "type": "number", "minimum": 0, "maximum": 1,
                    },
                    "detector_name":    {"type": "string"},
                    "detector_version": {"type": "string"},
                    "metadata":         {"type": "object"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["resource_chunk_id", "evidence_role"],
                            "properties": {
                                "resource_chunk_id": {"type": "string"},
                                "evidence_role": {
                                    "type": "string",
                                    "enum": sorted(ALLOWED_EVIDENCE_ROLES),
                                },
                                "quote_text":      {"type": "string"},
                                "relevance_score": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 1,
                                },
                                "metadata": {"type": "object"},
                            },
                        },
                    },
                },
            },
        },
    },
}


# ===========================================================================
# Validation exception
# ===========================================================================

class LlmAuditValidationError(ValueError):
    """Raised when the LLM provider response fails schema validation.

    Caught by ``audit_orchestration.orchestrate_audit``, which transitions
    the audit run to ``failed`` via ``end_audit_run_v1``.
    """


# ===========================================================================
# Public validation entry point
# ===========================================================================

def validate_llm_response(raw: object) -> List[dict]:
    """Validate a provider response and return the normalised findings list.

    Parameters
    ----------
    raw:
        Parsed JSON value returned by the provider (after JSON decode).
        Must be a ``dict`` with exactly the key ``"findings"``.

    Returns
    -------
    list of finding dicts compatible with ``complete_audit_run_v1``.

    Raises
    ------
    LlmAuditValidationError
        When the response is not a dict, contains unknown top-level keys,
        or any finding / evidence item fails validation.
    """
    if not isinstance(raw, dict):
        raise LlmAuditValidationError(
            f"Provider response must be a JSON object, got "
            f"{type(raw).__name__}"
        )

    unknown = set(raw.keys()) - _ALLOWED_RESPONSE_KEYS
    if unknown:
        raise LlmAuditValidationError(
            f"Provider response contains unknown top-level key(s): "
            f"{sorted(unknown)}"
        )

    findings_raw = raw.get("findings")
    if not isinstance(findings_raw, list):
        raise LlmAuditValidationError(
            "Response 'findings' must be a JSON array, got "
            + (
                type(findings_raw).__name__
                if findings_raw is not None
                else "null"
            )
        )

    return [normalize_llm_finding(_validate_finding(i, f)) for i, f in enumerate(findings_raw)]


# ===========================================================================
# Private helpers
# ===========================================================================

def _validate_finding(idx: int, finding: object) -> dict:
    """Validate one finding object; return normalised dict."""
    prefix = f"finding[{idx}]"

    if not isinstance(finding, dict):
        raise LlmAuditValidationError(
            f"{prefix} must be a JSON object, got {type(finding).__name__}"
        )

    # Required non-empty strings
    for field in ("finding_code", "finding_type", "severity", "title", "description"):
        val = finding.get(field)
        if not isinstance(val, str) or not val.strip():
            raise LlmAuditValidationError(
                f"{prefix} field '{field}' must be a non-empty string, "
                f"got {val!r}"
            )

    finding_type = finding["finding_type"]
    if finding_type not in ALLOWED_FINDING_TYPES:
        raise LlmAuditValidationError(
            f"{prefix} has invalid finding_type {finding_type!r}; "
            f"allowed: {sorted(ALLOWED_FINDING_TYPES)}"
        )

    severity = finding["severity"]
    if severity not in ALLOWED_SEVERITIES:
        raise LlmAuditValidationError(
            f"{prefix} has invalid severity {severity!r}; "
            f"allowed: {sorted(ALLOWED_SEVERITIES)}"
        )

    confidence = finding.get("confidence")
    if confidence is not None:
        # Reject booleans explicitly — bool is a subclass of int in Python.
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise LlmAuditValidationError(
                f"{prefix} confidence must be a number, got "
                f"{type(confidence).__name__}"
            )
        if not (0.0 <= float(confidence) <= 1.0):
            raise LlmAuditValidationError(
                f"{prefix} confidence must be in [0, 1], got {confidence}"
            )

    evidence_raw = finding.get("evidence", [])
    if not isinstance(evidence_raw, list):
        raise LlmAuditValidationError(
            f"{prefix} 'evidence' must be a JSON array"
        )

    return {
        "finding_code":     finding["finding_code"].strip(),
        "finding_type":     finding_type,
        "severity":         severity,
        "title":            finding["title"].strip(),
        "description":      finding["description"].strip(),
        "field_path":       finding.get("field_path"),
        "confidence":       float(confidence) if confidence is not None else None,
        "detector_name":    finding.get("detector_name"),
        "detector_version": finding.get("detector_version"),
        "metadata":         finding.get("metadata") or {},
        "evidence":         [
            _validate_evidence(idx, j, ev)
            for j, ev in enumerate(evidence_raw)
        ],
    }


def _validate_evidence(finding_idx: int, ev_idx: int, ev: object) -> dict:
    """Validate one evidence item; return normalised dict."""
    prefix = f"finding[{finding_idx}].evidence[{ev_idx}]"

    if not isinstance(ev, dict):
        raise LlmAuditValidationError(
            f"{prefix} must be a JSON object"
        )

    chunk_id = ev.get("resource_chunk_id")
    if not isinstance(chunk_id, str) or not chunk_id.strip():
        raise LlmAuditValidationError(
            f"{prefix} 'resource_chunk_id' must be a non-empty string"
        )

    role = ev.get("evidence_role")
    if not isinstance(role, str) or role not in ALLOWED_EVIDENCE_ROLES:
        raise LlmAuditValidationError(
            f"{prefix} has invalid evidence_role {role!r}; "
            f"allowed: {sorted(ALLOWED_EVIDENCE_ROLES)}"
        )

    relevance = ev.get("relevance_score")
    if relevance is not None:
        if isinstance(relevance, bool) or not isinstance(relevance, (int, float)):
            raise LlmAuditValidationError(
                f"{prefix} relevance_score must be a number"
            )
        if not (0.0 <= float(relevance) <= 1.0):
            raise LlmAuditValidationError(
                f"{prefix} relevance_score must be in [0, 1], got {relevance}"
            )

    quote_text = ev.get("quote_text")
    if quote_text is not None and not isinstance(quote_text, str):
        raise LlmAuditValidationError(
            f"{prefix} quote_text must be a string when present"
        )

    return {
        "resource_chunk_id": chunk_id.strip(),
        "evidence_role":     role,
        "quote_text":        quote_text,
        "relevance_score":   float(relevance) if relevance is not None else None,
        "metadata":          ev.get("metadata") or {},
    }
