"""
Free-mock curation helpers (V46 Phase 1).

Pure validation for tests and admin UI previews. Database RPCs enforce the same
rules atomically on publish. Learner runtime (app.py) is unchanged in Phase 1.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from utils.question_answer_key import is_answer_key_valid

FREE_MOCK_SLOT_COUNT = 15
FREE_MOCK_MIN_MULTI_SELECT = 2
DEFAULT_FREE_MOCK_LANGUAGE = "en"

ADM_EXAM_NAME = "Salesforce Certified Platform Administrator"
BA_EXAM_NAME = "Salesforce Certified Business Analyst"

SUPPORTED_EXAMS = frozenset({ADM_EXAM_NAME, BA_EXAM_NAME})

FREE_MOCK_BLUEPRINTS: Dict[str, Dict[str, int]] = {
    ADM_EXAM_NAME: {
        "Configuration and Setup": 2,
        "Object Manager and Lightning App Builder": 2,
        "Data and Analytics Management": 3,
        "Automation": 2,
        "Sales and Marketing Applications": 2,
        "Service and Support Applications": 2,
        "Productivity and Collaboration": 1,
        "Agentforce AI": 1,
    },
    BA_EXAM_NAME: {
        "Customer Discovery": 2,
        "Collaboration with Stakeholders": 3,
        "Business Process Mapping": 2,
        "Requirements": 3,
        "User Stories": 3,
        "User Acceptance": 2,
    },
}


class FreeMockCurationError(ValueError):
    """Raised when curation RPC calls fail."""


class FreeMockCurationSetupError(FreeMockCurationError):
    """Raised when curation tables/RPCs are not deployed yet."""


FREE_MOCK_CURATION_SETUP_MESSAGE = (
    "Free-mock curation is not available yet. Apply the database migration "
    "supabase/migrations/20260629120000_v46_free_mock_curation_foundation.sql "
    "to Supabase, then reload this page."
)


def is_missing_curation_backend_error(exc: BaseException) -> bool:
    """Return True when the error indicates tables/RPCs are not deployed."""
    text = str(exc).lower()
    markers = (
        "create_free_mock_draft_v1",
        "replace_free_mock_draft_items_v1",
        "validate_free_mock_draft_v1",
        "publish_free_mock_draft_v1",
        "get_free_mock_curation_state_v1",
        "free_mock_sets",
        "free_mock_set_items",
        "could not find the function",
        "does not exist",
        "pgrst202",
        "schema cache",
    )
    return any(marker in text for marker in markers)


def get_blueprint(exam_name: str) -> Dict[str, int]:
    blueprint = FREE_MOCK_BLUEPRINTS.get(exam_name or "")
    if not blueprint:
        raise FreeMockCurationError(f"unsupported exam for free-mock curation: {exam_name!r}")
    return dict(blueprint)


def blueprint_total(exam_name: str) -> int:
    return sum(get_blueprint(exam_name).values())


def _call_rpc(client, name: str, params: dict) -> List[dict]:
    try:
        result = client.rpc(name, params).execute()
    except Exception as exc:
        if is_missing_curation_backend_error(exc):
            raise FreeMockCurationSetupError(FREE_MOCK_CURATION_SETUP_MESSAGE) from exc
        raise FreeMockCurationError(f"RPC {name!r} failed: {exc}") from exc
    if getattr(result, "error", None):
        err_text = str(result.error)
        if is_missing_curation_backend_error(Exception(err_text)):
            raise FreeMockCurationSetupError(FREE_MOCK_CURATION_SETUP_MESSAGE)
        raise FreeMockCurationError(f"RPC {name!r} failed: {result.error}")
    return list(result.data or [])


def create_draft(client, *, exam_name: str, language_code: str, actor_email: str) -> dict:
    rows = _call_rpc(
        client,
        "create_free_mock_draft_v1",
        {
            "p_exam_name": exam_name,
            "p_language_code": language_code or DEFAULT_FREE_MOCK_LANGUAGE,
            "p_actor_email": actor_email,
        },
    )
    if not rows:
        raise FreeMockCurationError("create_free_mock_draft_v1 returned no rows")
    return rows[0]


def replace_draft_items(
    client,
    *,
    set_id: str,
    items: Sequence[Mapping[str, Any]],
    actor_email: str,
) -> int:
    payload = [
        {
            "slot_order": int(item["slot_order"]),
            "question_id": int(item["question_id"]),
        }
        for item in items
    ]
    rows = _call_rpc(
        client,
        "replace_free_mock_draft_items_v1",
        {
            "p_set_id": set_id,
            "p_items": payload,
            "p_actor_email": actor_email,
        },
    )
    if not rows:
        raise FreeMockCurationError("replace_free_mock_draft_items_v1 returned no rows")
    return int(rows[0].get("item_count") or 0)


def validate_draft(client, *, set_id: str) -> dict:
    rows = _call_rpc(client, "validate_free_mock_draft_v1", {"p_set_id": set_id})
    if not rows:
        raise FreeMockCurationError("validate_free_mock_draft_v1 returned no rows")
    row = rows[0]
    return {
        "valid": bool(row.get("valid")),
        "failures": list(row.get("failures") or []),
    }


def publish_draft(
    client,
    *,
    set_id: str,
    actor_email: str,
    reason: str,
) -> dict:
    rows = _call_rpc(
        client,
        "publish_free_mock_draft_v1",
        {
            "p_set_id": set_id,
            "p_actor_email": actor_email,
            "p_reason": reason,
        },
    )
    if not rows:
        raise FreeMockCurationError("publish_free_mock_draft_v1 returned no rows")
    return rows[0]


def get_curation_state(
    client,
    *,
    exam_name: str,
    language_code: str = DEFAULT_FREE_MOCK_LANGUAGE,
) -> dict:
    rows = _call_rpc(
        client,
        "get_free_mock_curation_state_v1",
        {
            "p_exam_name": exam_name,
            "p_language_code": language_code or DEFAULT_FREE_MOCK_LANGUAGE,
        },
    )
    draft = None
    published = None
    for row in rows:
        if row.get("status") == "published" and published is None:
            published = row
        elif row.get("status") == "draft" and draft is None:
            draft = row
    return {"draft": draft, "published": published}


def normalize_draft_items(items: Sequence[Mapping[str, Any]]) -> List[dict]:
    """Return sorted draft slots with int ids."""
    normalized = []
    for item in items or []:
        slot = int(item.get("slot_order") or 0)
        qid = int(item.get("question_id") or 0)
        domain = str(item.get("domain_name") or item.get("category") or "").strip()
        normalized.append(
            {
                "slot_order": slot,
                "question_id": qid,
                "domain_name": domain,
            }
        )
    normalized.sort(key=lambda row: row["slot_order"])
    return normalized


def build_runtime_question_snapshot(question: Mapping[str, Any], options: Sequence[Mapping[str, Any]]) -> dict:
    """Build the shape expected by question_answer_key validators."""
    qtype = str(question.get("question_type") or "single").strip().lower()
    select_count = question.get("select_count")
    if qtype == "single" and (select_count is None or select_count == 0):
        select_count = 1
    option_texts = [str(o.get("option_text") or "") for o in options]
    correct = [str(o.get("option_text") or "") for o in options if o.get("is_correct")]
    return {
        "question_type": qtype,
        "type": qtype,
        "select_count": select_count,
        "options": option_texts,
        "answers": correct,
        "explanation": question.get("explanation") or "",
    }


def validate_question_eligibility(
    question: Mapping[str, Any],
    options: Sequence[Mapping[str, Any]],
    *,
    exam_name: str,
    language_code: str = DEFAULT_FREE_MOCK_LANGUAGE,
) -> List[dict]:
    """Return structured failures for one candidate question."""
    failures: List[dict] = []
    qid = question.get("id")

    def _fail(code: str, message: str) -> None:
        failures.append({"code": code, "message": message})

    if str(question.get("exam_name") or "") != exam_name:
        _fail("EXAM_MISMATCH", f"question {qid} belongs to {question.get('exam_name')}")

    if str(question.get("language_code") or DEFAULT_FREE_MOCK_LANGUAGE) != language_code:
        _fail("LANGUAGE_MISMATCH", f"question {qid} language {question.get('language_code')}")

    if not question.get("is_active"):
        _fail("NOT_ACTIVE", f"question {qid} is not active")

    if not question.get("is_exam_eligible"):
        _fail("NOT_EXAM_ELIGIBLE", f"question {qid} is not exam eligible")

    if not question.get("mock_eligible", True):
        _fail("NOT_MOCK_ELIGIBLE", f"question {qid} is not mock eligible")

    if str(question.get("quality_status") or "") != "approved":
        _fail("NOT_APPROVED", f"question {qid} quality_status is {question.get('quality_status')}")

    if not str(question.get("explanation") or "").strip():
        _fail("MISSING_EXPLANATION", f"question {qid} has no explanation")

    if len(options or []) < 2:
        _fail("TOO_FEW_OPTIONS", f"question {qid} has fewer than 2 answer options")

    snapshot = build_runtime_question_snapshot(question, options)
    if not is_answer_key_valid(snapshot):
        _fail("INVALID_ANSWER_KEY", f"question {qid} has invalid answer key")

    return failures


def validate_draft_items_local(
    items: Sequence[Mapping[str, Any]],
    questions_by_id: Mapping[int, Mapping[str, Any]],
    options_by_question: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    exam_name: str,
    language_code: str = DEFAULT_FREE_MOCK_LANGUAGE,
) -> Tuple[bool, List[dict]]:
    """
    Pure validation mirroring collect_free_mock_draft_failures_v1 for unit tests.
    """
    failures: List[dict] = []

    def _fail(code: str, message: str) -> None:
        failures.append({"code": code, "message": message})

    try:
        blueprint = get_blueprint(exam_name)
    except FreeMockCurationError as exc:
        _fail("UNKNOWN_EXAM", str(exc))
        return False, failures

    normalized = normalize_draft_items(items)

    if len(normalized) != FREE_MOCK_SLOT_COUNT:
        _fail("ITEM_COUNT", f"expected exactly {FREE_MOCK_SLOT_COUNT} items, found {len(normalized)}")

    slots = [item["slot_order"] for item in normalized]
    if len(set(slots)) != len(slots):
        _fail("DUPLICATE_SLOT", "duplicate slot_order values")

    question_ids = [item["question_id"] for item in normalized]
    if len(set(question_ids)) != len(question_ids):
        _fail("DUPLICATE_QUESTION", "duplicate question_id values")

    for slot in range(1, FREE_MOCK_SLOT_COUNT + 1):
        if slot not in slots:
            _fail("MISSING_SLOT", f"missing slot_order {slot}")

    domain_counts: Counter = Counter()
    multi_count = 0

    for item in normalized:
        qid = item["question_id"]
        question = questions_by_id.get(qid)
        if question is None:
            _fail("QUESTION_NOT_FOUND", f"question {qid} not found")
            continue

        opts = options_by_question.get(qid) or []
        failures.extend(
            validate_question_eligibility(
                question,
                opts,
                exam_name=exam_name,
                language_code=language_code,
            )
        )

        domain = str(question.get("category") or item.get("domain_name") or "").strip()
        if domain:
            domain_counts[domain] += 1

        if str(question.get("question_type") or "").strip().lower() == "multiple":
            multi_count += 1

    for domain, required in blueprint.items():
        actual = domain_counts.get(domain, 0)
        if actual != required:
            _fail(
                "DOMAIN_COUNT",
                f"domain {domain} requires {required}, found {actual}",
            )

    if multi_count < FREE_MOCK_MIN_MULTI_SELECT:
        _fail(
            "MIN_MULTI_SELECT",
            f"expected at least {FREE_MOCK_MIN_MULTI_SELECT} multi-select questions, found {multi_count}",
        )

    return (len(failures) == 0), failures


def compare_domain_counts(
    items: Sequence[Mapping[str, Any]],
    questions_by_id: Mapping[int, Mapping[str, Any]],
    exam_name: str,
) -> List[dict]:
    """Return per-domain required/actual rows for admin UI."""
    blueprint = get_blueprint(exam_name)
    actual: Counter = Counter()
    for item in items or []:
        qid = int(item.get("question_id") or 0)
        question = questions_by_id.get(qid) or {}
        domain = str(question.get("category") or item.get("domain_name") or "").strip()
        if domain:
            actual[domain] += 1

    rows = []
    for domain, required in blueprint.items():
        rows.append(
            {
                "domain": domain,
                "required": required,
                "actual": actual.get(domain, 0),
                "delta": actual.get(domain, 0) - required,
            }
        )
    return rows


def count_multi_select(
    items: Sequence[Mapping[str, Any]],
    questions_by_id: Mapping[int, Mapping[str, Any]],
) -> int:
    count = 0
    for item in items or []:
        qid = int(item.get("question_id") or 0)
        question = questions_by_id.get(qid) or {}
        if str(question.get("question_type") or "").strip().lower() == "multiple":
            count += 1
    return count


def format_failures(failures: Sequence[Mapping[str, Any]]) -> List[str]:
    lines = []
    for item in failures or []:
        code = item.get("code") or "?"
        message = item.get("message") or ""
        lines.append(f"[{code}] {message}")
    return lines
