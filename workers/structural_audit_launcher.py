"""
Plan and enqueue structural full-bank audits (deterministic + lexical duplicate).

Uses existing ``list_certification_current_question_versions_v1`` for version
selection, ``deterministic_audit`` background jobs for per-version structural
checks, and ``certification_duplicate_audit`` for one scan per certification.

No LLM calls. Read-only Supabase table access is limited to loading version
snapshots and inspecting active background jobs for idempotency.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from workers.certification_question_loader import load_certification_current_question_versions
from workers.run_certification_duplicate_audit import (
    build_enqueue_params as build_duplicate_enqueue_params,
    build_payload as build_duplicate_payload,
    enqueue_certification_duplicate_audit_job,
)

ADM_EXAM_NAME = "Salesforce Certified Platform Administrator"
BA_EXAM_NAME = "Salesforce Certified Business Analyst"

DEFAULT_RULESET_VERSION = "1.0.0"
DEFAULT_BATCH_SIZE = 25
DEFAULT_CREATED_BY = "certbound-structural-audit-launcher"

DETERMINISTIC_JOB_TYPE = "deterministic_audit"
DUPLICATE_JOB_TYPE = "certification_duplicate_audit"
ENQUEUE_RPC = "enqueue_background_job_v1"

ACTIVE_JOB_STATUSES = frozenset({"pending", "leased", "running"})

CERTIFICATION_ALIASES = {
    "adm": ADM_EXAM_NAME,
    "adm-201": ADM_EXAM_NAME,
    "ba": BA_EXAM_NAME,
    "ba-201": BA_EXAM_NAME,
}

KNOWN_EXAM_NAMES = frozenset({ADM_EXAM_NAME, BA_EXAM_NAME})


class StructuralAuditLauncherError(ValueError):
    """Raised when launcher input or selection validation fails."""


class UnknownCertificationError(StructuralAuditLauncherError):
    """Raised when a certification scope is not supported."""


class MalformedSelectionError(StructuralAuditLauncherError):
    """Raised when current-version selection is inconsistent or incomplete."""


@dataclass(frozen=True)
class VersionTarget:
    certification_exam_name: str
    question_id: int
    question_version_id: str
    version_number: Optional[int] = None


@dataclass
class StructuralAuditPlan:
    certification_exam_names: List[str]
    version_targets: List[VersionTarget]
    batch_size: int
    ruleset_version: str
    created_by: str
    missing_version_question_ids: List[int] = field(default_factory=list)
    skipped_pending_deterministic: List[str] = field(default_factory=list)
    skipped_pending_duplicate: List[str] = field(default_factory=list)
    skipped_resume_deterministic: List[str] = field(default_factory=list)
    deterministic_jobs_to_enqueue: List[VersionTarget] = field(default_factory=list)
    duplicate_certifications_to_enqueue: List[str] = field(default_factory=list)

    @property
    def selected_live_questions(self) -> int:
        return len(self.version_targets)

    @property
    def current_versions_found(self) -> int:
        return len(self.version_targets)

    @property
    def batch_count(self) -> int:
        count = len(self.deterministic_jobs_to_enqueue)
        if count == 0:
            return 0
        return (count + self.batch_size - 1) // self.batch_size


@dataclass
class StructuralAuditSummary:
    certification_exam_names: List[str]
    selected_live_questions: int
    current_versions_found: int
    already_queued_running_skipped: int
    resume_skipped: int
    new_deterministic_jobs: int
    duplicate_scans_to_enqueue: int
    duplicate_scans_skipped: int
    questions_missing_current_versions: int
    batch_count: int
    batch_size: int
    ruleset_version: str
    dry_run: bool
    enqueued_deterministic_jobs: int = 0
    enqueued_duplicate_scans: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def resolve_certification_scope(scope: str) -> List[str]:
    """Map adm/ba/both aliases to supported exam_name values."""
    raw = str(scope or "").strip()
    if not raw:
        raise UnknownCertificationError("certification scope must not be empty")

    normalized = raw.lower()
    if normalized == "both":
        return [ADM_EXAM_NAME, BA_EXAM_NAME]
    if normalized in CERTIFICATION_ALIASES:
        return [CERTIFICATION_ALIASES[normalized]]
    if raw in KNOWN_EXAM_NAMES:
        return [raw]
    raise UnknownCertificationError(
        f"unsupported certification scope {scope!r}; "
        f"use adm, ba, both, or a known exam_name"
    )


def batch_items(items: Sequence[Any], batch_size: int) -> List[List[Any]]:
    if batch_size <= 0:
        raise StructuralAuditLauncherError("batch_size must be > 0")
    return [list(items[i : i + batch_size]) for i in range(0, len(items), batch_size)]


def _deterministic_job_key(payload: dict) -> Optional[Tuple[str, str]]:
    qvid = payload.get("target_question_version_id")
    if not qvid:
        return None
    ruleset = str(payload.get("ruleset_version") or DEFAULT_RULESET_VERSION).strip()
    return str(qvid), ruleset


def _duplicate_job_key(payload: dict) -> Optional[Tuple[str, str]]:
    cert = str(payload.get("certification_exam_name") or "").strip()
    if not cert:
        return None
    ruleset = str(payload.get("ruleset_version") or DEFAULT_RULESET_VERSION).strip()
    return cert, ruleset


def extract_active_job_keys(
    jobs: Iterable[dict],
    *,
    ruleset_version: str,
) -> Tuple[Set[Tuple[str, str]], Set[Tuple[str, str]]]:
    """Return active deterministic and duplicate job keys for one ruleset."""
    ruleset = str(ruleset_version or DEFAULT_RULESET_VERSION).strip()
    deterministic: Set[Tuple[str, str]] = set()
    duplicate: Set[Tuple[str, str]] = set()
    for job in jobs:
        status = str(job.get("job_status") or "").strip()
        if status not in ACTIVE_JOB_STATUSES:
            continue
        payload = job.get("payload") or {}
        job_type = str(job.get("job_type") or "").strip()
        if job_type == DETERMINISTIC_JOB_TYPE:
            key = _deterministic_job_key(payload)
            if key and key[1] == ruleset:
                deterministic.add(key)
        elif job_type == DUPLICATE_JOB_TYPE:
            key = _duplicate_job_key(payload)
            if key and key[1] == ruleset:
                duplicate.add(key)
    return deterministic, duplicate


def _rows_to_targets(rows: List[dict], certification_exam_name: str) -> Tuple[List[VersionTarget], List[int]]:
    targets: List[VersionTarget] = []
    missing_version: List[int] = []
    seen_question_ids: Dict[int, str] = {}

    for row in rows:
        question_id = row.get("question_id")
        qvid = row.get("question_version_id")
        if question_id is None:
            continue
        qid = int(question_id)
        if not qvid:
            missing_version.append(qid)
            continue
        qvid_str = str(qvid)
        if qid in seen_question_ids and seen_question_ids[qid] != qvid_str:
            raise MalformedSelectionError(
                f"duplicate current-version selection for question_id={qid}"
            )
        seen_question_ids[qid] = qvid_str
        targets.append(
            VersionTarget(
                certification_exam_name=certification_exam_name,
                question_id=qid,
                question_version_id=qvid_str,
                version_number=row.get("version_number"),
            )
        )
    return targets, missing_version


def load_active_question_ids(client, certification_exam_name: str) -> Set[int]:
    result = (
        client.table("questions")
        .select("id")
        .eq("exam_name", certification_exam_name)
        .eq("is_active", True)
        .execute()
    )
    return {int(row["id"]) for row in (result.data or []) if row.get("id") is not None}


def load_version_targets_for_certifications(
    client,
    certification_exam_names: Sequence[str],
) -> Tuple[List[VersionTarget], List[int]]:
    all_targets: List[VersionTarget] = []
    all_missing: List[int] = []
    for exam_name in certification_exam_names:
        rows = load_certification_current_question_versions(client, exam_name)
        targets, missing = _rows_to_targets(rows, exam_name)
        active_ids = load_active_question_ids(client, exam_name)
        target_ids = {target.question_id for target in targets}
        uncovered = sorted(active_ids - target_ids)
        all_missing.extend(missing)
        all_missing.extend(uncovered)
        all_targets.extend(targets)
    all_targets.sort(key=lambda item: (item.certification_exam_name, item.question_id))
    return all_targets, sorted(set(all_missing))


def apply_max_questions(
    targets: Sequence[VersionTarget],
    max_questions: Optional[int],
) -> List[VersionTarget]:
    if max_questions is None:
        return list(targets)
    if max_questions <= 0:
        raise StructuralAuditLauncherError("max_questions must be > 0 when provided")
    return list(targets[:max_questions])


def load_question_version_snapshot(client, question_version_id: str) -> dict:
    version_rows = (
        client.table("question_versions")
        .select("question_text, explanation, question_type, select_count")
        .eq("id", question_version_id)
        .execute()
    ).data or []
    if not version_rows:
        raise StructuralAuditLauncherError(
            f"question_version_id {question_version_id!r} not found"
        )
    version = version_rows[0]
    option_rows = (
        client.table("question_option_versions")
        .select("option_label, option_text, is_correct, display_order")
        .eq("question_version_id", question_version_id)
        .order("display_order")
        .execute()
    ).data or []
    options = [
        {
            "option_label": row.get("option_label"),
            "option_text": row.get("option_text"),
            "is_correct": bool(row.get("is_correct")),
            "display_order": row.get("display_order"),
        }
        for row in option_rows
    ]
    return {
        "question_text": version.get("question_text") or "",
        "explanation": version.get("explanation") or "",
        "question_type": version.get("question_type") or "single",
        "select_count": version.get("select_count"),
        "options": options,
    }


def build_deterministic_audit_payload(
    target: VersionTarget,
    *,
    question_snapshot: dict,
    created_by: str,
    ruleset_version: str,
) -> dict:
    return {
        "target_question_version_id": target.question_version_id,
        "created_by": created_by,
        "ruleset_version": ruleset_version,
        "question": question_snapshot,
        "metadata": {
            "certification_exam_name": target.certification_exam_name,
            "question_id": target.question_id,
            "launcher": "structural_full_bank_audit",
        },
    }


def build_deterministic_enqueue_params(
    payload: dict,
    *,
    created_by: str,
    priority: int = 100,
    max_attempts: int = 3,
    available_at: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Dict[str, Any]:
    if available_at is None:
        available_at = datetime.now(timezone.utc).isoformat()
    return {
        "p_job_type": DETERMINISTIC_JOB_TYPE,
        "p_payload": payload,
        "p_priority": priority,
        "p_max_attempts": max_attempts,
        "p_available_at": available_at,
        "p_created_by": created_by,
        "p_model_name": None,
        "p_prompt_version": None,
        "p_estimated_cost_usd": None,
        "p_metadata": metadata or {},
    }


def enqueue_deterministic_audit_job(client, params: dict) -> dict:
    result = client.rpc(ENQUEUE_RPC, params).execute()
    if getattr(result, "error", None):
        raise RuntimeError(f"RPC {ENQUEUE_RPC!r} returned error: {result.error}")
    rows = result.data or []
    if not rows:
        raise RuntimeError(f"RPC {ENQUEUE_RPC!r} returned no rows")
    return rows[0]


def load_resume_enqueued_version_ids(state: Optional[dict]) -> Set[str]:
    if not state:
        return set()
    values = state.get("enqueued_version_ids") or []
    return {str(value) for value in values if value}


def build_structural_audit_plan(
    client,
    *,
    certification_scope: str,
    ruleset_version: str = DEFAULT_RULESET_VERSION,
    created_by: str = DEFAULT_CREATED_BY,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_questions: Optional[int] = None,
    active_jobs: Optional[Iterable[dict]] = None,
    resume_state: Optional[dict] = None,
) -> StructuralAuditPlan:
    certification_exam_names = resolve_certification_scope(certification_scope)
    version_targets, missing_version_ids = load_version_targets_for_certifications(
        client,
        certification_exam_names,
    )
    if missing_version_ids:
        raise MalformedSelectionError(
            "active questions missing current question_version_id: "
            + ", ".join(str(qid) for qid in missing_version_ids)
        )

    version_targets = apply_max_questions(version_targets, max_questions)

    active_deterministic, active_duplicate = extract_active_job_keys(
        active_jobs or [],
        ruleset_version=ruleset_version,
    )
    resume_ids = load_resume_enqueued_version_ids(resume_state)

    skipped_pending_det: List[str] = []
    skipped_resume: List[str] = []
    to_enqueue: List[VersionTarget] = []

    for target in version_targets:
        key = (target.question_version_id, ruleset_version)
        if key in active_deterministic:
            skipped_pending_det.append(target.question_version_id)
            continue
        if target.question_version_id in resume_ids:
            skipped_resume.append(target.question_version_id)
            continue
        to_enqueue.append(target)

    skipped_dup: List[str] = []
    duplicate_to_enqueue: List[str] = []
    for exam_name in certification_exam_names:
        dup_key = (exam_name, ruleset_version)
        if dup_key in active_duplicate:
            skipped_dup.append(exam_name)
        else:
            duplicate_to_enqueue.append(exam_name)

    return StructuralAuditPlan(
        certification_exam_names=certification_exam_names,
        version_targets=version_targets,
        batch_size=batch_size,
        ruleset_version=ruleset_version,
        created_by=created_by,
        missing_version_question_ids=missing_version_ids,
        skipped_pending_deterministic=skipped_pending_det,
        skipped_resume_deterministic=skipped_resume,
        deterministic_jobs_to_enqueue=to_enqueue,
        duplicate_certifications_to_enqueue=duplicate_to_enqueue,
        skipped_pending_duplicate=skipped_dup,
    )


def summarize_plan(plan: StructuralAuditPlan, *, dry_run: bool) -> StructuralAuditSummary:
    return StructuralAuditSummary(
        certification_exam_names=list(plan.certification_exam_names),
        selected_live_questions=plan.selected_live_questions,
        current_versions_found=plan.current_versions_found,
        already_queued_running_skipped=len(plan.skipped_pending_deterministic),
        resume_skipped=len(plan.skipped_resume_deterministic),
        new_deterministic_jobs=len(plan.deterministic_jobs_to_enqueue),
        duplicate_scans_to_enqueue=len(plan.duplicate_certifications_to_enqueue),
        duplicate_scans_skipped=len(plan.skipped_pending_duplicate),
        questions_missing_current_versions=len(plan.missing_version_question_ids),
        batch_count=plan.batch_count,
        batch_size=plan.batch_size,
        ruleset_version=plan.ruleset_version,
        dry_run=dry_run,
    )


def format_human_report(plan: StructuralAuditPlan, summary: StructuralAuditSummary) -> str:
    lines = [
        "CertBound structural full-bank audit plan",
        f"certifications: {', '.join(summary.certification_exam_names)}",
        f"ruleset_version: {summary.ruleset_version}",
        f"selected live questions: {summary.selected_live_questions}",
        f"current versions found: {summary.current_versions_found}",
        f"questions missing current versions: {summary.questions_missing_current_versions}",
        f"already queued/running deterministic jobs skipped: {summary.already_queued_running_skipped}",
        f"resume skipped deterministic jobs: {summary.resume_skipped}",
        f"new deterministic jobs to enqueue: {summary.new_deterministic_jobs}",
        f"deterministic batch size: {summary.batch_size}",
        f"deterministic batch count: {summary.batch_count}",
        f"duplicate scans to enqueue: {summary.duplicate_scans_to_enqueue}",
        f"duplicate scans skipped (active jobs): {summary.duplicate_scans_skipped}",
        f"mode: {'dry-run' if summary.dry_run else 'enqueue'}",
    ]
    if plan.skipped_pending_duplicate:
        lines.append(
            "duplicate scans skipped for: "
            + ", ".join(plan.skipped_pending_duplicate)
        )
    return "\n".join(lines)


def execute_structural_audit_plan(
    client,
    plan: StructuralAuditPlan,
    *,
    dry_run: bool,
    load_snapshot: Callable[[str], dict] = None,
) -> StructuralAuditSummary:
    summary = summarize_plan(plan, dry_run=dry_run)
    if dry_run:
        return summary

    snapshot_loader = load_snapshot or (
        lambda qvid: load_question_version_snapshot(client, qvid)
    )

    for batch in batch_items(plan.deterministic_jobs_to_enqueue, plan.batch_size):
        for target in batch:
            snapshot = snapshot_loader(target.question_version_id)
            payload = build_deterministic_audit_payload(
                target,
                question_snapshot=snapshot,
                created_by=plan.created_by,
                ruleset_version=plan.ruleset_version,
            )
            params = build_deterministic_enqueue_params(
                payload,
                created_by=plan.created_by,
            )
            enqueue_deterministic_audit_job(client, params)
            summary.enqueued_deterministic_jobs += 1

    for exam_name in plan.duplicate_certifications_to_enqueue:
        payload = build_duplicate_payload(
            certification_exam_name=exam_name,
            created_by=plan.created_by,
            ruleset_version=plan.ruleset_version,
            metadata={"launcher": "structural_full_bank_audit"},
        )
        params = build_duplicate_enqueue_params(
            payload,
            created_by=plan.created_by,
        )
        enqueue_certification_duplicate_audit_job(client, params)
        summary.enqueued_duplicate_scans += 1

    return summary


def load_state_file(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_state_file(path: str, *, plan: StructuralAuditPlan, summary: StructuralAuditSummary) -> None:
    payload = {
        "certification_exam_names": plan.certification_exam_names,
        "ruleset_version": plan.ruleset_version,
        "enqueued_version_ids": [
            target.question_version_id
            for target in plan.deterministic_jobs_to_enqueue
        ],
        "summary": summary.to_dict(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
