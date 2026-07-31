"""Structured validation findings for Scenario Simulator content documents."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
from typing import Literal

ValidationLayer = Literal[
    "json_schema",
    "structural",
    "graph",
    "semantic",
    "publication",
    "runtime",
]

ValidationSeverity = Literal["blocker", "high", "medium", "low", "note"]

# Documented validation-layer order (not lexicographic by layer name).
_LAYER_ORDER: dict[str, int] = {
    "json_schema": 0,
    "structural": 1,
    "semantic": 2,
    "graph": 3,
    "publication": 4,
    "runtime": 5,
}


@dataclass(frozen=True)
class ValidationFinding:
    rule_id: str
    layer: ValidationLayer
    severity: ValidationSeverity
    path: str
    message: str
    identifier: str | None = None


def sort_validation_findings(findings: list[ValidationFinding]) -> tuple[ValidationFinding, ...]:
    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                _LAYER_ORDER.get(finding.layer, 99),
                finding.path,
                finding.rule_id,
                finding.message,
                finding.identifier or "",
            ),
        )
    )


def findings_contain_blocking(findings: list[ValidationFinding]) -> bool:
    return any(finding.severity in {"blocker", "high"} for finding in findings)


def first_blocking_finding(
    findings: Sequence[ValidationFinding] | tuple[ValidationFinding, ...],
) -> ValidationFinding | None:
    for finding in sort_validation_findings(list(findings)):
        if finding.severity in {"blocker", "high"}:
            return finding
    return None
