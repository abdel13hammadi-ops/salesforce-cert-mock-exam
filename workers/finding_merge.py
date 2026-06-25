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
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from workers.finding_policy import MATERIALITY_RANK

# Severity from lowest to highest.
SEVERITY_RANK: Dict[str, int] = {
    "info":     0,
    "low":      1,
    "medium":   2,
    "high":     3,
    "critical": 4,
}


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

    return merged
