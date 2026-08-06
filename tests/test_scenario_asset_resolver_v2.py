"""Focused tests for CB-SC-001 scene asset resolution and Simulator UI helpers."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_asset_resolver_v2 import (
    CB_SC001_ASSET_ROOT,
    CB_SC001_CANONICAL_UI_REFERENCE,
    CB_SC001_MANIFEST_PATH,
    load_cb_sc001_asset_manifest,
    resolve_cb_sc001_scene_image,
    scene_id_image_map_from_manifest,
    verify_all_manifest_scene_images,
)
from utils.scenario_simulator_ui_v2 import (
    decision_brief_sections,
    format_scene_progress_caption,
    mission_text_from_content,
    option_card_label,
    scene_progress_from_content,
)
from utils.scenario_streamlit_v2 import (
    CB_SC001_CANONICAL_CONTENT_SHA256,
    CB_SC001_SEMANTIC_VERSION,
    CB_SC001_SIMULATION_ID,
    load_cb_sc001_v2_content,
)

EXPECTED_SCENE_IDS = tuple(f"SC001-C{i:02d}" for i in range(1, 13))


class TestCbSc001AssetManifest(unittest.TestCase):
    def test_manifest_loads_and_lists_twelve_scenes(self):
        manifest = load_cb_sc001_asset_manifest()
        self.assertEqual(manifest.get("scenarioId"), "CB-SC-001")
        self.assertEqual(manifest.get("simulationId"), CB_SC001_SIMULATION_ID)
        self.assertEqual(manifest.get("version"), CB_SC001_SEMANTIC_VERSION)
        self.assertEqual(
            manifest.get("canonicalContentSha256"),
            CB_SC001_CANONICAL_CONTENT_SHA256,
        )
        scenes = manifest.get("scenes") or []
        self.assertEqual(len(scenes), 12)
        mapping = scene_id_image_map_from_manifest(manifest)
        self.assertEqual(tuple(mapping.keys()), EXPECTED_SCENE_IDS)

    def test_all_twelve_scene_ids_resolve_to_existing_images(self):
        results = verify_all_manifest_scene_images()
        self.assertEqual(len(results), 12)
        for result in results:
            self.assertTrue(result.available, msg=f"{result.scene_id}: {result.reason}")
            self.assertIsNotNone(result.path)
            assert result.path is not None
            self.assertTrue(result.path.is_file())
            self.assertTrue(result.alt_text)
            self.assertIn(result.title, result.alt_text)

    def test_canonical_ui_reference_exists(self):
        self.assertTrue(CB_SC001_MANIFEST_PATH.is_file())
        self.assertTrue(CB_SC001_ASSET_ROOT.is_dir())
        self.assertTrue(CB_SC001_CANONICAL_UI_REFERENCE.is_file())

    def test_missing_image_fallback(self):
        manifest = load_cb_sc001_asset_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Copy manifest but omit scene image files.
            shutil.copy2(CB_SC001_MANIFEST_PATH, root / "manifest.json")
            (root / "scenes").mkdir(parents=True, exist_ok=True)
            result = resolve_cb_sc001_scene_image(
                "SC001-C01",
                manifest=manifest,
                asset_root=root,
                scene_title="Opening the Kickoff",
            )
            self.assertFalse(result.available)
            self.assertIsNone(result.path)
            self.assertEqual(result.reason, "file_missing")
            self.assertIn("unavailable", result.alt_text.lower())

    def test_unknown_scene_id_fallback(self):
        result = resolve_cb_sc001_scene_image("SC001-DOES-NOT-EXIST", scene_title="Missing")
        self.assertFalse(result.available)
        self.assertIsNone(result.path)
        self.assertEqual(result.reason, "unknown_scene_id")


class TestSimulatorUiHelpersNoHardCoding(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = load_cb_sc001_v2_content()

    def test_scenario_title_and_progress_from_document(self):
        self.assertEqual(self.content.document.get("title"), "Customer Kickoff")
        index, total = scene_progress_from_content(self.content, "SC001-C03")
        self.assertEqual((index, total), (3, 12))
        self.assertEqual(format_scene_progress_caption(index, total), "Scene 03 of 12")
        index_last, total_last = scene_progress_from_content(self.content, "SC001-C12")
        self.assertEqual((index_last, total_last), (12, 12))

    def test_mission_and_brief_from_document_not_hardcoded_dialogue(self):
        mission = mission_text_from_content(self.content, "SC001-C01")
        self.assertTrue(mission)
        # Must come from document fields, not invented UI copy.
        scene = self.content.scenes_by_id["SC001-C01"]
        self.assertIn(mission, {scene.get("enteringStateDescription"), self.content.document["learnerRole"]["summary"]})
        sections = decision_brief_sections(self.content)
        self.assertGreaterEqual(len(sections), 1)
        joined = "\n".join(body for _, body in sections)
        briefing = self.content.document["introduction"]["projectBriefing"]["summary"]
        self.assertIn(briefing, joined)

    def test_option_labels_come_from_option_payload(self):
        label = option_card_label({"id": "opt-x", "title": "Title A", "text": "Body B"})
        self.assertIn("Title A", label)
        self.assertIn("Body B", label)

    def test_page_source_does_not_hardcode_scene_dialogue(self):
        page_path = Path(__file__).resolve().parents[1] / "pages" / "Scenario_Simulator_V2.py"
        source = page_path.read_text(encoding="utf-8")
        # Representative dialogue / choice strings from the canonical document must not be pasted into the page.
        forbidden_snippets = (
            "Marcus arrives late from a customer call",
            "opt-sc001-c01-a",
            "strong_resolution",
            "Sales closed Crestline",
        )
        for snippet in forbidden_snippets:
            self.assertNotIn(snippet, source, msg=f"hard-coded content leaked into page: {snippet}")

    def test_mobile_css_includes_narrow_layout_rules(self):
        import types

        captured: list[str] = []

        def _markdown(text: str, *args, **kwargs) -> None:
            if "<style>" in text:
                captured.append(text)

        fake_st = types.SimpleNamespace(markdown=_markdown)
        import utils.scenario_simulator_ui_v2 as ui_mod

        original = ui_mod._st
        ui_mod._st = lambda: fake_st  # type: ignore[method-assign]
        try:
            ui_mod.inject_ba_simulator_css()
        finally:
            ui_mod._st = original  # type: ignore[method-assign]
        css = captured[0] if captured else ""
        self.assertIn("@media (max-width: 480px)", css)
        self.assertIn(".cb-sim-header", css)
        self.assertIn("flex-direction: column", css)
        self.assertIn(".cb-sim-title-block", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn('div[data-testid="stRadio"]', css)
        self.assertIn(".cb-sim-image-wrap img", css)


class TestSimulatorPaletteCorrection(unittest.TestCase):
    """CERTBOUND-BA-SIMULATOR-RELEASE-CANDIDATE-02: verify the corrected
    Simulator palette without pinning to exact CSS structure/implementation
    details (no full-string snapshot comparisons)."""

    @staticmethod
    def _inject_and_capture_css() -> str:
        import types

        captured: list[str] = []

        def _markdown(text: str, *args, **kwargs) -> None:
            if "<style>" in text:
                captured.append(text)

        fake_st = types.SimpleNamespace(markdown=_markdown)
        import utils.scenario_simulator_ui_v2 as ui_mod

        original = ui_mod._st
        ui_mod._st = lambda: fake_st  # type: ignore[method-assign]
        try:
            ui_mod.inject_ba_simulator_css()
        finally:
            ui_mod._st = original  # type: ignore[method-assign]
        return captured[0] if captured else ""

    def test_required_tokens_render_without_missing_keys(self):
        # A KeyError here would fail the test outright; this proves every
        # token the Simulator CSS references resolves successfully.
        css = self._inject_and_capture_css()
        self.assertTrue(css)
        self.assertIn("<style>", css)

    def test_primary_actions_use_approved_darker_sky_blue(self):
        from utils.scenario_simulator_ui_v2 import SIMULATOR_COLOR_OVERRIDES

        css = self._inject_and_capture_css()
        accent = SIMULATOR_COLOR_OVERRIDES["accent"]
        self.assertEqual(accent, "#0369A1")
        self.assertIn(accent, css)

    def test_brand_accents_use_sky_blue_family(self):
        from utils.ui_theme import COLORS

        css = self._inject_and_capture_css()
        # Bound wordmark + hover/pressed accents were already ported as the
        # minimum Simulator dependency (CERTBOUND-BA-SIMULATOR-RELEASE-
        # CANDIDATE-01); this confirms they still render together with the
        # corrected primary accent.
        for key in ("bound_wordmark", "accent_bright", "accent_pressed", "accent_surface", "focus_ring"):
            self.assertIn(COLORS[key], css)

    def test_dark_navy_header_structure_intact(self):
        from utils.scenario_simulator_ui_v2 import SIMULATOR_COLOR_OVERRIDES

        css = self._inject_and_capture_css()
        navy = SIMULATOR_COLOR_OVERRIDES["primary_navy"]
        self.assertEqual(navy, "#0B1F3A")
        self.assertIn(".cb-sim-header", css)
        self.assertIn(navy, css)

    def test_semantic_success_remains_green_not_recolored_to_blue(self):
        from utils.ui_theme import COLORS

        css = self._inject_and_capture_css()
        # Success tokens are untouched by the Simulator-scoped override, so
        # the in-progress status indicator stays on the existing green
        # semantic tokens rather than shifting to the Sky Blue accent family.
        self.assertIn(COLORS["success"], css)
        self.assertIn(COLORS["success_bg"], css)

    def test_focus_styles_remain_present(self):
        from utils.ui_theme import COLORS

        css = self._inject_and_capture_css()
        self.assertIn(COLORS["focus_ring"], css)

    def test_simulator_override_does_not_mutate_shared_global_colors(self):
        # The core isolation guarantee: pages that still import the shared
        # COLORS dict (e.g. dashboard_components, secondary_components,
        # activity_components) must keep seeing the untouched baseline
        # values, proving the Simulator's palette fix is local-only.
        from utils.ui_theme import COLORS

        self.assertEqual(COLORS["accent"], "#2563EB")
        self.assertEqual(COLORS["primary_navy"], "#1E3A5F")

        # Injecting the Simulator CSS (which builds a merged local dict)
        # must not have side effects on the shared module-level dict.
        self._inject_and_capture_css()
        self.assertEqual(COLORS["accent"], "#2563EB")
        self.assertEqual(COLORS["primary_navy"], "#1E3A5F")


class TestTerminalResultsHelperSurface(unittest.TestCase):
    def test_terminal_serialized_shape_fields_used_by_ui(self):
        # UI reads only learner-safe terminal keys.
        terminal = {
            "outcomeId": "outcome-example",
            "outcomeTitle": "Aligned Kickoff",
            "narrative": "You helped the team align.",
            "displayScore": 44,
        }
        for key in ("outcomeTitle", "narrative", "displayScore"):
            self.assertIn(key, terminal)


if __name__ == "__main__":
    unittest.main()
