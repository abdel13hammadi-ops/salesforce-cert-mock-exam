"""
Deterministic smoke-question selection for the CertBound AI quality-audit pilot.

Read-only Supabase access. No AI calls, enqueues, or writes.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from workers.certification_question_loader import load_certification_current_question_versions
from workers.structural_audit_launcher import (
    ADM_EXAM_NAME,
    BA_EXAM_NAME,
    load_question_version_snapshots_bulk,
)

DEFAULT_SMOKE_SEED = 42
QUESTIONS_PER_CERTIFICATION = 5

PILOT_CERTIFICATIONS: tuple[str, ...] = (ADM_EXAM_NAME, BA_EXAM_NAME)


class QualityAuditPilotError(ValueError):
    """Raised when smoke selection cannot be completed deterministically."""


@dataclass(frozen=True)
class SmokeSelectedQuestion:
    certification_exam_name: str
    rank: int
    question_version_id: str
    question_id: int
    category: str
    domain_name: str
    question_type: str
    select_count: Optional[int]
    question_text: str
    options: List[Dict[str, Any]]
    rank_key: str


@dataclass(frozen=True)
class CertificationSmokeSelection:
    certification_exam_name: str
    seed: int
    selected: List[SmokeSelectedQuestion]
    domain_allocation: Dict[str, int] = field(default_factory=dict)
    domain_weights: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class QualityAuditSmokeSelection:
    seed: int
    certifications: List[CertificationSmokeSelection]


def load_certification_domain_weights(client, exam_name: str) -> Dict[str, float]:
    """Load active domain weights for one certification."""
    cert = str(exam_name or "").strip()
    if not cert:
        raise QualityAuditPilotError("certification_exam_name must not be empty")

    result = (
        client.table("certification_domains")
        .select("domain_name,weight")
        .eq("exam_name", cert)
        .eq("is_active", True)
        .execute()
    )
    if getattr(result, "error", None):
        raise RuntimeError(
            f"certification_domains lookup failed: {result.error}"
        )

    weights: Dict[str, float] = {}
    for row in result.data or []:
        name = str(row.get("domain_name") or "").strip()
        if not name:
            continue
        try:
            weight = float(row.get("weight") or 0)
        except (TypeError, ValueError):
            continue
        if weight > 0:
            weights[name] = weight
    return weights


def _rank_key(seed: int, certification: str, scope: str, question_version_id: str) -> str:
    key = f"{seed}|{certification}|{scope}".encode("utf-8")
    msg = str(question_version_id).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def is_multi_select(question_type: Optional[str], select_count: Optional[int]) -> bool:
    if str(question_type or "").strip().lower() == "multiple":
        return True
    return isinstance(select_count, int) and select_count > 1


def public_options(options: Sequence[dict]) -> List[Dict[str, Any]]:
    """Return option rows safe for pilot display (no correctness metadata)."""
    cleaned: List[Dict[str, Any]] = []
    for row in sorted(options or [], key=lambda item: item.get("display_order") or 0):
        cleaned.append({
            "option_label": row.get("option_label"),
            "option_text": row.get("option_text"),
            "display_order": row.get("display_order"),
        })
    return cleaned


def allocate_weighted_domain_slots(
    domain_weights: Mapping[str, float],
    available_by_domain: Mapping[str, int],
    total_slots: int,
) -> Dict[str, int]:
    """
    Allocate integer slots across domains using largest-remainder weighting.

    Only domains with positive weight and available inventory participate.
    Allocation is capped by availability and shortfalls are redistributed
    deterministically (higher weight first, then domain name).
    """
    if total_slots <= 0:
        return {}

    eligible_domains = sorted(
        domain
        for domain, weight in domain_weights.items()
        if weight > 0 and available_by_domain.get(domain, 0) > 0
    )
    if not eligible_domains:
        return {}

    total_weight = sum(domain_weights[domain] for domain in eligible_domains)
    exact = {
        domain: (total_slots * domain_weights[domain] / total_weight)
        for domain in eligible_domains
    }
    allocation = {domain: int(exact[domain]) for domain in eligible_domains}
    remainder = total_slots - sum(allocation.values())
    for domain in sorted(
        eligible_domains,
        key=lambda name: (-(exact[name] - allocation[name]), name),
    ):
        if remainder <= 0:
            break
        allocation[domain] += 1
        remainder -= 1

    while True:
        surplus_slots = 0
        for domain in eligible_domains:
            cap = available_by_domain[domain]
            if allocation[domain] > cap:
                surplus_slots += allocation[domain] - cap
                allocation[domain] = cap
        if surplus_slots == 0:
            break
        added = 0
        for domain in sorted(
            eligible_domains,
            key=lambda name: (-domain_weights[name], name),
        ):
            if surplus_slots <= 0:
                break
            if allocation[domain] < available_by_domain[domain]:
                allocation[domain] += 1
                surplus_slots -= 1
                added += 1
        if added == 0:
            break

    shortfall = total_slots - sum(allocation.values())
    while shortfall > 0:
        added = 0
        for domain in sorted(
            eligible_domains,
            key=lambda name: (-domain_weights[name], name),
        ):
            if shortfall <= 0:
                break
            if allocation[domain] < available_by_domain[domain]:
                allocation[domain] += 1
                shortfall -= 1
                added += 1
        if added == 0:
            break

    return {
        domain: allocation[domain]
        for domain in eligible_domains
        if allocation[domain] > 0
    }


@dataclass(frozen=True)
class _Candidate:
    question_version_id: str
    question_id: int
    certification_exam_name: str
    category: str
    domain_name: str
    question_text: str
    question_type: str
    select_count: Optional[int]
    options: List[Dict[str, Any]]
    rank_key: str
    is_multi: bool


def _build_candidates(
    rows: Sequence[dict],
    snapshots: Mapping[str, dict],
    *,
    seed: int,
    certification_exam_name: str,
    domain_weights: Mapping[str, float],
) -> List[_Candidate]:
    candidates: List[_Candidate] = []
    for row in rows:
        qvid = str(row.get("question_version_id") or "").strip()
        category = str(row.get("category") or "").strip()
        if not qvid or not category:
            continue
        if category not in domain_weights:
            continue

        snapshot = snapshots.get(qvid)
        if not snapshot:
            raise QualityAuditPilotError(
                f"question_version_id {qvid!r} missing snapshot for "
                f"{certification_exam_name!r}"
            )

        question_id = row.get("question_id")
        if question_id is None:
            continue

        question_type = str(snapshot.get("question_type") or "single")
        select_count = snapshot.get("select_count")
        candidates.append(
            _Candidate(
                question_version_id=qvid,
                question_id=int(question_id),
                certification_exam_name=certification_exam_name,
                category=category,
                domain_name=category,
                question_text=str(snapshot.get("question_text") or row.get("question_text") or ""),
                question_type=question_type,
                select_count=select_count if isinstance(select_count, int) else None,
                options=public_options(snapshot.get("options") or []),
                rank_key=_rank_key(seed, certification_exam_name, category, qvid),
                is_multi=is_multi_select(question_type, select_count),
            )
        )
    return candidates


def _select_from_domain(
    candidates: Sequence[_Candidate],
    *,
    domain: str,
    count: int,
) -> List[_Candidate]:
    domain_candidates = [c for c in candidates if c.domain_name == domain]
    ranked = sorted(domain_candidates, key=lambda item: (item.rank_key, item.question_version_id))
    return ranked[:count]


def _fill_shortfall(
    selected: List[_Candidate],
    candidates: Sequence[_Candidate],
    *,
    needed: int,
    seed: int,
    certification_exam_name: str,
) -> List[_Candidate]:
    if needed <= 0:
        return selected

    selected_ids = {item.question_version_id for item in selected}
    remaining = [
        candidate
        for candidate in candidates
        if candidate.question_version_id not in selected_ids
    ]
    ranked = sorted(
        remaining,
        key=lambda item: (
            _rank_key(seed, certification_exam_name, "__global__", item.question_version_id),
            item.question_version_id,
        ),
    )
    return selected + ranked[:needed]


def _ensure_multi_select_included(
    selected: List[_Candidate],
    candidates: Sequence[_Candidate],
    *,
    seed: int,
    certification_exam_name: str,
) -> List[_Candidate]:
    if any(item.is_multi for item in selected):
        return selected

    available_multis = [item for item in candidates if item.is_multi]
    if not available_multis:
        return selected

    replaceable = [item for item in selected if not item.is_multi]
    if not replaceable:
        return selected

    worst_single = max(
        replaceable,
        key=lambda item: (
            _rank_key(seed, certification_exam_name, "__global__", item.question_version_id),
            item.question_version_id,
        ),
    )
    best_multi = min(
        available_multis,
        key=lambda item: (
            _rank_key(seed, certification_exam_name, "__global__", item.question_version_id),
            item.question_version_id,
        ),
    )

    updated = [
        best_multi if item.question_version_id == worst_single.question_version_id else item
        for item in selected
    ]
    return sorted(updated, key=lambda item: (item.rank_key, item.question_version_id))


def select_smoke_questions_for_certification(
    client,
    *,
    certification_exam_name: str,
    seed: int = DEFAULT_SMOKE_SEED,
    question_count: int = QUESTIONS_PER_CERTIFICATION,
) -> CertificationSmokeSelection:
    """Select deterministic smoke questions for one certification."""
    cert = str(certification_exam_name).strip()
    domain_weights = load_certification_domain_weights(client, cert)
    if not domain_weights:
        raise QualityAuditPilotError(
            f"no active domain weights found for {cert!r}"
        )

    rows = load_certification_current_question_versions(client, cert)
    if not rows:
        raise QualityAuditPilotError(
            f"no current question versions found for {cert!r}"
        )

    snapshots = load_question_version_snapshots_bulk(
        client,
        [row["question_version_id"] for row in rows],
    )
    candidates = _build_candidates(
        rows,
        snapshots,
        seed=seed,
        certification_exam_name=cert,
        domain_weights=domain_weights,
    )
    if not candidates:
        raise QualityAuditPilotError(
            f"no category/domain-matched candidates found for {cert!r}"
        )

    available_by_domain: Dict[str, int] = {}
    for candidate in candidates:
        available_by_domain[candidate.domain_name] = (
            available_by_domain.get(candidate.domain_name, 0) + 1
        )

    slot_allocation = allocate_weighted_domain_slots(
        domain_weights,
        available_by_domain,
        question_count,
    )

    selected: List[_Candidate] = []
    for domain in sorted(slot_allocation):
        selected.extend(
            _select_from_domain(
                candidates,
                domain=domain,
                count=slot_allocation[domain],
            )
        )

    if len(selected) < question_count:
        selected = _fill_shortfall(
            selected,
            candidates,
            needed=question_count - len(selected),
            seed=seed,
            certification_exam_name=cert,
        )

    if len(selected) > question_count:
        selected = sorted(selected, key=lambda item: (item.rank_key, item.question_version_id))[
            :question_count
        ]

    selected = _ensure_multi_select_included(
        selected,
        candidates,
        seed=seed,
        certification_exam_name=cert,
    )

    if len(selected) < question_count:
        raise QualityAuditPilotError(
            f"insufficient matched candidates for {cert!r}: "
            f"need {question_count}, found {len(selected)}"
        )

    selected = sorted(selected, key=lambda item: (item.rank_key, item.question_version_id))

    domain_allocation: Dict[str, int] = {}
    for item in selected:
        domain_allocation[item.domain_name] = domain_allocation.get(item.domain_name, 0) + 1

    smoke_selected = [
        SmokeSelectedQuestion(
            certification_exam_name=cert,
            rank=index,
            question_version_id=item.question_version_id,
            question_id=item.question_id,
            category=item.category,
            domain_name=item.domain_name,
            question_type=item.question_type,
            select_count=item.select_count,
            question_text=item.question_text,
            options=item.options,
            rank_key=item.rank_key,
        )
        for index, item in enumerate(selected, start=1)
    ]

    return CertificationSmokeSelection(
        certification_exam_name=cert,
        seed=seed,
        selected=smoke_selected,
        domain_allocation=domain_allocation,
        domain_weights=dict(domain_weights),
    )


def select_quality_audit_smoke_questions(
    client,
    *,
    seed: int = DEFAULT_SMOKE_SEED,
) -> QualityAuditSmokeSelection:
    """Select exactly five smoke questions for each pilot certification."""
    certifications = [
        select_smoke_questions_for_certification(
            client,
            certification_exam_name=cert,
            seed=seed,
        )
        for cert in PILOT_CERTIFICATIONS
    ]
    return QualityAuditSmokeSelection(seed=seed, certifications=certifications)


def format_quality_audit_smoke_selection(selection: QualityAuditSmokeSelection) -> str:
    """Format the read-only smoke selection report."""
    lines: List[str] = [
        f"seed: {selection.seed}",
        f"certification_count: {len(selection.certifications)}",
        f"question_count: {sum(len(cert.selected) for cert in selection.certifications)}",
        "",
    ]

    for cert_selection in selection.certifications:
        lines.extend([
            f"=== {cert_selection.certification_exam_name} ===",
            "domain_allocation:",
        ])
        for domain in sorted(cert_selection.domain_allocation):
            lines.append(
                f"  {domain}: selected={cert_selection.domain_allocation[domain]} "
                f"weight={cert_selection.domain_weights.get(domain, 0)}"
            )
        lines.append("")

        for question in cert_selection.selected:
            lines.extend([
                f"rank: {question.rank}",
                f"certification: {question.certification_exam_name}",
                f"question_version_id: {question.question_version_id}",
                f"question_id: {question.question_id}",
                f"category: {question.category}",
                f"question_type: {question.question_type}",
                f"select_count: {question.select_count}",
                f"question_text: {question.question_text}",
            ])
            for option in question.options:
                lines.append(
                    "option: "
                    f"{option.get('option_label')} | {option.get('option_text')}"
                )
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
