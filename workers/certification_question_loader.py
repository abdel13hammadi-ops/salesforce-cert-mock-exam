"""
Load current question-version stems for one certification.

Uses list_certification_current_question_versions_v1 only — no direct table
access.  Returns at most one row per live question (highest version_number).
"""

from __future__ import annotations

from typing import List, Tuple


def _call_rpc(client, name: str, params: dict) -> List[dict]:
    result = client.rpc(name, params).execute()
    if getattr(result, "error", None):
        raise RuntimeError(f"RPC {name!r} failed: {result.error}")
    return result.data or []


def _version_rank(row: dict) -> Tuple[int, str, str]:
    return (
        int(row.get("version_number") or 0),
        str(row.get("created_at") or ""),
        str(row.get("question_version_id") or ""),
    )


def _dedupe_latest_version_per_question(rows: List[dict]) -> List[dict]:
    """Keep only the highest version_number per question_id (defense in depth)."""
    latest_by_question: dict[int, dict] = {}
    for row in rows:
        question_id = row.get("question_id")
        if question_id is None:
            continue
        existing = latest_by_question.get(question_id)
        if existing is None or _version_rank(row) > _version_rank(existing):
            latest_by_question[question_id] = row
    return list(latest_by_question.values())


def load_certification_current_question_versions(
    client,
    certification_exam_name: str,
) -> List[dict]:
    """Return detector-ready rows for the latest version of each live question."""
    cert = str(certification_exam_name).strip()
    if not cert:
        raise ValueError("certification_exam_name must not be empty")

    rows = _call_rpc(
        client,
        "list_certification_current_question_versions_v1",
        {"p_certification_exam_name": cert},
    )
    deduped = _dedupe_latest_version_per_question(rows)

    loaded: List[dict] = []
    for row in deduped:
        qvid = row.get("question_version_id")
        text = row.get("question_text")
        if not qvid or text is None:
            continue
        loaded.append(
            {
                "question_version_id": str(qvid),
                "certification_exam_name": str(
                    row.get("certification_exam_name") or cert
                ),
                "question_text": str(text),
                "category": row.get("category"),
                "question_id": row.get("question_id"),
                "version_number": row.get("version_number"),
            }
        )
    return loaded


# Backwards-compatible alias for tests importing the old private name.
_dedupe_latest_published_per_question = _dedupe_latest_version_per_question
