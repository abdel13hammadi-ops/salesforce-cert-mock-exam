"""Tests for workers.certification_registry and certification validation wiring."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import workers.certification_registry as certification_registry
from workers.ai_quality_audit_context import (
    AiQualityAuditContextError,
    load_blind_audit_context,
)
from workers.certification_registry import (
    ADM_EXAM_NAME,
    BA_EXAM_NAME,
    CERTIFICATION_CODES,
    DECIMAL_DOMAIN_WEIGHT_PUBLISHED_TOTAL,
    DECIMAL_DOMAIN_WEIGHT_TOTAL_TOLERANCE,
    INTEGER_DOMAIN_WEIGHT_TOTAL,
    LEGACY_AUTOMATION_GUARDRAIL,
    PAB_EXAM_NAME,
    SCC_EXAM_NAME,
    SVC_EXAM_NAME,
    CertificationDefinition,
    CertificationRegistryError,
    domain_metadata_for_certification,
    get_business_analyst_definition,
    get_certification_definition,
    get_platform_app_builder_definition,
    get_sales_cloud_consultant_definition,
    get_service_cloud_consultant_definition,
    normalize_certification_exam_name,
    validate_audit_retry_context_certification,
    validate_certification_domain,
    validate_frozen_audit_context_certification,
    validate_generation_request_certification,
    validate_supported_certification_exam_name,
)
from workers.question_candidate_generation import (
    AuditRetryContext,
    CandidateValidationError,
    GenerationRequest,
    enqueue_candidate_audits,
    load_audit_retry_context,
    validate_generation_request,
)


class TestPlatformAppBuilderRegistry(unittest.TestCase):
    def test_registry_lookup_returns_canonical_definition(self):
        definition = get_certification_definition("Salesforce Platform App Builder")
        self.assertIsNotNone(definition)
        assert definition is not None
        self.assertEqual(definition.exam_name, PAB_EXAM_NAME)

    def test_exact_domain_names(self):
        definition = get_platform_app_builder_definition()
        self.assertEqual(
            tuple(domain.domain_name for domain in definition.domains),
            (
                "Salesforce Fundamentals",
                "User Interface",
                "Data Modeling and Management",
                "Business Logic and Process Automation",
                "App Deployment",
            ),
        )

    def test_exact_domain_weights(self):
        definition = get_platform_app_builder_definition()
        self.assertEqual(
            [domain.weight for domain in definition.domains],
            [23, 17, 22, 28, 10],
        )

    def test_domain_weight_total_equals_100(self):
        definition = get_platform_app_builder_definition()
        self.assertEqual(definition.total_domain_weight, 100)

    def test_stable_unique_domain_identifiers(self):
        definition = get_platform_app_builder_definition()
        domain_ids = [domain.domain_id for domain in definition.domains]
        self.assertEqual(
            domain_ids,
            [
                "salesforce_fundamentals",
                "user_interface",
                "data_modeling_and_management",
                "business_logic_and_process_automation",
                "app_deployment",
            ],
        )
        self.assertEqual(len(domain_ids), len(set(domain_ids)))

    def test_legacy_automation_guardrail_present(self):
        definition = get_platform_app_builder_definition()
        automation_domain = definition.domains[3]
        self.assertIn(LEGACY_AUTOMATION_GUARDRAIL, automation_domain.policy_guidance)

    def test_canonical_exam_name_matches_repo_db_row_naming_convention(self):
        # Matches the "Salesforce Certified <X>" pattern used by
        # ADM_EXAM_NAME / BA_EXAM_NAME throughout the repo (e.g.
        # workers/structural_audit_launcher.py), not the bare
        # "Salesforce Platform App Builder" form, which is an alias only.
        self.assertEqual(PAB_EXAM_NAME, "Salesforce Certified Platform App Builder")

    def test_all_certification_domain_weights_use_documented_totals(self):
        for definition in certification_registry.CERTIFICATION_DEFINITIONS:
            total = definition.total_domain_weight
            if definition.uses_integral_domain_weights:
                self.assertEqual(
                    total,
                    INTEGER_DOMAIN_WEIGHT_TOTAL,
                    msg=f"{definition.exam_name} integer domain weights must total 100",
                )
            else:
                self.assertAlmostEqual(
                    total,
                    DECIMAL_DOMAIN_WEIGHT_PUBLISHED_TOTAL,
                    delta=DECIMAL_DOMAIN_WEIGHT_TOTAL_TOLERANCE,
                    msg=(
                        f"{definition.exam_name} published decimal domain weights "
                        f"must total approximately {DECIMAL_DOMAIN_WEIGHT_PUBLISHED_TOTAL}"
                    ),
                )

    def test_domain_ids_are_globally_unique_across_certifications(self):
        all_domain_ids = [
            domain.domain_id
            for definition in certification_registry.CERTIFICATION_DEFINITIONS
            for domain in definition.domains
        ]
        self.assertEqual(len(all_domain_ids), len(set(all_domain_ids)))


class TestBusinessAnalystRegistry(unittest.TestCase):
    def test_registry_lookup_returns_canonical_definition(self):
        definition = get_certification_definition("Salesforce Business Analyst")
        self.assertIsNotNone(definition)
        assert definition is not None
        self.assertEqual(definition.exam_name, BA_EXAM_NAME)

    def test_exact_domain_names(self):
        definition = get_business_analyst_definition()
        self.assertEqual(
            tuple(domain.domain_name for domain in definition.domains),
            (
                "Customer Discovery",
                "Collaboration with Stakeholders",
                "Business Process Mapping",
                "Requirements",
                "User Stories",
                "User Acceptance",
            ),
        )

    def test_domain_weight_total_equals_100(self):
        definition = get_business_analyst_definition()
        self.assertEqual(definition.total_domain_weight, 100)

    def test_ba_201_internal_code_resolves_to_canonical_certification(self):
        self.assertEqual(CERTIFICATION_CODES[BA_EXAM_NAME], "BA-201")
        self.assertEqual(normalize_certification_exam_name("BA-201"), BA_EXAM_NAME)
        self.assertEqual(normalize_certification_exam_name("ba-201"), BA_EXAM_NAME)


class TestSalesCloudConsultantRegistry(unittest.TestCase):
    def test_registry_lookup_returns_canonical_definition(self):
        definition = get_certification_definition(SCC_EXAM_NAME)
        self.assertIsNotNone(definition)
        assert definition is not None
        self.assertEqual(definition.exam_name, SCC_EXAM_NAME)

    def test_official_code_is_sales_con_201(self):
        self.assertEqual(CERTIFICATION_CODES[SCC_EXAM_NAME], "Sales-Con-201")

    def test_sales_con_201_resolves_to_canonical_certification(self):
        self.assertEqual(normalize_certification_exam_name("Sales-Con-201"), SCC_EXAM_NAME)
        self.assertEqual(normalize_certification_exam_name("sales-con-201"), SCC_EXAM_NAME)

    def test_semantic_aliases_resolve(self):
        self.assertEqual(
            normalize_certification_exam_name("sales_cloud_consultant"),
            SCC_EXAM_NAME,
        )
        self.assertEqual(normalize_certification_exam_name("scc"), SCC_EXAM_NAME)
        self.assertEqual(
            normalize_certification_exam_name("Salesforce Sales Cloud Consultant"),
            SCC_EXAM_NAME,
        )

    def test_unofficial_scc_201_alias_is_rejected(self):
        self.assertIsNone(normalize_certification_exam_name("SCC-201"))
        self.assertIsNone(normalize_certification_exam_name("scc-201"))

    def test_exact_domain_names(self):
        definition = get_sales_cloud_consultant_definition()
        self.assertEqual(
            tuple(domain.domain_name for domain in definition.domains),
            (
                "Practical Application of Sales Cloud Expertise",
                "Sales Lifecycle",
                "Consulting & Implementation Strategies",
                "Data Management",
                "Predictive and Generative AI",
            ),
        )

    def test_exact_decimal_domain_weights(self):
        definition = get_sales_cloud_consultant_definition()
        self.assertEqual(
            [domain.weight for domain in definition.domains],
            [23.3, 20.0, 25.0, 18.3, 13.3],
        )

    def test_published_decimal_total_is_approximately_99_9(self):
        definition = get_sales_cloud_consultant_definition()
        self.assertAlmostEqual(
            definition.total_domain_weight,
            DECIMAL_DOMAIN_WEIGHT_PUBLISHED_TOTAL,
            delta=DECIMAL_DOMAIN_WEIGHT_TOTAL_TOLERANCE,
        )
        self.assertFalse(definition.uses_integral_domain_weights)

    def test_strict_domain_validation_enabled(self):
        definition = get_sales_cloud_consultant_definition()
        self.assertTrue(definition.enforce_domain_contract_at_request_validation)

    def test_all_five_official_domains_are_accepted(self):
        for domain in get_sales_cloud_consultant_definition().domains:
            with self.subTest(domain=domain.domain_name):
                canonical = validate_generation_request_certification(
                    certification_exam_name=SCC_EXAM_NAME,
                    domain=domain.domain_name,
                )
                self.assertEqual(canonical, SCC_EXAM_NAME)

    def test_cross_certification_domains_are_rejected(self):
        invalid_domains = (
            "Configuration and Setup",
            "Customer Discovery",
            "App Deployment",
            "Some Domain Never Enumerated In The Registry",
        )
        for domain in invalid_domains:
            with self.subTest(domain=domain):
                with self.assertRaises(CertificationRegistryError):
                    validate_generation_request_certification(
                        certification_exam_name=SCC_EXAM_NAME,
                        domain=domain,
                    )

    def test_ampersand_domain_name_must_match_exactly(self):
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=SCC_EXAM_NAME,
                domain="Consulting and Implementation Strategies",
            )

    def test_blank_domain_is_rejected(self):
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=SCC_EXAM_NAME,
                domain="   ",
            )

    def test_domain_metadata_preserves_decimal_weight(self):
        metadata = domain_metadata_for_certification(
            SCC_EXAM_NAME,
            "Practical Application of Sales Cloud Expertise",
        )
        self.assertEqual(metadata["weight"], 23.3)


class TestServiceCloudConsultantRegistry(unittest.TestCase):
    """SVC-EXP-01: engine profile only — no database migration, no evidence
    fixtures, no ingestion, no generation/audit wiring, no activation."""

    def test_registry_lookup_returns_canonical_definition(self):
        definition = get_certification_definition(SVC_EXAM_NAME)
        self.assertIsNotNone(definition)
        assert definition is not None
        self.assertEqual(definition.exam_name, SVC_EXAM_NAME)

    def test_canonical_name_is_correct(self):
        self.assertEqual(SVC_EXAM_NAME, "Salesforce Certified Service Cloud Consultant")

    def test_canonical_internal_code_resolves(self):
        # Requirement 1: the canonical internal code resolves both as the
        # certification_code value and as an alias lookup.
        self.assertEqual(CERTIFICATION_CODES[SVC_EXAM_NAME], "service_cloud_consultant")
        self.assertEqual(
            normalize_certification_exam_name("service_cloud_consultant"),
            SVC_EXAM_NAME,
        )

    def test_canonical_certification_name_resolves(self):
        # Requirement 2.
        self.assertEqual(normalize_certification_exam_name(SVC_EXAM_NAME), SVC_EXAM_NAME)
        definition = get_service_cloud_consultant_definition()
        self.assertEqual(definition.exam_name, SVC_EXAM_NAME)

    def test_official_exam_code_is_registered_as_an_alias_not_the_internal_code(self):
        # The verified official exam code "Service-Con-201" is deliberately
        # NOT the certification_code value (that is the distinct internal
        # code "service_cloud_consultant", per the CertificationDefinition
        # docstring: certification_code is "Never an official Salesforce
        # exam code"). It is registered as an alias instead.
        self.assertNotEqual(CERTIFICATION_CODES[SVC_EXAM_NAME], "Service-Con-201")
        self.assertNotEqual(CERTIFICATION_CODES[SVC_EXAM_NAME], "service-con-201")

    def test_exam_code_resolves(self):
        # Requirement 3.
        self.assertEqual(normalize_certification_exam_name("Service-Con-201"), SVC_EXAM_NAME)
        self.assertEqual(normalize_certification_exam_name("service-con-201"), SVC_EXAM_NAME)
        self.assertEqual(normalize_certification_exam_name("SERVICE-CON-201"), SVC_EXAM_NAME)

    def test_each_approved_alias_resolves(self):
        # Requirement 4.
        approved_aliases = (
            "Salesforce Service Cloud Consultant",
            SVC_EXAM_NAME,
            "service_cloud_consultant",
            "service-con-201",
            "service-cloud",
            "svc",
        )
        for alias in approved_aliases:
            with self.subTest(alias=alias):
                self.assertEqual(normalize_certification_exam_name(alias), SVC_EXAM_NAME)

    def test_ambiguous_aliases_are_not_added(self):
        # Requirement 5.
        for ambiguous in ("service", "cloud", "consultant"):
            with self.subTest(ambiguous=ambiguous):
                self.assertIsNone(normalize_certification_exam_name(ambiguous))

    def test_exactly_eight_domains(self):
        # Requirement 6.
        definition = get_service_cloud_consultant_definition()
        self.assertEqual(len(definition.domains), 8)

    def test_domain_names_and_ordering_match_official_blueprint(self):
        # Requirement 7.
        definition = get_service_cloud_consultant_definition()
        self.assertEqual(
            tuple(domain.domain_name for domain in definition.domains),
            (
                "Industry Knowledge",
                "Implementation Strategies",
                "Service Cloud Solution Design",
                "Knowledge Management",
                "Intake and Interaction Channels",
                "Case Management",
                "Contact Center Analytics",
                "Integrations",
            ),
        )

    def test_domain_weights_match_exactly(self):
        # Requirement 8.
        definition = get_service_cloud_consultant_definition()
        self.assertEqual(
            [domain.weight for domain in definition.domains],
            [12, 12, 15, 12, 13, 13, 13, 10],
        )

    def test_domain_weight_total_equals_100(self):
        # Requirement 9.
        definition = get_service_cloud_consultant_definition()
        self.assertEqual(definition.total_domain_weight, 100)
        self.assertTrue(definition.uses_integral_domain_weights)

    def test_stable_unique_domain_identifiers(self):
        definition = get_service_cloud_consultant_definition()
        domain_ids = [domain.domain_id for domain in definition.domains]
        self.assertEqual(
            domain_ids,
            [
                "industry_knowledge",
                "implementation_strategies",
                "service_cloud_solution_design",
                "knowledge_management",
                "intake_and_interaction_channels",
                "case_management",
                "contact_center_analytics",
                "integrations",
            ],
        )
        self.assertEqual(len(domain_ids), len(set(domain_ids)))

    def test_strict_domain_validation_enabled(self):
        definition = get_service_cloud_consultant_definition()
        self.assertTrue(definition.enforce_domain_contract_at_request_validation)

    def test_all_eight_official_domains_are_accepted(self):
        for domain in get_service_cloud_consultant_definition().domains:
            with self.subTest(domain=domain.domain_name):
                canonical = validate_generation_request_certification(
                    certification_exam_name=SVC_EXAM_NAME,
                    domain=domain.domain_name,
                )
                self.assertEqual(canonical, SVC_EXAM_NAME)

    def test_cross_certification_domains_are_rejected(self):
        invalid_domains = (
            "Configuration and Setup",
            "Customer Discovery",
            "App Deployment",
            "Sales Lifecycle",
            "Some Domain Never Enumerated In The Registry",
        )
        for domain in invalid_domains:
            with self.subTest(domain=domain):
                with self.assertRaises(CertificationRegistryError):
                    validate_generation_request_certification(
                        certification_exam_name=SVC_EXAM_NAME,
                        domain=domain,
                    )

    def test_blank_domain_is_rejected(self):
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=SVC_EXAM_NAME,
                domain="   ",
            )

    def test_domain_id_is_rejected_as_a_domain_value(self):
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=SVC_EXAM_NAME,
                domain="case_management",  # domain_id, not domain_name
            )

    def test_audit_retry_context_accepts_service_cloud_consultant(self):
        canonical = validate_audit_retry_context_certification(
            certification_exam_name="svc",
            domain="Case Management",
        )
        self.assertEqual(canonical, SVC_EXAM_NAME)

    def test_validate_certification_domain_for_service_cloud_consultant(self):
        canonical = validate_certification_domain(
            SVC_EXAM_NAME,
            "Contact Center Analytics",
        )
        self.assertEqual(canonical, SVC_EXAM_NAME)

    def test_domain_metadata_preserves_integer_weight(self):
        metadata = domain_metadata_for_certification(
            SVC_EXAM_NAME,
            "Service Cloud Solution Design",
        )
        self.assertEqual(metadata["weight"], 15)

    def test_registry_module_has_no_evidence_ingestion_or_database_dependency(self):
        # SVC-EXP-01 is registry-only: adding this profile must not pull in
        # any evidence-fixture, ingestion, or database dependency into this
        # module (mirrors TestEngineProfileVersusPersistenceCatalogSeparation
        # below, re-asserted here specifically for the new certification).
        self.assertFalse(hasattr(certification_registry, "official_evidence_seed"))
        self.assertFalse(hasattr(certification_registry, "official_evidence_fixture_ingestion"))
        self.assertFalse(hasattr(certification_registry, "client"))
        self.assertFalse(hasattr(certification_registry, "supabase"))


class TestExistingCertificationsUnchangedAfterServiceCloudAddition(unittest.TestCase):
    """SVC-EXP-01 requirement 11: adding SVC must not alter any existing
    certification's identity or domain profile."""

    def test_certification_definition_count_increased_by_exactly_one(self):
        self.assertEqual(len(certification_registry.CERTIFICATION_DEFINITIONS), 5)

    def test_administrator_identity_and_domains_unchanged(self):
        definition = get_certification_definition(ADM_EXAM_NAME)
        assert definition is not None
        self.assertEqual(CERTIFICATION_CODES[ADM_EXAM_NAME], "ADM-201")
        self.assertEqual(len(definition.domains), 8)
        self.assertEqual(definition.total_domain_weight, 100)

    def test_business_analyst_identity_and_domains_unchanged(self):
        definition = get_business_analyst_definition()
        self.assertEqual(CERTIFICATION_CODES[BA_EXAM_NAME], "BA-201")
        self.assertEqual(len(definition.domains), 6)
        self.assertEqual(definition.total_domain_weight, 100)

    def test_platform_app_builder_identity_and_domains_unchanged(self):
        definition = get_platform_app_builder_definition()
        self.assertEqual(CERTIFICATION_CODES[PAB_EXAM_NAME], "platform_app_builder")
        self.assertEqual(len(definition.domains), 5)
        self.assertEqual(definition.total_domain_weight, 100)

    def test_sales_cloud_consultant_identity_and_domains_unchanged(self):
        definition = get_sales_cloud_consultant_definition()
        self.assertEqual(CERTIFICATION_CODES[SCC_EXAM_NAME], "Sales-Con-201")
        self.assertEqual(len(definition.domains), 5)
        self.assertAlmostEqual(
            definition.total_domain_weight,
            DECIMAL_DOMAIN_WEIGHT_PUBLISHED_TOTAL,
            delta=DECIMAL_DOMAIN_WEIGHT_TOTAL_TOLERANCE,
        )

    def test_service_cloud_consultant_does_not_normalize_from_sales_cloud_aliases(self):
        self.assertEqual(normalize_certification_exam_name("scc"), SCC_EXAM_NAME)
        self.assertEqual(normalize_certification_exam_name("svc"), SVC_EXAM_NAME)
        self.assertNotEqual(
            normalize_certification_exam_name("scc"),
            normalize_certification_exam_name("svc"),
        )


