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
import os
import tempfile
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
DEFAULT_SNAPSHOT_PAGE_SIZE = 100
STATE_SCHEMA_VERSION = 1

DETERMINISTIC_JOB_TYPE = "deterministic_audit"
DUPLICATE_JOB_TYPE = "certification_duplicate_audit"
ENQUEUE_RPC = "enqueue_background_job_v1"

ACTIVE_JOB_STATUSES = frozenset({"pending", "leased", "running"})
RETRYABLE_JOB_STATUSES = frozenset({"failed", "dead_letter"})
DUPLICATE_SCAN_TYPE = "duplicate_question_stem"

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


class StructuralAuditEnqueueError(RuntimeError):
    """Raised when enqueue stops after partial progress."""

    def __init__(
        self,
        message: str,
        *,
        summary: "StructuralAuditSummary",
        failed_target: str,
    ) -> None:
        super().__init__(message)
        self.summary = summary
        self.failed_target = failed_target


@dataclass
class EnqueueState:
    certification_exam_names: List[str]
    ruleset_version: str
    created_by: str
    enqueued_version_ids: List[str] = field(default_factory=list)
    enqueued_duplicate_scans: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "certification_exam_names": list(self.certification_exam_names),
            "ruleset_version": self.ruleset_version,
            "created_by": self.created_by,
            "enqueued_version_ids": list(self.enqueued_version_ids),
            "enqueued_duplicate_scans": list(self.enqueued_duplicate_scans),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EnqueueState":
        duplicate_scans = []
        for entry in data.get("enqueued_duplicate_scans") or []:
            cert = str(entry.get("certification_exam_name") or "").strip()
            ruleset = str(entry.get("ruleset_version") or DEFAULT_RULESET_VERSION).strip()
            if cert:
                duplicate_scans.append(
                    {
                        "certification_exam_name": cert,
                        "ruleset_version": ruleset,
                    }
                )
        return cls(
            certification_exam_names=list(data.get("certification_exam_names") or []),
            ruleset_version=str(data.get("ruleset_version") or DEFAULT_RULESET_VERSION).strip(),
            created_by=str(data.get("created_by") or DEFAULT_CREATED_BY).strip(),
            enqueued_version_ids=[
                str(value) for value in (data.get("enqueued_version_ids") or []) if value
            ],
            enqueued_duplicate_scans=duplicate_scans,
        )

    @classmethod
    def initial_from_plan(cls, plan: "StructuralAuditPlan") -> "EnqueueState":
        return cls(
            certification_exam_names=list(plan.certification_exam_names),
            ruleset_version=plan.ruleset_version,
            created_by=plan.created_by,
        )

    def version_id_set(self) -> Set[str]:
        return set(self.enqueued_version_ids)

    def duplicate_key_set(self) -> Set[Tuple[str, str]]:
        return {
            (entry["certification_exam_name"], entry["ruleset_version"])
            for entry in self.enqueued_duplicate_scans
        }

    def record_deterministic(self, question_version_id: str) -> None:
        qvid = str(question_version_id).strip()
        if qvid and qvid not in self.enqueued_version_ids:
            self.enqueued_version_ids.append(qvid)

    def record_duplicate(self, certification_exam_name: str, ruleset_version: str) -> None:
        cert = str(certification_exam_name).strip()
        ruleset = str(ruleset_version or DEFAULT_RULESET_VERSION).strip()
        key = (cert, ruleset)
        if cert and key not in self.duplicate_key_set():
            self.enqueued_duplicate_scans.append(
                {
                    "certification_exam_name": cert,
                    "ruleset_version": ruleset,
                }
            )


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
    skipped_resume_duplicate: List[str] = field(default_factory=list)
    skipped_completed_deterministic: List[str] = field(default_factory=list)
    skipped_completed_duplicate: List[str] = field(default_factory=list)
    retryable_failed_deterministic: List[str] = field(default_factory=list)
    retryable_failed_duplicate: List[str] = field(default_factory=list)
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
    completed_audit_skipped: int
    retryable_failed_deterministic: int
    new_deterministic_jobs: int
    duplicate_scans_to_enqueue: int
    duplicate_scans_skipped: int
    duplicate_completed_audit_skipped: int
    retryable_failed_duplicate: int
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


