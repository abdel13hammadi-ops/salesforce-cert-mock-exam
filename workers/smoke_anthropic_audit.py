#!/usr/bin/env python3
"""
Manual live smoke test for the CertBound Anthropic audit provider.

This module is **not** collected by pytest. It refuses to run unless
``CERTBOUND_ALLOW_LIVE_AI_TEST=1`` is set and will exit immediately when
imported under pytest.

Usage::

    set CERTBOUND_ALLOW_LIVE_AI_TEST=1
    set CERTBOUND_ANTHROPIC_API_KEY=sk-ant-...
    python -m workers.smoke_anthropic_audit
"""

from __future__ import annotations

import json
import os
import sys
import time

from workers.anthropic_provider import (
    ENV_API_KEY,
    ENV_MODEL,
    AnthropicAuditProvider,
)
from workers.llm_audit import AUDIT_RESPONSE_SCHEMA, validate_llm_response

_LIVE_FLAG = "CERTBOUND_ALLOW_LIVE_AI_TEST"

_SAMPLE_SYSTEM_PROMPT = (
    "You are a CertBound certification question auditor. "
    "Review the question and official evidence. "
    "Return findings only in the required JSON schema."
)

_SAMPLE_USER_PROMPT = (
    "Audit the following Salesforce Administrator practice question for "
    "correctness, clarity, and alignment with the supplied official source excerpt."
)

_SAMPLE_QUESTION = {
    "question_text": (
        "What is the primary responsibility of a Salesforce Administrator?"
    ),
    "explanation": (
        "Administrators configure and maintain the Salesforce platform "
        "for their organization."
    ),
    "question_type": "single",
    "select_count": 1,
    "options": [
        {
            "option_label": "A",
            "option_text": "Configure and maintain the Salesforce platform",
            "is_correct": True,
            "display_order": 1,
        },
        {
            "option_label": "B",
            "option_text": "Write Apex triggers for all business logic",
            "is_correct": False,
            "display_order": 2,
        },
    ],
}

_SAMPLE_RESOURCE_SNAPSHOT = {
    "chunks": [
        {
            "resource_chunk_id": "00000000-0000-0000-0000-000000000101",
            "chunk_index": 0,
            "chunk_text": (
                "Salesforce Administrators customize the platform, manage users, "
                "maintain data quality, and support business processes."
            ),
        }
    ]
}


def main() -> int:
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        print("Refusing to run under pytest.")
        return 2

    if os.environ.get(_LIVE_FLAG) != "1":
        print(
            f"Refusing live API call. Set {_LIVE_FLAG}=1 to run this smoke test."
        )
        return 1

    if not os.environ.get(ENV_API_KEY, "").strip():
        print(f"Refusing live API call. Set {ENV_API_KEY} before running.")
        return 1

    provider = AnthropicAuditProvider.from_env()
    model = os.environ.get(ENV_MODEL, "claude-sonnet-4-6").strip()

    started = time.perf_counter()
    response = provider(
        model_name=model,
        system_prompt=_SAMPLE_SYSTEM_PROMPT,
        user_prompt=_SAMPLE_USER_PROMPT,
        response_schema=AUDIT_RESPONSE_SCHEMA,
        metadata={
            "question": _SAMPLE_QUESTION,
            "resource_snapshot": _SAMPLE_RESOURCE_SNAPSHOT,
        },
    )
    elapsed = time.perf_counter() - started

    findings = validate_llm_response(response.parsed_response)

    print("CertBound Anthropic audit smoke test")
    print(f"  provider:       {response.provider_name}")
    print(f"  model:          {response.model_name}")
    print(f"  duration_sec:   {elapsed:.2f}")
    print(f"  input_tokens:   {response.input_tokens}")
    print(f"  output_tokens:  {response.output_tokens}")
    print(f"  estimated_cost: {response.actual_cost_usd}")
    print(f"  request_id:     {response.provider_request_id}")
    print(f"  finding_count:  {len(findings)}")
    print("  findings:")
    print(json.dumps(findings, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
