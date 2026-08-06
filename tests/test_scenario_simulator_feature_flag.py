"""Production feature-flag and Simulator discoverability gate tests.

Covers CERTBOUND_ENABLE_SCENARIO_SIMULATOR parsing and the minimum Premium
sidebar entry added to origin/main access_control.render_sidebar_navigation.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.navigation import (
    CERTBOUND_ENABLE_SCENARIO_SIMULATOR,
    is_feature_flag_enabled,
    route_for_page_path,
)


class TestScenarioSimulatorFeatureFlag(unittest.TestCase):
    def test_variable_name_is_stable(self):
        self.assertEqual(CERTBOUND_ENABLE_SCENARIO_SIMULATOR, "CERTBOUND_ENABLE_SCENARIO_SIMULATOR")

    def test_missing_env_defaults_to_disabled(self):
        env = {k: v for k, v in os.environ.items() if k != CERTBOUND_ENABLE_SCENARIO_SIMULATOR}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(is_feature_flag_enabled(CERTBOUND_ENABLE_SCENARIO_SIMULATOR))

    def test_enabled_values(self):
        for value in ("1", "true", "TRUE", "yes", "on", " Yes "):
            with self.subTest(value=value):
                with patch.dict(os.environ, {CERTBOUND_ENABLE_SCENARIO_SIMULATOR: value}):
                    self.assertTrue(is_feature_flag_enabled(CERTBOUND_ENABLE_SCENARIO_SIMULATOR))

    def test_disabled_values(self):
        for value in ("0", "false", "FALSE", "no", "off", "", "disabled"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {CERTBOUND_ENABLE_SCENARIO_SIMULATOR: value}):
                    self.assertFalse(is_feature_flag_enabled(CERTBOUND_ENABLE_SCENARIO_SIMULATOR))

    def test_none_env_name_means_enabled(self):
        # Helper contract: no env name => treat as enabled (non-gated routes).
        self.assertTrue(is_feature_flag_enabled(None))

    def test_v2_route_is_registered(self):
        route = route_for_page_path("pages/Scenario_Simulator_V2.py")
        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.key, "scenario_simulator_v2")
        self.assertEqual(route.feature_flag_env, CERTBOUND_ENABLE_SCENARIO_SIMULATOR)
        self.assertTrue(route.requires_premium)


class TestScenarioSimulatorSidebarDiscoverability(unittest.TestCase):
    """Prove a visible production chrome path exists for eligible users."""

    def _render_sidebar_markdown(self, *, flag_enabled: bool, access_level: str = "paid") -> str:
        captured: list[str] = []

        class _Sidebar:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        fake_st = types.SimpleNamespace(
            sidebar=_Sidebar(),
            markdown=lambda text, *a, **k: captured.append(str(text)),
            caption=lambda text, *a, **k: captured.append(str(text)),
            divider=lambda: captured.append("---"),
        )

        import utils.access_control as access_control

        with patch.object(access_control, "st", fake_st), \
             patch.object(access_control, "restore_login_from_signed_url"), \
             patch.object(access_control, "_hide_native_sidebar_nav_css"), \
             patch.object(access_control, "get_current_user_email", return_value="learner@example.com"), \
             patch.object(access_control, "get_user_access_level", return_value=access_level), \
             patch.object(access_control, "_current_signed_session_token", return_value=""), \
             patch(
                 "utils.navigation.is_feature_flag_enabled",
                 side_effect=lambda name: bool(flag_enabled) if name else True,
             ):
            access_control.render_sidebar_navigation()

        return "\n".join(captured)

    def test_flag_enabled_renders_simulator_premium_link(self):
        html = self._render_sidebar_markdown(flag_enabled=True, access_level="paid")
        self.assertIn("BA Scenario Simulator", html)
        self.assertIn("/Scenario_Simulator_V2", html)
        self.assertIn("### Premium", html)

    def test_flag_disabled_hides_simulator_link(self):
        html = self._render_sidebar_markdown(flag_enabled=False, access_level="paid")
        self.assertNotIn("BA Scenario Simulator", html)
        self.assertNotIn("/Scenario_Simulator_V2", html)
        # Other premium links remain.
        self.assertIn("Practice By Category", html)

    def test_flag_enabled_still_shows_link_for_free_user_with_premium_caption(self):
        # Matches existing Premium-section convention: links visible, caption warns,
        # page-level require_paid_access remains authoritative.
        html = self._render_sidebar_markdown(flag_enabled=True, access_level="free")
        self.assertIn("BA Scenario Simulator", html)
        self.assertIn("Premium access required", html)


class TestAccessControlSourceKeepsMinimumHunk(unittest.TestCase):
    def test_simulator_link_is_feature_flag_gated_in_source(self):
        source = Path(__file__).resolve().parents[1].joinpath("utils", "access_control.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("pages/Scenario_Simulator_V2.py", source)
        self.assertIn("CERTBOUND_ENABLE_SCENARIO_SIMULATOR", source)
        self.assertIn("is_feature_flag_enabled", source)
        # Must not import the broad redesigned shell helpers.
        self.assertNotIn("inject_shell_theme", source)
        self.assertNotIn("build_nav_href", source)


if __name__ == "__main__":
    unittest.main()
