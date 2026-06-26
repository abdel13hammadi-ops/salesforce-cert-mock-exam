"""
CertBound deterministic duplicate-question stem detector.

Pure detection functions compare question stems within one certification
(exam_name) across all domains.  Two methods are supported:

  exact_normalized   — identical stems after Unicode/case/punctuation/whitespace
                       normalization (similarity_score = 1.0)
  near_exact_lexical — difflib.SequenceMatcher ratio on normalized stems
                       >= NEAR_EXACT_LEXICAL_THRESHOLD (default 0.92)

No LLM calls.  Candidate pairs for near-exact detection are narrowed with a
token inverted index so large banks (1,200+ questions) stay practical without
an O(n^2) all-pairs scan.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from workers.audit_orchestration import orchestrate_audit
from workers.finding_policy import normalize_deterministic_finding

DETECTOR_NAME = "certbound-duplicate-question-detector"
DETECTOR_VERSION = "1.0.0"

# Documented deterministic thresholds
EXACT_NORMALIZED_THRESHOLD = 1.0
NEAR_EXACT_LEXICAL_THRESHOLD = 0.92

# Near-exact blocking: minimum shared tokens before SequenceMatcher runs.
MIN_SHARED_TOKENS = 2
MIN_SHARED_TOKENS_SHORT = 1
SHORT_STEM_TOKEN_COUNT = 3
NEAR_EXACT_MIN_TOKEN_JACCARD = 0.80

# Skip near-exact candidates when normalized lengths differ by more than this
# fraction of the longer stem (always allow at least NEAR_EXACT_MAX_LEN_DELTA).
NEAR_EXACT_MAX_LEN_DELTA = 10
NEAR_EXACT_MAX_LEN_RATIO = 0.15

DETECTION_METHOD_EXACT = "exact_normalized"
DETECTION_METHOD_NEAR_EXACT = "near_exact_lexical"

FINDING_CODE_EXACT = "DUPLICATE_QUESTION_STEM_EXACT"
FINDING_CODE_NEAR_EXACT = "DUPLICATE_QUESTION_STEM_NEAR_EXACT"

_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_question_stem(text: str) -> str:
    """Normalize a question stem for deterministic comparison.

    Steps (in order):
      1. Unicode NFKC normalization
      2. lower-case
      3. replace punctuation with whitespace (Unicode letters/digits kept)
      4. collapse repeated whitespace and strip
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.lower()
    normalized = _PUNCTUATION_RE.sub(" ", normalized)
    return " ".join(normalized.split())


def _stem_tokens(normalized_stem: str) -> Tuple[str, ...]:
    if not normalized_stem:
        return ()
    return tuple(normalized_stem.split())


def canonical_pair_ids(id_a: str, id_b: str) -> Tuple[str, str]:
    """Return lexicographically ordered pair IDs; rejects self-pairs."""
    a = str(id_a).strip()
    b = str(id_b).strip()
    if not a or not b:
        raise ValueError("question_version_id values must be non-empty")
    if a == b:
        raise ValueError("cannot compare a question version against itself")
    return (a, b) if a < b else (b, a)


def pair_dedupe_key(
    question_version_id_a: str,
    question_version_id_b: str,
    detection_method: str,
    ruleset_version: str = "1.0.0",
) -> Tuple[str, str, str, str]:
    """Stable key for deduplicating findings for the same pair, method, and ruleset."""
    a, b = canonical_pair_ids(question_version_id_a, question_version_id_b)
    return (a, b, detection_method, str(ruleset_version).strip() or "1.0.0")


def pair_key_from_finding(finding: dict) -> Optional[Tuple[str, str, str, str]]:
    """Extract a pair dedupe key from a persisted or in-memory finding."""
    meta = finding.get("metadata") or {}
    id_a = meta.get("question_version_id_a")
    id_b = meta.get("question_version_id_b")
    method = meta.get("detection_method")
    ruleset = meta.get("ruleset_version", "1.0.0")
    if not id_a or not id_b or not method:
        return None
    try:
        return pair_dedupe_key(str(id_a), str(id_b), str(method), str(ruleset))
    except ValueError:
        return None


def _coerce_row(row: dict) -> Tuple[str, str, str]:
    """Return (question_version_id, certification_exam_name, question_text)."""
    qvid = str(row.get("question_version_id", "")).strip()
    cert = str(
        row.get("certification_exam_name")
        or row.get("certification_id")
        or row.get("exam_name")
        or ""
    ).strip()
    text = str(row.get("question_text", ""))
    if not qvid:
        raise ValueError("each row must include question_version_id")
    if not cert:
        raise ValueError("each row must include certification_exam_name")
    return qvid, cert, text