class TestCertificationCodeIdentifierDecision(unittest.TestCase):
    """PAB-EXP-02: APP-401 had no repository evidence and was removed."""

    def test_app_401_is_not_used_as_platform_app_builder_internal_code(self):
        self.assertNotEqual(CERTIFICATION_CODES[PAB_EXAM_NAME], "APP-401")
        self.assertEqual(CERTIFICATION_CODES[PAB_EXAM_NAME], "platform_app_builder")

    def test_app_401_alias_no_longer_normalizes(self):
        self.assertIsNone(normalize_certification_exam_name("app-401"))
        self.assertIsNone(normalize_certification_exam_name("APP-401"))

    def test_established_codes_for_existing_certifications_are_unchanged(self):
        # ADM-201 / BA-201 remain: evidenced by workers/official_evidence_seed.py.
        self.assertEqual(CERTIFICATION_CODES[ADM_EXAM_NAME], "ADM-201")
        self.assertEqual(CERTIFICATION_CODES[BA_EXAM_NAME], "BA-201")
        self.assertEqual(CERTIFICATION_CODES[PAB_EXAM_NAME], "platform_app_builder")


class TestEngineProfileVersusPersistenceCatalogSeparation(unittest.TestCase):
    """Guards against the registry becoming a second persistence source of truth."""

    def test_registry_module_has_no_database_client_dependency(self):
        self.assertFalse(hasattr(certification_registry, "client"))
        self.assertFalse(hasattr(certification_registry, "certification_domain_exists"))
        self.assertFalse(hasattr(certification_registry, "supabase"))

    def test_registry_functions_do_not_accept_a_client_argument(self):
        import inspect

        for name in (
            "validate_supported_certification_exam_name",
            "validate_certification_domain",
            "validate_generation_request_certification",
            "validate_audit_retry_context_certification",
            "validate_frozen_audit_context_certification",
        ):
            func = getattr(certification_registry, name)
            params = inspect.signature(func).parameters
            self.assertNotIn("client", params)


