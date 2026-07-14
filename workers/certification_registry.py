"""
Engine-side certification capability registry for CertBound generation and
audit flows.

Two distinct, non-overlapping responsibilities exist in this codebase and
must never be allowed to silently disagree:

1. **Database catalog** (``public.certifications`` / ``public.certification_domains``,
   queried at runtime via ``QuestionCandidateRepository.certification_domain_exists()``
   in ``workers/question_candidate_generation.py``). This is the sole
   authority for which certification/domain rows actually exist for
   *persistence*. It decides whether a specific
   ``(certification_exam_name, domain_name)`` pair may be written to
   ``question_candidates`` right now. This module never queries, mutates, or
   duplicates those tables, and never claims to know what rows currently
   exist in the database.

2. **Engine certification profile** (this module). This is the code-side
   contract for which certifications the generation/audit *engine* is built
   to understand at all: normalized identifiers/aliases, the domain
   taxonomy, domain weights, coverage guidance, and certification-specific
   policy metadata (e.g. the legacy-automation guardrail). Passing this
   module's validation is a *capability* check ("does the engine know how to
   handle this certification/domain shape"), not a persistence-existence
   check. A request can pass here and still be correctly rejected by
   ``certification_domain_exists()`` (for example: Platform App Builder has
   an engine profile below, but has no ``certifications`` /
   ``certification_domains`` rows yet, so PAB generation requests will
   validate here and then correctly fail at the database gate until a
   migration adds those rows — see the module-level note on
   ``enforce_domain_contract_at_request_validation`` below).

Validation performed by this module is intentionally read-only and
non-mutating: callers must not replace a caller-supplied certification/domain
string with the canonical form returned by these functions. Historical or
already-frozen values (persisted candidate rows, frozen blind/comparison
audit context) must continue to round-trip through this module unchanged —
only their *validity* is checked, never their spelling.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

ADM_EXAM_NAME = "Salesforce Certified Platform Administrator"
BA_EXAM_NAME = "Salesforce Certified Business Analyst"
PAB_EXAM_NAME = "Salesforce Certified Platform App Builder"

CERTIFICATION_CODES: Dict[str, str] = {
    # "ADM-201" / "BA-201" mirror the pre-existing codes already used by
    # workers/official_evidence_seed.py (imported there via
    # workers/structural_audit_launcher.ADM_EXAM_NAME / BA_EXAM_NAME) and by
    # existing frozen audit context fixtures — repository-evidenced, not
    # invented here.
    ADM_EXAM_NAME: "ADM-201",
    BA_EXAM_NAME: "BA-201",
    # "APP-401" is NOT an official Salesforce exam code and has no
    # repository evidence of prior use anywhere outside this module (see
    # PAB-EXP-02 review). It is not a real Salesforce credential code, so a
    # semantic identifier is used instead, following the same snake_case
    # convention already used for domain_id values below.
    PAB_EXAM_NAME: "platform_app_builder",
}

LEGACY_AUTOMATION_GUARDRAIL = (
    "Newly designed automation must not recommend Workflow Rules or Process "
    "Builder when Salesforce Flow is the supported declarative solution, unless "
    "the scenario specifically tests legacy behavior, migration, or an existing "
    "implementation."
)


class CertificationRegistryError(ValueError):
    """Raised when a certification identifier or domain fails registry validation."""


@dataclass(frozen=True)
class CertificationDomain:
    domain_id: str
    domain_name: str
    weight: int
    coverage_topics: Tuple[str, ...] = ()
    policy_guidance: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CertificationDefinition:
    exam_name: str
    # Internal/display label only (mirrors the free-text, no-fixed-format
    # ``certifications.certification_code`` DB column used for UI/metadata
    # display — see app.py / pages/Dashboard.py). Never an official
    # Salesforce exam code and never used for validation or lookups.
    certification_code: str
    aliases: FrozenSet[str]
    domains: Tuple[CertificationDomain, ...]
    enforce_domain_contract_at_request_validation: bool = False

    @property
    def domain_names(self) -> FrozenSet[str]:
        return frozenset(domain.domain_name for domain in self.domains)

    @property
    def total_domain_weight(self) -> int:
        return sum(domain.weight for domain in self.domains)


def _normalize_certification_key(value: str) -> str:
    collapsed = re.sub(r"\s+", " ", value.strip())
    return unicodedata.normalize("NFKC", collapsed).casefold()


def _adm_definition() -> CertificationDefinition:
    return CertificationDefinition(
        exam_name=ADM_EXAM_NAME,
        certification_code=CERTIFICATION_CODES[ADM_EXAM_NAME],
        aliases=frozenset(
            {
                "Salesforce Administrator",
                "Salesforce Certified Administrator",
                ADM_EXAM_NAME,
                "adm",
                "adm-201",
            }
        ),
        domains=(
            CertificationDomain("configuration_and_setup", "Configuration and Setup", 15),
            CertificationDomain(
                "object_manager_and_lightning_app_builder",
                "Object Manager and Lightning App Builder",
                15,
            ),
            CertificationDomain(
                "data_and_analytics_management",
                "Data and Analytics Management",
                17,
            ),
            CertificationDomain("automation", "Automation", 15),
            CertificationDomain(
                "sales_and_marketing_applications",
                "Sales and Marketing Applications",
                10,
            ),
            CertificationDomain(
                "service_and_support_applications",
                "Service and Support Applications",
                10,
            ),
            CertificationDomain("agentforce_ai", "Agentforce AI", 8),
            CertificationDomain(
                "productivity_and_collaboration",
                "Productivity and Collaboration",
                10,
            ),
        ),
        enforce_domain_contract_at_request_validation=False,
    )


def _ba_definition() -> CertificationDefinition:
    return CertificationDefinition(
        exam_name=BA_EXAM_NAME,
        certification_code=CERTIFICATION_CODES[BA_EXAM_NAME],
        aliases=frozenset(
            {
                "Salesforce Business Analyst",
                "Salesforce Certified Business Analyst",
                BA_EXAM_NAME,
                "ba",
                "ba-201",
            }
        ),
        domains=(
            CertificationDomain("customer_discovery", "Customer Discovery", 17),
            CertificationDomain(
                "collaboration_with_stakeholders",
                "Collaboration with Stakeholders",
                17,
            ),
            CertificationDomain(
                "business_process_mapping",
                "Business Process Mapping",
                17,
            ),
            CertificationDomain("requirements", "Requirements", 17),
            CertificationDomain("user_stories", "User Stories", 16),
            CertificationDomain("user_acceptance", "User Acceptance", 16),
        ),
        enforce_domain_contract_at_request_validation=True,
    )


def _pab_definition() -> CertificationDefinition:
    return CertificationDefinition(
        exam_name=PAB_EXAM_NAME,
        certification_code=CERTIFICATION_CODES[PAB_EXAM_NAME],
        aliases=frozenset(
            {
                # Bare official credential name without "Certified"; the
                # canonical exam_name below keeps the "Salesforce Certified
                # <X>" form to match the repo-wide DB-row naming convention
                # already used for ADM_EXAM_NAME / BA_EXAM_NAME.
                "Salesforce Platform App Builder",
                PAB_EXAM_NAME,
                "pab",
                "app-builder",
                # Internal certification_code alias (PAB-EXP-05): callers may
                # pass the snake_case code used in certifications.certification_code
                # and evidence fixtures without using the full exam_name string.
                CERTIFICATION_CODES[PAB_EXAM_NAME],
                # NOTE: "app-401" was intentionally removed (PAB-EXP-02):
                # no repository evidence supports it as an official or
                # established internal identifier.
            }
        ),
        domains=(
            CertificationDomain(
                domain_id="salesforce_fundamentals",
                domain_name="Salesforce Fundamentals",
                weight=23,
                coverage_topics=(
                    "Standard Salesforce objects",
                    "Declarative versus programmatic customization boundaries",
                    "Object, record, and field access",
                    "Sharing solutions",
                    "Reports, report types, and dashboards",
                    "Salesforce mobile capabilities",
                    "Chatter",
                    "AppExchange or official extension capabilities relevant to the certification",
                ),
            ),
            CertificationDomain(
                domain_id="user_interface",
                domain_name="User Interface",
                weight=17,
                coverage_topics=(
                    "User-interface customization options",
                    "Custom buttons, links, and actions",
                    "Record types",
                    "Lightning App Builder",
                    "Standard and custom Lightning components",
                    "Declarative versus programmatic UI customization",
                ),
            ),
            CertificationDomain(
                domain_id="data_modeling_and_management",
                domain_name="Data Modeling and Management",
                weight=22,
                coverage_topics=(
                    "Core CRM object capabilities",
                    "Appropriate data-model selection",
                    "Lookup and master-detail relationships",
                    "Relationship effects on access, ownership, deletion, user interface, and reporting",
                    "Field-type selection",
                    "Field-type conversion considerations",
                    "Schema Builder",
                    "Data import and export",
                    "External objects",
                    "External data relationships",
                ),
            ),
            CertificationDomain(
                domain_id="business_logic_and_process_automation",
                domain_name="Business Logic and Process Automation",
                weight=28,
                coverage_topics=(
                    "Formula fields",
                    "Roll-up summary fields",
                    "Validation rules",
                    "Approval processes",
                    "Salesforce Flow",
                    "Automation-tool selection",
                    "Order of execution",
                    "Recursion considerations",
                    "Automation conflicts and errors",
                    "Legacy Workflow Rule and Process Builder concepts only where required for exam coverage, migration scenarios, or existing implementations",
                ),
                policy_guidance=(LEGACY_AUTOMATION_GUARDRAIL,),
            ),
            CertificationDomain(
                domain_id="app_deployment",
                domain_name="App Deployment",
                weight=10,
                coverage_topics=(
                    "Application lifecycle management",
                    "Sandbox types and selection",
                    "Change sets",
                    "Managed packages",
                    "Unmanaged packages",
                    "Deployment planning",
                    "Deployment validation",
                    "Deployment troubleshooting considerations",
                ),
            ),
        ),
        enforce_domain_contract_at_request_validation=True,
    )


CERTIFICATION_DEFINITIONS: Tuple[CertificationDefinition, ...] = (
    _adm_definition(),
    _ba_definition(),
    _pab_definition(),
)

_DEFINITION_BY_EXAM_NAME: Dict[str, CertificationDefinition] = {
    definition.exam_name: definition for definition in CERTIFICATION_DEFINITIONS
}

_ALIAS_TO_EXAM_NAME: Dict[str, str] = {}
for _definition in CERTIFICATION_DEFINITIONS:
    for _alias in _definition.aliases:
        _ALIAS_TO_EXAM_NAME[_normalize_certification_key(_alias)] = _definition.exam_name
    _ALIAS_TO_EXAM_NAME[_normalize_certification_key(_definition.exam_name)] = (
        _definition.exam_name
    )

SUPPORTED_EXAM_NAMES: FrozenSet[str] = frozenset(_DEFINITION_BY_EXAM_NAME)


def normalize_certification_exam_name(raw: str) -> Optional[str]:
    """Resolve a raw certification string or alias to the canonical exam name."""
    key = _normalize_certification_key(raw)
    if not key:
        return None
    return _ALIAS_TO_EXAM_NAME.get(key)


def get_certification_definition(exam_name: str) -> Optional[CertificationDefinition]:
    """Look up a certification's engine profile by canonical name or alias.

    Returns ``None`` for any certification the engine does not have a
    profile for — this says nothing about whether the certification exists
    in the ``certifications`` database table.
    """
    canonical = normalize_certification_exam_name(exam_name)
    if canonical is None:
        return None
    return _DEFINITION_BY_EXAM_NAME.get(canonical)


def get_platform_app_builder_definition() -> CertificationDefinition:
    definition = _DEFINITION_BY_EXAM_NAME[PAB_EXAM_NAME]
    return definition


def get_business_analyst_definition() -> CertificationDefinition:
    definition = _DEFINITION_BY_EXAM_NAME[BA_EXAM_NAME]
    return definition


def list_supported_exam_names() -> Tuple[str, ...]:
    return tuple(sorted(SUPPORTED_EXAM_NAMES))


def validate_supported_certification_exam_name(raw: str) -> str:
    """Return the canonical exam name or raise ``CertificationRegistryError``."""
    canonical = normalize_certification_exam_name(raw)
    if canonical is None or canonical not in SUPPORTED_EXAM_NAMES:
        raise CertificationRegistryError(
            f"unsupported certification_exam_name: {raw!r}"
        )
    return canonical


def validate_certification_domain(exam_name: str, domain_name: str) -> str:
    """Validate a domain against this module's engine-profile domain taxonomy.

    This is an engine-capability check only: it compares ``domain_name``
    against each domain's persisted-facing display name
    (``CertificationDomain.domain_name``), never against the internal
    ``domain_id``. It does not query ``public.certification_domains`` and
    does not guarantee a matching database row exists — callers still need
    ``QuestionCandidateRepository.certification_domain_exists()`` (or
    equivalent) to confirm persistence-time existence.

    Returns the canonical exam name. Raises ``CertificationRegistryError`` when
    the certification or domain is not recognized by the engine profile.
    """
    canonical = validate_supported_certification_exam_name(exam_name)
    cleaned_domain = domain_name.strip()
    if not cleaned_domain:
        raise CertificationRegistryError("domain must not be blank")

    definition = _DEFINITION_BY_EXAM_NAME[canonical]
    if cleaned_domain not in definition.domain_names:
        raise CertificationRegistryError(
            f"unsupported domain {cleaned_domain!r} for certification {canonical!r}"
        )
    return canonical


def validate_generation_request_certification(
    *,
    certification_exam_name: str,
    domain: str,
) -> str:
    """Validate certification and domain for a generation request.

    Platform App Builder and Business Analyst enforce their full domain
    contracts at request validation time. Administrator behavior is preserved
    by accepting any non-blank domain once the certification itself is
    recognized.

    This is an engine-capability pre-check that runs before, and is strictly
    stricter-or-equal to, the database persistence gate
    (``certification_domain_exists()``). Passing here does not guarantee the
    exact caller-supplied ``certification_exam_name``/``domain`` strings match
    an active row in ``public.certification_domains`` — the database gate
    still performs that check verbatim (unnormalized) immediately afterward.
    Callers must not substitute this function's canonicalized return value
    for the original request fields; doing so would let the database gate
    silently diverge from what the caller actually asked to persist.
    """
    canonical = validate_supported_certification_exam_name(certification_exam_name)
    cleaned_domain = domain.strip()
    if not cleaned_domain:
        raise CertificationRegistryError("domain must not be blank")

    definition = _DEFINITION_BY_EXAM_NAME[canonical]
    if definition.enforce_domain_contract_at_request_validation:
        validate_certification_domain(canonical, cleaned_domain)
    return canonical


def validate_audit_retry_context_certification(
    *,
    certification_exam_name: str,
    domain: str,
) -> str:
    """Validate certification/domain for audit retry rebuilt from a candidate row."""
    return validate_generation_request_certification(
        certification_exam_name=certification_exam_name,
        domain=domain,
    )


def validate_frozen_audit_context_certification(
    *,
    certification_exam_name: str,
    domain_name: str,
) -> str:
    """Validate certification/domain loaded from frozen blind audit context."""
    return validate_generation_request_certification(
        certification_exam_name=certification_exam_name,
        domain=domain_name,
    )


def domain_metadata_for_certification(
    exam_name: str,
    domain_name: str,
) -> Mapping[str, object]:
    """Return registry metadata for one domain, when defined."""
    definition = get_certification_definition(exam_name)
    if definition is None:
        raise CertificationRegistryError(
            f"unsupported certification_exam_name: {exam_name!r}"
        )
    for domain in definition.domains:
        if domain.domain_name == domain_name:
            return {
                "domain_id": domain.domain_id,
                "domain_name": domain.domain_name,
                "weight": domain.weight,
                "coverage_topics": list(domain.coverage_topics),
                "policy_guidance": list(domain.policy_guidance),
            }
    raise CertificationRegistryError(
        f"unsupported domain {domain_name!r} for certification {definition.exam_name!r}"
    )
