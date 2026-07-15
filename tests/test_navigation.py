"""Focused tests for centralized CertBound navigation and shell helpers."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.navigation as navigation
from utils.access_control import SESSION_PARAM
from utils.certification_context import (
    EXAM_NAME_QUERY_PARAM,
    validate_exam_name,
)
from utils.navigation import (
    CERTBOUND_ENABLE_SCENARIO_SIMULATOR,
    NAV_ROUTES,
    PRIMARY_LEARNER_LABELS,
    admin_routes,
    build_daily_sprint_href,
    build_nav_href,
    build_practice_domain_href,
    hidden_routes,
    is_feature_flag_enabled,
    is_route_active,
    is_route_visible,
    legal_routes,
    navigation_definitions_for_user,
    primary_learner_routes,
    route_for_key,
    route_for_page_path,
    scenario_simulator_route_when_enabled,
    streamlit_route_for_page,
    validate_unique_route_paths,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCESS_CONTROL_PATH = REPO_ROOT / "utils" / "access_control.py"
RESET_PASSWORD_PATH = REPO_ROOT / "pages" / "Reset_Password.py"
LEGAL_PAGES = {
    "Terms of Service": REPO_ROOT / "pages" / "Terms_of_Service.py",
    "Privacy Policy": REPO_ROOT / "pages" / "Privacy_Policy.py",
    "Refund and Cancellation Policy": REPO_ROOT / "pages" / "Refund_and_Cancellation_Policy.py",
}


class TestNavigationRegistry(unittest.TestCase):
    def test_routes_are_unique(self):
        validate_unique_route_paths()
        keys = [route.key for route in NAV_ROUTES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_primary_learner_labels_exact(self):
        labels = tuple(route.label for route in primary_learner_routes())
        self.assertEqual(labels, PRIMARY_LEARNER_LABELS)

    def test_existing_page_paths_unchanged(self):
        expected = {
            "home": "pages/Dashboard.py",
            "mock_exams": "app.py",
            "progress": "pages/My_Progress.py",
            "practice_by_category": "pages/Practice_By_Category.py",
            "weak_areas_practice": "pages/Weak_Areas_Practice.py",
            "account": "pages/Account.py",
            "support": "pages/Support.py",
            "admin_hub": "pages/Admin.py",
        }
        for key, page_path in expected.items():
            route = route_for_key(key)
            self.assertIsNotNone(route)
            self.assertEqual(route.page_path, page_path)

    def test_streamlit_route_mapping(self):
        self.assertEqual(streamlit_route_for_page("app.py"), "/")
        self.assertEqual(streamlit_route_for_page("pages/Dashboard.py"), "/Dashboard")
        self.assertEqual(streamlit_route_for_page("pages/My_Progress.py"), "/My_Progress")


class TestScenarioSimulatorPlaceholder(unittest.TestCase):
    def test_hidden_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_feature_flag_enabled(CERTBOUND_ENABLE_SCENARIO_SIMULATOR))
            self.assertEqual(hidden_routes(), ())
            self.assertIsNone(scenario_simulator_route_when_enabled())

    def test_enabled_flag_exposes_definition_only(self):
        with patch.dict(os.environ, {CERTBOUND_ENABLE_SCENARIO_SIMULATOR: "true"}, clear=True):
            route = scenario_simulator_route_when_enabled()
            self.assertIsNotNone(route)
            self.assertEqual(route.key, "scenario_simulator")
            self.assertEqual(route.group, navigation.NAV_GROUP_HIDDEN)
            visible = navigation_definitions_for_user(
                access_level="paid",
                is_admin_email=False,
                admin_unlocked=False,
            )
            self.assertNotIn(route, visible)


class TestAdminNavigationVisibility(unittest.TestCase):
    def test_admin_hub_only_for_unlocked_admin(self):
        admin_hub = route_for_key("admin_hub")
        self.assertTrue(
            is_route_visible(
                admin_hub,
                access_level="admin",
                is_admin_email=True,
                admin_unlocked=True,
            )
        )
        self.assertFalse(
            is_route_visible(
                admin_hub,
                access_level="admin",
                is_admin_email=True,
                admin_unlocked=False,
            )
        )

    def test_ordinary_users_do_not_see_admin_definitions(self):
        routes = navigation_definitions_for_user(
            access_level="paid",
            is_admin_email=False,
            admin_unlocked=False,
        )
        self.assertFalse(any(route.group == navigation.NAV_GROUP_ADMIN for route in routes))

    def test_admin_group_contains_expected_routes(self):
        labels = {route.label for route in admin_routes()}
        self.assertEqual(
            labels,
            {
                "Admin Hub",
                "Users",
                "Import",
                "Question Review",
                "Free Mock Curation",
                "Audit Review",
                "Support Tickets",
            },
        )


class TestPublicPages(unittest.TestCase):
    def test_legal_pages_use_public_chrome_without_login_gate(self):
        for label, path in LEGAL_PAGES.items():
            with self.subTest(page=label):
                source = path.read_text(encoding="utf-8")
                self.assertIn("render_public_chrome()", source)
                self.assertNotIn("require_login", source)
                self.assertNotIn("render_app_chrome()", source)
                self.assertNotIn("enforce_session_timeout()", source)

    def test_reset_password_uses_public_chrome(self):
        source = RESET_PASSWORD_PATH.read_text(encoding="utf-8")
        self.assertIn("render_public_chrome()", source)
        self.assertNotIn("render_app_chrome()", source)
        self.assertNotIn("enforce_session_timeout()", source)
        self.assertNotIn("require_login", source)


class TestSessionPreservingLinks(unittest.TestCase):
    def test_fr_session_preserved_in_generated_links(self):
        href = build_nav_href("pages/Dashboard.py", session_token="signed-token-123")
        params = parse_qs(urlparse(href).query)
        self.assertEqual(params[navigation.SESSION_QUERY_PARAM], ["signed-token-123"])

    def test_existing_query_params_preserved_when_adding_session(self):
        href = build_nav_href(
            "pages/Practice_By_Category.py",
            session_token="signed-token-123",
            extra_params={"exam_name": "Salesforce Certified Platform Administrator", "category": "Automation"},
        )
        params = parse_qs(urlparse(href).query)
        self.assertEqual(params[SESSION_PARAM if SESSION_PARAM else navigation.SESSION_QUERY_PARAM][0], "signed-token-123")
        self.assertEqual(params["exam_name"][0], "Salesforce Certified Platform Administrator")
        self.assertEqual(params["category"][0], "Automation")

    def test_daily_sprint_deep_link_shape(self):
        href = build_daily_sprint_href(
            "Salesforce Certified Platform Administrator",
            "Automation",
            session_token="token",
        )
        self.assertTrue(href.endswith("/Practice_By_Category") or "/Practice_By_Category?" in href)
        params = parse_qs(urlparse(href).query)
        self.assertEqual(params["daily_sprint"], ["1"])
        self.assertEqual(params["count"], ["10"])
        self.assertEqual(params["exam_name"][0], "Salesforce Certified Platform Administrator")
        self.assertEqual(params["category"][0], "Automation")

    def test_practice_domain_link_encoding(self):
        href = build_practice_domain_href(
            "Salesforce Certified Platform Administrator",
            "Data & Analytics",
            session_token="token",
        )
        params = parse_qs(urlparse(href).query)
        self.assertEqual(params["category"][0], "Data & Analytics")


class TestCurrentPageState(unittest.TestCase):
    def test_current_page_state_is_deterministic(self):
        route = route_for_page_path("pages/Dashboard.py")
        self.assertIsNotNone(route)
        self.assertTrue(is_route_active(route, "pages/Dashboard.py"))
        self.assertFalse(is_route_active(route, "pages/My_Progress.py"))


class TestEntitlementSeparation(unittest.TestCase):
    def test_premium_navigation_does_not_grant_premium_authorization(self):
        progress = route_for_key("progress")
        self.assertTrue(progress.requires_premium)
        visible = is_route_visible(
            progress,
            access_level="free",
            is_admin_email=False,
            admin_unlocked=False,
        )
        self.assertTrue(visible)


class TestCertificationContextValidation(unittest.TestCase):
    def test_supported_certification_accepted(self):
        supported = ["Salesforce Certified Platform Administrator", "Salesforce Certified Business Analyst"]
        self.assertEqual(
            validate_exam_name("Salesforce Certified Platform Administrator", supported),
            "Salesforce Certified Platform Administrator",
        )

    def test_unsupported_certification_rejected(self):
        supported = ["Salesforce Certified Platform Administrator"]
        self.assertIsNone(validate_exam_name("Totally Fake Certification", supported))


class TestAccessControlIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.access_source = ACCESS_CONTROL_PATH.read_text(encoding="utf-8")

    def test_sidebar_uses_navigation_registry(self):
        self.assertIn("from utils.navigation import", self.access_source)
        self.assertIn("primary_learner_routes", self.access_source)
        self.assertIn("admin_routes", self.access_source)
        self.assertIn("legal_routes", self.access_source)

    def test_public_chrome_exists(self):
        self.assertIn("def render_public_chrome", self.access_source)
        self.assertIn("inject_shell_theme", self.access_source)

    def test_session_query_param_matches_access_control(self):
        self.assertEqual(navigation.SESSION_QUERY_PARAM, SESSION_PARAM)


class TestLegalNavigationDefinitions(unittest.TestCase):
    def test_legal_routes_remain_public(self):
        for route in legal_routes():
            self.assertFalse(route.requires_auth)
            self.assertFalse(route.requires_premium)
            self.assertFalse(route.requires_admin_email)


if __name__ == "__main__":
    unittest.main()
