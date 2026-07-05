#!/usr/bin/env python3
"""
V57-GENERATION-04 — local runner for the single-candidate generation slice.

This is a thin invocation wrapper around the real generation service in
``workers.question_candidate_generation``. It does not duplicate any
generation, validation, persistence, or audit-enqueue logic — it only:

  1. Parses bounded CLI inputs into a ``GenerationRequest``.
  2. Builds a service-role Supabase client (``workers.background_worker``)
     and an LLM provider (``workers.llm_provider_factory``) from the
     existing environment-based abstractions. No new provider framework.
  3. Calls ``generate_and_persist_candidate`` (generation mode) or
     ``retry_candidate_audits`` (retry mode) and prints a redacted report.

Safety
------
* Refuses to run under pytest (mirrors ``workers.run_hybrid_audit_pilot``).
* Refuses to run unless ``CERTBOUND_ALLOW_LIVE_AI_TEST=1`` is set, so it is
  never triggered accidentally (e.g. via test collection or import).
* Never prints ``SUPABASE_SERVICE_ROLE_KEY``, provider API keys, or any
  other credential value. Only structured, non-secret result fields
  (candidate id, content hash, job ids, booleans) are printed.
* Never targets ``questions``/``answer_options``/``question_versions``/
  ``question_option_versions`` — those tables are not referenced anywhere
  in this script or in the service it calls.

Usage — generate one candidate
-------------------------------
    set CERTBOUND_ALLOW_LIVE_AI_TEST=1
    set CERTBOUND_LLM_PROVIDER=anthropic
    set CERTBOUND_ANTHROPIC_API_KEY=your-key-here
    set SUPABASE_URL=https://your-project.supabase.co
    set SUPABASE_SERVICE_ROLE_KEY=your-service-role-key

    python scripts/v57_generate_one_candidate.py generate ^
        --certification "Salesforce Administrator" ^
        --domain "Data and Analytics Management" ^
        --model claude-3-5-sonnet-20241022 ^
        --prompt-template-id certbound-question-gen ^
        --prompt-version v1.0.0 ^
        --created-by generation-service@certbound.internal ^
        --source-evidence "{\\"resource_reference\\": \\"Salesforce Help: Standard Objects\\"}"

Usage — retry audit initiation for an existing candidate (no regeneration)
---------------------------------------------------------------------------
    python scripts/v57_generate_one_candidate.py retry-audits ^
        --candidate-id <question_candidates.id> ^
        --created-by ops@certbound.internal ^
        --job-types llm_audit

This script never executes against production automatically: it always
requires an explicit, operator-set ``CERTBOUND_ALLOW_LIVE_AI_TEST=1`` and
explicit credentials pointed at whatever Supabase project the operator has
configured. It performs no environment/project detection of its own.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workers.background_worker import build_supabase_client  # noqa: E402
from workers.llm_provider_factory import build_llm_provider_from_env  # noqa: E402
from workers.question_candidate_generation import (  # noqa: E402
    AuditEnqueueOutcome,
    AuditInitiationError,
    CandidateGenerationError,
    GenerationRequest,
    GenerationResult,
    generate_and_persist_candidate,
    retry_candidate_audits,
)

_LIVE_FLAG = "CERTBOUND_ALLOW_LIVE_AI_TEST"


class RunnerUsageError(RuntimeError):
    """Raised for CLI-input problems, before any client/provider is built."""


def running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def assert_supabase_configured() -> None:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required to run this script."
        )


def _parse_json_object(raw: Optional[str], *, field_name: str) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RunnerUsageError(f"--{field_name} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RunnerUsageError(f"--{field_name} must be a JSON object")
    return parsed


def _parse_job_types(raw: Optional[str]) -> Optional[Sequence[str]]:
    if raw is None or not raw.strip():
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}


# ===========================================================================
# Argument parsing
# ===========================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded local runner for the V57 single-candidate generation "
            "service. Delegates entirely to "
            "workers.question_candidate_generation; introduces no new "
            "generation, validation, persistence, or audit logic."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser(
        "generate",
        help="Generate and persist exactly one new question candidate.",
    )
    generate_parser.add_argument("--certification", required=True, dest="certification_exam_name")
    generate_parser.add_argument("--domain", required=True)
    generate_parser.add_argument("--model", required=True, dest="model_name")
    generate_parser.add_argument("--prompt-template-id", required=True)
    generate_parser.add_argument("--prompt-version", required=True)
    generate_parser.add_argument("--created-by", required=True)
    generate_parser.add_argument(
        "--source-evidence",
        required=True,
        help="JSON object, e.g. '{\"resource_reference\": \"...\"}'",
    )
    generate_parser.add_argument("--question-type", default="single", choices=["single", "multiple"])
    generate_parser.add_argument("--select-count", type=int, default=1)
    generate_parser.add_argument("--difficulty", default=None, choices=[None, "easy", "medium", "hard"])
    generate_parser.add_argument(
        "--cognitive-level",
        default=None,
        choices=[None, "recall", "understanding", "application", "analysis", "judgment"],
    )
    generate_parser.add_argument("--concept-key", default=None)
    generate_parser.add_argument("--language-code", default="en")
    generate_parser.add_argument("--source-reference", default=None)
    generate_parser.add_argument("--generation-request-id", default=None)
    generate_parser.add_argument(
        "--request-metadata",
        default=None,
        help="Optional JSON object for GenerationRequest.request_metadata",
    )
    generate_parser.add_argument(
        "--no-initiate-audits",
        action="store_true",
        help="Persist the candidate but do not enqueue audit jobs.",
    )
    generate_parser.add_argument(
        "--initiate-audits-on-duplicate",
        action="store_true",
        help=(
            "If the request deduplicates to an existing candidate, also "
            "enqueue a full new set of audits for it (off by default to "
            "avoid duplicate audit_runs on an idempotent retry)."
        ),
    )

    retry_parser = subparsers.add_parser(
        "retry-audits",
        help=(
            "Retry audit initiation for an existing candidate without "
            "regenerating or duplicating it."
        ),
    )
    retry_parser.add_argument("--candidate-id", required=True)
    retry_parser.add_argument("--created-by", required=True)
    retry_parser.add_argument(
        "--job-types",
        default=None,
        help="Comma-separated subset of: deterministic_audit,llm_audit (default: both)",
    )

    return parser


def build_generation_request_from_args(args: argparse.Namespace) -> GenerationRequest:
    source_evidence = _parse_json_object(args.source_evidence, field_name="source-evidence")
    request_metadata = _parse_json_object(args.request_metadata, field_name="request-metadata")
    return GenerationRequest(
        certification_exam_name=args.certification_exam_name,
        domain=args.domain,
        prompt_template_id=args.prompt_template_id,
        prompt_version=args.prompt_version,
        model_name=args.model_name,
        created_by=args.created_by,
        source_evidence=source_evidence or {},
        question_type=args.question_type,
        select_count=args.select_count,
        difficulty=args.difficulty,
        cognitive_level=args.cognitive_level,
        concept_key=args.concept_key,
        language_code=args.language_code,
        source_reference=args.source_reference,
        generation_request_id=args.generation_request_id,
        request_metadata=request_metadata,
    )


# ===========================================================================
# Delegating entry points (no pytest/live-flag guard here — those live only
# in main() — so tests can exercise real delegation with fake dependencies).
# ===========================================================================

def run_generate_one_candidate(
    client: Any,
    llm_provider: Any,
    args: argparse.Namespace,
) -> GenerationResult:
    """Build one GenerationRequest from CLI args and delegate to the real
    generation service. Introduces no generation/validation/persistence
    logic of its own.
    """
    request = build_generation_request_from_args(args)
    return generate_and_persist_candidate(
        client,
        llm_provider,
        request,
        initiate_audits=not args.no_initiate_audits,
        initiate_audits_on_duplicate=args.initiate_audits_on_duplicate,
    )


def run_retry_audits(client: Any, args: argparse.Namespace) -> List[AuditEnqueueOutcome]:
    """Delegate directly to the real, self-contained audit-retry service
    entry point. Never regenerates or duplicates the candidate.
    """
    job_types = _parse_job_types(args.job_types)
    return retry_candidate_audits(
        client,
        args.candidate_id,
        job_types=job_types,
        created_by=args.created_by,
    )


# ===========================================================================
# Redacted report formatting — never includes credentials or raw model
# output; only structured, non-secret identifiers/flags.
# ===========================================================================

def format_generation_report(result: GenerationResult) -> str:
    payload = {
        "candidate_id": result.candidate_id,
        "content_hash": result.content_hash,
        "deduplicated": result.deduplicated,
        "audit_outcomes": [
            {"job_type": o.job_type, "job_id": o.job_id, "enqueued": o.enqueued, "error": o.error}
            for o in result.audit_outcomes
        ],
        "provider_request_id": result.provider_request_id,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def format_audit_outcomes_report(outcomes: List[AuditEnqueueOutcome]) -> str:
    payload = {
        "audit_outcomes": [
            {"job_type": o.job_type, "job_id": o.job_id, "enqueued": o.enqueued, "error": o.error}
            for o in outcomes
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# ===========================================================================
# CLI entry point
# ===========================================================================

def main(argv: Optional[List[str]] = None) -> int:
    if running_under_pytest():
        print("Refusing to run under pytest.")
        return 2

    if os.environ.get(_LIVE_FLAG) != "1":
        print(f"Refusing live run. Set {_LIVE_FLAG}=1 to proceed.")
        return 2

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        assert_supabase_configured()
    except RuntimeError as exc:
        print(str(exc))
        return 1

    try:
        client = build_supabase_client()
    except RuntimeError as exc:
        print(str(exc))
        return 1

    if args.command == "retry-audits":
        try:
            outcomes = run_retry_audits(client, args)
        except (CandidateGenerationError, AuditInitiationError) as exc:
            if isinstance(exc, AuditInitiationError):
                print(format_audit_outcomes_report(exc.outcomes))
            print(f"ERROR: {type(exc).__name__}: {exc}")
            return 1
        print(format_audit_outcomes_report(outcomes))
        return 0

    llm_provider = build_llm_provider_from_env()
    if llm_provider is None:
        print(
            "No LLM provider configured. Set CERTBOUND_LLM_PROVIDER=anthropic "
            "and CERTBOUND_ANTHROPIC_API_KEY before generating a candidate."
        )
        return 1

    try:
        result = run_generate_one_candidate(client, llm_provider, args)
    except AuditInitiationError as exc:
        # Candidate persistence already succeeded (see
        # generate_and_persist_candidate docstring) — this is NOT rolled
        # back. Print what audits did/didn't enqueue and how to retry only
        # the failed one(s) without regenerating the candidate.
        print(format_audit_outcomes_report(exc.outcomes))
        print(f"ERROR: {type(exc).__name__}: {exc}")
        failed_job_types = ",".join(o.job_type for o in exc.outcomes if not o.enqueued)
        print(
            f"Candidate {exc.candidate_id} was persisted. Retry failed audits via: "
            f"python {os.path.basename(__file__)} retry-audits "
            f"--candidate-id {exc.candidate_id} --created-by <you> "
            f"--job-types {failed_job_types}"
        )
        return 1
    except (RunnerUsageError, CandidateGenerationError, RuntimeError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1

    print(format_generation_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