class TestCertificationNormalization(unittest.TestCase):
    def test_platform_app_builder_aliases_normalize(self):
        self.assertEqual(
            normalize_certification_exam_name("Salesforce Platform App Builder"),
            PAB_EXAM_NAME,
        )
        self.assertEqual(
            normalize_certification_exam_name("pab"),
            PAB_EXAM_NAME,
        )
        self.assertEqual(
            normalize_certification_exam_name("platform_app_builder"),
            PAB_EXAM_NAME,
        )

    def test_administrator_aliases_normalize(self):
        self.assertEqual(
            normalize_certification_exam_name("Salesforce Administrator"),
            ADM_EXAM_NAME,
        )
        self.assertEqual(
            normalize_certification_exam_name("adm-201"),
            ADM_EXAM_NAME,
        )

    def test_business_analyst_aliases_normalize(self):
        self.assertEqual(
            normalize_certification_exam_name("Salesforce Business Analyst"),
            BA_EXAM_NAME,
        )
        self.assertEqual(
            normalize_certification_exam_name("ba-201"),
            BA_EXAM_NAME,
        )
        self.assertEqual(
            normalize_certification_exam_name("BA-201"),
            BA_EXAM_NAME,
        )

    def test_sales_cloud_consultant_aliases_normalize(self):
        self.assertEqual(
            normalize_certification_exam_name("Salesforce Sales Cloud Consultant"),
            SCC_EXAM_NAME,
        )
        self.assertEqual(
            normalize_certification_exam_name("Sales-Con-201"),
            SCC_EXAM_NAME,
        )
        self.assertEqual(
            normalize_certification_exam_name("sales-cloud-consultant"),
            SCC_EXAM_NAME,
        )

    def test_service_cloud_consultant_aliases_normalize(self):
        self.assertEqual(
            normalize_certification_exam_name("Salesforce Service Cloud Consultant"),
            SVC_EXAM_NAME,
        )
        self.assertEqual(
            normalize_certification_exam_name("Service-Con-201"),
            SVC_EXAM_NAME,
        )
        self.assertEqual(
            normalize_certification_exam_name("service-cloud"),
            SVC_EXAM_NAME,
        )
        self.assertEqual(
            normalize_certification_exam_name("service_cloud_consultant"),
            SVC_EXAM_NAME,
        )
        self.assertEqual(normalize_certification_exam_name("svc"), SVC_EXAM_NAME)

    def test_unexpected_certification_string_is_not_guessed(self):
        self.assertIsNone(
            normalize_certification_exam_name("Salesforce Certified Data Cloud Consultant")
        )