def extract_retryable_job_keys(
    jobs: Iterable[dict],
    *,
    ruleset_version: str,
) -> Tuple[Set[Tuple[str, str]], Set[Tuple[str, str]]]:
    """Return failed/dead-letter deterministic and duplicate job keys for one ruleset."""
    ruleset = str(ruleset_version or DEFAULT_RULESET_VERSION).strip()
    deterministic: Set[Tuple[str, str]] = set()
    duplicate: Set[Tuple[str, str]] = set()
    for job in jobs:
        status = str(job.get("job_status") or "").strip()
        if status not in RETRYABLE_JOB_STATUSES:
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


def _is_duplicate_scan_audit_row(row: dict) -> bool:
    metadata = row.get("metadata") or {}
    return str(metadata.get("scan_type") or "").strip() == DUPLICATE_SCAN_TYPE


def load_completed_deterministic_audit_keys(
    client,
    question_version_ids: Sequence[str],
    *,
    ruleset_version: str,
    page_size: int = DEFAULT_SNAPSHOT_PAGE_SIZE,
) -> Set[Tuple[str, str]]:
    """Return completed per-version deterministic audit keys, excluding duplicate scans."""
    ruleset = str(ruleset_version or DEFAULT_RULESET_VERSION).strip()
    keys: Set[Tuple[str, str]] = set()
    for chunk in _paginate_ids(question_version_ids, page_size):
        result = (
            client.table("audit_runs")
            .select("target_question_version_id, ruleset_version, metadata")
            .eq("audit_type", "deterministic")
            .eq("run_status", "completed")
            .eq("ruleset_version", ruleset)
            .in_("target_question_version_id", chunk)
            .execute()
        )
        for row in result.data or []:
            if _is_duplicate_scan_audit_row(row):
                continue
            qvid = row.get("target_question_version_id")
            if qvid:
                keys.add((str(qvid), ruleset))
    return keys


def load_completed_duplicate_audit_keys(
    client,
    certification_exam_names: Sequence[str],
    *,
    ruleset_version: str,
) -> Set[Tuple[str, str]]:
    """Return completed certification duplicate scan keys from audit_runs metadata."""
    ruleset = str(ruleset_version or DEFAULT_RULESET_VERSION).strip()
    keys: Set[Tuple[str, str]] = set()
    for exam_name in certification_exam_names:
        cert = str(exam_name or "").strip()
        if not cert:
            continue
        result = (
            client.table("audit_runs")
            .select("ruleset_version, metadata")
            .eq("audit_type", "deterministic")
            .eq("run_status", "completed")
            .eq("ruleset_version", ruleset)
            .filter("metadata->>scan_type", "eq", DUPLICATE_SCAN_TYPE)
            .filter("metadata->>certification_exam_name", "eq", cert)
            .execute()
        )
        if result.data:
            keys.add((cert, ruleset))
    return keys


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


def load_resume_enqueued_version_ids(state: Optional[dict]) -> Set[str]:
    if not state:
        return set()
    values = state.get("enqueued_version_ids") or []
    return {str(value) for value in values if value}


def load_resume_enqueued_duplicate_keys(state: Optional[dict]) -> Set[Tuple[str, str]]:
    if not state:
        return set()
    keys: Set[Tuple[str, str]] = set()
    for entry in state.get("enqueued_duplicate_scans") or []:
        cert = str(entry.get("certification_exam_name") or "").strip()
        ruleset = str(entry.get("ruleset_version") or DEFAULT_RULESET_VERSION).strip()
        if cert:
            keys.add((cert, ruleset))
    return keys


def _paginate_ids(ids: Sequence[str], page_size: int) -> Iterable[List[str]]:
    if page_size <= 0:
        raise StructuralAuditLauncherError("page_size must be > 0")
    unique_ids = list(dict.fromkeys(str(value) for value in ids if value))
    for index in range(0, len(unique_ids), page_size):
        yield unique_ids[index : index + page_size]


