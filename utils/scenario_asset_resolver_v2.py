"""Local scene-image resolution for CB-SC-001 BA Scenario Simulator.

Loads ``assets/scenarios/business_analyst/cb-sc-001/manifest.json`` and maps
scene IDs to on-disk PNG paths. Never embeds base64 and never fetches remote
images. Missing assets return a safe unavailable result for UI fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from utils.scenario_schema import REPO_ROOT

CB_SC001_ASSET_ROOT = REPO_ROOT / "assets" / "scenarios" / "business_analyst" / "cb-sc-001"
CB_SC001_MANIFEST_PATH = CB_SC001_ASSET_ROOT / "manifest.json"
CB_SC001_SCENES_DIR = CB_SC001_ASSET_ROOT / "scenes"
CB_SC001_CANONICAL_UI_REFERENCE = (
    CB_SC001_ASSET_ROOT / "references" / "canonical-simulator-ui-reference.png"
)

__all__ = (
    "CB_SC001_ASSET_ROOT",
    "CB_SC001_CANONICAL_UI_REFERENCE",
    "CB_SC001_MANIFEST_PATH",
    "CB_SC001_SCENES_DIR",
    "SceneImageResolution",
    "load_cb_sc001_asset_manifest",
    "resolve_cb_sc001_scene_image",
    "scene_id_image_map_from_manifest",
)


@dataclass(frozen=True)
class SceneImageResolution:
    """Learner-safe local image resolution result for one scene ID."""

    scene_id: str
    available: bool
    path: Optional[Path]
    relative_filename: Optional[str]
    title: str
    alt_text: str
    reason: str = ""


def load_cb_sc001_asset_manifest(
    *,
    manifest_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load and lightly validate the CB-SC-001 asset manifest."""
    path = Path(manifest_path) if manifest_path is not None else CB_SC001_MANIFEST_PATH
    if not path.is_file():
        raise FileNotFoundError(f"CB-SC-001 asset manifest not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("CB-SC-001 asset manifest must be a JSON object")
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("CB-SC-001 asset manifest must include a non-empty scenes list")
    return payload


def scene_id_image_map_from_manifest(manifest: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return ``{sceneId: scene_entry}`` from a loaded manifest."""
    mapping: Dict[str, Dict[str, Any]] = {}
    scenes = manifest.get("scenes") or []
    if not isinstance(scenes, list):
        return mapping
    for entry in scenes:
        if not isinstance(entry, Mapping):
            continue
        scene_id = str(entry.get("sceneId") or "").strip()
        if not scene_id:
            continue
        mapping[scene_id] = dict(entry)
    return mapping


def resolve_cb_sc001_scene_image(
    scene_id: str,
    *,
    manifest: Optional[Mapping[str, Any]] = None,
    asset_root: Optional[Path] = None,
    scene_title: Optional[str] = None,
) -> SceneImageResolution:
    """Resolve a local scene image path for ``scene_id``.

    When the mapped file is missing or the scene ID is unknown, ``available``
    is False and ``path`` is None so the UI can render a placeholder instead
    of a broken image.
    """
    sid = str(scene_id or "").strip()
    root = Path(asset_root) if asset_root is not None else CB_SC001_ASSET_ROOT
    title_hint = str(scene_title or "").strip()

    if not sid:
        return SceneImageResolution(
            scene_id="",
            available=False,
            path=None,
            relative_filename=None,
            title=title_hint or "Scene",
            alt_text=title_hint or "Scene image unavailable",
            reason="missing_scene_id",
        )

    try:
        loaded = load_cb_sc001_asset_manifest(manifest_path=root / "manifest.json") if manifest is None else manifest
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        fallback_title = title_hint or sid
        return SceneImageResolution(
            scene_id=sid,
            available=False,
            path=None,
            relative_filename=None,
            title=fallback_title,
            alt_text=f"{fallback_title} — image unavailable",
            reason=f"manifest_error:{exc.__class__.__name__}",
        )

    entry = scene_id_image_map_from_manifest(loaded).get(sid)
    if entry is None:
        fallback_title = title_hint or sid
        return SceneImageResolution(
            scene_id=sid,
            available=False,
            path=None,
            relative_filename=None,
            title=fallback_title,
            alt_text=f"{fallback_title} — image unavailable",
            reason="unknown_scene_id",
        )

    filename = str(entry.get("filename") or "").strip().replace("\\", "/")
    entry_title = str(entry.get("title") or "").strip()
    title = title_hint or entry_title or sid
    alt_text = f"{title} scene illustration"

    if not filename:
        return SceneImageResolution(
            scene_id=sid,
            available=False,
            path=None,
            relative_filename=None,
            title=title,
            alt_text=f"{title} — image unavailable",
            reason="missing_filename",
        )

    # Reject absolute / parent-path escapes; keep resolution local to asset root.
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return SceneImageResolution(
            scene_id=sid,
            available=False,
            path=None,
            relative_filename=filename,
            title=title,
            alt_text=f"{title} — image unavailable",
            reason="path_escape",
        )

    if not candidate.is_file():
        return SceneImageResolution(
            scene_id=sid,
            available=False,
            path=None,
            relative_filename=filename,
            title=title,
            alt_text=f"{title} — image unavailable",
            reason="file_missing",
        )

    return SceneImageResolution(
        scene_id=sid,
        available=True,
        path=candidate,
        relative_filename=filename,
        title=title,
        alt_text=alt_text,
        reason="ok",
    )


def verify_all_manifest_scene_images(
    *,
    manifest: Optional[Mapping[str, Any]] = None,
    asset_root: Optional[Path] = None,
) -> Tuple[SceneImageResolution, ...]:
    """Resolve every scene entry in the manifest (for tests / install checks)."""
    loaded = load_cb_sc001_asset_manifest() if manifest is None else manifest
    root = Path(asset_root) if asset_root is not None else CB_SC001_ASSET_ROOT
    results = []
    for entry in loaded.get("scenes") or []:
        if not isinstance(entry, Mapping):
            continue
        sid = str(entry.get("sceneId") or "").strip()
        if not sid:
            continue
        results.append(
            resolve_cb_sc001_scene_image(
                sid,
                manifest=loaded,
                asset_root=root,
                scene_title=str(entry.get("title") or ""),
            )
        )
    return tuple(results)