class TestGenerationRequestValidation(unittest.TestCase):
    def _pab_request(self, *, domain: str) -> GenerationRequest:
        return GenerationRequest(
            certification_exam_name="Salesforce Certified Platform App Builder",
            domain=domain,
            prompt_template_id="certbound-question-gen",
            prompt_version="v1.0.0",
            model_name="claude-test",
            created_by="tester@certbound.internal",
            source_evidence={"resource_reference": "Salesforce Help"},
        )

    def test_generation_request_accepts_platform_app_builder(self):
        validate_generation_request(self._pab_request(domain="User Interface"))

    def test_generation_request_rejects_unknown_platform_app_builder_domain(self):
        request = self._pab_request(domain="Security and Access")
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_generation_request_rejects_unsupported_certification(self):
        request = GenerationRequest(
            certification_exam_name="Salesforce Certified Data Cloud Consultant",
            domain="Any Domain",
            prompt_template_id="certbound-question-gen",
            prompt_version="v1.0.0",
            model_name="claude-test",
            created_by="tester@certbound.internal",
            source_evidence={"resource_reference": "Salesforce Help"},
        )
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_administrator_regression_still_accepted(self):
        request = GenerationRequest(
            certification_exam_name="Salesforce Administrator",
            domain="Security and Access",
            prompt_template_id="certbound-question-gen",
            prompt_version="v1.0.0",
            model_name="claude-test",
            created_by="tester@certbound.internal",
            source_evidence={"resource_reference": "Salesforce Help"},
        )
        validate_generation_request(request)

    def _ba_request(self, *, domain: str) -> GenerationRequest:
        return GenerationRequest(
            certification_exam_name=BA_EXAM_NAME,
            domain=domain,
            prompt_template_id="certbound-question-gen",
            prompt_version="v1.0.0",
            model_name="claude-test",
            created_by="tester@certbound.internal",
            source_evidence={"resource_reference": "Salesforce Help"},
        )

    def test_generation_request_accepts_business_analyst(self):
        validate_generation_request(self._ba_request(domain="Requirements"))

    def test_generation_request_rejects_unknown_business_analyst_domain(self):
        request = self._ba_request(domain="Configuration and Setup")
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def _scc_request(self, *, domain: str) -> GenerationRequest:
        return GenerationRequest(
            certification_exam_name=SCC_EXAM_NAME,
            domain=domain,
            prompt_template_id="certbound-question-gen",
            prompt_version="v1.0.0",
            model_name="claude-test",
            created_by="tester@certbound.internal",
            source_evidence={"resource_reference": "Salesforce Help"},
        )

    def test_generation_request_accepts_sales_cloud_consultant(self):
        validate_generation_request(
            self._scc_request(domain="Sales Lifecycle")
        )

    def test_generation_request_rejects_unknown_sales_cloud_consultant_domain(self):
        request = self._scc_request(domain="User Interface")
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def _svc_request(self, *, domain: str) -> GenerationRequest:
        return GenerationRequest(
            certification_exam_name=SVC_EXAM_NAME,
            domain=domain,
            prompt_template_id="certbound-question-gen",
            prompt_version="v1.0.0",
            model_name="claude-test",
            created_by="tester@certbound.internal",
            source_evidence={"resource_reference": "Salesforce Help"},
        )

    def test_generation_request_accepts_service_cloud_consultant(self):
        validate_generation_request(self._svc_request(domain="Case Management"))

    def test_generation_request_rejects_unknown_service_cloud_consultant_domain(self):
        request = self._svc_request(domain="User Interface")
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)


