"""Add the project root to sys.path for Streamlit entrypoints and pages."""
from __future__ import annotations

import sys
from pathlib import Path


def project_root_from_file(file_path: str | Path) -> Path:
    file_path = Path(file_path).resolve()
    if file_path.parent.name == "pages":
        return file_path.parent.parent
    return file_path.parent


def ensure_project_root(caller_file: str | Path | None = None) -> Path:
    if caller_file is None:
        caller_file = __file__
    project_root = project_root_from_file(caller_file)
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return project_root