def _group_rows_by_certification(rows: Sequence[dict]) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = {}
    for row in rows:
        qvid, cert, text = _coerce_row(row)
        grouped.setdefault(cert, []).append(
            {
                "question_version_id": qvid,
                "certification_exam_name": cert,
                "question_text": text,
            }
        )
    return grouped


def _shared_token_requirement(tokens_a: Tuple[str, ...], tokens_b: Tuple[str, ...]) -> int:
    if (
        len(tokens_a) <= SHORT_STEM_TOKEN_COUNT
        or len(tokens_b) <= SHORT_STEM_TOKEN_COUNT
    ):
        return MIN_SHARED_TOKENS_SHORT
    return MIN_SHARED_TOKENS


def _lengths_compatible(normalized_a: str, normalized_b: str) -> bool:
    len_a = len(normalized_a)
    len_b = len(normalized_b)
    if len_a == 0 or len_b == 0:
        return False
    longer = max(len_a, len_b)
    delta = abs(len_a - len_b)
    allowed = max(NEAR_EXACT_MAX_LEN_DELTA, int(longer * NEAR_EXACT_MAX_LEN_RATIO))
    return delta <= allowed


def _lexical_similarity(normalized_a: str, normalized_b: str) -> float:
    return difflib.SequenceMatcher(None, normalized_a, normalized_b).ratio()


def _token_jaccard(tokens_a: Tuple[str, ...], tokens_b: Tuple[str, ...]) -> float:
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union


def _build_finding(
    *,
    finding_code: str,
    detection_method: str,
    certification_exam_name: str,
    question_version_id_a: str,
    question_version_id_b: str,
    similarity_score: float,
    similarity_threshold: float,
    ruleset_version: str,
    stem_a: str,
    stem_b: str,
) -> dict:
    id_a, id_b = canonical_pair_ids(question_version_id_a, question_version_id_b)
    if detection_method == DETECTION_METHOD_EXACT:
        title = "Duplicate question stem (exact normalized match)"
        severity = "high"
        description = (
            f"Question versions {id_a} and {id_b} have identical normalized stems "
            f"within certification {certification_exam_name!r}."
        )
    else:
        title = "Duplicate question stem (near-exact lexical match)"
        severity = "medium"
        description = (
            f"Question versions {id_a} and {id_b} have near-identical stems "
            f"(similarity {similarity_score:.4f} >= threshold "
            f"{similarity_threshold:.4f}) within certification "
            f"{certification_exam_name!r}."
        )

    metadata = {
        "certification_exam_name": certification_exam_name,
        "certification_id": certification_exam_name,
        "question_version_id_a": id_a,
        "question_version_id_b": id_b,
        "detection_method": detection_method,
        "similarity_score": round(similarity_score, 6),
        "similarity_threshold": similarity_threshold,
        "ruleset_version": ruleset_version,
        "normalized_stem_a": normalize_question_stem(stem_a),
        "normalized_stem_b": normalize_question_stem(stem_b),
    }
    if detection_method == DETECTION_METHOD_NEAR_EXACT:
        metadata["token_jaccard_threshold"] = NEAR_EXACT_MIN_TOKEN_JACCARD

    finding = {
        "finding_code": finding_code,
        "finding_type": "duplication",
        "severity": severity,
        "title": title,
        "description": description,
        "field_path": "question_text",
        "confidence": similarity_score,
        "detector_name": DETECTOR_NAME,
        "detector_version": DETECTOR_VERSION,
        "metadata": metadata,
        "evidence": [],
    }
    return normalize_deterministic_finding(finding)


def _detect_exact_duplicates(
    entries: Sequence[dict],
    *,
    certification_exam_name: str,
    ruleset_version: str,
) -> List[dict]:
    by_normalized: Dict[str, List[dict]] = {}
    for entry in entries:
        normalized = normalize_question_stem(entry["question_text"])
        by_normalized.setdefault(normalized, []).append(entry)

    findings: List[dict] = []
    for normalized, group in by_normalized.items():
        if not normalized or len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                left = group[i]
                right = group[j]
                findings.append(
                    _build_finding(
                        finding_code=FINDING_CODE_EXACT,
                        detection_method=DETECTION_METHOD_EXACT,
                        certification_exam_name=certification_exam_name,
                        question_version_id_a=left["question_version_id"],
                        question_version_id_b=right["question_version_id"],
                        similarity_score=EXACT_NORMALIZED_THRESHOLD,
                        similarity_threshold=EXACT_NORMALIZED_THRESHOLD,
                        ruleset_version=ruleset_version,
                        stem_a=left["question_text"],
                        stem_b=right["question_text"],
                    )
                )
    return findings