def _snapshot_from_rows(version_row: dict, option_rows: Sequence[dict]) -> dict:
    ordered_options = sorted(
        option_rows,
        key=lambda row: (
            row.get("display_order") if row.get("display_order") is not None else 0,
            str(row.get("option_label") or ""),
        ),
    )
    options = [
        {
            "option_label": row.get("option_label"),
            "option_text": row.get("option_text"),
            "is_correct": bool(row.get("is_correct")),
            "display_order": row.get("display_order"),
        }
        for row in ordered_options
    ]
    return {
        "question_text": version_row.get("question_text") or "",
        "explanation": version_row.get("explanation") or "",
        "question_type": version_row.get("question_type") or "single",
        "select_count": version_row.get("select_count"),
        "options": options,
    }


def load_question_version_snapshots_bulk(
    client,
    question_version_ids: Sequence[str],
    *,
    page_size: int = DEFAULT_SNAPSHOT_PAGE_SIZE,
) -> Dict[str, dict]:
    """Load immutable version snapshots in paginated bulk queries."""
    unique_ids = list(dict.fromkeys(str(value) for value in question_version_ids if value))
    if not unique_ids:
        return {}

    versions_by_id: Dict[str, dict] = {}
    options_by_version: Dict[str, List[dict]] = {qvid: [] for qvid in unique_ids}

    for chunk in _paginate_ids(unique_ids, page_size):
        version_rows = (
            client.table("question_versions")
            .select("id, question_text, explanation, question_type, select_count")
            .in_("id", chunk)
            .execute()
        ).data or []
        for row in version_rows:
            versions_by_id[str(row.get("id"))] = row

        option_rows = (
            client.table("question_option_versions")
            .select(
                "question_version_id, option_label, option_text, is_correct, display_order"
            )
            .in_("question_version_id", chunk)
            .order("display_order")
            .execute()
        ).data or []
        for row in option_rows:
            qvid = str(row.get("question_version_id"))
            options_by_version.setdefault(qvid, []).append(row)

    snapshots: Dict[str, dict] = {}
    for qvid in unique_ids:
        version_row = versions_by_id.get(qvid)
        if not version_row:
            raise StructuralAuditLauncherError(
                f"question_version_id {qvid!r} not found"
            )
        snapshots[qvid] = _snapshot_from_rows(version_row, options_by_version.get(qvid, []))
    return snapshots


def load_question_version_snapshot(client, question_version_id: str) -> dict:
    snapshots = load_question_version_snapshots_bulk(
        client,
        [question_version_id],
        page_size=1,
    )
    qvid = str(question_version_id)
    if qvid not in snapshots:
        raise StructuralAuditLauncherError(f"question_version_id {qvid!r} not found")
    return snapshots[qvid]


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