class TestAuditRetryAndFrozenContextValidation(unittest.TestCase):
    def test_audit_retry_context_accepts_platform_app_builder(self):
        canonical = validate_audit_retry_context_certification(
            certification_exam_name="Salesforce Platform App Builder",
            domain="App Deployment",
        )
        self.assertEqual(canonical, PAB_EXAM_NAME)

    def test_frozen_audit_context_accepts_administrator_alias(self):
        canonical = validate_frozen_audit_context_certification(
            certification_exam_name="adm-201",
            domain_name="Configuration",
        )
        self.assertEqual(canonical, ADM_EXAM_NAME)

    def test_frozen_audit_context_rejects_unsupported_certification(self):
        with self.assertRaises(CertificationRegistryError):
            validate_frozen_audit_context_certification(
                certification_exam_name="Salesforce Certified Data Cloud Consultant",
                domain_name="Any Domain",
            )

    def test_validate_certification_domain_for_platform_app_builder(self):
        canonical = validate_certification_domain(
            "Salesforce Certified Platform App Builder",
            "Business Logic and Process Automation",
        )
        self.assertEqual(canonical, PAB_EXAM_NAME)

    def test_audit_retry_context_accepts_business_analyst(self):
        canonical = validate_audit_retry_context_certification(
            certification_exam_name="ba-201",
            domain="User Stories",
        )
        self.assertEqual(canonical, BA_EXAM_NAME)

    def test_validate_certification_domain_for_business_analyst(self):
        canonical = validate_certification_domain(
            "Salesforce Certified Business Analyst",
            "Customer Discovery",
        )
        self.assertEqual(canonical, BA_EXAM_NAME)

    def test_audit_retry_context_accepts_sales_cloud_consultant(self):
        canonical = validate_audit_retry_context_certification(
            certification_exam_name="scc",
            domain="Data Management",
        )
        self.assertEqual(canonical, SCC_EXAM_NAME)


