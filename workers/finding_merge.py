"""
Finding merge logic for CertBound hybrid audits (Phase 8I).

Merges deterministic and LLM findings into a single deduplicated list.

Deduplication key
-----------------
Three-tuple of normalized strings:
  (finding_code, field_path, description)
"Normalized" means stripped of surrounding whitespace and lowercased.

When two findings share the same key
--------------------------------------
* Highest severity is kept.
* Highest materiality is kept (``informational < warning < blocking``).
* Highest confidence is kept (``None`` only when both are ``None``).
* Evidence combined, deduped by ``(resource_chunk_id, evidence_role)``.
* Metadata merged: LLM metadata is the base; deterministic metadata values
  overwrite on conflict so that deterministic provenance (e.g.
  ``ruleset_version``) is not lost.
* LLM ``detector_name`` / ``detector_version`` are recorded under
  ``"llm_detector_name"`` / ``"llm_detector_version"`` keys in the merged
  metadata so no provenance information is discarded.
* Deterministic identity wins: ``finding_code``, ``finding_type``, ``title``,
  ``description``, ``field_path``, ``detector_name``, and ``detector_version``
  from the deterministic finding are used in the merged output.

Output ordering
---------------
Deterministic findings appear first in their original (stable) order,
followed by any unmatched LLM findings in their original order.
Conflicting findings with distinct deduplication keys are never silently dropped.

Relaxed second pass (V45)
-------------------------
After the primary merge, ``EXPLANATION_MISSING`` findings with equivalent
logical field paths are collapsed:

* ``question.explanation``, ``explanation``, and empty/null field paths are
  treated as the same logical target.
* Severity, materiality, confidence, evidence, and metadata follow the same
  escalation rules as the primary merge.
* Distinct ``metadata.original_finding_code`` values are preserved as a list
  when multiple sources contributed.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from workers.deterministic_audit import DETECTOR_NAME
from workers.finding_policy import MATERIALITY_RANK

# Severity from lowest to highest.
SEVERITY_RANK: Dict[str, int] = {
    "info":     0,
    "low":      1,
    "medium":   2,
    "high":     3,
    "critical": 4,
}

_RELAXED_DEDUP_CODES = frozenset({"explanation_missing"})
_EXPLANATION_LOGICAL_FIELD = "explanation"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _normalize(val: object) -> str:
    """Strip and lowercase *val* for stable deduplication keying."""
    if val is None:
        return ""
    return str(val).strip().lower()


def _merge_key(finding: dict) -> Tuple[str, str, str]:
    """Return the three-part deduplication key for *finding*."""
    return (
        _normalize(finding.get("finding_code")),
        _normalize(finding.get("field_path")),
        _normalize(finding.get("description")),
    )


def _pick_severity(a: str, b: str) -> str:
    """Return the higher-ranked of severities *a* and *b*."""
    return a if SEVERITY_RANK.get(a, -1) >= SEVERITY_RANK.get(b, -1) else b


def _pick_materiality(a: str, b: str) -> str:
    """Return the higher-ranked materiality of *a* and *b*."""
    return a if MATERIALITY_RANK.get(a, -1) >= MATERIALITY_RANK.get(b, -1) else b


def _pick_confidence(
    a: Optional[float],
    b: Optional[float],
) -> Optional[float]:
    """Return the higher confidence; ``None`` only when both are ``None``."""
    if a is None:
        return b
    if b is None:
        return a
    return max(float(a), float(b))


def _merge_evidence(
    ev_a: Optional[List[dict]],
    ev_b: Optional[List[dict]],
) -> List[dict]:
    """Combine two evidence lists, deduplicating by (resource_chunk_id, evidence_role)."""
    seen: Set[Tuple] = set()
    result: List[dict] = []
    for ev in (ev_a or []) + (ev_b or []):
        key = (ev.get("resource_chunk_id"), ev.get("evidence_role"))
        if key not in seen:
            seen.add(key)
            result.append(ev)
    return result


def _merge_meta(
    det_meta: Optional[dict],
    llm_meta: Optional[dict],
) -> dict:
    """Merge finding metadata.

    LLM metadata is the base; deterministic metadata values overwrite on
    conflict so deterministic provenance (e.g. ``ruleset_version``) is
    preserved in the merged output.
    """
    return {**(llm_meta or {}), **(det_meta or {})}


def _is_deterministic_finding(finding: dict) -> bool:
    return finding.get("detector_name") == DETECTOR_NAME


def _logical_field_path(finding: dict) -> str:
    """Return the relaxed dedup field-path key for *finding*."""
    code = _normalize(finding.get("finding_code"))
    path = _normalize(finding.get("field_path"))
    if code == "explanation_missing":
        if path in {"", _EXPLANATION_LOGICAL_FIELD, "question.explanation"}:
            return _EXPLANATION_LOGICAL_FIELD
    return path


def _relaxed_dedup_key(finding: dict) -> Tuple[str, str]:
    return (_normalize(finding.get("finding_code")), _logical_field_path(finding))


def _original_finding_codes_from_meta(meta: Optional[dict]) -> List[str]:
    if not meta:
        return []
    raw = meta.get("original_finding_code")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if raw:
        return [str(raw).strip()]
    return []


def _collect_original_finding_codes(*findings: dict) -> List[str]:
    seen: Set[str] = set()
    codes: List[str] = []
    for finding in findings:
        for code in _original_finding_codes_from_meta(finding.get("metadata")):
            if code not in seen:
                seen.add(code)
                codes.append(code)
    return codes


def _apply_original_finding_codes(meta: dict, *findings: dict) -> dict:
    merged = dict(meta)
    originals = _collect_original_finding_codes(*findings)
    if len(originals) > 1:
        merged["original_finding_code"] = originals
    elif len(originals) == 1:
        merged["original_finding_code"] = originals[0]
    return merged


def _merge_relaxed_metadata(left: dict, right: dict) -> dict:
    """Merge metadata for relaxed dedup, preserving provenance."""
    if _is_deterministic_finding(left):
        merged = _merge_meta(left.get("metadata"), right.get("metadata"))
    elif _is_deterministic_finding(right):
        merged = _merge_meta(right.get("metadata"), left.get("metadata"))
    else:
        merged = {**(left.get("metadata") or {}), **(right.get("metadata") or {})}

    merged = _apply_original_finding_codes(merged, left, right)

    for src in (left, right):
        if _is_deterministic_finding(src):
            continue
        det_name = src.get("detector_name")
        det_ver = src.get("detector_version")
        if det_name and "llm_detector_name" not in merged:
            merged["llm_detector_name"] = det_name
        if det_ver and "llm_detector_version" not in merged:
            merged["llm_detector_version"] = det_ver

    return merged


def _identity_base(left: dict, right: dict) -> dict:
    if _is_deterministic_finding(left):
        return left
    if _is_deterministic_finding(right):
        return right
    return left


def _collapse_relaxed_pair(left: dict, right: dict) -> dict:
    """Collapse two allowlisted findings that share a relaxed dedup key."""
    base = _identity_base(left, right)
    merged_meta = _merge_relaxed_metadata(left, right)
    return {
        "finding_code":     base["finding_code"],
        "finding_type":     base["finding_type"],
        "title":            base["title"],
        "description":      base["description"],
        "field_path":       base.get("field_path"),
        "detector_name":    base.get("detector_name"),
        "detector_version": base.get("detector_version"),
        "severity":         _pick_severity(left["severity"], right["severity"]),
        "materiality":      _pick_materiality(
            left.get("materiality", "warning"),
            right.get("materiality", "warning"),
        ),
        "confidence":       _pick_confidence(
            left.get("confidence"), right.get("confidence")
        ),
        "evidence":         _merge_evidence(
            left.get("evidence"), right.get("evidence")
        ),
        "metadata":         merged_meta,
    }


def _dedup_relaxed_allowlist(findings: List[dict]) -> List[dict]:
    """Second pass: collapse allowlisted findings by code + logical field path."""
    result: List[dict] = []
    for finding in findings:
        code = _normalize(finding.get("finding_code"))
        if code not in _RELAXED_DEDUP_CODES:
            result.append(dict(finding))
            continue

        relaxed_key = _relaxed_dedup_key(finding)
        match_idx: Optional[int] = None
        for i, existing in enumerate(result):
            if (
                _normalize(existing.get("finding_code")) in _RELAXED_DEDUP_CODES
                and _relaxed_dedup_key(existing) == relaxed_key
            ):
                match_idx = i
                break

        if match_idx is None:
            result.append(dict(finding))
        else:
            result[match_idx] = _collapse_relaxed_pair(result[match_idx], finding)

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def merge_findings(
    deterministic: Optional[List[dict]],
    llm: Optional[List[dict]],
) -> List[dict]:
    """Merge deterministic and LLM findings into a single deduplicated list.

    Parameters
    ----------
    deterministic:
        Findings from the deterministic engine (may be ``None`` or empty).
    llm:
        Findings from the LLM provider (already validated; may be ``None``
        or empty).

    Returns
    -------
    Merged list of finding dicts compatible with ``complete_audit_run_v1``.

    Algorithm
    ---------
    For each deterministic finding, search for an unmatched LLM finding
    sharing the same deduplication key.

    * If found: build a merged finding (deterministic identity + escalated
      severity/confidence + combined evidence + merged metadata) and consume
      the LLM finding.
    * If not found: include the deterministic finding unchanged.

    After all deterministic findings are processed, append the remaining
    unmatched LLM findings in their original order.

    Findings with distinct deduplication keys are never silently dropped.
    """
    det_list: List[dict] = list(deterministic or [])
    llm_list: List[dict] = list(llm or [])

    merged: List[dict] = []
    llm_matched: Set[int] = set()

    for det_finding in det_list:
        det_key = _merge_key(det_finding)

        # Find the first unmatched LLM finding with the same key.
        match_idx: Optional[int] = None
        for j, llm_finding in enumerate(llm_list):
            if j in llm_matched:
                continue
            if _merge_key(llm_finding) == det_key:
                match_idx = j
                break

        if match_idx is not None:
            llm_finding = llm_list[match_idx]
            llm_matched.add(match_idx)

            # Merge metadata: deterministic provenance takes precedence.
            merged_meta = _merge_meta(
                det_finding.get("metadata"),
                llm_finding.get("metadata"),
            )
            # Record LLM detector provenance under stable keys.
            llm_det_name = llm_finding.get("detector_name")
            llm_det_ver  = llm_finding.get("detector_version")
            if llm_det_name:
                merged_meta["llm_detector_name"]    = llm_det_name
            if llm_det_ver:
                merged_meta["llm_detector_version"] = llm_det_ver

            merged.append({
                # Deterministic identity fields win.
                "finding_code":     det_finding["finding_code"],
                "finding_type":     det_finding["finding_type"],
                "title":            det_finding["title"],
                "description":      det_finding["description"],
                "field_path":       det_finding.get("field_path"),
                "detector_name":    det_finding.get("detector_name"),
                "detector_version": det_finding.get("detector_version"),
                # Escalation rules.
                "severity":   _pick_severity(
                    det_finding["severity"], llm_finding["severity"]
                ),
                "materiality": _pick_materiality(
                    det_finding.get("materiality", "warning"),
                    llm_finding.get("materiality", "warning"),
                ),
                "confidence": _pick_confidence(
                    det_finding.get("confidence"), llm_finding.get("confidence")
                ),
                # Combined and deduped evidence.
                "evidence": _merge_evidence(
                    det_finding.get("evidence"), llm_finding.get("evidence")
                ),
                "metadata": merged_meta,
            })
        else:
            # No match: include the deterministic finding unchanged.
            merged.append(dict(det_finding))

    # Append unmatched LLM findings in their original order.
    for j, llm_finding in enumerate(llm_list):
        if j not in llm_matched:
            merged.append(dict(llm_finding))

    return _dedup_relaxed_allowlist(merged)