def build_structural_audit_plan(
    client,
    *,
    certification_scope: str,
    ruleset_version: str = DEFAULT_RULESET_VERSION,
    created_by: str = DEFAULT_CREATED_BY,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_questions: Optional[int] = None,
    background_jobs: Optional[Iterable[dict]] = None,
    resume_state: Optional[dict] = None,
    completed_deterministic_audits: Optional[Set[Tuple[str, str]]] = None,
    completed_duplicate_audits: Optional[Set[Tuple[str, str]]] = None,
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

    jobs = list(background_jobs or [])
    active_deterministic, active_duplicate = extract_active_job_keys(
        jobs,
        ruleset_version=ruleset_version,
    )
    retryable_deterministic, retryable_duplicate = extract_retryable_job_keys(
        jobs,
        ruleset_version=ruleset_version,
    )
    resume_ids = load_resume_enqueued_version_ids(resume_state)
    resume_duplicate_keys = load_resume_enqueued_duplicate_keys(resume_state)

    if completed_deterministic_audits is None:
        completed_deterministic_audits = load_completed_deterministic_audit_keys(
            client,
            [target.question_version_id for target in version_targets],
            ruleset_version=ruleset_version,
        )
    if completed_duplicate_audits is None:
        completed_duplicate_audits = load_completed_duplicate_audit_keys(
            client,
            certification_exam_names,
            ruleset_version=ruleset_version,
        )

    skipped_pending_det: List[str] = []
    skipped_resume: List[str] = []
    skipped_completed_det: List[str] = []
    to_enqueue: List[VersionTarget] = []

    for target in version_targets:
        key = (target.question_version_id, ruleset_version)
        if key in active_deterministic:
            skipped_pending_det.append(target.question_version_id)
            continue
        if target.question_version_id in resume_ids:
            skipped_resume.append(target.question_version_id)
            continue
        if key in completed_deterministic_audits:
            skipped_completed_det.append(target.question_version_id)
            continue
        to_enqueue.append(target)

    retryable_failed_det = sorted(
        qvid
        for qvid, _ruleset in retryable_deterministic
        if any(target.question_version_id == qvid for target in to_enqueue)
    )

    skipped_dup: List[str] = []
    skipped_resume_dup: List[str] = []
    skipped_completed_dup: List[str] = []
    duplicate_to_enqueue: List[str] = []
    for exam_name in certification_exam_names:
        dup_key = (exam_name, ruleset_version)
        if dup_key in active_duplicate:
            skipped_dup.append(exam_name)
        elif dup_key in resume_duplicate_keys:
            skipped_resume_dup.append(exam_name)
        elif dup_key in completed_duplicate_audits:
            skipped_completed_dup.append(exam_name)
        else:
            duplicate_to_enqueue.append(exam_name)

    retryable_failed_dup = sorted(
        cert
        for cert, _ruleset in retryable_duplicate
        if cert in duplicate_to_enqueue
    )

    return StructuralAuditPlan(
        certification_exam_names=certification_exam_names,
        version_targets=version_targets,
        batch_size=batch_size,
        ruleset_version=ruleset_version,
        created_by=created_by,
        missing_version_question_ids=missing_version_ids,
        skipped_pending_deterministic=skipped_pending_det,
        skipped_resume_deterministic=skipped_resume,
        skipped_resume_duplicate=skipped_resume_dup,
        skipped_completed_deterministic=skipped_completed_det,
        skipped_completed_duplicate=skipped_completed_dup,
        retryable_failed_deterministic=retryable_failed_det,
        retryable_failed_duplicate=retryable_failed_dup,
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
        completed_audit_skipped=len(plan.skipped_completed_deterministic),
        retryable_failed_deterministic=len(plan.retryable_failed_deterministic),
        new_deterministic_jobs=len(plan.deterministic_jobs_to_enqueue),
        duplicate_scans_to_enqueue=len(plan.duplicate_certifications_to_enqueue),
        duplicate_scans_skipped=(
            len(plan.skipped_pending_duplicate)
            + len(plan.skipped_resume_duplicate)
            + len(plan.skipped_completed_duplicate)
        ),
        duplicate_completed_audit_skipped=len(plan.skipped_completed_duplicate),
        retryable_failed_duplicate=len(plan.retryable_failed_duplicate),
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
        f"completed deterministic audits skipped: {summary.completed_audit_skipped}",
        f"retryable failed/dead-letter deterministic jobs: {summary.retryable_failed_deterministic}",
        f"new deterministic jobs to enqueue: {summary.new_deterministic_jobs}",
        f"deterministic batch size: {summary.batch_size}",
        f"deterministic batch count: {summary.batch_count}",
        f"duplicate scans to enqueue: {summary.duplicate_scans_to_enqueue}",
        f"duplicate scans skipped (total): {summary.duplicate_scans_skipped}",
        f"completed duplicate audits skipped: {summary.duplicate_completed_audit_skipped}",
        f"retryable failed/dead-letter duplicate scans: {summary.retryable_failed_duplicate}",
        f"mode: {'dry-run' if summary.dry_run else 'enqueue'}",
    ]
    if (
        plan.skipped_pending_duplicate
        or plan.skipped_resume_duplicate
        or plan.skipped_completed_duplicate
    ):
        skipped = (
            plan.skipped_pending_duplicate
            + plan.skipped_resume_duplicate
            + plan.skipped_completed_duplicate
        )
        lines.append(
            "duplicate scans skipped for: "
            + ", ".join(sorted(set(skipped)))
        )
    return "\n".join(lines)


def _write_progress(
    progress_writer: Optional[Callable[[str], None]],
    message: str,
) -> None:
    if progress_writer:
        progress_writer(message)


def atomic_write_enqueue_state(path: str, state: EnqueueState) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".structural_audit_state.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def execute_structural_audit_plan(
    client,
    plan: StructuralAuditPlan,
    *,
    dry_run: bool,
    state_file: Optional[str] = None,
    progress_writer: Optional[Callable[[str], None]] = print,
    bulk_load_snapshots: Callable[..., Dict[str, dict]] = load_question_version_snapshots_bulk,
    enqueue_deterministic_fn: Callable = enqueue_deterministic_audit_job,
    enqueue_duplicate_fn: Callable = enqueue_certification_duplicate_audit_job,
) -> StructuralAuditSummary:
    summary = summarize_plan(plan, dry_run=dry_run)
    if dry_run:
        return summary

    version_ids = [
        target.question_version_id
        for target in plan.deterministic_jobs_to_enqueue
    ]
    snapshots = bulk_load_snapshots(client, version_ids)

    enqueue_state: Optional[EnqueueState] = None
    if state_file and (
        plan.deterministic_jobs_to_enqueue or plan.duplicate_certifications_to_enqueue
    ):
        enqueue_state = EnqueueState.initial_from_plan(plan)
        atomic_write_enqueue_state(state_file, enqueue_state)

    total_deterministic = plan.selected_live_questions
    completed_deterministic = (
        len(plan.skipped_pending_deterministic)
        + len(plan.skipped_resume_deterministic)
        + len(plan.skipped_completed_deterministic)
    )

    enqueued_this_run = 0

    for batch in batch_items(plan.deterministic_jobs_to_enqueue, plan.batch_size):
        for target in batch:
            snapshot = snapshots[target.question_version_id]
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
            try:
                enqueue_deterministic_fn(client, params)
            except Exception as exc:
                summary.enqueued_deterministic_jobs = enqueued_this_run
                raise StructuralAuditEnqueueError(
                    f"failed to enqueue deterministic audit for question_version_id="
                    f"{target.question_version_id}: {exc}",
                    summary=summary,
                    failed_target=target.question_version_id,
                ) from exc

            enqueued_this_run += 1
            completed_deterministic += 1
            summary.enqueued_deterministic_jobs = enqueued_this_run
            if enqueue_state and state_file:
                enqueue_state.record_deterministic(target.question_version_id)
                atomic_write_enqueue_state(state_file, enqueue_state)
            _write_progress(
                progress_writer,
                f"deterministic jobs: {completed_deterministic}/{total_deterministic}",
            )

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
        try:
            enqueue_duplicate_fn(client, params)
        except Exception as exc:
            raise StructuralAuditEnqueueError(
                f"failed to enqueue duplicate scan for certification="
                f"{exam_name}: {exc}",
                summary=summary,
                failed_target=exam_name,
            ) from exc

        summary.enqueued_duplicate_scans += 1
        if enqueue_state and state_file:
            enqueue_state.record_duplicate(exam_name, plan.ruleset_version)
            atomic_write_enqueue_state(state_file, enqueue_state)
        _write_progress(
            progress_writer,
            f"duplicate scans: {summary.enqueued_duplicate_scans}/"
            f"{len(plan.duplicate_certifications_to_enqueue)}",
        )

    return summary


def load_state_file(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_resume_state(state_file: Optional[str], *, resume: bool) -> Optional[dict]:
    if not resume:
        return None
    if not state_file:
        raise StructuralAuditLauncherError("--resume requires --state-file")
    if not os.path.exists(state_file):
        return {}
    return load_state_file(state_file)