class TestDomainBoundaryEnforcement(unittest.TestCase):
    """Internal domain_id values must never be accepted as a request domain."""

    def test_domain_id_is_rejected_as_a_domain_value_for_platform_app_builder(self):
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=PAB_EXAM_NAME,
                domain="user_interface",  # domain_id, not domain_name
            )

    def test_cross_certification_domain_is_rejected(self):
        # Administrator's "Configuration and Setup" is not a Platform App
        # Builder domain and must not be silently accepted.
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=PAB_EXAM_NAME,
                domain="Configuration and Setup",
            )

    def test_blank_domain_is_rejected_for_platform_app_builder(self):
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=PAB_EXAM_NAME,
                domain="   ",
            )

    def test_administrator_accepts_any_non_blank_domain_unchanged(self):
        # Administrator never enforced a domain contract at request-validation
        # time; real existence is left entirely to certification_domain_exists().
        # This must remain true so historical generation callers are unaffected.
        canonical = validate_generation_request_certification(
            certification_exam_name=ADM_EXAM_NAME,
            domain="Some Domain Never Enumerated In The Registry",
        )
        self.assertEqual(canonical, ADM_EXAM_NAME)

    def test_business_analyst_rejects_unregistered_domain(self):
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=BA_EXAM_NAME,
                domain="Some Domain Never Enumerated In The Registry",
            )

    def test_business_analyst_rejects_administrator_domain(self):
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=BA_EXAM_NAME,
                domain="Configuration and Setup",
            )

    def test_business_analyst_rejects_platform_app_builder_domain(self):
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=BA_EXAM_NAME,
                domain="App Deployment",
            )

    def test_business_analyst_rejects_blank_domain(self):
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=BA_EXAM_NAME,
                domain="   ",
            )

    def test_all_six_business_analyst_domains_are_accepted(self):
        for domain in (
            "Customer Discovery",
            "Collaboration with Stakeholders",
            "Business Process Mapping",
            "Requirements",
            "User Stories",
            "User Acceptance",
        ):
            with self.subTest(domain=domain):
                canonical = validate_generation_request_certification(
                    certification_exam_name=BA_EXAM_NAME,
                    domain=domain,
                )
                self.assertEqual(canonical, BA_EXAM_NAME)

    def test_sales_cloud_consultant_rejects_unregistered_domain(self):
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=SCC_EXAM_NAME,
                domain="Some Domain Never Enumerated In The Registry",
            )

    def test_sales_cloud_consultant_rejects_platform_app_builder_domain(self):
        with self.assertRaises(CertificationRegistryError):
            validate_generation_request_certification(
                certification_exam_name=SCC_EXAM_NAME,
                domain="Salesforce Fundamentals",
            )