def _detect_near_exact_duplicates(
    entries: Sequence[dict],
    *,
    certification_exam_name: str,
    ruleset_version: str,
    near_exact_threshold: float,
    skip_pairs: Iterable[Tuple[str, str, str]],
) -> List[dict]:
    skip = set(skip_pairs)
    indexed: List[dict] = []
    token_to_indices: Dict[str, List[int]] = {}

    for idx, entry in enumerate(entries):
        normalized = normalize_question_stem(entry["question_text"])
        tokens = _stem_tokens(normalized)
        indexed.append(
            {
                **entry,
                "normalized_stem": normalized,
                "tokens": tokens,
            }
        )
        seen_tokens = set(tokens)
        for token in seen_tokens:
            token_to_indices.setdefault(token, []).append(idx)

    candidate_pairs: set[Tuple[int, int]] = set()
    for indices in token_to_indices.values():
        if len(indices) < 2:
            continue
        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                left_idx = indices[i]
                right_idx = indices[j]
                if left_idx == right_idx:
                    continue
                pair = (left_idx, right_idx) if left_idx < right_idx else (right_idx, left_idx)
                candidate_pairs.add(pair)

    findings: List[dict] = []
    for left_idx, right_idx in sorted(candidate_pairs):
        left = indexed[left_idx]
        right = indexed[right_idx]
        if left["normalized_stem"] == right["normalized_stem"]:
            continue

        dedupe_key = pair_dedupe_key(
            left["question_version_id"],
            right["question_version_id"],
            DETECTION_METHOD_NEAR_EXACT,
        )
        if dedupe_key in skip:
            continue

        shared = set(left["tokens"]) & set(right["tokens"])
        required = _shared_token_requirement(left["tokens"], right["tokens"])
        if len(shared) < required:
            continue
        if not _lengths_compatible(left["normalized_stem"], right["normalized_stem"]):
            continue

        jaccard = _token_jaccard(left["tokens"], right["tokens"])
        if jaccard < NEAR_EXACT_MIN_TOKEN_JACCARD:
            continue

        score = _lexical_similarity(left["normalized_stem"], right["normalized_stem"])
        if score < near_exact_threshold:
            continue

        findings.append(
            _build_finding(
                finding_code=FINDING_CODE_NEAR_EXACT,
                detection_method=DETECTION_METHOD_NEAR_EXACT,
                certification_exam_name=certification_exam_name,
                question_version_id_a=left["question_version_id"],
                question_version_id_b=right["question_version_id"],
                similarity_score=score,
                similarity_threshold=near_exact_threshold,
                ruleset_version=ruleset_version,
                stem_a=left["question_text"],
                stem_b=right["question_text"],
            )
        )
    return findings


def dedupe_duplicate_findings(findings: Sequence[dict]) -> List[dict]:
    """Drop duplicate findings for the same canonical pair, method, and ruleset."""
    seen: set[Tuple[str, str, str, str]] = set()
    deduped: List[dict] = []
    for finding in findings:
        key = pair_key_from_finding(finding)
        if key is None:
            deduped.append(finding)
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped


def filter_unpersisted_duplicate_findings(
    findings: Sequence[dict],
    persisted_keys: Iterable[Tuple[str, str, str, str]],
) -> List[dict]:
    """Remove findings whose durable pair keys already exist in audit_findings."""
    persisted = set(persisted_keys)
    filtered: List[dict] = []
    for finding in findings:
        key = pair_key_from_finding(finding)
        if key is not None and key in persisted:
            continue
        filtered.append(finding)
    return filtered


def filter_new_duplicate_findings(
    findings: Sequence[dict],
    existing_findings: Sequence[dict],
) -> List[dict]:
    """Remove findings whose pair keys already exist in a supplied finding list."""
    existing_keys = {
        key
        for finding in existing_findings
        if (key := pair_key_from_finding(finding)) is not None
    }
    return filter_unpersisted_duplicate_findings(findings, existing_keys)


def _call_rpc(client, name: str, params: dict) -> List[dict]:
    """Invoke a Supabase RPC and return all result rows."""
    result = client.rpc(name, params).execute()
    if getattr(result, "error", None):
        raise RuntimeError(f"RPC {name!r} failed: {result.error}")
    return result.data or []


