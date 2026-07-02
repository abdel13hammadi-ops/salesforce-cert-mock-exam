#!/usr/bin/env python3
"""
Local/admin entrypoint for the ten-question V48 AI quality-audit smoke batch.

Default mode is dry-run (no enqueue, no provider calls). Non-dry execution
requires explicit --execute and --confirm flags.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable, List, Optional, Sequence

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workers.ai_quality_audit_evidence import (  # noqa: E402
    AiQualityAuditEvidenceError,
    empty_evidence_set_hash,
    prepare_smoke_evidence_set,
    prepare_smoke_retrieval_replay_export,
    write_retrieval_replay_export,
)
from workers.ai_quality_provider_factory import (  # noqa: E402
    AiQualityProviderConfigError,
    resolve_ai_quality_model_provenance_from_env,
)
from workers.quality_audit_pilot import (  # noqa: E402
    DEFAULT_SMOKE_SEED,
    select_quality_audit_smoke_questions,
)

_ENQUEUE_RPC = "enqueue_background_job_v1"
_JOB_TYPE = "ai_quality_audit_smoke"
_REQUIRED_QUESTION_COUNT = 10


def running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def resolve_question_version_ids(
    *,
    explicit_ids: Sequence[str],
    seed: Optional[int],
    selection_loader: Callable,
) -> List[str]:
    """Return exactly ten unique question_version_ids."""
    if explicit_ids:
        normalized = [str(item).strip().lower() for item in explicit_ids if str(item).strip()]
        if len(normalized) != _REQUIRED_QUESTION_COUNT:
            raise ValueError(
                f"exactly {_REQUIRED_QUESTION_COUNT} question_version_ids are required, "
                f"got {len(normalized)}"
            )
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate question_version_ids are not allowed")
        return normalized

    if seed is None:
        raise ValueError(
            "provide exactly ten --question-version-id values or use --seed "
            "to load the pilot smoke batch"
        )

    selection = selection_loader(seed=seed)
    ids: List[str] = []
    for cert in selection.certifications:
        for question in cert.selected:
            ids.append(str(question.question_version_id).strip().lower())
    if len(ids) != _REQUIRED_QUESTION_COUNT:
        raise ValueError(
            f"pilot selection returned {len(ids)} question_version_ids; "
            f"expected {_REQUIRED_QUESTION_COUNT}"
        )
    if len(set(ids)) != len(ids):
        raise ValueError("pilot selection returned duplicate question_version_ids")
    return ids


def build_smoke_job_payload(
    *,
    audit_run_id: str,
    question_version_id: str,
    created_by: str,
) -> dict:
    return {
        "audit_run_id": audit_run_id,
        "question_version_id": question_version_id,
        "worker_id": created_by,
        "metadata": {"pilot_batch": "ai_quality_audit_smoke"},
    }


def build_create_run_params(
    *,
    question_version_id: str,
    prompt_version: str,
    ruleset_version: str,
    primary_model_name: str,
    dispute_model_name: str,
    pilot_batch_id: str,
    created_by: str,
    evidence_set_hash: str,
    evidence_chunks: Sequence[dict],
) -> dict:
    return {
        "p_target_question_version_id": question_version_id,
        "p_prompt_version": prompt_version,
        "p_ruleset_version": ruleset_version,
        "p_primary_model_name": primary_model_name,
        "p_dispute_model_name": dispute_model_name,
        "p_pilot_batch_id": pilot_batch_id,
        "p_evidence_set_hash": evidence_set_hash,
        "p_evidence_chunks": list(evidence_chunks),
        "p_created_by": created_by,
        "p_metadata": {"pilot_batch": pilot_batch_id},
    }


def _is_evidence_gap(summary: dict) -> bool:
    return bool(summary.get("evidence_gap"))


def _gap_summary_from_prepared(prepared) -> dict:
    summary = prepared.to_summary_dict()
    summary["evidence_gap"] = True
    summary["evidence_gap_reason"] = (
        "evidence retrieval returned zero qualified chunks for question_version "
        f"{prepared.question_version_id!r} "
        f"(certification={prepared.certification_exam_name!r}, "
        f"candidates={prepared.candidate_count}, "
        f"qualified={prepared.qualified_candidate_count}, "
        f"rejected={prepared.rejected_below_threshold_count})"
    )
    return summary


def _gap_summary_from_error(
    question_version_id: str,
    exc: AiQualityAuditEvidenceError,
) -> dict:
    return {
        "question_version_id": question_version_id,
        "certification_exam_name": "unknown",
        "candidate_count": 0,
        "qualified_candidate_count": 0,
        "rejected_below_threshold_count": 0,
        "selected_count": 0,
        "evidence_count": 0,
        "chunk_count": 0,
        "evidence_set_hash": empty_evidence_set_hash(),
        "total_evidence_characters": 0,
        "estimated_tokens": 0,
        "retrieval_method": "unknown",
        "chunk_previews": [],
        "evidence_gap": True,
        "evidence_gap_reason": str(exc),
    }


def prepare_all_smoke_evidence_dry_run(
    client,
    question_version_ids: Sequence[str],
) -> List[dict]:
    """Retrieve evidence for every smoke question; continue after per-question gaps."""
    summaries: List[dict] = []
    for question_version_id in question_version_ids:
        try:
            prepared = prepare_smoke_evidence_set(
                client,
                question_version_id,
                allow_no_evidence=True,
            )
        except AiQualityAuditEvidenceError as exc:
            summaries.append(_gap_summary_from_error(question_version_id, exc))
            continue

        if prepared.qualified_candidate_count == 0:
            summaries.append(_gap_summary_from_prepared(prepared))
        else:
            summaries.append(prepared.to_summary_dict())
    return summaries


def format_dry_run_report(
    *,
    question_version_ids: Sequence[str],
    prompt_version: str,
    ruleset_version: str,
    primary_model_name: str,
    dispute_model_name: str,
    pilot_batch_id: str,
    created_by: str,
    evidence_summaries: Sequence[dict],
) -> str:
    lines = [
        "AI quality audit smoke dry-run",
        f"question_count: {len(question_version_ids)}",
        f"prompt_version: {prompt_version}",
        f"ruleset_version: {ruleset_version}",
        f"primary_model_name: {primary_model_name}",
        f"dispute_model_name: {dispute_model_name}",
        f"pilot_batch_id: {pilot_batch_id}",
        f"created_by: {created_by}",
        "question_version_ids:",
    ]
    lines.extend(f"  - {qvid}" for qvid in question_version_ids)
    lines.append("evidence_freeze_preview:")
    for summary in evidence_summaries:
        if _is_evidence_gap(summary):
            lines.append(
                "  - "
                f"{summary['question_version_id']}: "
                "EVIDENCE_GAP "
                f"certification={summary.get('certification_exam_name', 'unknown')!r}, "
                f"candidates={summary.get('candidate_count', 0)}, "
                f"qualified={summary.get('qualified_candidate_count', 0)}, "
                f"rejected={summary.get('rejected_below_threshold_count', 0)}, "
                f"reason={summary.get('evidence_gap_reason', 'unknown')}"
            )
            for rejected in (summary.get("rejected_previews") or [])[:3]:
                reasons = ", ".join(rejected.get("match_reasons") or []) or "n/a"
                lines.append(
                    "      rejected_"
                    f"rank={rejected.get('retrieval_rank')} "
                    f"chunk={rejected.get('resource_chunk_id')} "
                    f"score={rejected.get('relevance_score')} "
                    f"threshold={rejected.get('applicable_threshold')} "
                    f"title={rejected.get('title')!r} "
                    f"reasons={reasons} "
                    f"rejection={rejected.get('rejection_reason', 'unknown')}"
                )
            continue

        lines.append(
            "  - "
            f"{summary['question_version_id']}: "
            f"candidates={summary.get('candidate_count', 0)}, "
            f"qualified={summary.get('qualified_candidate_count', 0)}, "
            f"selected={summary.get('selected_count', summary.get('evidence_count', 0))}, "
            f"rejected={summary.get('rejected_below_threshold_count', 0)}, "
            f"total_chars={summary.get('total_evidence_characters', 0)}, "
            f"estimated_tokens={summary.get('estimated_tokens', 0)}, "
            f"method={summary.get('retrieval_method', 'unknown')}, "
            f"hash={summary['evidence_set_hash']}"
        )
        for preview in summary.get("chunk_previews") or []:
            reasons = ", ".join(preview.get("match_reasons") or []) or "n/a"
            lines.append(
                "      "
                f"rank={preview.get('retrieval_rank')} "
                f"chunk={preview.get('resource_chunk_id')} "
                f"score={preview.get('relevance_score')} "
                f"title={preview.get('title')!r} "
                f"reasons={reasons}"
            )

    with_qualified = sum(1 for summary in evidence_summaries if not _is_evidence_gap(summary))
    with_gaps = sum(1 for summary in evidence_summaries if _is_evidence_gap(summary))
    lines.append("evidence_summary:")
    lines.append(f"  total_questions: {len(question_version_ids)}")
    lines.append(f"  questions_with_qualified_evidence: {with_qualified}")
    lines.append(f"  questions_with_evidence_gaps: {with_gaps}")
    lines.append("  qualified_evidence_count_by_question:")
    for summary in evidence_summaries:
        qvid = summary["question_version_id"]
        if _is_evidence_gap(summary):
            lines.append(f"    - {qvid}: EVIDENCE_GAP")
        else:
            selected = summary.get(
                "selected_count",
                summary.get("evidence_count", 0),
            )
            lines.append(f"    - {qvid}: {selected}")

    if with_gaps:
        lines.append(
            f"Dry-run completed with {with_gaps} evidence gap(s). Exit code 1."
        )
    else:
        lines.append("Dry-run completed with qualified evidence for all questions.")
    lines.append("No jobs enqueued (dry-run).")
    return "\n".join(lines)


def prepare_all_smoke_evidence(
    client,
    question_version_ids: Sequence[str],
) -> List[dict]:
    """Retrieve and validate evidence for every smoke question (read-only)."""
    summaries: List[dict] = []
    for question_version_id in question_version_ids:
        prepared = prepare_smoke_evidence_set(client, question_version_id)
        summaries.append(prepared.to_summary_dict())
    return summaries


def execute_smoke_batch(
    client,
    *,
    question_version_ids: Sequence[str],
    prompt_version: str,
    ruleset_version: str,
    primary_model_name: str,
    dispute_model_name: str,
    pilot_batch_id: str,
    created_by: str,
    evidence_summaries: Sequence[dict],
) -> List[dict]:
    """Create audit runs and enqueue one smoke job per question."""
    if len(evidence_summaries) != len(question_version_ids):
        raise ValueError(
            "evidence_summaries length "
            f"{len(evidence_summaries)} does not match question count "
            f"{len(question_version_ids)}"
        )

    evidence_by_qvid = {
        str(item["question_version_id"]).lower(): item for item in evidence_summaries
    }
    results: List[dict] = []
    for question_version_id in question_version_ids:
        evidence = evidence_by_qvid.get(str(question_version_id).lower())
        if evidence is None:
            raise ValueError(
                f"missing evidence summary for question_version_id {question_version_id!r}"
            )
        evidence_chunks = list(evidence.get("evidence_chunks") or [])
        if not evidence_chunks:
            raise AiQualityAuditEvidenceError(
                f"refusing to enqueue smoke job for question_version_id "
                f"{question_version_id!r} with an empty evidence set"
            )
        create_row = client.rpc(
            "create_or_get_ai_quality_audit_run_v1",
            build_create_run_params(
                question_version_id=question_version_id,
                prompt_version=prompt_version,
                ruleset_version=ruleset_version,
                primary_model_name=primary_model_name,
                dispute_model_name=dispute_model_name,
                pilot_batch_id=pilot_batch_id,
                created_by=created_by,
                evidence_set_hash=evidence["evidence_set_hash"],
                evidence_chunks=evidence_chunks,
            ),
        ).execute()
        if getattr(create_row, "error", None):
            raise RuntimeError(
                f"create_or_get_ai_quality_audit_run_v1 failed: {create_row.error}"
            )
        create_data = create_row.data or []
        if not create_data:
            raise RuntimeError("create_or_get_ai_quality_audit_run_v1 returned no rows")
        audit_run_id = str(create_data[0]["audit_run_id"])

        payload = build_smoke_job_payload(
            audit_run_id=audit_run_id,
            question_version_id=question_version_id,
            created_by=created_by,
        )
        enqueue_row = client.rpc(
            _ENQUEUE_RPC,
            {
                "p_job_type": _JOB_TYPE,
                "p_payload": payload,
                "p_priority": 100,
                "p_max_attempts": 3,
                "p_created_by": created_by,
                "p_model_name": primary_model_name,
                "p_prompt_version": prompt_version,
                "p_metadata": {"pilot_batch_id": pilot_batch_id},
            },
        ).execute()
        if getattr(enqueue_row, "error", None):
            raise RuntimeError(f"{_ENQUEUE_RPC} failed: {enqueue_row.error}")
        enqueue_data = enqueue_row.data or []
        if not enqueue_data:
            raise RuntimeError(f"{_ENQUEUE_RPC} returned no rows")

        results.append(
            {
                "question_version_id": question_version_id,
                "audit_run_id": audit_run_id,
                "job_id": enqueue_data[0].get("job_id"),
                "job_status": enqueue_data[0].get("job_status"),
                "evidence_chunk_count": len(evidence_chunks),
                "evidence_set_hash": evidence["evidence_set_hash"],
            }
        )
    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan or enqueue the ten-question AI quality-audit smoke batch.",
    )
    parser.add_argument(
        "--question-version-id",
        action="append",
        default=[],
        dest="question_version_ids",
        help="Question version UUID (repeat exactly ten times total)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help=f"Load pilot smoke selection (default seed {DEFAULT_SMOKE_SEED} when used alone)",
    )
    parser.add_argument("--prompt-version", default="v48-smoke-prompt-v1")
    parser.add_argument("--ruleset-version", default="v48-smoke-ruleset-v1")
    parser.add_argument("--pilot-batch-id", default="v48-ai-quality-smoke")
    parser.add_argument("--created-by", default="ai-quality-smoke-cli")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Create audit runs and enqueue jobs (requires --confirm)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required with --execute to acknowledge non-dry execution",
    )
    parser.add_argument(
        "--export-retrieval-replay",
        default=None,
        metavar="PATH",
        help=(
            "Dry-run only: write compact BM25 replay JSON to PATH "
            "(use '-' for stdout between export markers)"
        ),
    )
    args = parser.parse_args(argv)

    if args.export_retrieval_replay and args.execute:
        print(
            "ERROR: --export-retrieval-replay is allowed only in dry-run mode.",
            file=sys.stderr,
        )
        return 1

    try:
        model_provenance = resolve_ai_quality_model_provenance_from_env()
    except AiQualityProviderConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    seed = args.seed
    if not args.question_version_ids and seed is None:
        seed = DEFAULT_SMOKE_SEED

    try:
        if args.question_version_ids:
            ids = resolve_question_version_ids(
                explicit_ids=args.question_version_ids,
                seed=None,
                selection_loader=lambda seed: (_ for _ in ()).throw(RuntimeError("unused")),
            )
        else:
            ids = resolve_question_version_ids(
                explicit_ids=[],
                seed=seed,
                selection_loader=lambda seed: select_quality_audit_smoke_questions(
                    _load_client_for_selection(),
                    seed=seed,
                ),
            )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not args.execute:
        try:
            preview_client = _load_client_for_evidence_preview()
            evidence_summaries = prepare_all_smoke_evidence_dry_run(preview_client, ids)
            if args.export_retrieval_replay:
                replay_export = prepare_smoke_retrieval_replay_export(
                    preview_client,
                    ids,
                )
                write_retrieval_replay_export(
                    replay_export,
                    args.export_retrieval_replay,
                )
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        print(
            format_dry_run_report(
                question_version_ids=ids,
                prompt_version=args.prompt_version,
                ruleset_version=args.ruleset_version,
                primary_model_name=model_provenance.primary_model_name,
                dispute_model_name=model_provenance.dispute_model_name,
                pilot_batch_id=args.pilot_batch_id,
                created_by=args.created_by,
                evidence_summaries=evidence_summaries,
            )
        )
        gap_count = sum(1 for summary in evidence_summaries if _is_evidence_gap(summary))
        return 1 if gap_count else 0

    if not args.confirm:
        print(
            "ERROR: --execute requires --confirm for non-dry execution.",
            file=sys.stderr,
        )
        return 1

    if running_under_pytest():
        print("ERROR: refusing live smoke execution under pytest.", file=sys.stderr)
        return 2

    try:
        client = _load_client_for_execution()
        evidence_summaries = prepare_all_smoke_evidence(client, ids)
        rows = execute_smoke_batch(
            client,
            question_version_ids=ids,
            prompt_version=args.prompt_version,
            ruleset_version=args.ruleset_version,
            primary_model_name=model_provenance.primary_model_name,
            dispute_model_name=model_provenance.dispute_model_name,
            pilot_batch_id=args.pilot_batch_id,
            created_by=args.created_by,
            evidence_summaries=evidence_summaries,
        )
    except (RuntimeError, AiQualityAuditEvidenceError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


def _load_client_for_evidence_preview():
    if running_under_pytest():
        raise RuntimeError("evidence preview client must be injected in tests")
    return _load_client_for_selection()


def _load_client_for_selection():
    if running_under_pytest():
        raise RuntimeError("selection client must be injected in tests")
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for --seed selection"
        )
    from workers.background_worker import build_supabase_client  # noqa: PLC0415

    return build_supabase_client()


def _load_client_for_execution():
    if running_under_pytest():
        raise RuntimeError("execution client must be injected in tests")
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for --execute"
        )
    from workers.background_worker import build_supabase_client  # noqa: PLC0415

    return build_supabase_client()


if __name__ == "__main__":
    raise SystemExit(main())