class TestNoMutationOfFrozenOrHistoricalValues(unittest.TestCase):
    """Registry validation must never rewrite caller-supplied strings."""

    def test_frozen_blind_context_certification_and_domain_are_not_rewritten(self):
        from tests.test_ai_quality_audit_context import FakeSupabase, _QVID, _blind_row

        client = FakeSupabase()
        client.set_rpc_response(
            "get_question_version_blind_context_v1",
            [_blind_row(certification_exam_name="adm-201", domain_name="Configuration")],
        )
        context = load_blind_audit_context(client, _QVID)
        # Must round-trip verbatim, not get canonicalized to ADM_EXAM_NAME.
        self.assertEqual(context["certification_exam_name"], "adm-201")
        self.assertEqual(context["domain_name"], "Configuration")

    def test_audit_retry_context_certification_and_domain_are_not_rewritten(self):
        from tests.test_question_candidate_generation import FakeSupabase

        fake = FakeSupabase()
        fake.tables["question_candidates"] = [
            {
                "id": "candidate-1",
                "content_hash": "abc123",
                "certification_exam_name": "adm-201",
                "category": "Some Historical Domain",
                "question_text": "Sample?",
                "explanation": "Because.",
                "question_type": "single",
                "select_count": 1,
                "candidate_payload": {
                    "options": [
                        {"option_label": "A", "option_text": "One", "display_order": 1},
                        {"option_label": "B", "option_text": "Two", "display_order": 2},
                    ],
                    "provenance": {
                        "source_evidence": {"resource_reference": "Salesforce Help"},
                        "prompt_template_id": "certbound-question-gen",
                        "prompt_version": "v1.0.0",
                        "model_name": "claude-test",
                    },
                },
                "metadata": {"domain": "Some Historical Domain"},
            }
        ]
        context = load_audit_retry_context(fake, "candidate-1")
        self.assertEqual(context.request.certification_exam_name, "adm-201")
        self.assertEqual(context.request.domain, "Some Historical Domain")