def fetch_persisted_duplicate_pair_keys(
    client,
    *,
    certification_exam_name: str,
    ruleset_version: str,
) -> set[Tuple[str, str, str, str]]:
    """Load durable duplicate pair keys already stored in audit_findings."""
    rows = _call_rpc(
        client,
        "list_duplicate_question_pair_keys_v1",
        {
            "p_certification_exam_name": certification_exam_name,
            "p_ruleset_version": ruleset_version,
        },
    )
    keys: set[Tuple[str, str, str, str]] = set()
    for row in rows:
        id_a = str(row.get("question_version_id_a", "")).strip()
        id_b = str(row.get("question_version_id_b", "")).strip()
        method = str(row.get("detection_method", "")).strip()
        ruleset = str(row.get("ruleset_version", "")).strip() or ruleset_version
        if not id_a or not id_b or not method:
            continue
        try:
            keys.add(pair_dedupe_key(id_a, id_b, method, ruleset))
        except ValueError:
            continue
    return keys


def detect_duplicate_question_stems(
    rows: Sequence[dict],
    *,
    ruleset_version: str = "1.0.0",
    near_exact_threshold: float = NEAR_EXACT_LEXICAL_THRESHOLD,
    existing_findings: Optional[Sequence[dict]] = None,
) -> List[dict]:
    """Detect duplicate stems within each certification present in *rows*."""
    if near_exact_threshold <= 0 or near_exact_threshold > 1:
        raise ValueError("near_exact_threshold must be in (0, 1]")

    all_findings: List[dict] = []
    for certification_exam_name, cert_rows in _group_rows_by_certification(rows).items():
        exact_findings = _detect_exact_duplicates(
            cert_rows,
            certification_exam_name=certification_exam_name,
            ruleset_version=ruleset_version,
        )
        exact_keys = {
            pair_dedupe_key(
                finding["metadata"]["question_version_id_a"],
                finding["metadata"]["question_version_id_b"],
                DETECTION_METHOD_EXACT,
            )
            for finding in exact_findings
        }
        near_findings = _detect_near_exact_duplicates(
            cert_rows,
            certification_exam_name=certification_exam_name,
            ruleset_version=ruleset_version,
            near_exact_threshold=near_exact_threshold,
            skip_pairs=exact_keys,
        )
        all_findings.extend(exact_findings)
        all_findings.extend(near_findings)

    deduped = dedupe_duplicate_findings(all_findings)
    if existing_findings:
        deduped = filter_new_duplicate_findings(deduped, existing_findings)
    return deduped


def _select_audit_anchor_question_version_id(rows: Sequence[dict]) -> str:
    ids = sorted(str(row["question_version_id"]).strip() for row in rows)
    if not ids:
        raise ValueError("cannot orchestrate duplicate audit with zero rows")
    return ids[0]


def orchestrate_certification_duplicate_audit(
    client,
    *,
    rows: Sequence[dict],
    created_by: str,
    ruleset_version: str = "1.0.0",
    near_exact_threshold: float = NEAR_EXACT_LEXICAL_THRESHOLD,
    existing_findings: Optional[Sequence[dict]] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Run duplicate detection and persist findings through the audit RPC lifecycle."""
    if not rows:
        raise ValueError("rows must not be empty")

    grouped = _group_rows_by_certification(rows)
    if len(grouped) != 1:
        raise ValueError(
            "orchestrate_certification_duplicate_audit requires rows from one certification"
        )
    certification_exam_name = next(iter(grouped.keys()))
    cert_rows = grouped[certification_exam_name]

    run_metadata = {
        "scan_type": "duplicate_question_stem",
        "certification_exam_name": certification_exam_name,
        "certification_id": certification_exam_name,
        "question_count": len(cert_rows),
        "near_exact_threshold": near_exact_threshold,
    }
    if metadata:
        run_metadata.update(metadata)

    def _check_fn() -> List[dict]:
        findings = detect_duplicate_question_stems(
            cert_rows,
            ruleset_version=ruleset_version,
            near_exact_threshold=near_exact_threshold,
            existing_findings=existing_findings,
        )
        persisted_keys = fetch_persisted_duplicate_pair_keys(
            client,
            certification_exam_name=certification_exam_name,
            ruleset_version=ruleset_version,
        )
        return filter_unpersisted_duplicate_findings(findings, persisted_keys)

    return orchestrate_audit(
        client,
        audit_type="deterministic",
        target_question_version_id=_select_audit_anchor_question_version_id(cert_rows),
        target_candidate_id=None,
        created_by=created_by,
        ruleset_version=ruleset_version,
        resource_snapshot={},
        metadata=run_metadata,
        check_fn=_check_fn,
    )