class TestAuditEnqueueValidation(unittest.TestCase):
    def test_enqueue_candidate_audits_rejects_unsupported_certification_before_any_insert(self):
        from tests.test_question_candidate_generation import FakeSupabase

        fake = FakeSupabase()
        request = GenerationRequest(
            certification_exam_name="Salesforce Certified Data Cloud Consultant",
            domain="Any Domain",
            prompt_template_id="certbound-question-gen",
            prompt_version="v1.0.0",
            model_name="claude-test",
            created_by="tester@certbound.internal",
            source_evidence={"resource_reference": "Salesforce Help"},
        )
        with self.assertRaises(CandidateValidationError):
            enqueue_candidate_audits(
                fake,
                candidate_id="candidate-1",
                question_snapshot={"question_text": "Sample?", "options": []},
                request=request,
                content_hash="abc123",
            )
        self.assertEqual(fake.insert_calls, [])


class TestBlindContextRegistryIntegration(unittest.TestCase):
    def test_load_blind_audit_context_rejects_unsupported_certification(self):
        from tests.test_ai_quality_audit_context import FakeSupabase, _QVID, _blind_row

        client = FakeSupabase()
        client.set_rpc_response(
            "get_question_version_blind_context_v1",
            [
                _blind_row(
                    certification_exam_name="Salesforce Certified Data Cloud Consultant"
                )
            ],
        )
        with self.assertRaises(AiQualityAuditContextError):
            load_blind_audit_context(client, _QVID)


class TestAuditRetryContextLoader(unittest.TestCase):
    def test_load_audit_retry_context_rejects_unsupported_certification(self):
        from tests.test_question_candidate_generation import FakeSupabase

        fake = FakeSupabase()
        fake.tables["question_candidates"] = [
            {
                "id": "candidate-1",
                "content_hash": "abc123",
                "certification_exam_name": "Salesforce Certified Data Cloud Consultant",
                "category": "Any Domain",
                "question_text": "Sample?",
                "explanation": "Because.",
                "question_type": "single",
                "select_count": 1,
                "candidate_payload": {
                    "options": [
                        {"option_label": "A", "option_text": "One", "display_order": 1},
                        {"option_label": "B", "option_text": "Two", "display_order": 2},
                    ],
                    "provenance": {
                        "source_evidence": {"resource_reference": "Salesforce Help"},
                        "prompt_template_id": "certbound-question-gen",
                        "prompt_version": "v1.0.0",
                        "model_name": "claude-test",
                    },
                },
                "metadata": {"domain": "Any Domain"},
            }
        ]
        with self.assertRaises(CandidateValidationError):
            load_audit_retry_context(fake, "candidate-1")


if __name__ == "__main__":
    unittest.main()
